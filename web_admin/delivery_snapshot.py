"""Short-lived, bounded snapshots for background notification checks only."""
import threading
import time
from flask import g

USER_FIELDS = {'user_id': 1, 'blocked': 1, 'blocked_manually': 1, 'blocked_by_delivery': 1,
               'telegram_delivery_status': 1, 'last_message_delivered_at': 1,
               'api_enabled': 1, 'api_key_hash': 1}


class DeliverySnapshot:
    def __init__(self, db, shop, ttl=0.5):
        self.db, self.shop, self.ttl = db, shop, ttl
        self.lock = threading.RLock()
        self.rows = {}
        self.bookkeeping = {}

    def get(self, key, loader):
        with self.lock:
            current = time.monotonic()
            existing = self.rows.get(key)
            if existing is not None and current - existing[0] < self.ttl:
                return existing[1]
            if len(self.rows) > 512:
                self.rows = {k: v for k, v in self.rows.items() if current - v[0] < self.ttl}
            value = loader()
            self.rows[key] = (time.monotonic(), value)
            return value

    def settings(self):
        return self.get(('settings',), lambda: self.shop.get_secret_settings(self.db))

    def maintenance(self):
        return self.get(('maintenance',), lambda: self.shop.get_setting(self.db, 'maintenance_mode', False))

    def pause(self):
        return self.get(("pause",), lambda: self.db.delivery_control.find_one({"key": "telegram_pause"}) or {})

    def invalidate(self, key):
        with self.lock:
            self.rows.pop(key, None)

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

    def product(self, name):
        def load():
            rows = list(self.db.products.aggregate([
                {'$match': {'name': self.shop.name_regex(name)}}, {'$limit': 1}, {'$project': {
                    '_id': 1, 'name': 1, 'enabled': 1, 'premium_emoji_id': 1,
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
        ids = {j.get('payload', {}).get('chat_id') for j in jobs if j.get('kind') != 'bulk'}
        ids.discard(None)
        with self.lock:
            current = time.monotonic()
            if len(self.rows) > 512:
                self.rows = {k: v for k, v in self.rows.items() if current - v[0] < self.ttl}
            missing = [uid for uid in ids if current - self.rows.get(('user', uid), (-100,))[0] >= self.ttl]
            if missing:
                profiles = {r['user_id']: r for r in self.db.users.find({'user_id': {'$in': missing}}, USER_FIELDS)}
                for uid in missing:
                    self.rows[('user', uid)] = (time.monotonic(), profiles.get(uid) or {})
            names = {j.get('payload', {}).get('product_name') for j in jobs
                     if j.get('payload', {}).get('cleanup_previous_stock') and not j.get('payload', {}).get('stock_subscription_id')}
            names.discard(None)
            from utils.stock_subscriptions import subscription_id
            watch_ids = {subscription_id(p['_id'], j['payload']['chat_id'])
                         for name in names if (p := self.product(name))
                         for j in jobs if j.get('payload', {}).get('product_name') == name
                         and j.get('payload', {}).get('chat_id') in ids}
            watch_ids = [key for key in watch_ids
                         if current - self.rows.get(('subscription', key), (-100,))[0] >= self.ttl]
            if watch_ids:
                watches = {r['_id']: r for r in self.db.stock_subscriptions.find({'_id': {'$in': watch_ids}})}
                for key in watch_ids:
                    self.rows[('subscription', key)] = (time.monotonic(), watches.get(key))

    def user(self, uid):
        return self.get(('user', uid), lambda: self.db.users.find_one({'user_id': uid}, USER_FIELDS) or {})


def cached_product(db, shop, name):
    snapshot = getattr(g, 'delivery_snapshot', None)
    return snapshot.product(name) if snapshot else db.products.find_one({'name': shop.name_regex(name)})
