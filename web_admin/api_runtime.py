"""Bounded API read caches, early burst rejection and non-financial usage batching.

Wallets, credentials, order retries and purchase stock/price checks are never
cached. Durable minute limits remain shared between all Gunicorn workers.
"""
import copy
import hashlib
import math
import threading
import time
import weakref
from collections import OrderedDict
from datetime import datetime, timezone

from utils.data_guard import raw_database, work_guard

DEFAULTS = {
    'api_catalog_cache_seconds': '5',
    'api_key_burst_per_second': '10',
    'api_ip_burst_per_second': '20',
    'api_key_requests_per_minute': '60',
    'api_ip_requests_per_minute': '180',
    'api_allow_legacy_query_keys': '1',
}
_RUNTIMES = weakref.WeakSet()
_REGISTRY_LOCK = threading.Lock()


def invalidate(db):
    with _REGISTRY_LOCK:
        runtimes = list(_RUNTIMES)
    for runtime in runtimes:
        if raw_database(runtime.db) is raw_database(db):
            runtime.clear()


class CacheBusy(RuntimeError):
    pass


class ReadCache:
    """Bounded single-flight loads; concurrent misses do not occupy web threads."""
    def __init__(self, maximum=256):
        self.maximum = maximum
        self.lock = threading.RLock()
        self.rows = OrderedDict()
        self.loading = set()
        self.generation = 0

    def clear(self):
        with self.lock:
            self.rows.clear()
            self.generation += 1

    def get(self, key, seconds, loader):
        if seconds <= 0:
            return loader()
        with self.lock:
            row = self.rows.get(key)
            if row and row[0] > time.monotonic():
                self.rows.move_to_end(key)
                return copy.deepcopy(row[1])
            if key in self.loading:
                raise CacheBusy('API data is refreshing. Retry shortly.')
            self.loading.add(key)
            generation = self.generation
        try:
            value = loader()
            with self.lock:
                if self.generation == generation:
                    self.rows[key] = (time.monotonic() + seconds, copy.deepcopy(value))
                    self.rows.move_to_end(key)
                    while len(self.rows) > self.maximum:
                        self.rows.popitem(last=False)
            return value
        finally:
            with self.lock:
                self.loading.discard(key)


class ApiRuntime:
    def __init__(self, db):
        self.db = db
        self.settings_cache = ReadCache(1)
        self.catalog = ReadCache()
        self.status_cache = ReadCache(1)
        self.limits_lock = threading.Lock()
        self.buckets = OrderedDict()
        self.blocks = OrderedDict()
        self.limit_settings = dict(DEFAULTS)
        self.usage_lock = threading.Lock()
        self.flush_lock = threading.Lock()
        self.usage = {}
        with _REGISTRY_LOCK:
            _RUNTIMES.add(self)

    def clear(self):
        self.settings_cache.clear()
        self.catalog.clear()
        self.status_cache.clear()
        with self.limits_lock:
            self.blocks.clear()
            self.buckets.clear()

    def settings(self):
        def load():
            doc = self.db.settings.find_one({'key': 'secret_settings'},
                {'value.' + key: 1 for key in DEFAULTS}) or {}
            result = dict(DEFAULTS)
            result.update({key: value for key, value in (doc.get('value') or {}).items() if key in DEFAULTS})
            self.limit_settings = result
            return result
        return self.settings_cache.get('settings', 1, load)

    def status(self):
        from web_admin.api_maintenance import status
        return self.status_cache.get('status', 1, lambda: status(self.db))

    def local_limit(self, identity, per_second, per_minute):
        """Token bucket plus memoized shared-limit rejections. No database I/O.

        Uses REMOTE_ADDR, never trusts arbitrary forwarded headers. These local
        burst limits supplement (and do not replace) the shared minute limits.
        """
        identity = hashlib.sha256(identity.encode()).hexdigest()
        current = time.monotonic()
        with self.limits_lock:
            blocked = self.blocks.get(identity)
            if blocked:
                if blocked > current:
                    self.blocks.move_to_end(identity)
                    return max(1, math.ceil(blocked - current))
                self.blocks.pop(identity, None)
            capacity = max(1, min(int(per_second), int(per_minute)))
            previous, tokens, old_capacity = self.buckets.get(identity, (current, float(capacity), capacity))
            if old_capacity != capacity:
                tokens = max(0.0, capacity - max(0.0, old_capacity - tokens))
            tokens = min(float(capacity), tokens + (current - previous) * capacity)
            delay = 0
            if tokens < 1:
                delay = 1
                self.blocks[identity] = current + 1
            else:
                tokens -= 1
            self.buckets[identity] = (current, tokens, capacity)
            self.buckets.move_to_end(identity)
            while len(self.buckets) > 4096:
                self.buckets.popitem(last=False)
            while len(self.blocks) > 4096:
                self.blocks.popitem(last=False)
            return delay

    def block(self, identity, seconds):
        identity = hashlib.sha256(identity.encode()).hexdigest()
        with self.limits_lock:
            self.blocks[identity] = time.monotonic() + seconds
            self.blocks.move_to_end(identity)
            while len(self.blocks) > 4096:
                self.blocks.popitem(last=False)

    def touch(self, user):
        """Only accepted authenticated requests count; no financial data here."""
        with self.usage_lock:
            key = user['_id']
            count, previous = self.usage.get(key, (0, datetime.min.replace(tzinfo=timezone.utc)))
            self.usage[key] = (count + 1, max(previous, datetime.now(timezone.utc)))

    def flush_usage(self):
        """Each process flushes its own increments; failures retain unflushed rows.

        A hard process kill can lose this short buffer of usage statistics. It
        cannot lose orders, wallet events, stock locks or rate-limit counters.
        """
        if not self.flush_lock.acquire(blocking=False):
            return
        try:
            with self.usage_lock:
                pending = dict(list(self.usage.items())[:100])
            if not pending:
                return
            # A single update per user lets us acknowledge partial success
            # precisely; one lease covers the entire batch.
            with work_guard(self.db, wait=False):
                for uid, (count, touched) in pending.items():
                    self.db.users.update_one({'_id': uid}, {
                        '$inc': {'api_request_count': count}, '$max': {'api_last_used_at': touched}})
                    with self.usage_lock:
                        live_count, live_touched = self.usage.get(uid, (0, touched))
                        if live_count <= count:
                            self.usage.pop(uid, None)
                        else:
                            self.usage[uid] = (live_count - count, live_touched)
        finally:
            self.flush_lock.release()

    def flush_usage_safely(self, logger):
        """Usage telemetry must never prevent financial/background recovery."""
        try:
            self.flush_usage()
        except Exception as error:
            logger.warning('API usage statistics delayed (%s); background services will continue.', type(error).__name__)
