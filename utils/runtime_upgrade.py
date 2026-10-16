"""Idempotent removal of obsolete installation metadata; no financial writes."""
import re
from utils.data_guard import work_guard, async_work_guard


def _cleanup_plan():
    old_settings = ['wallet_reconcile_enabled', 'wallet_reconcile_interval_minutes', 'wallet_reconcile_alert_chat_id']
    yield 'users', 'update_many', ({'$or': [{'wallet_reconciliation': {'$exists': True}}, {'wallet_audit': {'$exists': True}}]},
        {'$unset': {'wallet_reconciliation': '', 'wallet_audit': ''}})
    yield 'settings', 'update_one', ({'key': 'secret_settings'}, {'$unset': {'value.' + key: '' for key in old_settings}})
    yield 'settings', 'delete_many', ({'key': {'$in': old_settings}},)
    yield 'operational_control', 'delete_one', ({'_id': 'reconciliation'},)
    notice = re.compile(r'^⚠\ufe0f? Wallet review required\s+User: \d+\s')
    yield 'delivery_jobs', 'delete_many', ({'$or': [{'payload.wallet_review_user_id': {'$exists': True}}, {'payload.text': notice}]},)
    yield 'sent_notifications', 'delete_many', ({'text_preview': notice},)
    for collection in ('pending_payments', 'orders'):
        # Keep the terminal marker: old completed credits must never replay.
        yield collection, 'update_many', ({'$and': [
            {'wallet_credit_version': {'$in': [None, 0]}},
            {'$or': [{'status': 'completed'}, {'wallet_credited_at': {'$exists': True}}, {'refund_status': 'wallet_credited'}]},
            {'$or': [{'wallet_credit_state': 'legacy_unverified'}, {'wallet_credit_state': 'review',
                'wallet_credit_error': 'Legacy completion has no reliable wallet-event reference'}]}]},
            {'$set': {'wallet_credit_state': 'legacy_completed'}, '$unset': {'wallet_credit_error': ''}})
        yield collection, 'update_many', ({'$or': [{'wallet_credit_history_note': {'$exists': True}},
            {'wallet_credit_classified_at': {'$exists': True}}]}, {'$unset': {
                'wallet_credit_history_note': '', 'wallet_credit_classified_at': ''}})


def remove_obsolete_state(db):
    with work_guard(db):
        for collection, method, args in _cleanup_plan():
            getattr(db[collection], method)(*args)


async def remove_obsolete_state_async(db):
    async with async_work_guard(db):
        for collection, method, args in _cleanup_plan():
            await getattr(db[collection], method)(*args)
