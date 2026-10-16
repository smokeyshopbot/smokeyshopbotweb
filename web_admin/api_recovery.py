"""Recover interrupted API orders using saved evidence, without buying from a vendor."""
import uuid
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from flask import request, flash, redirect, url_for
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError
from utils.wallet_audit import change_wallet, event_id
from utils.stock_reservations import release_stock


def now(): return datetime.now(timezone.utc)


def _event(db, user, identifier):
    return db.wallet_ledger.find_one({'_id': identifier}) or (user.get('wallet_outbox') or {}).get(identifier)


def _review(db, shop, order, reason):
    changed = db.orders.update_one({'_id': order['_id'], 'status': 'api_processing', 'api_recovery_state': {'$ne': 'review'}},
        {'$set': {'api_recovery_state': 'review', 'api_recovery_reason': reason, 'api_recovery_checked_at': now()}})
    if changed.modified_count:
        from web_admin.delivery_queue import enqueue_job
        for uid in shop.get_admin_ids(db):
            enqueue_job(db, 'text', {'chat_id': uid, 'text': '⚠️ API order needs review\nOrder: ' + order['order_id'] +
                '\n' + reason + '\nOpen this order in WebAdmin to resolve it safely.'},
                job_id='api-recovery:' + order['order_id'] + ':' + str(uid))
    return 'review'


def _failed(db, shop, order, user, *, charged, reason):
    oid, uid = order['order_id'], order['user_id']
    amount = float(order['amount_usdt'])
    refund_id = event_id(uid, 'usdt', 'api-refund-' + oid)
    if charged:
        updated = change_wallet(db, uid, 'usdt', amount, reference='api-refund-' + oid, source='api_refund',
            extra={'$unset': {'api_wallet_debits.' + oid: ''}})
        if not updated:
            raise RuntimeError('Wallet recovery is temporarily unavailable')
    response = {'success': False, 'status': 'failed', 'orderCode': oid,
        'client_order_id': order.get('api_client_order_id'), 'code': 'interrupted_refunded' if charged else 'interrupted_before_charge',
        'message': 'Interrupted purchase refunded. Submit a new client_order_id to place another order.' if charged else
                   'Purchase interrupted before charging. Submit a new client_order_id to place another order.',
        'retryable': False, 'retryWithNewClientOrderId': True, 'refunded': bool(charged)}
    fields = {'status': 'failed', 'api_stage': 'rolled_back' if charged else 'rejected_before_charge',
        'api_recovery_state': 'resolved', 'api_recovery_reason': reason, 'api_recovered_at': now(),
        'api_error_code': response['code'], 'api_error_message': response['message'], 'failed_at': now(),
        'api_error_response': response, 'api_response_status': 409}
    if charged:
        fields.update(wallet_refund_event_id=refund_id, wallet_refund_event_delta=amount)
    db.orders.update_one({'_id': order['_id'], 'status': 'api_processing'}, {'$set': fields})
    db.users.update_one({'_id': user['_id']}, {'$unset': {'api_wallet_debits.' + oid: ''}})
    release_stock(db, oid, order['product_name'])
    return 'refunded' if charged else 'cancelled'


def _complete(db, shop, order, user, product, result, charge):
    items = list(result.get('items') or [])
    if len(items) != int(order['quantity']):
        raise ValueError('Confirmed delivery must contain exactly the ordered quantity')
    buyer = dict(user, wallet_usdt=charge.get('after', user.get('wallet_usdt', 0))) if charge else user
    product = product or {'name': order['product_name'], 'api_product_id': order.get('api_product_id')}
    response = shop.build_buyer_purchase_response(db, order, buyer, product, items)
    fields = {'status': 'delivered', 'api_stage': 'complete', 'items': items, 'delivered_at': now(),
        'payment_verified_at': now(), 'api_purchase_response': response, 'api_response_status': 200,
        'api_balance_after': buyer['wallet_usdt'], 'api_receipt_status': 'pending',
        'api_recovery_state': 'resolved', 'api_recovered_at': now(), 'api_recovery_reason': 'Completed from saved delivery evidence'}
    if result.get('source') == 'supplier':
        fields.update(external_supplier_delivery=True, local_delivered_quantity=result.get('local_quantity', 0),
                      supplier_delivered_quantity=result.get('supplier_quantity', len(items)))
        for key in ('supplier_id', 'supplier_alias', 'supplier_product_id', 'supplier_order_code'):
            if result.get(key): fields[key] = result[key]
    saved = db.orders.update_one({'_id': order['_id'], 'status': 'api_processing'}, {'$set': fields})
    if not saved.matched_count:
        return 'unchanged'
    # A restart after this commit only repeats idempotent bookkeeping, never the purchase.
    release_stock(db, order['order_id'], order['product_name'], discard=True)
    db.users.update_one({'_id': user['_id']}, {'$unset': {'api_wallet_debits.' + order['order_id']: ''}})
    shop.record_order_items_delivered_in_ledger(db, {**order, **fields}, items, source='api_recovery')
    return 'completed'


