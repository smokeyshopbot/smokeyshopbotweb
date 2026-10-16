"""Persistent, bounded Telegram delivery outside Gunicorn request threads."""
import threading
from concurrent.futures import ThreadPoolExecutor
from utils.data_guard import work_guard, raw_database
import time
import uuid
from datetime import datetime, timezone, timedelta

from flask import flash, redirect, render_template, request, url_for, g
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError, BulkWriteError
from utils.notification_limits import NotificationDeferred, notification_slot


def now():
    return datetime.now(timezone.utc)


def ensure_delivery_indexes(db):
    db.delivery_jobs.create_index("job_id", unique=True)
    db.delivery_jobs.create_index([("status", 1), ("next_attempt_at", 1), ("priority", 1)])
    db.delivery_jobs.create_index([("parent_id", 1), ("status", 1)])
    db.delivery_jobs.create_index([("lease_until", 1)])
    db.delivery_jobs.create_index("expires_at", expireAfterSeconds=0)
    db.delivery_control.create_index("key", unique=True)
    db.delivery_control.create_index("expires_at", expireAfterSeconds=0)


def enqueue_job(db, kind, payload, *, job_id=None, parent_id=None, priority=5):
    job_id = job_id or uuid.uuid4().hex
    document = {"job_id": job_id, "kind": kind, "payload": payload, "status": "queued",
                "priority": priority, "attempts": 0, "created_at": now(), "next_attempt_at": now()}
    if parent_id:
        document["parent_id"] = parent_id
    db.delivery_jobs.update_one({"job_id": job_id}, {"$setOnInsert": document}, upsert=True)
    return {"queued": True, "job_id": job_id}


def enqueue_bulk(db, payload):
    return enqueue_job(db, "bulk", payload, priority=10)


def _bulk_query(payload):
    query = {"blocked": {"$ne": True}}
    if payload.get("exclude_ids"):
        query["user_id"] = {"$nin": payload["exclude_ids"]}
    return query


def bulk_count(db, payload):
    if payload.get("recipient_ids") is not None:
        return len(set(payload["recipient_ids"]))
    return db.users.count_documents(_bulk_query(payload))


def _finish(db, job, fields):
    fields["updated_at"] = now()
    if fields.get("status") in {"done", "cancelled", "expanded"}:
        fields["expires_at"] = now() + timedelta(days=30)
    return db.delivery_jobs.update_one({"job_id": job["job_id"], "claim_token": job["claim_token"]},
        {"$set": fields, "$unset": {"lease_until": "", "claim_token": ""}})


