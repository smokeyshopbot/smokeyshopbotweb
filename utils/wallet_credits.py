"""Retryable wallet-credit intents. Completion follows an idempotent wallet event."""
from datetime import datetime, timezone
from decimal import Decimal
from utils.wallet_audit import change_wallet, change_wallet_async, event_id, LEGACY_CREDIT_ERROR, legacy_credit_history


class WalletCreditReview(RuntimeError):
    pass


def _refund_amount(row):
    currency = str(row.get('refund_currency') or '').lower()
    amount = float(row.get('refund_amount') or 0)
    if currency in {'inr', 'usdt'} and amount > 0:
        return currency, round(amount, 2)
    method = str(row.get('payment_method') or '').lower()
    if method in {'upi', 'wallet_inr', 'inr'} or 'upi' in method or method.endswith('_inr'):
        return 'inr', round(max(0, float(row.get('amount_inr') or 0)), 2)
    if float(row.get('amount_usdt') or 0) > 0:
        return 'usdt', round(float(row['amount_usdt']), 2)
    return 'inr', round(max(0, float(row.get('amount_inr') or 0)), 2)


def _credit(kind, reference, user_id=None):
    collection = 'pending_payments' if kind == 'topup' else 'orders'
    key = 'ref_id' if kind == 'topup' else 'order_id'
    row = yield collection, 'find_one', ({key: reference},), {}
    if not row or (user_id is not None and int(row.get('user_id', 0)) != int(user_id)):
        return None
    if row.get('wallet_credit_state') in {'done', 'legacy_unverified'}:
        return None
    if legacy_credit_history(row):
        yield collection, 'update_one', ({'_id': row['_id'], 'wallet_credit_state': 'review',
            'wallet_credit_error': LEGACY_CREDIT_ERROR, 'wallet_credit_version': {'$in': [None, 0]}}, {'$set': {
                'wallet_credit_state': 'legacy_unverified', 'wallet_credit_history_note': LEGACY_CREDIT_ERROR,
                'wallet_credit_classified_at': datetime.now(timezone.utc)}, '$unset': {'wallet_credit_error': ''}}), {}
        return None
    if row.get('wallet_credit_state') == 'review':
        raise WalletCreditReview('Wallet credit needs owner review; no extra credit was applied')
    if kind == 'topup':
        if row.get('admin_wallet_adjustment') or row.get('method') in {'admin_add', 'admin_remove'}:
            return None
        if row.get('pay_type') != 'wallet' or row.get('status') not in {'confirmed', 'approved', 'completed'}:
            return None
        currency, amount = row.get('currency', 'inr'), round(float(row.get('load_amount') or 0), 2)
        marker = 'wallet_event_id'
        eligible = {'status': {'$in': ['confirmed', 'approved', 'completed']}, 'pay_type': 'wallet'}
        legacy_complete = bool(row.get('wallet_credited_at') or row.get('status') == 'completed')
    else:
        if row.get('refund_status') not in {'waiting_user_choice', 'wallet_credit_pending', 'wallet_credited'} or row.get('refund_wallet_enabled') is False:
            return None
        currency, amount = _refund_amount(row)
        marker = 'wallet_refund_event_id'
        eligible = {'refund_status': {'$in': ['waiting_user_choice', 'wallet_credit_pending', 'wallet_credited']},
                    'refund_wallet_enabled': {'$ne': False}}
        legacy_complete = row.get('refund_status') == 'wallet_credited'
    uid = int(row['user_id'])
    currency = row.get('wallet_credit_currency') or currency
    amount = float(row.get('wallet_credit_amount', amount))
    if currency not in {'inr', 'usdt'} or not Decimal(str(amount)).is_finite() or amount <= 0:
        yield collection, 'update_one', ({'_id': row['_id']}, {'$set': {'wallet_credit_state': 'review', 'wallet_credit_error': 'Invalid credit amount or currency'}}), {}
        raise WalletCreditReview('Wallet credit amount is invalid; owner review required')
    wallet_reference = ('topup-' if kind == 'topup' else 'refund-') + reference
    identifier = event_id(uid, currency, wallet_reference)
    if legacy_complete and not row.get('wallet_credit_version') and row.get(marker) != identifier:
        # Older unaudited credits cannot safely be distinguished from missing credits.
        event = yield 'wallet_ledger', 'find_one', ({'_id': identifier},), {}
        if not event:
            yield collection, 'update_one', ({'_id': row['_id'], 'wallet_credit_state': {'$exists': False}},
                {'$set': {'wallet_credit_state': 'legacy_unverified', 'wallet_credit_history_note': LEGACY_CREDIT_ERROR,
                          'wallet_credit_classified_at': datetime.now(timezone.utc)}}), {}
            return None  # The old completion is not proof that another credit is owed.
    if row.get('wallet_credit_state') != 'pending':
        fields = {'wallet_credit_state': 'pending', 'wallet_credit_version': 1,
                  'wallet_credit_currency': currency, 'wallet_credit_amount': amount,
                  'wallet_credit_started_at': datetime.now(timezone.utc), marker: identifier,
                  'wallet_event_delta' if kind == 'topup' else 'wallet_refund_event_delta': amount}
        if kind == 'refund':
            fields.update(refund_status='wallet_credit_pending', refund_choice='wallet')
        changed = yield collection, 'update_one', ({'_id': row['_id'], **eligible,
            'wallet_credit_state': row.get('wallet_credit_state') if 'wallet_credit_state' in row else {'$exists': False}}, {'$set': fields}), {}
        if not changed.matched_count:
            return (yield from _credit(kind, reference, user_id))
    user = yield 'wallet', 'credit', (uid, currency, amount), {'reference': wallet_reference, 'source': kind if kind == 'topup' else 'refund'}
    if not user:
        yield collection, 'update_one', ({'_id': row['_id'], 'wallet_credit_state': 'pending'}, {'$set': {
            'wallet_credit_state': 'review', 'wallet_credit_error': 'Wallet account is missing; verify the user before crediting'}}), {}
        raise WalletCreditReview('Wallet account is missing; owner review required')
    completed = datetime.now(timezone.utc)
    fields = {'wallet_credit_state': 'done', 'wallet_credit_completed_at': completed}
    if kind == 'topup':
        fields.update(wallet_credited_at=row.get('wallet_credited_at') or completed, status='completed')
    else:
        fields.update(refund_status='wallet_credited', refund_wallet_credited_at=row.get('refund_wallet_credited_at') or completed,
                      refund_completed_at=row.get('refund_completed_at') or completed, refund_currency=currency, refund_amount=amount)
    changed = yield collection, 'update_one', ({'_id': row['_id'], 'wallet_credit_state': 'pending'},
        {'$set': fields, '$unset': {'wallet_credit_error': ''}}), {}
    if not changed.modified_count:
        return None
    return {**row, **fields, marker: identifier, 'wallet_event_delta' if kind == 'topup' else 'wallet_refund_event_delta': amount}


