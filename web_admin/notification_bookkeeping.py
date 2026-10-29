"""Batch optional notice records after the Telegram acknowledgement is durable.

Paid deliveries keep their existing immediate bookkeeping. Every deferred job
retains its acknowledged result and lease in MongoDB until this batch finishes;
normal expired-job recovery can replay bookkeeping without sending again.
"""
from datetime import datetime, timezone, timedelta

from flask import g
from pymongo import UpdateOne


def finish_notices(db, shop, jobs):
    logs, users, products, cleanup, finished = [], [], [], [], []
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    current = datetime.now(timezone.utc)
    def stamp(value):
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value
    jobs = sorted(jobs, key=lambda j: stamp(j['acknowledged_at']))
    keys = [{'user_id': j['payload']['chat_id'],
             'product_key': shop._product_notification_key(j['payload']['product_name']), 'kind': 'new_stock'}
            for j in jobs if j['payload'].get('cleanup_previous_stock')]
    previous = {(r['user_id'], r['product_key']): r for r in
                db.user_product_notifications.find({'$or': keys})} if keys else {}
    for job in jobs:
        payload, result = job['payload'], job['telegram_result']
        uid = payload['chat_id']
        snapshot = getattr(g, 'delivery_snapshot', None)
        if snapshot:
            g.delivery_user_profile = (uid, job['_notice_user'] if '_notice_user' in job else snapshot.user(uid))
        meta = payload.get('notification') or {}
        shop.record_sent_notification(db, chat_id=uid, result=result, text=payload.get('text', ''),
            notification_type=meta.get('notification_type', 'message'),
            batch_id=meta.get('notification_batch_id') or job['job_id'], title=meta.get('notification_title') or 'Message',
            source=meta.get('notification_source', 'web_admin'), product_name=meta.get('notification_product_name', ''),
            parse_mode=payload.get('parse_mode'), target_kind=meta.get('notification_target_kind', 'customer'), operations=logs)
        shop.mark_user_delivery_success(db, uid, source='background_delivery', operations=users)
        mid = shop._telegram_result_message_id(result)
        if payload.get('cleanup_previous_stock') and mid:
            product_key = shop._product_notification_key(payload['product_name'])
            key = (uid, product_key)
            old = previous.get(key) or {}
            sent_at = stamp(job['acknowledged_at'])
            old_at = old.get('delivery_sent_at') or old.get('updated_at') or epoch
            if old_at.tzinfo is None:
                old_at = old_at.replace(tzinfo=timezone.utc)
            # A retry of an older acknowledged batch must not delete a newer
            # stock notice or replace its tracking record.
            if old_at <= sent_at:
                old_mid = old.get('message_id')
                if old_mid and old_mid != mid:
                    jid = f'stock-cleanup:{uid}:{old_mid}'
                    cleanup.append(UpdateOne({'job_id': jid}, {'$setOnInsert': {
                        'job_id': jid, 'kind': 'delete', 'payload': {'chat_id': uid, 'message_id': old_mid,
                            'group_recipient': False}, 'status': 'queued', 'priority': 25, 'attempts': 0,
                        'created_at': current, 'next_attempt_at': current}}, upsert=True))
                fields = {'user_id': uid, 'product_name': payload['product_name'], 'product_key': product_key,
                          'kind': 'new_stock', 'message_id': mid, 'updated_at': current, 'delivery_sent_at': sent_at}
                newer = {'$lte': [{'$ifNull': ['$delivery_sent_at', {'$ifNull': ['$updated_at', epoch]}]}, sent_at]}
                products.append(UpdateOne({'user_id': uid, 'product_key': product_key, 'kind': 'new_stock'},
                    [{'$set': {**{name: {'$cond': [newer, {'$literal': value}, '$' + name]}
                               for name, value in fields.items()},
                               'created_at': {'$ifNull': ['$created_at', current]}}}], upsert=True))
                previous[key] = fields
        finished.append(UpdateOne({'job_id': job['job_id'], 'claim_token': job['claim_token'],
                                   'acknowledged_at': {'$exists': True}}, {'$set': {
            'status': 'done', 'error': '', 'updated_at': current, 'expires_at': current + timedelta(days=30)},
            '$unset': {'lease_until': '', 'claim_token': ''}}))
    # All records and cleanup jobs are idempotent. A partial failure leaves the
    # acknowledged delivery unfinished, so retry/recovery only repeats records.
    for collection, operations, ordered in [
            (db.sent_notifications, logs, False), (db.users, users, False),
            (db.delivery_jobs, cleanup, False), (db.user_product_notifications, products, True),
            (db.delivery_jobs, finished, False)]:
        if operations:
            collection.bulk_write(operations, ordered=ordered)
