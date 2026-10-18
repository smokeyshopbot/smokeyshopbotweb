"""Durable coupon claims. Reward references stay identical across every retry."""
import hashlib
import re
import uuid
from datetime import datetime, timezone, timedelta
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError
from utils.promotions import utc
from utils.stock_reservations import operation, _release
from utils.wallet_events import change_wallet, change_wallet_async, event_id
from utils.data_guard import work_guard, async_work_guard
from utils.i18n import tr


def now():
    return datetime.now(timezone.utc)


def normalize_code(value):
    value = str(value or '').strip().upper()
    if not re.fullmatch(r'[A-Z0-9_-]{8,64}', value):
        raise ValueError('Use 8–64 letters, numbers, hyphens, or underscores for a coupon code.')
    return value


def coupon_id(code):
    return hashlib.sha256(normalize_code(code).encode()).hexdigest()


def redemption_id(identifier, user_id):
    return hashlib.sha256((identifier + ':' + str(int(user_id))).encode()).hexdigest()


def _new_order_id():
    return uuid.uuid4().hex[:8].upper()


def _limit_status(coupon, user_id):
    if str(user_id) in (coupon.get('claimed_users') or {}):
        return 'already'
    return 'used_other' if int(coupon.get('max_uses', 1)) == 1 else 'exhausted'


def _job(identifier, kind, payload):
    return operation('delivery_jobs', 'update_one', {'job_id': identifier}, {'$setOnInsert': {
        'job_id': identifier, 'kind': kind, 'payload': payload, 'status': 'queued',
        'priority': 5, 'attempts': 0, 'created_at': now(), 'next_attempt_at': now()}}, upsert=True)


def _forget_pending(coupon, rid):
    return operation('coupons', 'update_one', {'_id': coupon['_id'], 'pending_claims.' + rid: {'$exists': True}},
                     {'$unset': {'pending_claims.' + rid: ''}, '$inc': {'pending_count': -1}})


def _redeem(code, user_id):
    try:
        identifier = coupon_id(code)
    except ValueError:
        return {'status': 'invalid'}
    uid = int(user_id)
    coupon = yield operation('coupons', 'find_one', {'_id': identifier})
    if not coupon:
        return {'status': 'invalid'}
    rid = redemption_id(identifier, uid)
    existing = yield operation('coupon_redemptions', 'find_one', {'_id': rid})
    if existing and existing.get('status') == 'completed':
        return {**existing, 'status': 'already'}
    claim = (coupon.get('pending_claims') or {}).get(rid)
    if not claim:
        if str(uid) in (coupon.get('claimed_users') or {}):
            return {'status': 'already'}
        if int(coupon.get('claimed_count', 0)) >= int(coupon['max_uses']):
            return {'status': _limit_status(coupon, uid)}
        current = now()
        if not coupon.get('enabled') or (utc(coupon.get('expires_at')) and utc(coupon['expires_at']) <= current):
            return {'status': 'expired'}
        user = yield operation('users', 'find_one', {'user_id': uid, 'blocked': {'$ne': True}})
        if not user:
            return {'status': 'unavailable'}
        if coupon['kind'] == 'product':
            product = yield operation('products', 'find_one', {'_id': coupon['product_id'], 'enabled': {'$ne': False}})
            if not product:
                return {'status': 'unavailable'}
            pending = yield operation('service', 'pending_quantity', product['name'])
            supplier = product.get('external_supplier_fallback_enabled') and product.get('supplier_fallbacks')
            if len(product.get('stock') or []) - pending < coupon['quantity']:
                external = (yield operation('service', 'external_stock', product)) if supplier else 0
                if len(product.get('stock') or []) + int(external or 0) - pending < coupon['quantity']:
                    return {'status': 'out_of_stock'}
        claim = {'redemption_id': rid, 'user_id': uid, 'username': user.get('username', ''),
                 'language': user.get('language', 'en'), 'created_at': current,
                 'kind': coupon['kind'], 'code': coupon['code'], 'coupon_id': identifier}
        if coupon['kind'] == 'wallet':
            claim.update(currency=coupon['currency'], amount=coupon['amount'])
        else:
            claim.update(product_id=product['_id'], product_name=product['name'], quantity=coupon['quantity'],
                         order_id=_new_order_id())
        changed = yield operation('coupons', 'update_one', {'_id': identifier, 'enabled': True,
            'claimed_count': {'$lt': int(coupon['max_uses'])}, 'claimed_users.' + str(uid): {'$exists': False},
            '$or': [{'expires_at': None}, {'expires_at': {'$gt': current}}]},
            {'$inc': {'claimed_count': 1, 'pending_count': 1}, '$set': {
                'claimed_users.' + str(uid): rid, 'pending_claims.' + rid: claim}})
        if not changed.modified_count:
            fresh = yield operation('coupons', 'find_one', {'_id': identifier})
            if rid in (fresh.get('pending_claims') or {}):
                return (yield from _process(fresh, fresh['pending_claims'][rid]))
            return {'status': _limit_status(fresh, uid)}
    return (yield from _process(coupon, claim))