def _expand_bulk(app, shop, job):
    db = app.db
    payload = job["payload"]
    maintenance = shop.get_setting(db, "maintenance_mode", False)
    if maintenance and payload.get("maintenance_policy") == "pause":
        _finish(db, job, {"status": "queued", "next_attempt_at": now() + timedelta(seconds=10)})
        return
    product_name = payload.get("product_name")
    if product_name:
        product = db.products.find_one({"name": shop.name_regex(product_name)})
        if not product or product.get("enabled") is False:
            _finish(db, job, {"status": "cancelled", "error": "Product is unavailable"})
            return
    cursor = int(job.get("recipient_cursor", 0))
    if payload.get("recipient_ids") is not None:
        ids = sorted({int(uid) for uid in payload["recipient_ids"] if int(uid) > cursor})[:100]
        profiles = {u["user_id"]: u for u in db.users.find({"user_id": {"$in": ids}},
                     {"user_id": 1, "language": 1, "language_code": 1})}
        users = [profiles.get(uid) or {"user_id": uid, "language": "en"} for uid in ids]
    else:
        query = _bulk_query(payload)
        query["$and"] = [{"user_id": {"$gt": cursor}}]
        users = list(db.users.find(query, {"user_id": 1, "language": 1, "language_code": 1}).sort("user_id", 1).limit(100))
    children = []
    created = now()
    for user in users:
        uid = int(user["user_id"])
        language = shop.lang_from_user(user)
        text = payload["texts"].get(language, "")
        child = {"chat_id": uid, "text": text, "parse_mode": payload.get("parse_mode"),
                 "language": language,
                 "reply_markup": payload.get("markups", {}).get(language), "notification": payload.get("notification", {}),
                 "product_name": product_name, "cleanup_previous_stock": payload.get("cleanup_previous_stock", False),
                 "stock_notice_started_at": job["created_at"],
                 "maintenance_policy": payload.get("maintenance_policy", "continue"), "bulk_recipient": True,
                 "admin_recipient": uid in payload.get("admin_ids", [])}
        children.append({"job_id": f"{job['job_id']}:{uid}", "kind": "text", "payload": child,
                         "parent_id": job["job_id"], "priority": 20, "attempts": 0,
                         "status": "queued" if text else "cancelled", "created_at": created,
                         "next_attempt_at": created, **({"error": "No message for this user's language",
                             "expires_at": created + timedelta(days=30)} if not text else {})})
    if children:
        # One batch write replaces one round trip per recipient. Unique job IDs
        # make a replay safe after a crash between inserting children and saving the cursor.
        try:
            db.delivery_jobs.insert_many(children, ordered=False)
        except BulkWriteError as error:
            details = error.details or {}
            if details.get("writeConcernErrors") or any(e.get("code") != 11000 for e in details.get("writeErrors", [])):
                raise

    _finish(db, job, {"status": "queued" if len(users) == 100 else "expanded",
                     "recipient_cursor": max([cursor] + [int(u["user_id"]) for u in users]),
                     "next_attempt_at": now()})


def _handle_stock_work(app, shop, job):
    payload = job["payload"]
    name = payload["product_name"]
    replacement = shop._process_pending_replacement_reports_now(app.db, name) if payload.get("replacements", True) else {}
    summary = shop._process_pending_stock_orders_now(app.db, name) if payload.get("pending_orders", True) else {}
    if payload.get("restock"):
        available = shop.get_available_stock_count(app.db, name)
        previous = int(payload.get("previous_available", 0))
        if shop.claim_restock_notification_slot(app.db, name, previous, available,
                cooldown_minutes=shop.get_runtime_int(app.db, "restock_notification_cooldown_minutes", shop.RESTOCK_NOTIFICATION_COOLDOWN_MINUTES),
                back_in_stock_cooldown_minutes=shop.get_runtime_int(app.db, "restock_back_in_stock_cooldown_minutes", shop.RESTOCK_BACK_IN_STOCK_COOLDOWN_MINUTES),
                long_cooldown_minutes=shop.get_runtime_int(app.db, "restock_long_notification_cooldown_minutes", shop.RESTOCK_LONG_NOTIFICATION_COOLDOWN_MINUTES),
                big_restock_quantity=shop.get_runtime_int(app.db, "restock_big_addition_threshold", shop.RESTOCK_BIG_ADDITION_THRESHOLD),
                added_stock_count=payload.get("added_count")):
            shop.notify_users_new_stock(app.db, name, available)
    _finish(app.db, job, {"status": "done", "result": {**replacement, **summary}})


