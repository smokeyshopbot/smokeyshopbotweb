"""Grouped, low-priority notification cleanup with per-chat delivery outcomes."""
import hashlib
from datetime import datetime, timezone, timedelta

from pymongo import UpdateOne

PERMANENT_DELETE_ERRORS = (
    "message can't be deleted", "message cannot be deleted", "message can’t be deleted",
    "message_delete_forbidden", "not enough rights to delete",
)


def utc(value):
    if not isinstance(value, datetime):
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def permanent_refusal(error):
    return any(text in str(error or '').lower() for text in PERMANENT_DELETE_ERRORS)


def expired(payload, current=None):
    sent = utc(payload.get('sent_at'))
    return bool(sent and sent <= (current or datetime.now(timezone.utc)) - timedelta(hours=48))


def parent_id(source):
    return 'cleanup:' + hashlib.sha256(str(source).encode()).hexdigest()[:24]


class CleanupBatch:
    def __init__(self):
        self.parents = {}
        self.children = []

    def add(self, *, source, title, chat_id, message_ids, job_id, product_name='',
            sent_at=None, record_ids=None, deleted_by='', group=False, targets=None):
        current = datetime.now(timezone.utc)
        parent = parent_id(source)
        self.parents[parent] = UpdateOne({'job_id': parent}, {'$setOnInsert': {
            'job_id': parent, 'kind': 'cleanup', 'payload': {'title': title, 'product_name': product_name},
            'status': 'expanded', 'priority': 25, 'attempts': 0, 'created_at': current,
            'expires_at': current + timedelta(days=30)}}, upsert=True)
        ids = sorted({int(mid) for mid in message_ids})
        payload = {'chat_id': chat_id, 'message_id': ids[0], 'message_ids': ids,
                   'cleanup_batch': True, 'group_recipient': bool(group), 'deleted_by': deleted_by}
        if sent_at is not None:
            payload['sent_at'] = sent_at
        if record_ids:
            payload['notification_record_ids'] = list(record_ids)
        if targets:
            payload['targets'] = list(targets)
        too_old = expired(payload, current)
        self.children.append(UpdateOne({'job_id': job_id}, {'$setOnInsert': {
            'job_id': job_id, 'parent_id': parent, 'kind': 'delete', 'payload': payload,
            'status': 'cancelled' if too_old else 'queued', 'priority': 25, 'attempts': 0,
            'created_at': current, 'next_attempt_at': current,
            **({'cleanup_unavailable': True, 'error': 'Skipped: message is outside Telegram deletion window.',
                'expires_at': current + timedelta(days=30)} if too_old else {})}}, upsert=True))

    def add_stock(self, job, previous):
        payload = job['payload']
        meta = payload.get('notification') or {}
        name = payload.get('product_name', '')
        started = utc(job.get('created_at')) or datetime.now(timezone.utc)
        source = meta.get('notification_batch_id') or job.get('parent_id') or f'stock:{name}:{int(started.timestamp() // 60)}'
        self.add(source='stock:' + source, title='Previous stock notification cleanup',
            chat_id=payload['chat_id'], message_ids=[previous['message_id']],
            job_id=f"stock-cleanup:{payload['chat_id']}:{previous['message_id']}", product_name=name,
            sent_at=previous.get('telegram_sent_at') or previous.get('delivery_sent_at'),
            group=payload.get('group_recipient', False))

    @property
    def operations(self):
        return list(self.parents.values()) + self.children


def enqueue_stock_cleanup(db, job, previous):
    batch = CleanupBatch()
    batch.add_stock(job, previous)
    db.delivery_jobs.bulk_write(batch.operations, ordered=False)


def record_delete_outcome(db, payload, *, deleted=False, skipped=False, error=''):
    query = ({'_id': {'$in': payload['notification_record_ids']}} if payload.get('notification_record_ids') else
             {'_id': payload['notification_record_id']} if payload.get('notification_record_id') else
             {'chat_id': payload['chat_id'], 'message_id': {'$in': payload.get('message_ids') or [payload['message_id']]}})
    fields = {'deletion_pending': False}
    if deleted:
        fields.update(deleted=True, deleted_at=datetime.now(timezone.utc), deleted_by=payload.get('deleted_by', 'owner'),
                      deletion_error='', deletion_skipped=False)
    elif skipped:
        fields.update(deletion_skipped=True, deletion_error=str(error)[:500])
    else:
        fields['deletion_error'] = str(error)[:500]
    db.sent_notifications.update_many(query, {'$set': fields})