def _reward_order(coupon, row, order, token):
    """Bind a short ID before fulfilment; a retry always uses the saved binding."""
    rid = row['redemption_id']
    for _ in range(25):
        oid = row['order_id']
        existing = yield operation('orders', 'find_one', {'order_id': oid})
        if existing and existing.get('coupon_redemption_id') == rid:
            return existing
        if not existing:
            try:
                yield operation('orders', 'update_one', {'order_id': oid},
                    {'$setOnInsert': {**order, 'order_id': oid}}, upsert=True)
            except DuplicateKeyError:
                pass  # A normal order or another coupon won the same short ID.
            else:
                saved = yield operation('orders', 'find_one', {'order_id': oid})
                if saved and saved.get('coupon_redemption_id') == rid:
                    return saved
                if not saved:
                    raise RuntimeError('Coupon order could not be read after creation')
        replacement = _new_order_id()
        changed = yield operation('coupon_redemptions', 'update_one',
            {'_id': rid, 'lock_token': token, 'order_id': oid}, {'$set': {'order_id': replacement}})
        if not changed.matched_count:
            raise RuntimeError('Coupon order ID binding changed during creation')
        # The redemption record is authoritative if a restart precedes this snapshot update.
        yield operation('coupons', 'update_one', {'_id': coupon['_id'], 'pending_claims.' + rid: {'$exists': True}},
            {'$set': {'pending_claims.' + rid + '.order_id': replacement}})
        row['order_id'] = replacement
    raise RuntimeError('Could not allocate a unique coupon order ID; retry this redemption')


