"""Bounded batch claims for promotional messages; paid delivery paths stay unchanged."""
import threading
import uuid
from datetime import datetime, timezone, timedelta

from pymongo import UpdateOne
from pymongo.errors import BulkWriteError

from utils.notification_limits import _chat_key, _chat_query, _chat_fields, _cooldown


def eligible(row):
    payload = row.get('payload') or {}
    return (row.get('kind') == 'text' and payload.get('bulk_recipient') and
            not payload.get('group_recipient') and not (payload.get('delivery_meta') or {}).get('order_id'))


class ChatReservation:
    def __init__(self, snapshot, chat_id, owner, valid_until):
        self.snapshot, self.chat_id, self.owner = snapshot, chat_id, owner
        self.valid_until = valid_until
        self.lock = threading.Lock()
        self.released = False

    def valid(self):
        # Leave room for persisting send intent and a complete HTTP timeout.
        return datetime.now(timezone.utc) + timedelta(seconds=60) < self.valid_until

    def release(self, entered=False):
        with self.lock:
            if self.released:
                return
            self.released = True
            fields = {'$unset': {'owner': '', 'lease_until': ''}}
            if entered:
                fields['$set'] = {'next_at': datetime.now(timezone.utc) + timedelta(seconds=_cooldown(self.chat_id, False))}
            self.snapshot.defer_chat_release(self.owner, UpdateOne(
                {'key': _chat_key(self.chat_id), 'owner': self.owner}, fields))


def claim_notices(db, ready, snapshot, sort):
    """Claim exactly the selected jobs, then atomically lease each distinct chat.

    Unsatisfied leases return to the queue without setting send_started_at.
    Only a confirmed successful lease is handed to the transport worker.
    """
    token = uuid.uuid4().hex
    current = datetime.now(timezone.utc)
    ids = [row['_id'] for row in ready]
    db.delivery_jobs.update_many({'_id': {'$in': ids}, 'status': 'queued', 'next_attempt_at': {'$lte': current},
        'kind': 'text', 'payload.bulk_recipient': True, 'payload.group_recipient': {'$ne': True},
        'payload.delivery_meta.order_id': {'$exists': False}},
        {'$set': {'status': 'working', 'claim_token': token, 'lease_until': current + timedelta(minutes=5)}})
    jobs = list(db.delivery_jobs.find({'_id': {'$in': ids}, 'status': 'working', 'claim_token': token}).sort(sort))
    releases = snapshot.take_chat_releases()
    operations, candidates, owners, deferred = list(releases.values()), [], [], []
    release_count = len(operations)
    seen = set()
    for job in jobs:
        uid = job['payload']['chat_id']
        if uid in seen:
            deferred.append(job['job_id'])
            continue
        seen.add(uid)
        owner = uuid.uuid4().hex
        owners.append(owner)
        candidates.append((job, owner))
        operations.append(UpdateOne(_chat_query(_chat_key(uid), current), _chat_fields(owner, current), upsert=True))
    rejected = set()
    try:
        if operations:
            try:
                db.delivery_control.bulk_write(operations, ordered=False)
            except BulkWriteError as error:
                details = error.details or {}
                failures = details.get('writeErrors', [])
                if details.get('writeConcernErrors') or any(e.get('code') != 11000 for e in failures):
                    raise
                if any(e['index'] < release_count for e in failures):
                    raise
                rejected = {e['index'] - release_count for e in failures}
        accepted = []
        for index, (job, owner) in enumerate(candidates):
            if index in rejected:
                deferred.append(job['job_id'])
            else:
                job['_chat_reservation'] = ChatReservation(snapshot, job['payload']['chat_id'], owner,
                                                            current + timedelta(minutes=5))
                accepted.append(job)
        if deferred:
            db.delivery_jobs.update_many({'job_id': {'$in': deferred}, 'claim_token': token}, {'$set': {
                'status': 'queued', 'next_attempt_at': current + timedelta(seconds=.5)},
                '$unset': {'claim_token': '', 'lease_until': ''}})
        return accepted
    except Exception:
        for owner, operation in releases.items():
            snapshot.defer_chat_release(owner, operation)
        # No transport has run. Release only leases belonging to this batch and
        # keep every job retryable, including an ambiguous bulk-write outcome.
        try:
            db.delivery_control.update_many({'owner': {'$in': owners}}, {'$unset': {'owner': '', 'lease_until': ''}})
        finally:
            db.delivery_jobs.update_many({'job_id': {'$in': [j['job_id'] for j in jobs]}, 'claim_token': token}, {
                '$set': {'status': 'queued', 'next_attempt_at': current + timedelta(seconds=5)},
                '$unset': {'claim_token': '', 'lease_until': ''}})
        raise