def recover_order(db, shop, order_id, *, action=None, confirmed_items=None, verified=False, note='', actor=''):
    """Acquire the existing buyer mutex; a live purchase always wins."""
    order = db.orders.find_one({'order_id': str(order_id).upper(), 'api_purchase': True})
    if not order or order.get('status') != 'api_processing':
        return 'unchanged'
    uid, token = int(order['user_id']), uuid.uuid4().hex
    key = 'buyer:' + str(uid)
    try:
        db.api_purchase_locks.find_one_and_update({'key': key, '$or': [
            {'expires_at': {'$lte': now()}}, {'expires_at': {'$exists': False}}]},
            {'$set': {'owner': token, 'expires_at': now() + timedelta(minutes=10)}}, upsert=True,
            return_document=ReturnDocument.AFTER)
    except DuplicateKeyError:
        return 'busy'
    try:
        order = db.orders.find_one({'_id': order['_id']})
        if order.get('status') != 'api_processing': return 'unchanged'
        user = db.users.find_one({'user_id': uid})
        if not user: return _review(db, shop, order, 'The wallet account is unavailable')
        oid = order['order_id']
        charge = _event(db, user, event_id(uid, 'usdt', 'api-purchase-' + oid))
        debit = (user.get('api_wallet_debits') or {}).get(oid)
        charged = bool(charge or debit is not None)
        amount = Decimal(str(order['amount_usdt']))
        if (charge and Decimal(str(charge['delta'])) != -amount) or (debit is not None and Decimal(str(debit)) != amount):
            return _review(db, shop, order, 'Wallet debit does not match the order amount')
        refund = _event(db, user, event_id(uid, 'usdt', 'api-refund-' + oid))
        if refund:
            if Decimal(str(refund['delta'])) != amount:
                return _review(db, shop, order, 'Wallet refund does not match the order amount')
            return _failed(db, shop, order, user, charged=True, reason='Finished an already recorded refund')
        if not charged:
            if order.get('api_recovery_version') != 1 and (order.get('api_stage') in {'charged', 'complete'} or order.get('paid_at')):
                return _review(db, shop, order, 'Legacy paid order has no reliable debit evidence; verify its wallet history')
            return _failed(db, shop, order, user, charged=False, reason='No wallet debit was applied')
        product = db.products.find_one({'name': shop.name_regex(order['product_name'])})
        hold = ((product or {}).get('stock_reservations') or {}).get(oid) or {}
        local = list(hold.get('items') or [])
        saved = order.get('fulfillment_reservation') or {}
        if action:
            if saved.get('phase') == 'ready' and len((saved.get('result') or {}).get('items') or []) == int(order['quantity']):
                raise ValueError('Confirmed delivery is already saved. Use safe recovery to complete this order')
            if not verified or not str(note).strip():
                raise ValueError('Confirm that you checked the vendor order and enter a review note')
            if action not in {'refund', 'complete'}:
                raise ValueError('Choose a supported recovery action')
            db.orders.update_one({'_id': order['_id']}, {'$set': {'api_recovery_owner_verification': {
                'action': action, 'note': str(note).strip()[:1000], 'actor': actor, 'at': now()}}})
            if action == 'refund':
                return _failed(db, shop, order, user, charged=True, reason='Owner verified no delivery: ' + str(note).strip()[:500])
            supplied = list(confirmed_items or [])
            result = {'items': local + supplied, 'source': 'supplier', 'local_quantity': len(local), 'supplier_quantity': len(supplied)}
            if len(result['items']) != int(order['quantity']):
                raise ValueError('Enter exactly ' + str(int(order['quantity']) - len(local)) + ' confirmed vendor items; reserved local units are included automatically')
            db.orders.update_one({'_id': order['_id']}, {'$set': {'fulfillment_reservation': {'phase': 'ready', 'result': result}}})
            return _complete(db, shop, order, user, product, result, charge)
        if saved.get('phase') == 'ready' and len((saved.get('result') or {}).get('items') or []) == int(order['quantity']):
            return _complete(db, shop, order, user, product, saved['result'], charge)
        if len(local) == int(order['quantity']) and saved.get('phase') not in {'requesting', 'uncertain'}:
            return _complete(db, shop, order, user, product, {'items': local, 'source': 'local_stock'}, charge)
        if saved.get('phase') in {'requesting', 'uncertain'} or order.get('supplier_outcome_uncertain') or saved.get('supplier_partial_items'):
            return _review(db, shop, order, 'Vendor purchase may have been processed. Check the existing vendor order; recovery will not purchase again')
        # Version 1 always persists 'requesting' before any vendor HTTP call.
        if saved.get('phase') == 'failed' or (order.get('api_recovery_version') == 1 and not saved.get('phase')):
            return _failed(db, shop, order, user, charged=True, reason='Confirmed that fulfillment did not start or was rejected')
        return _review(db, shop, order, 'Saved information is insufficient to safely deliver or refund automatically')
    finally:
        db.api_purchase_locks.delete_one({'key': key, 'owner': token})


def process_recovery(app, shop, *, limit=10):
    query = {'api_purchase': True, 'status': 'api_processing', 'created_at': {'$lt': now() - timedelta(minutes=2)},
             'api_recovery_state': {'$ne': 'review'}}
    for row in app.db.orders.find(query).sort('created_at', 1).limit(limit):
        try:
            recover_order(app.db, shop, row['order_id'])
        except Exception:
            app.logger.exception('API recovery will retry order %s', row['order_id'])


def register_routes(app, shop):
    @app.post('/orders/<order_id>/recover-api')
    @shop.login_required
    @shop.owner_required
    @shop.csrf_required
    def recover_api_order(order_id):
        try:
            result = recover_order(app.db, shop, order_id, action=request.form.get('action') or None,
                confirmed_items=shop.split_stock(request.form.get('items', '')),
                verified=request.form.get('verified') == '1', note=request.form.get('note', ''), actor=shop.current_admin_username())
            flash({'busy': 'A purchase is still processing for this user. Try again after its lease expires.',
                'review': 'Vendor outcome needs review. No new purchase or extra charge was made.',
                'completed': 'API order completed from confirmed items.', 'refunded': 'Wallet refunded and stock released.',
                'cancelled': 'Uncharged order cancelled and stock released.'}.get(result, 'Order is already resolved.'), 'info')
        except ValueError as error:
            flash(str(error), 'error')
        return redirect(url_for('order_detail', order_id=order_id.upper()))