def _process(coupon, claim):
    rid, uid = claim['redemption_id'], claim['user_id']
    yield operation('coupon_redemptions', 'update_one', {'_id': rid},
                    {'$setOnInsert': {**claim, 'status': 'processing', 'next_attempt_at': now()}}, upsert=True)
    token = uuid.uuid4().hex
    row = yield operation('coupon_redemptions', 'find_one_and_update', {'_id': rid, '$or': [
        {'lock_until': {'$exists': False}}, {'lock_until': {'$lte': now()}}]},
        {'$set': {'lock_token': token, 'lock_until': now() + timedelta(minutes=10)}}, return_document=ReturnDocument.AFTER)
    if not row:
        return {'status': 'processing'}
    try:
        if row.get('status') == 'completed':
            yield _forget_pending(coupon, rid)
            return row
        if row['kind'] == 'wallet':
            reference = 'coupon-' + rid
            user = yield operation('wallet', 'credit', uid, row['currency'], row['amount'],
                                    reference=reference, source='coupon')
            if not user:
                raise RuntimeError('Coupon wallet account is unavailable')
            ref = 'COUPON-' + rid[:24].upper()
            yield operation('pending_payments', 'update_one', {'ref_id': ref}, {'$setOnInsert': {
                'ref_id': ref, 'user_id': uid, 'username': row.get('username', ''), 'pay_type': 'wallet',
                'method': 'coupon', 'currency': row['currency'], 'load_amount': row['amount'],
                'expected_inr': row['amount'] if row['currency'] == 'inr' else 0,
                'expected_usdt': row['amount'] if row['currency'] == 'usdt' else 0,
                'status': 'completed', 'created_at': row['created_at'], 'wallet_credited_at': now(),
                'wallet_credit_state': 'done', 'wallet_credit_version': 1,
                'wallet_event_id': event_id(uid, row['currency'], reference), 'coupon_redemption_id': rid,
                'coupon_code': row['code']}}, upsert=True)
            text = tr(row.get('language'), 'coupon_wallet_success', code=row['code'],
                      amount=f"{row['amount']:.2f}", currency=row['currency'].upper(),
                      balance=f"{float(user.get('wallet_' + row['currency']) or 0):.2f}")
            yield _job('coupon-receipt:' + rid, 'text', {'chat_id': uid, 'text': text})
        else:
            oid = row['order_id']
            order = {key: row[key] for key in ('user_id', 'username', 'product_name', 'quantity', 'order_id',
                     'created_at')}
            product = yield operation('products', 'find_one', {'_id': row['product_id']},
                {'name': 1, 'order_txt_instructions_en': 1, 'order_txt_instructions_es': 1,
                 'external_supplier_fallback_enabled': 1})
            if product:
                order['product_name'] = product['name']
            for language in ('en', 'es'):
                field = 'order_txt_instructions_' + language
                order[field] = (product or {}).get(field, '')
            order.update(payment_method='coupon', amount_inr=0, amount_usdt=0, items=[], status='coupon_processing',
                         coupon_redemption_id=rid, coupon_code=row['code'], coupon_product_id=row['product_id'],
                         stock_reservation_enabled=True, payment_verified_at=row['created_at'])
            order = yield from _reward_order(coupon, row, order, token)
            oid = order['order_id']
            if order.get('coupon_redemption_id') != rid:
                raise RuntimeError('Coupon order reference conflict')
            if order['status'] in {'cancelled', 'expired', 'failed', 'rejected', 'revoked'}:
                yield operation('coupon_redemptions', 'update_one', {'_id': rid}, {'$set': {
                    'status': 'cancelled', 'error': 'Reward order was cancelled by admin.'}})
                yield _forget_pending(coupon, rid)
                return {**row, 'status': 'cancelled'}
            if order['status'] != 'delivered':
                if order['status'] != 'coupon_processing':
                    raise RuntimeError('Coupon order was changed by admin; review its delivery before retrying')
                result = yield operation('service', 'fulfill', order['product_name'], order['quantity'], order_id=oid)
                items = list(result.get('items') or [])
                if len(items) != order['quantity']:
                    review = bool(result.get('uncertain') or (result.get('source') == 'supplier' and
                                  (product or {}).get('external_supplier_fallback_enabled')))
                    yield operation('coupon_redemptions', 'update_one', {'_id': rid}, {'$set': {
                        'status': 'review' if review else 'pending_stock', 'error': result.get('error', 'Waiting for stock'),
                        'next_attempt_at': now() + timedelta(seconds=30)}})
                    return {**row, 'status': 'review' if review else 'pending_stock'}
                fields = {'status': 'delivered', 'items': items, 'delivered_at': now()}
                if result.get('source') == 'supplier':
                    fields.update(external_supplier_delivery=True, local_delivered_quantity=result.get('local_quantity', 0),
                                  supplier_delivered_quantity=result.get('supplier_quantity', len(items)))
                    for key in ('supplier_id', 'supplier_alias', 'supplier_product_id', 'supplier_order_code'):
                        fields[key] = result.get(key)
                changed = yield operation('orders', 'update_one', {'_id': order['_id'], 'status': 'coupon_processing'}, {'$set': fields})
                if not changed.matched_count:
                    raise RuntimeError('Coupon delivery finalization needs review')
                order.update(fields)
            yield from _release(oid, order['product_name'], discard=True)
            yield operation('service', 'record_delivery', order, order['items'])
            lang = row.get('language', 'en')
            lines = [tr(lang, 'order_items_title'), tr(lang, 'order_id') + ': ' + oid,
                     tr(lang, 'product') + ': ' + order['product_name'], tr(lang, 'quantity') + ': ' + str(order['quantity']),
                     tr(lang, 'payment_method') + ': ' + tr(lang, 'coupon_method')]
            instructions = order.get('order_txt_instructions_' + ('es' if lang == 'es' else 'en'))
            if instructions:
                lines.extend(['', tr(lang, 'delivery_instructions_label') + ':', instructions])
            lines.extend(['', tr(lang, 'items_label') + ':', '\n\n'.join(str(item) for item in order['items'])])
            yield _job('coupon-receipt:' + rid, 'document', {'chat_id': uid, 'filename': oid + '.txt',
                'content': '\n'.join(lines) + '\n', 'caption': tr(lang, 'coupon_product_success', order_id=oid, code=row['code']),
                'delivery_meta': {'order_id': oid, 'sent_by': 'coupon', 'resent': False}})
        yield operation('coupon_redemptions', 'update_one', {'_id': rid, 'lock_token': token}, {'$set': {
            'status': 'completed', 'completed_at': now(), 'error': ''}})
        yield _forget_pending(coupon, rid)
        return {**row, 'status': 'completed'}
    except Exception as error:
        yield operation('coupon_redemptions', 'update_one', {'_id': rid, 'lock_token': token}, {'$set': {
            'error': 'Reward processing interrupted (' + type(error).__name__ + '). Safe retry is pending.',
            'next_attempt_at': now() + timedelta(seconds=30)}})
        raise
    finally:
        yield operation('coupon_redemptions', 'update_one', {'_id': rid, 'lock_token': token},
                        {'$unset': {'lock_token': '', 'lock_until': ''}})