def _deliver(app, shop, job):
    db = app.db
    payload = job["payload"]
    if job['kind'] in {'emoji_test_source', 'emoji_forward', 'channel_poll', 'channel_poll_close'}:
        _finish(db, job, {'status': 'cancelled', 'error': 'Channel announcements and forwarding have been removed.'})
        return
    from utils.group_notifications import settings as group_settings, enabled as group_enabled
    if payload.get('replace_channel_job_id') and not job.get('acknowledged_at'):
        _finish(db, job, {'status': 'cancelled', 'error': 'Legacy channel results delivery is retired.'})
        return
    g.pop("delivery_secret_settings", None)
    snapshot = getattr(g, "delivery_snapshot", None)
    current_settings = snapshot.settings() if snapshot else shop.get_secret_settings(db)
    g.delivery_secret_settings = current_settings
    current_groups = group_settings(current_settings)
    if payload.get('api_low_balance_alert') and not job.get('acknowledged_at'):
        from web_admin.api_balance_alerts import validate_settings, eligible, alert_text
        alert_settings = validate_settings(current_settings)
        user = db.users.find_one({'user_id': payload['chat_id']})
        if not eligible(user, alert_settings):
            _finish(db, job, {'status': 'cancelled', 'error': 'API balance alert is no longer applicable'})
            return
        payload['text'] = alert_text(user, alert_settings)
    if payload.get('channel_recipient'):
        if job['kind'] != 'text':
            _finish(db, job, {'status': 'cancelled', 'error': 'Channel announcement actions have been removed.'})
            return
        source = job.get('channel_source_result') or {}
        if job.get('channel_source_acknowledged_at'):
            if not source.get('message_id'):
                _finish(db, job, {'status': 'review', 'error': 'The old group delivery has no acknowledged message ID.'})
                return
            group_id = (source.get('chat') or {}).get('id') or payload.get('channel_forward_source_chat_id')
            previous = job.get('telegram_result') if job.get('acknowledged_at') else None
            payload.update(chat_id=group_id, group_recipient=True)
            payload.pop('channel_recipient', None)
            payload.pop('channel_forward_source_chat_id', None)
            payload.setdefault('notification', {})['notification_target_kind'] = 'group'
            job.update(telegram_result=source, acknowledged_at=job['channel_source_acknowledged_at'])
            fields = {'payload': payload, 'telegram_result': source, 'acknowledged_at': job['acknowledged_at']}
            if previous:
                fields['previous_channel_result'] = previous
            db.delivery_jobs.update_one({'job_id': job['job_id'], 'claim_token': job['claim_token']},
                {'$set': fields, '$unset': {'send_started_at': ''}})
        elif job.get('acknowledged_at'):
            _finish(db, job, {'status': 'done', 'error': ''})
            return  # An already delivered historical channel post is left alone.
        elif job.get('send_started_at'):
            _finish(db, job, {'status': 'review', 'error': 'Old delivery outcome is unknown; inspect the group before retrying.'})
            return
        else:
            payload.pop('channel_recipient', None)
            payload.pop('channel_forward_source_chat_id', None)
            payload['group_recipient'] = True
            payload['chat_id'] = current_groups['group_notifications_chat_id']
            payload.setdefault('notification', {})['notification_target_kind'] = 'group'
    group_post = job['kind'] == 'text' and bool(payload.get('group_recipient'))
    if group_post and not job.get('acknowledged_at'):
        if not group_enabled(current_groups, payload):
            _finish(db, job, {'status': 'cancelled', 'error': 'Group announcements are disabled for this notice.'})
            return
        group_id = current_groups['group_notifications_chat_id']
        if not group_id:
            _finish(db, job, {'status': 'failed', 'error': 'Save an announcement group in Secret Settings → Notifications.'})
            return
        payload['chat_id'] = int(group_id) if group_id.startswith('-') else group_id
        db.delivery_jobs.update_one({'job_id': job['job_id'], 'claim_token': job['claim_token']}, {'$set': {'payload': payload}})
    if payload.get('group_recipient') and not job.get('acknowledged_at'):
        from web_admin.group_delivery import validate_group
        status = validate_group(db, shop, payload['chat_id'])
        if not status.get('ok'):
            attempts = int(job.get('attempts', 0)) + 1
            delay = status.get('retry_after_seconds')
            if delay is not None:
                attempts = int(job.get('attempts', 0))
                db.delivery_control.update_one({'key': 'telegram_pause'}, {'$max': {
                    'until': now() + timedelta(seconds=max(1, float(delay)))}}, upsert=True)
            permanent = status.get('status_code') in {400, 401, 403, 404}
            _finish(db, job, {'status': 'failed' if permanent or attempts >= 5 else 'queued',
                'attempts': attempts, 'error': status.get('error', 'Could not verify the announcement group.'),
                'next_attempt_at': now() + timedelta(seconds=max(1, float(delay)) if delay is not None else 30)})
            return
    if payload.get('deep_link_poll_id') and not job.get('acknowledged_at'):
        campaign = db.user_polls.find_one({'campaign_id': payload.get('deep_link_poll_id') or payload['campaign_id']}) or {}
        if campaign.get('status') in {'closing', 'closed', 'close_failed', 'draft'} or not campaign:
            _finish(db, job, {'status': 'cancelled', 'error': 'Poll closed before group delivery'})
            return
        from utils.survey import utc
        if campaign.get('close_requested') or (campaign.get('ends_at') and utc(campaign['ends_at']) <= now()):
            _finish(db, job, {'status': 'cancelled', 'error': 'Poll deadline passed'})
            return
    meta = payload.get("delivery_meta") or {}
    if payload.get("cleanup_previous_stock") and not job.get("acknowledged_at"):
        from web_admin.stock_notifications import prepare_delivery
        error = prepare_delivery(db, shop, job)
        if error:
            retry = isinstance(error, dict) and error.get("retry")
            _finish(db, job, {"status": "queued" if retry else "cancelled",
                "error": error["error"] if retry else error,
                "next_attempt_at": now() + timedelta(seconds=5)})
            return
    if meta.get("order_id"):
        order = db.orders.find_one({"order_id": meta["order_id"]})
        if not order:
            expired = job["created_at"].replace(tzinfo=timezone.utc) < now() - timedelta(minutes=1)
            _finish(db, job, {"status": "review" if expired else "queued", "next_attempt_at": now() + timedelta(seconds=3),
                             "error": "Delivery order has not been saved yet"})
            return
        if order.get("delivery_revoked"):
            _finish(db, job, {"status": "cancelled", "error": "Order delivery has been revoked"})
            return
    if (payload.get("bulk_recipient") or payload.get('group_recipient')) and not job.get("acknowledged_at"):
        user = snapshot.user(payload["chat_id"]) if snapshot else db.users.find_one({"user_id": payload["chat_id"]}) or {}
        g.delivery_user_profile = (payload["chat_id"], user)
        if payload.get('bulk_recipient') and user.get("blocked") and not payload.get("admin_recipient"):
            _finish(db, job, {"status": "cancelled", "error": "User is blocked"})
            return
        maintenance = snapshot.maintenance() if snapshot else shop.get_setting(db, "maintenance_mode", False)
        if maintenance and not payload.get("admin_recipient"):
            policy = payload.get("maintenance_policy")
            if policy in {"pause", "skip"}:
                _finish(db, job, {"status": "cancelled" if policy == "skip" else "queued",
                                 "next_attempt_at": now() + timedelta(seconds=10), "error": "Maintenance mode"})
                return
        if payload.get("product_name"):
            product = snapshot.product(payload["product_name"]) if snapshot else db.products.find_one({"name": shop.name_regex(payload["product_name"])})
            if not product or product.get("enabled") is False:
                _finish(db, job, {"status": "cancelled", "error": "Product is unavailable"})
                return
            if payload.get("cleanup_previous_stock"):
                available = (payload["current_available_stock"] if payload.get("stock_subscription_id")
                             else snapshot.available(product["name"]) if snapshot else shop.get_available_stock_count(db, product["name"]))
                if available <= 0:
                    _finish(db, job, {"status": "cancelled", "error": "Stock is no longer available"})
                    return
                if not job.get("acknowledged_at"):
                    payload["text"] = shop.tr(payload.get("language", "en"), payload.get("stock_notice_key", "fresh_stock_added"),
                        product=shop.html.escape(shop.product_emoji_label(product), quote=False), stock=available)
    if (payload.get('deep_link_product_id') or payload.get('deep_link_poll_id')) and not job.get('acknowledged_at'):
        from web_admin.group_announcements import bot_username
        username = bot_username(db, shop)
        if not username:
            attempts = int(job.get('attempts', 0)) + 1
            _finish(db, job, {'status': 'failed' if attempts >= 5 else 'queued', 'attempts': attempts,
                             'error': 'Could not resolve the bot username for the link button.',
                             'next_attempt_at': now() + timedelta(seconds=30)})
            return
        if payload.get('deep_link_poll_id'):
            from utils.survey import words
            label = words(payload.get('language', 'en'))['vote']
            link = 'poll_' + payload['deep_link_poll_id']
        else:
            label = shop.tr(payload.get('language', 'en'), 'channel_buy_product')
            link = 'product_' + payload['deep_link_product_id']
        payload['reply_markup'] = {'inline_keyboard': [[{'text': label, 'url': 'https://t.me/' + username + '?start=' + link}]]}
    if job.get("acknowledged_at"):
        return _send_delivery(app, shop, job)
    try:
        with notification_slot(db, payload["chat_id"], group=bool(payload.get("group_recipient"))):
            return _send_delivery(app, shop, job)
    except NotificationDeferred as error:
        _finish(db, job, {"status": "queued", "next_attempt_at": now() + timedelta(seconds=error.retry_after)})


