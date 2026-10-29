"""Bounded availability checks and durable Telegram jobs for opted-in users."""
import uuid
from datetime import datetime, timezone, timedelta

from utils.stock_subscriptions import DEFAULTS, subscription_id
from web_admin.delivery_queue import enqueue_job, STOCK_NOTICE_PRIORITY


def now():
    return datetime.now(timezone.utc)


def utc(value):
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def complete(db, record_id, token, *, kind="stock_available_again"):
    """Acknowledge only the watch that existed when this delivery began."""
    db.stock_subscriptions.update_one({"_id": record_id, "token": token, "active": True}, {
        "$set": {"active": False, "last_notified_at": now(), "last_notice_kind": kind,
                 "expires_at": now() + timedelta(days=30)},
        "$unset": {"pending_job": "", "pending_payload": "", "available_since": "", "delivery_job": ""}})


def _stock_products(db, shop, ids):
    return {p["_id"]: p for p in shop.get_products_with_availability(
        db, product_query={"_id": {"$in": list(ids)}})}


def process_subscriptions(app, shop, settings, limit=200):
    db = app.db
    current = now()
    delay = timedelta(seconds=int(settings.get("stock_available_again_delay_seconds", DEFAULTS["stock_available_again_delay_seconds"])))
    cooldown = timedelta(minutes=int(settings.get("stock_available_again_cooldown_minutes", DEFAULTS["stock_available_again_cooldown_minutes"])))
    rows = list(db.stock_subscriptions.find({"active": True, "next_check_at": {"$lte": current}})
                .sort([("next_check_at", 1), ("_id", 1)]).limit(limit))
    products = _stock_products(db, shop, {r["product_id"] for r in rows}) if rows else {}
    for row in rows:
        match = {"_id": row["_id"], "token": row["token"], "active": True}
        next_check = current + timedelta(seconds=30)
        if row.get("pending_job"):
            job = db.delivery_jobs.find_one({"job_id": row["pending_job"]})
            if not job:
                # The durable outbox was saved before enqueueing. Reuse its ID
                # after a crash, so the same watch cannot send two jobs.
                enqueue_job(db, "text", row["pending_payload"], job_id=row["pending_job"], priority=STOCK_NOTICE_PRIORITY)
            elif job.get("acknowledged_at") or job["status"] == "done":
                complete(db, row["_id"], row["token"])
                continue
            elif job["status"] in {"failed", "cancelled"}:
                db.stock_subscriptions.update_one(match, {"$unset": {
                    "pending_job": "", "pending_payload": "", "available_since": ""},
                    "$set": {"next_check_at": next_check}})
                continue
            # Unknown Telegram outcomes stay in the existing review queue.
            db.stock_subscriptions.update_one(match, {"$set": {"next_check_at": next_check}})
            continue
        product = products.get(row["product_id"])
        user = db.users.find_one({"user_id": row["user_id"]})
        if not product or not user or user.get("blocked"):
            db.stock_subscriptions.update_one(match, {"$set": {
                "active": False, "expires_at": current + timedelta(days=30)}})
            continue
        if int(product.get("available_stock") or 0) <= 0:
            db.stock_subscriptions.update_one(match, {"$set": {"next_check_at": next_check},
                                                    "$unset": {"available_since": ""}})
            continue
        first_available = utc(row.get("available_since") or current)
        due = first_available + delay
        if row.get("last_notified_at"):
            due = max(due, utc(row["last_notified_at"]) + cooldown)
        if current < due:
            db.stock_subscriptions.update_one(match, {"$set": {
                "available_since": first_available, "next_check_at": due}})
            continue
        language = shop.lang_from_user(user)
        payload = {"chat_id": row["user_id"], "text": "", "parse_mode": "HTML",
            "language": language, "product_name": product["name"], "product_id": product["_id"],
            "stock_subscription_id": row["_id"], "stock_subscription_token": row["token"],
            "stock_notice_key": "stock_available_again", "cleanup_previous_stock": True,
            "bulk_recipient": True, "maintenance_policy": "pause",
            "notification": {"notification_type": "stock_available_again",
                "notification_batch_id": "stock-available:" + row["token"],
                "notification_title": "Stock available again: " + product["name"],
                "notification_source": "stock_subscription", "notification_product_name": product["name"],
                "notification_target_kind": "customer"}}
        job_id = "stock-available:" + row["token"] + ":" + uuid.uuid4().hex
        changed = db.stock_subscriptions.update_one({**match, "pending_job": {"$exists": False}}, {"$set": {
            "pending_job": job_id, "pending_payload": payload, "next_check_at": next_check}})
        if changed.modified_count:
            enqueue_job(db, "text", payload, job_id=job_id, priority=STOCK_NOTICE_PRIORITY)