def discard_expired_targets(db, job):
    """Drop only expired members of a same-chat multi-delete, never fresh ones."""
    if job.get('acknowledged_at'):
        return
    payload = job['payload']
    targets = payload.get('targets') or []
    old = [target for target in targets if expired(target)]
    if not old:
        return
    record_delete_outcome(db, {**payload, 'notification_record_ids': [t['record_id'] for t in old]},
        skipped=True, error='Skipped: message is outside Telegram deletion window.')
    live = [target for target in targets if not expired(target)]
    payload['skipped_message_count'] = payload.get('skipped_message_count', 0) + len(old)
    if live:
        payload.update(targets=live, message_ids=[t['message_id'] for t in live], message_id=live[0]['message_id'],
            notification_record_ids=[t['record_id'] for t in live], sent_at=min(utc(t['sent_at']) for t in live))
    else:
        payload['targets'] = []
    db.delivery_jobs.update_one({'job_id': job['job_id'], 'claim_token': job['claim_token']}, {'$set': {'payload': payload}})


def sending_work_exists(db):
    return bool(db.delivery_jobs.find_one({'kind': {'$nin': ['delete', 'cleanup']}, '$or': [
        {'status': 'queued', 'next_attempt_at': {'$lte': datetime.now(timezone.utc)}},
        {'status': 'working', 'acknowledged_at': {'$exists': False}}]}, {'_id': 1}))


def upgrade_pending(db):
    cleanup = bool(db.delivery_jobs.find_one({'kind': 'delete', 'parent_id': {'$exists': False},
        'status': {'$ne': 'working'}}, {'_id': 1}))
    legacy = bool(db.user_product_notifications.find_one({
        'updated_at': {'$gte': datetime.now(timezone.utc)-timedelta(hours=48)},
        'message_id': {'$exists': True}}, {'_id': 1}))
    complete = db.delivery_control.find_one({'key': 'notification_backfill_v45', 'complete': True}, {'_id': 1}) if legacy else True
    backfill = legacy and not complete
    return cleanup, backfill


def split_refused_batch(db, job):
    """A batch rejection must not prevent valid messages in that chat being deleted."""
    payload = job['payload']
    targets = payload.get('targets') or []
    if len(payload.get('message_ids') or []) <= 1 or len(targets) <= 1:
        return False
    current = datetime.now(timezone.utc)
    operations = []
    for target in targets:
        jid = job['job_id'] + ':' + str(target['message_id'])
        child = {**payload, 'message_id': target['message_id'], 'message_ids': [target['message_id']],
                 'notification_record_ids': [target['record_id']], 'targets': [target], 'sent_at': target['sent_at']}
        operations.append(UpdateOne({'job_id': jid}, {'$setOnInsert': {
            'job_id': jid, 'kind': 'delete', 'parent_id': job.get('parent_id') or job['job_id'], 'payload': child,
            'priority': 25, 'status': 'queued', 'attempts': 0, 'created_at': current, 'next_attempt_at': current}}, upsert=True))
    db.delivery_jobs.bulk_write(operations, ordered=False)
    return True


def adopt_legacy_cleanup(db, limit=100):
    """Bounded idle-worker upgrade; never change a working job or resend a notice."""
    rows = list(db.delivery_jobs.find({'kind': 'delete', 'parent_id': {'$exists': False},
        'status': {'$ne': 'working'}}).sort([('created_at', -1), ('_id', -1)]).limit(limit))
    parents, updates, log_updates = {}, [], []
    current = datetime.now(timezone.utc)
    for row in rows:
        created = utc(row.get('created_at')) or current
        category = 'stock' if row['job_id'].startswith('stock-cleanup:') else 'messages'
        parent = parent_id(f'legacy:{category}:{int(created.timestamp() // 60)}')
        parents[parent] = UpdateOne({'job_id': parent}, {'$setOnInsert': {
            'job_id': parent, 'kind': 'cleanup', 'payload': {'title': 'Previous stock notification cleanup' if category == 'stock' else 'Message cleanup'},
            'status': 'expanded', 'priority': 25, 'attempts': 0, 'created_at': created,
            'expires_at': current + timedelta(days=30)}}, upsert=True)
        fields = {'parent_id': parent, 'priority': 25, 'payload.cleanup_batch': True}
        if row.get('status') != 'done' and (permanent_refusal(row.get('error')) or expired(row.get('payload') or {})):
            fields.update(status='cancelled', cleanup_unavailable=True, expires_at=current + timedelta(days=30))
            payload = row.get('payload') or {}
            query = ({'_id': payload['notification_record_id']} if payload.get('notification_record_id') else
                     {'chat_id': payload.get('chat_id'), 'message_id': payload.get('message_id')})
            log_updates.append(UpdateOne(query, {'$set': {'deletion_pending': False, 'deletion_skipped': True,
                'deletion_error': str(row.get('error') or 'Message is outside Telegram deletion window.')[:500]}}))
        updates.append(UpdateOne({'_id': row['_id'], 'status': {'$ne': 'working'}, 'parent_id': {'$exists': False}}, {'$set': fields}))
    if updates:
        db.delivery_jobs.bulk_write(list(parents.values()) + updates, ordered=False)
    if log_updates:
        db.sent_notifications.bulk_write(log_updates, ordered=False)
    return len(rows)