def _send_delivery(app, shop, job):
    db, payload = app.db, job["payload"]
    meta = payload.get("delivery_meta") or {}
    group_post = job["kind"] == "text" and bool(payload.get("group_recipient"))
    # Persist the fact that sending began. A process crash with no Telegram
    # acknowledgement is flagged for review rather than silently sent again.
    if not job.get("acknowledged_at"):
        started_fields = {"send_started_at": now()}
        if payload.get("cleanup_previous_stock"):
            started_fields["payload"] = payload
        db.delivery_jobs.update_one({"job_id": job["job_id"], "claim_token": job["claim_token"]}, {"$set": started_fields})
    if job.get("acknowledged_at"):
        status = {"ok": True, "result": job.get("telegram_result") or {}}
    elif job["kind"] == "document":
        status = shop._send_telegram_document_now(payload["chat_id"], payload["filename"], payload["content"], caption=payload.get("caption"))
    elif job["kind"] == "delete":
        status = shop.delete_telegram_message_status(payload["chat_id"], payload["message_id"])
    else:
        notification = payload.get("notification", {})
        status = shop._send_telegram_message_now(payload["chat_id"], payload["text"], parse_mode=payload.get("parse_mode"),
                    reply_markup=payload.get("reply_markup"), **notification)
    if status.get("ok"):
        # deleteMessage returns True, while send/forward methods return a
        # Message. Only Message objects have chat and message_id fields.
        result = status.get("result") if isinstance(status.get("result"), dict) else {}
        # Save the acknowledgement before optional bookkeeping/cleanup.
        db.delivery_jobs.update_one({"job_id": job["job_id"], "claim_token": job["claim_token"]}, {"$set": {"telegram_result": result, "acknowledged_at": now()}})
        if job.get("stock_subscription_delivery"):
            from web_admin.stock_notifications import acknowledge_delivery
            acknowledge_delivery(db, job)
        if payload.get('group_recipient') and (result.get('chat') or {}).get('id'):
            payload['chat_id'] = result['chat']['id']
            db.delivery_jobs.update_one({'job_id': job['job_id']}, {'$set': {'payload.chat_id': payload['chat_id']}})
        if job["kind"] in {"text", "document", "channel_poll"}:
            notification = payload.get("notification", {})
            shop.record_sent_notification(db, chat_id=payload["chat_id"], result=result,
                text=payload.get("text") or payload.get('question') or payload.get("caption") or payload.get("filename", ""),
                notification_type=notification.get("notification_type", "document" if job["kind"] == "document" else "message"),
                batch_id=notification.get("notification_batch_id") or job["job_id"],
                title=notification.get("notification_title") or payload.get("filename", "Message"),
                source=notification.get("notification_source", "web_admin"),
                product_name=notification.get("notification_product_name", ""), parse_mode=payload.get("parse_mode"),
                target_kind=notification.get("notification_target_kind", "customer"))
        if job["kind"] != "delete" and not payload.get('group_recipient'):
            shop.mark_user_delivery_success(db, payload["chat_id"], source="background_delivery")
        if meta.get("order_id"):
            shop.record_order_delivery_message(db, meta["order_id"], payload["chat_id"], result,
                filename=payload.get("filename", ""), sent_by=meta.get("sent_by", "webadmin"), resent=meta.get("resent", False))
            db.orders.update_one({"order_id": meta["order_id"]}, {"$set": {"telegram_delivery_status": "sent"}})
            for flag in ("admin_stock_telegram_sent", "manual_replacement_telegram_sent"):
                db.orders.update_one({"order_id": meta["order_id"], flag: {"$exists": True}}, {"$set": {flag: True}})
        if payload.get("cleanup_previous_stock"):
            previous = shop._last_product_notification(db, payload["chat_id"], payload["product_name"])
            mid = shop._telegram_result_message_id(result)
            if mid:
                if previous and previous.get("message_id") != mid:
                    enqueue_job(db, "delete", {"chat_id": payload["chat_id"], "message_id": previous["message_id"],
                                'group_recipient': bool(payload.get('group_recipient'))},
                                job_id=f"stock-cleanup:{payload['chat_id']}:{previous['message_id']}", priority=25)
                shop._save_product_notification(db, payload["chat_id"], payload["product_name"], mid)
        if payload.get("notification_record_id"):
            db.sent_notifications.update_one({"_id": payload["notification_record_id"]}, {"$set": {
                "deleted": True, "deleted_at": now(), "deleted_by": payload.get("deleted_by", "owner"), "deletion_pending": False}})
        elif job["kind"] == "delete":
            db.sent_notifications.update_many({"chat_id": payload["chat_id"], "message_id": payload["message_id"]},
                {"$set": {"deleted": True, "deleted_at": now(), "deletion_pending": False}})
        _finish(db, job, {"status": "done", "error": ""})
        return
    attempts = int(job.get("attempts", 0)) + 1
    delay = status.get("retry_after_seconds")
    permanent = status.get("blocked") or (status.get("status_code") in {400, 401, 403, 404})
    unknown = status.get("error_type") in {"ReadTimeout", "Timeout"}
    if group_post:
        unknown = unknown or bool(status.get('error_type')) or (status.get('status_code') or 0) >= 500
    state = "review" if unknown else "failed" if permanent or attempts >= 5 else "queued"
    if delay is not None:
        state = "queued"
        attempts = int(job.get("attempts", 0))
        db.delivery_control.update_one({"key": "telegram_pause"}, {"$max": {"until": now() + timedelta(seconds=max(1, float(delay)))}}, upsert=True)
    _finish(db, job, {"status": state, "attempts": attempts, "error": str(status.get("error", "Delivery failed"))[:500],
                     "next_attempt_at": now() + timedelta(seconds=max(1, float(delay)) if delay is not None else min(600, 10 * 2 ** attempts))})
    db.delivery_jobs.update_one({"job_id": job["job_id"]}, {"$unset": {"send_started_at": ""}})
    if status.get("blocked") and not payload.get('group_recipient'):
        shop.mark_user_delivery_failure(db, payload["chat_id"], source="background_delivery", error=status.get("error", ""))
    if meta.get("order_id"):
        db.orders.update_one({"order_id": meta["order_id"]}, {"$set": {"telegram_delivery_status": state, "telegram_delivery_error": status.get("error", "")}})