def _recover(limit=20):
    state = yield operation('operational_control', 'find_one', {'_id': 'coupon_recovery_cursor'})
    query = {'pending_count': {'$gt': 0}}
    if state and state.get('cursor'):
        query['_id'] = {'$gte': state['cursor']}
    coupons = yield operation('coupons', 'find_many', query, {'pending_claims': 1}, limit=limit)
    if not coupons:
        if state and state.get('cursor'):
            yield operation('operational_control', 'update_one', {'_id': 'coupon_recovery_cursor'}, {'$unset': {'cursor': ''}})
        return 0
    count = checked = 0
    for coupon in coupons:
        for rid, claim in sorted((coupon.get('pending_claims') or {}).items()):
            if state and coupon['_id'] == state.get('cursor') and rid <= state.get('claim_cursor', ''):
                continue
            row = yield operation('coupon_redemptions', 'find_one', {'_id': claim['redemption_id']})
            waiting = row and (row.get('status') == 'review' or (utc(row.get('next_attempt_at')) and utc(row['next_attempt_at']) > now()))
            if not waiting:
                yield from _process(coupon, claim)
                count += 1
            checked += 1
            if checked >= limit:
                yield operation('operational_control', 'update_one', {'_id': 'coupon_recovery_cursor'},
                    {'$set': {'cursor': coupon['_id'], 'claim_cursor': rid}}, upsert=True)
                return count
    if len(coupons) < limit:
        yield operation('operational_control', 'update_one', {'_id': 'coupon_recovery_cursor'},
            {'$unset': {'cursor': '', 'claim_cursor': ''}})
    else:
        yield operation('operational_control', 'update_one', {'_id': 'coupon_recovery_cursor'},
            {'$set': {'cursor': coupons[-1]['_id'], 'claim_cursor': '~'}}, upsert=True)
    return count


def _run(db, generator, service):
    value = None
    error = None
    while True:
        try:
            collection, method, args, kwargs = generator.throw(error) if error is not None else generator.send(value)
        except StopIteration as result:
            return result.value
        error = None
        try:
            if collection == 'wallet':
                value = change_wallet(db, *args, **kwargs)
            elif collection == 'service':
                value = service[method](*args, **kwargs)
            elif method == 'find_many':
                value = list(db[collection].find(*args).sort('_id', 1).limit(kwargs['limit']))
            else:
                value = getattr(db[collection], method)(*args, **kwargs)
        except BaseException as caught:
            error = caught


async def _arun(db, generator, service):
    value = None
    error = None
    while True:
        try:
            collection, method, args, kwargs = generator.throw(error) if error is not None else generator.send(value)
        except StopIteration as result:
            return result.value
        error = None
        try:
            if collection == 'wallet':
                value = await change_wallet_async(db, *args, **kwargs)
            elif collection == 'service':
                value = await service[method](*args, **kwargs)
            elif method == 'find_many':
                value = await db[collection].find(*args).sort('_id', 1).limit(kwargs['limit']).to_list(length=kwargs['limit'])
            else:
                value = await getattr(db[collection], method)(*args, **kwargs)
        except BaseException as caught:
            error = caught


def redeem(db, code, user_id, service):
    with work_guard(db):
        return _run(db, _redeem(code, user_id), service)


async def redeem_async(db, code, user_id, service):
    async with async_work_guard(db):
        return await _arun(db, _redeem(code, user_id), service)


def recover(db, service, limit=20):
    with work_guard(db):
        return _run(db, _recover(limit), service)


async def recover_async(db, service, limit=20):
    async with async_work_guard(db):
        return await _arun(db, _recover(limit), service)