def _claim_delivery(db, row, job):
    """Serialize overlapping normal/availability sends, including slow HTTP."""
    match = {"_id": row["_id"], "token": row["token"], "active": True}
    owner = row.get("delivery_job")
    if owner and owner != job["job_id"]:
        previous = db.delivery_jobs.find_one({"job_id": owner})
        if previous and (previous.get("acknowledged_at") or previous.get("status") == "done"):
            acknowledge_delivery(db, previous)
            return "This availability was already announced to this user"
        if previous and previous.get("status") in {"failed", "cancelled"}:
            db.stock_subscriptions.update_one({**match, "delivery_job": owner}, {"$unset": {"delivery_job": ""}})
        else:
            # A queued/working/review send owns this watch until its outcome is
            # known. Do not expire this lock and risk two delivered messages.
            return {"retry": True, "error": "Another stock message is already delivering this availability"}
    changed = db.stock_subscriptions.update_one({**match, "$or": [
        {"delivery_job": {"$exists": False}}, {"delivery_job": job["job_id"]}]},
        {"$set": {"delivery_job": job["job_id"]}})
    if not changed.matched_count:
        return {"retry": True, "error": "Stock notification preference changed during delivery"}
    job["stock_subscription_delivery"] = {"id": row["_id"], "token": row["token"]}
    return ""


def prepare_delivery(db, shop, job):
    """Validate a live watch; return an error or a retry for an overlapping send."""
    payload = job["payload"]
    if job.get("acknowledged_at"):
        return ""
    if payload.get("stock_subscription_id"):
        row = db.stock_subscriptions.find_one({"_id": payload["stock_subscription_id"],
            "token": payload["stock_subscription_token"], "active": True,
            "pending_job": job["job_id"]})
        if not row:
            return "Stock notification subscription was cancelled or already fulfilled"
        product = _stock_products(db, shop, [row["product_id"]]).get(row["product_id"])
        if not product or int(product.get("available_stock") or 0) <= 0:
            return "Stock is no longer available"
        user = db.users.find_one({"user_id": row["user_id"]})
        if not user or user.get("blocked"):
            return "User is unavailable or blocked"
        payload.update(product_name=product["name"], language=shop.lang_from_user(user),
                       current_available_stock=int(product["available_stock"]))
        payload["notification"].update(notification_title="Stock available again: " + product["name"],
                                       notification_product_name=product["name"])
        payload["reply_markup"] = shop.product_notify_markup(product["name"], payload["language"], product["_id"])
        error = _claim_delivery(db, row, job)
        if error:
            return error
    elif payload.get("cleanup_previous_stock") and not payload.get("group_recipient"):
        from web_admin.delivery_snapshot import cached_product
        product = cached_product(db, shop, payload["product_name"])
        if product:
            from flask import g
            snapshot = getattr(g, "delivery_snapshot", None)
            record_id = subscription_id(product["_id"], payload["chat_id"])
            row = snapshot.subscription(record_id) if snapshot else db.stock_subscriptions.find_one({"_id": record_id})
            started = payload.get("stock_notice_started_at") or job["created_at"]
            if row and row.get("last_notice_kind") == "stock_available_again" and row.get("last_notified_at") and utc(row["last_notified_at"]) >= utc(started):
                return "This availability was already announced to this user"
            if row and row.get("active"):
                error = _claim_delivery(db, row, job)
                if error:
                    return error
    if job.get("stock_subscription_delivery"):
        db.delivery_jobs.update_one({"job_id": job["job_id"], "claim_token": job["claim_token"]},
            {"$set": {"stock_subscription_delivery": job["stock_subscription_delivery"]}})
    return ""


def acknowledge_delivery(db, job):
    receipt = job.get("stock_subscription_delivery")
    if receipt:
        complete(db, receipt["id"], receipt["token"], kind=job["payload"].get("stock_notice_key", "fresh_stock_added"))
        from flask import g
        snapshot = getattr(g, "delivery_snapshot", None)
        if snapshot:
            snapshot.invalidate(("subscription", receipt["id"]))