def _recover_expired_jobs(db):
    current = now()
    expired = list(db.delivery_jobs.find({"status": "working", "lease_until": {"$lt": current}}).limit(100))
    for job in expired:
        known_group = job.get("channel_source_acknowledged_at") and (job.get("channel_source_result") or {}).get("message_id")
        state = "queued" if job.get("acknowledged_at") or known_group else "review" if job.get("send_started_at") else "queued"
        db.delivery_jobs.update_one({"job_id": job["job_id"], "status": "working", "lease_until": {"$lt": current}},
            {"$set": {"status": state, "error": "Worker interrupted; review before retrying" if state == "review" else "", "next_attempt_at": current},
             "$unset": {"claim_token": "", "lease_until": ""}})


def process_delivery_jobs(app, shop, *, limit=1, recover=True):
    db = app.db
    if recover:
        _recover_expired_jobs(db)
    processed = 0
    for _ in range(limit):
        token = uuid.uuid4().hex
        query = {"status": "queued", "next_attempt_at": {"$lte": now()}}
        snapshot = getattr(g, "delivery_snapshot", None)
        pause = snapshot.pause() if snapshot else db.delivery_control.find_one({"key": "telegram_pause"}) or {}
        if pause.get("until") and pause["until"].replace(tzinfo=timezone.utc) > now():
            query["kind"] = {"$in": ["bulk", "stock_work"]}
        job = db.delivery_jobs.find_one_and_update(query,
            {"$set": {"status": "working", "claim_token": token, "lease_until": now() + timedelta(minutes=5)}},
            sort=[("priority", 1), ("created_at", 1)], return_document=ReturnDocument.AFTER)
        if not job:
            break
        processed += 1
        stop_heartbeat = threading.Event()
        def heartbeat():
            while not stop_heartbeat.wait(30):
                try:
                    raw_database(db).delivery_jobs.update_one({"job_id": job["job_id"], "claim_token": token, "status": "working"},
                        {"$set": {"lease_until": now() + timedelta(minutes=5)}})
                except Exception:
                    app.logger.exception("Delivery job heartbeat failed")
        heartbeat_thread = threading.Thread(target=heartbeat, daemon=True)
        heartbeat_thread.start()
        try:
            if job["kind"] == "bulk": _expand_bulk(app, shop, job)
            elif job["kind"] == "stock_work": _handle_stock_work(app, shop, job)
            else: _deliver(app, shop, job)
        except Exception:
            app.logger.exception("Background delivery job failed: %s", job["job_id"])
            latest = db.delivery_jobs.find_one({"job_id": job["job_id"]}) or {}
            if latest.get("status") == "working":
                _finish(db, job, {"status": "queued" if latest.get("acknowledged_at") else "review" if latest.get("send_started_at") else "queued",
                    "next_attempt_at": now() + timedelta(seconds=30), "error": "Worker error; check hosting logs"})
        finally:
            stop_heartbeat.set()
            heartbeat_thread.join(timeout=1)
    return processed


