"""Persistent, one-time stock watches shared by the bot and WebAdmin worker."""
import uuid
from datetime import datetime, timezone, timedelta

from utils.stock_reservations import operation, _run, _arun

DEFAULTS = {
    "stock_available_again_delay_seconds": "30",
    "stock_available_again_cooldown_minutes": "30",
}


def now():
    return datetime.now(timezone.utc)


def subscription_id(product_id, user_id):
    return str(product_id) + ":" + str(int(user_id))


def ensure_indexes(db):
    db.stock_subscriptions.create_index([("active", 1), ("next_check_at", 1), ("_id", 1)])
    db.stock_subscriptions.create_index("expires_at", expireAfterSeconds=0)


def _subscribe(product_id, user_id):
    key = subscription_id(product_id, user_id)
    yield operation("stock_subscriptions", "update_one", {"_id": key}, {"$setOnInsert": {
        "product_id": product_id, "user_id": int(user_id), "active": False}}, upsert=True)
    for _ in range(5):
        record = yield operation("stock_subscriptions", "find_one", {"_id": key})
        if record.get("active"):
            return record
        token = uuid.uuid4().hex
        changed = yield operation("stock_subscriptions", "update_one",
            {"_id": key, "active": False, "token": record.get("token")}, {"$set": {
                "active": True, "token": token, "subscribed_at": now(), "next_check_at": now()},
                "$unset": {"expires_at": "", "available_since": "", "pending_job": "", "pending_payload": "", "delivery_job": ""}})
        if changed.modified_count:
            return (yield operation("stock_subscriptions", "find_one", {"_id": key}))
    raise RuntimeError("Could not update stock notification preference")


def _unsubscribe(product_id, user_id):
    return (yield operation("stock_subscriptions", "update_one",
        {"_id": subscription_id(product_id, user_id)}, {"$set": {
            "active": False, "token": uuid.uuid4().hex, "expires_at": now() + timedelta(days=30)},
            "$unset": {"available_since": "", "pending_job": "", "pending_payload": "", "delivery_job": ""}}))


def subscribe(db, product_id, user_id):
    return _run(db, _subscribe(product_id, user_id))


async def subscribe_async(db, product_id, user_id):
    return await _arun(db, _subscribe(product_id, user_id))


async def unsubscribe_async(db, product_id, user_id):
    return await _arun(db, _unsubscribe(product_id, user_id))


async def is_subscribed_async(db, product_id, user_id):
    return bool(await db.stock_subscriptions.find_one({
        "_id": subscription_id(product_id, user_id), "active": True}))