def _run(db, generator):
    value = None
    while True:
        try:
            collection, method, args, kwargs = generator.send(value)
        except StopIteration as result:
            return result.value
        value = change_wallet(db, *args, **kwargs) if collection == 'wallet' else getattr(db[collection], method)(*args, **kwargs)


async def _arun(db, generator):
    value = None
    while True:
        try:
            collection, method, args, kwargs = generator.send(value)
        except StopIteration as result:
            return result.value
        value = await change_wallet_async(db, *args, **kwargs) if collection == 'wallet' else await getattr(db[collection], method)(*args, **kwargs)


def credit_topup(db, reference, user_id=None): return _run(db, _credit('topup', str(reference), user_id))
async def credit_topup_async(db, reference, user_id=None): return await _arun(db, _credit('topup', str(reference), user_id))
def credit_refund(db, order_id, user_id=None): return _run(db, _credit('refund', str(order_id).upper(), user_id))
async def credit_refund_async(db, order_id, user_id=None): return await _arun(db, _credit('refund', str(order_id).upper(), user_id))


def ensure_indexes(db):
    for name in ('pending_payments', 'orders'):
        db[name].create_index([('wallet_credit_state', 1), ('_id', 1)], name='wallet_credit_recovery')


def recover_credits(db, *, limit=10):
    """New intents and auditable old completions; never infer an unaudited credit."""
    recovered = 0
    for name, kind, query in _recovery_sources():
        for row in db[name].find(query).sort('_id', 1).limit(limit):
            try:
                result = credit_topup(db, row['ref_id']) if kind == 'topup' else credit_refund(db, row['order_id'])
                recovered += bool(result)
            except WalletCreditReview:
                continue
            except Exception:
                # A transient failure keeps the same durable intent for the next pass.
                continue
    return recovered


def _recovery_sources():
    return (
        ('pending_payments', 'topup', {'pay_type': 'wallet', 'admin_wallet_adjustment': {'$ne': True}, 'method': {'$nin': ['admin_add', 'admin_remove']}, '$or': [
            {'wallet_credit_state': 'pending'},
            {'wallet_credit_state': 'review', 'wallet_credit_error': LEGACY_CREDIT_ERROR, 'wallet_credit_version': {'$in': [None, 0]}},
            {'wallet_credit_state': {'$exists': False}, 'status': {'$in': ['confirmed', 'approved', 'completed']}}]}),
        ('orders', 'refund', {'$or': [
            {'wallet_credit_state': 'pending', 'refund_status': 'wallet_credit_pending'},
            {'wallet_credit_state': 'review', 'wallet_credit_error': LEGACY_CREDIT_ERROR, 'wallet_credit_version': {'$in': [None, 0]}},
            {'wallet_credit_state': {'$exists': False}, 'refund_status': 'wallet_credited'}]}))


async def recover_credits_async(db, *, limit=10):
    recovered = 0
    for name, kind, query in _recovery_sources():
        rows = await db[name].find(query).sort('_id', 1).limit(limit).to_list(length=limit)
        for row in rows:
            try:
                result = await credit_topup_async(db, row['ref_id']) if kind == 'topup' else await credit_refund_async(db, row['order_id'])
                recovered += bool(result)
            except WalletCreditReview:
                continue
            except Exception:
                continue
    return recovered
