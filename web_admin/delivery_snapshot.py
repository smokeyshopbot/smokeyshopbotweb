"""Short-lived, bounded snapshots for background notification checks only."""
import threading
import time
from flask import g


class DeliverySnapshot:
    def __init__(self, db, shop, ttl=0.5):
        self.db, self.shop, self.ttl = db, shop, ttl
        self.lock = threading.RLock()
        self.rows = {}

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

    def product(self, name):
        return self.get(('product', str(name).lower()), lambda: self.db.products.find_one({'name': self.shop.name_regex(name)}))

    def available(self, name):
        def load():
            product = self.product(name)
            return max(0, len((product or {}).get('stock') or []) - self.shop.get_pending_stock_quantity(self.db, name))
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
                profiles = {r['user_id']: r for r in self.db.users.find({'user_id': {'$in': missing}}, {
                    'user_id': 1, 'blocked': 1, 'blocked_manually': 1, 'blocked_by_delivery': 1,
                    'telegram_delivery_status': 1, 'last_message_delivered_at': 1,
                    'api_enabled': 1, 'api_key_hash': 1})}
                for uid in missing:
                    self.rows[('user', uid)] = (time.monotonic(), profiles.get(uid) or {})

    def user(self, uid):
        return self.get(('user', uid), lambda: self.db.users.find_one({'user_id': uid}) or {})


def cached_product(db, shop, name):
    snapshot = getattr(g, 'delivery_snapshot', None)
    return snapshot.product(name) if snapshot else db.products.find_one({'name': shop.name_regex(name)})
