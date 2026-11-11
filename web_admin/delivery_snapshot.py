"""Short-lived, bounded snapshots for background notification checks only."""
import threading
import time
from flask import g

USER_FIELDS = {'user_id': 1, 'blocked': 1, 'blocked_manually': 1, 'blocked_by_delivery': 1,
               'telegram_delivery_status': 1, 'last_message_delivered_at': 1,
               'api_enabled': 1, 'api_key_hash': 1, 'language': 1, 'language_code': 1}


class DeliverySnapshot:
    def __init__(self, db, shop, ttl=0.5, *, recipient_ttl=None):
        self.db, self.shop, self.ttl = db, shop, ttl
        # Only nonfinancial recipient/preferences data gets a slightly longer
        # window. Explicit short TTLs remain useful for callers and tests.
        self.recipient_ttl = (max(ttl, 2.0) if ttl == 0.5 else ttl) if recipient_ttl is None else recipient_ttl
        self.lock = threading.RLock()
        self.rows = {}
        self.loading = {}
        self.bookkeeping = {}
        self.chat_releases = {}

    def _ttl(self, key):
        return self.recipient_ttl if key[0] in {'user', 'subscription'} else self.ttl

    def _prune(self, current):
        if len(self.rows) > 512:
            self.rows = {key: row for key, row in self.rows.items()
                         if current - row[0] < self._ttl(key)}
            if len(self.rows) > 512:
                oldest = sorted(self.rows, key=lambda key: self.rows[key][0])[:-512]
                for key in oldest:
                    self.rows.pop(key, None)

    def get_many(self, keys, loader):
        """Coalesce overlapping misses without holding the cache lock over I/O.

        Loaders return a value for each requested key (including missing rows).
        An invalidated in-flight read may finish, but cannot refill the cache.
        """
        remaining, values = set(keys), {}
        while remaining:
            owned, waiting = {}, []
            with self.lock:
                current = time.monotonic()
                self._prune(current)
                for key in remaining:
                    existing = self.rows.get(key)
                    if existing is not None and current - existing[0] < self._ttl(key):
                        values[key] = existing[1]
                    elif key in self.loading:
                        waiting.append(self.loading[key])
                    else:
                        owned[key] = self.loading[key] = threading.Event()
            remaining.difference_update(values)
            if owned:
                try:
                    loaded = loader(list(owned))
                    with self.lock:
                        for key, event in owned.items():
                            if self.loading.get(key) is event:
                                value = loaded[key]
                                self.rows[key] = (time.monotonic(), value)
                                values[key] = value
                        self._prune(time.monotonic())
                finally:
                    with self.lock:
                        for key, event in owned.items():
                            if self.loading.get(key) is event:
                                self.loading.pop(key)
                            event.set()
                remaining.difference_update(values)
            for event in waiting:
                event.wait()
        return values

    def get(self, key, loader):
        return self.get_many([key], lambda keys: {key: loader()})[key]

    def settings(self):
        return self.get(('settings',), lambda: self.shop.get_secret_settings(self.db))

    def maintenance(self):
        return self.get(('maintenance',), lambda: self.shop.get_setting(self.db, 'maintenance_mode', False))

    def pause(self):
        return self.get(("pause",), lambda: self.db.delivery_control.find_one({"key": "telegram_pause"}) or {})

    def invalidate(self, key):
        with self.lock:
            self.rows.pop(key, None)
            pending = self.loading.pop(key, None)
            if pending is not None:
                pending.set()

    def defer_bookkeeping(self, job):
        with self.lock:
            self.bookkeeping[job['job_id']] = job

    def take_bookkeeping(self, limit=100):
        with self.lock:
            keys = list(self.bookkeeping)[:limit]
            return [self.bookkeeping.pop(key) for key in keys]

    def bookkeeping_count(self):
        with self.lock:
            return len(self.bookkeeping)

    def defer_chat_release(self, owner, operation):
        with self.lock:
            self.chat_releases[owner] = operation

    def take_chat_releases(self):
        with self.lock:
            result, self.chat_releases = self.chat_releases, {}
            return result

    def chat_release_count(self):
        with self.lock:
            return len(self.chat_releases)

    def product(self, name):
        def load():
            rows = list(self.db.products.aggregate([
                {'$match': {'name': self.shop.name_regex(name)}}, {'$limit': 1}, {'$project': {
                    '_id': 1, 'name': 1, 'enabled': 1, 'premium_emoji_id': 1,
                    'sale': 1,
                    'stock_count': {'$size': {'$ifNull': ['$stock', []]}}}}]))
            return rows[0] if rows else None
        return self.get(('product', str(name).lower()), load)

    def subscription(self, record_id):
        return self.get(('subscription', record_id), lambda: self.db.stock_subscriptions.find_one({'_id': record_id}))

    def available(self, name):
        def load():
            product = self.product(name)
            return max(0, int((product or {}).get('stock_count') or 0) - self.shop.get_pending_stock_quantity(self.db, name))
        return self.get(('available', str(name).lower()), load)

    def prefetch_users(self, jobs):
        # One bounded query for the next worker page, rather than one per user.
        ids = {j.get('payload', {}).get('chat_id') for j in jobs
               if j.get('kind') not in {'bulk', 'stock_work', 'delete', 'cleanup'}}
        ids.discard(None)
        def users(keys):
            profiles = {r['user_id']: r for r in self.db.users.find(
                {'user_id': {'$in': [key[1] for key in keys]}}, USER_FIELDS)}
            return {key: profiles.get(key[1]) or {} for key in keys}
        self.get_many([('user', uid) for uid in ids], users)
        names = {j.get('payload', {}).get('product_name') for j in jobs
                 if j.get('payload', {}).get('cleanup_previous_stock') and not j.get('payload', {}).get('stock_subscription_id')}
        names.discard(None)
        from utils.stock_subscriptions import subscription_id
        watch_ids = {subscription_id(p['_id'], j['payload']['chat_id'])
                     for name in names if (p := self.product(name))
                     for j in jobs if j.get('payload', {}).get('product_name') == name
                     and j.get('payload', {}).get('chat_id') in ids}
        def watches(keys):
            rows = {r['_id']: r for r in self.db.stock_subscriptions.find(
                {'_id': {'$in': [key[1] for key in keys]}})}
            return {key: rows.get(key[1]) for key in keys}
        self.get_many([('subscription', record_id) for record_id in watch_ids], watches)

    def user(self, uid):
        return self.get(('user', uid), lambda: self.db.users.find_one({'user_id': uid}, USER_FIELDS) or {})


def cached_product(db, shop, name):
    snapshot = getattr(g, 'delivery_snapshot', None)
    return snapshot.product(name) if snapshot else db.products.find_one(
        {'name': shop.name_regex(name)}, {'_id': 1, 'name': 1, 'enabled': 1, 'premium_emoji_id': 1, 'sale': 1})