class DeliveryPool:
    """A rolling pool: one slow recipient never holds up the next whole batch."""
    def __init__(self, app, shop, *, workers=12):
        self.app, self.shop, self.workers = app, shop, workers
        self.executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="telegram-delivery")
        self.pending = set()
        self.last_recovery = 0.0
        self.last_probe = -1.0
        from web_admin.delivery_snapshot import DeliverySnapshot
        self.snapshot = DeliverySnapshot(app.db, shop)

    def _run_one(self):
        with self.app.app_context(), work_guard(self.app.db):
            g.delivery_snapshot = self.snapshot
            return process_delivery_jobs(self.app, self.shop, recover=False, limit=3)

    def tick(self):
        for future in list(self.pending):
            if future.done():
                self.pending.remove(future)
                try:
                    future.result()
                except Exception:
                    self.app.logger.exception("Delivery worker failed")
        if time.monotonic() - self.last_recovery >= 10:
            with self.app.app_context(), work_guard(self.app.db, wait=False):
                _recover_expired_jobs(self.app.db)
            self.last_recovery = time.monotonic()
        room = self.workers - len(self.pending)
        if room <= 0 or time.monotonic() - self.last_probe < 0.25:
            return
        self.last_probe = time.monotonic()
        query = {"status": "queued", "next_attempt_at": {"$lte": now()}}
        pause = self.app.db.delivery_control.find_one({"key": "telegram_pause"}) or {}
        if pause.get("until") and pause["until"].replace(tzinfo=timezone.utc) > now():
            query["kind"] = {"$in": ["bulk", "stock_work"]}
        # Probe once while idle instead of making every worker poll MongoDB.
        ready = list(self.app.db.delivery_jobs.find(query, {"kind": 1, "payload.chat_id": 1}).limit(room * 3))
        self.snapshot.prefetch_users(ready)
        for _ in range(min(room, (len(ready) + 2) // 3)):
            self.pending.add(self.executor.submit(self._run_one))

    def shutdown(self):
        self.executor.shutdown(wait=True)


def start_delivery_worker(app, shop):
    if app.config.get("DELIVERY_WORKER_STARTED"):
        return
    app.config["DELIVERY_WORKER_STARTED"] = True
    pool = DeliveryPool(app, shop)
    app.delivery_pool = pool
    def worker():
        while True:
            try:
                pool.tick()
            except Exception:
                app.logger.exception("Delivery queue unavailable")
            time.sleep(0.05 if pool.pending else 0.5)
    app.delivery_thread = threading.Thread(target=worker, name="webadmin-delivery", daemon=True)
    app.delivery_thread.start()


def register_delivery_routes(app, shop):
    @app.get("/delivery-queue")
    @shop.login_required
    @shop.owner_required
    def delivery_queue():
        jobs = list(app.db.delivery_jobs.find({"parent_id": {"$exists": False}}).sort("created_at", -1).limit(100))
        parent_ids = [job["job_id"] for job in jobs if job["kind"] == "bulk"]
        counts = {parent: {state: 0 for state in ("queued", "working", "done", "failed", "review", "cancelled")} for parent in parent_ids}
        for row in app.db.delivery_jobs.aggregate([
                {"$match": {"parent_id": {"$in": parent_ids}}},
                {"$group": {"_id": {"parent": "$parent_id", "state": "$status"}, "count": {"$sum": 1}}}]):
            counts[row["_id"]["parent"]][row["_id"]["state"]] = row["count"]
        for job in jobs:
            if job["kind"] == "bulk":
                job["counts"] = counts[job["job_id"]]
        return render_template("delivery_queue.html", jobs=jobs)

    @app.get("/delivery-queue/<job_id>")
    @shop.login_required
    @shop.owner_required
    def delivery_job_detail(job_id):
        from flask import abort
        job = app.db.delivery_jobs.find_one({"job_id": job_id})
        if not job:
            abort(404)
        try:
            page = max(1, int(request.args.get("page", 1)))
        except ValueError:
            page = 1
        query = ({'$or': [{'parent_id': job_id}, {'job_id': job_id}]} if job['kind'] == 'emoji_forward'
                 else {"parent_id": job_id} if job["kind"] == "bulk" else {"job_id": job_id})
        total = app.db.delivery_jobs.count_documents(query)
        rows = list(app.db.delivery_jobs.find(query).sort("created_at", 1).skip((page - 1) * 50).limit(50))
        return render_template("delivery_job_detail.html", job=job, rows=rows, total=total, page=page)

    @app.post("/delivery-queue/<job_id>/retry")
    @shop.login_required
    @shop.owner_required
    @shop.csrf_required
    def retry_delivery_job(job_id):
        job = app.db.delivery_jobs.find_one({"job_id": job_id})
        if job and job["kind"] == "bulk":
            query = {"parent_id": job_id, "status": {"$in": ["failed", "review"]}}
        else:
            query = {"job_id": job_id, "status": {"$in": ["failed", "review"]}}
        app.db.delivery_jobs.update_many(query, {"$set": {"status": "queued", "attempts": 0, "next_attempt_at": now()},
            "$unset": {"send_started_at": "", "error": ""}})
        flash("Failed deliveries queued for retry. Completed deliveries are not resent.", "info")
        return redirect(url_for("delivery_queue"))
