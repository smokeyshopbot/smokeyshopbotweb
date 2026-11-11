"""Persistent, bounded Telegram delivery outside Gunicorn request threads."""
import threading
import hashlib
from concurrent.futures import ThreadPoolExecutor
from utils.data_guard import work_guard, raw_database, DatabaseWritesPaused
from utils.performance import monitor
import time
import uuid
from datetime import datetime, timezone, timedelta

from flask import flash, redirect, render_template, request, url_for, g
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError, BulkWriteError
from utils.notification_limits import NotificationDeferred, notification_slot, effective_rate

STOCK_NOTICE_PRIORITY = 10
JOB_SORT = [("priority", 1), ("created_at", 1), ("recipient_rank", 1)]


def is_stock_notice(payload):
    return bool(payload.get("cleanup_previous_stock") or
                (payload.get("notification") or {}).get("notification_type") in {"new_stock", "stock_available_again"})


def now():
    return datetime.now(timezone.utc)


def ensure_delivery_indexes(db):
    db.delivery_jobs.create_index("job_id", unique=True)
    db.delivery_jobs.create_index([("status", 1), ("next_attempt_at", 1), ("priority", 1)])
    db.delivery_jobs.create_index([("status", 1), *JOB_SORT])
    db.delivery_jobs.create_index([("parent_id", 1), ("status", 1)])
    db.delivery_jobs.create_index([("kind", 1), ("status", 1), ("parent_id", 1), ("created_at", 1)])
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
    return enqueue_job(db, "bulk", payload, priority=8 if is_stock_notice(payload) else 15)


def _bulk_query(payload):
    query = {"blocked": {"$ne": True}}
    if payload.get("exclude_ids"):
        query["user_id"] = {"$nin": payload["exclude_ids"]}
    if payload.get('api_audience'):
        query.update(api_enabled={'$ne': False}, api_key_hash={'$type': 'string', '$ne': ''})
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


def _sale_unavailable(db, payload):
    if not payload.get('sale_id'):
        return False
    from utils.promotions import active_sale
    snapshot = getattr(g, 'delivery_snapshot', None)
    product = (snapshot.product(payload.get('product_name')) if snapshot else
               db.products.find_one({'name': payload.get('product_name')}, {'sale': 1, 'enabled': 1}))
    return not product or product.get('enabled') is False or active_sale(product).get('id') != payload['sale_id']


def _expand_bulk(app, shop, job):
    db = app.db
    payload = job["payload"]
    from web_admin.api_maintenance import notice_is_current
    if not notice_is_current(db, payload):
        _finish(db, job, {'status': 'cancelled', 'error': 'API availability changed again'})
        return
    if _sale_unavailable(db, payload):
        _finish(db, job, {'status': 'cancelled', 'error': 'Sale ended or was replaced before delivery'})
        return
    maintenance = shop.get_setting(db, "maintenance_mode", False)
    if maintenance and payload.get("maintenance_policy") == "pause":
        _finish(db, job, {"status": "queued", "next_attempt_at": now() + timedelta(seconds=10)})
        return
    product_name = payload.get("product_name")
    if product_name:
        from web_admin.delivery_snapshot import cached_product
        product = cached_product(db, shop, product_name)
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
    stock_notice = is_stock_notice(payload)
    # All pages in a restock share one creation time, so their random ranks
    # compete together rather than always notifying the first user-ID page.
    created = job["created_at"] if stock_notice else now()
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
        if payload.get('api_status_version'):
            child.update(api_status_version=payload['api_status_version'], api_audience=True)
        if payload.get('sale_id'):
            child.update(sale_id=payload['sale_id'], sale_expires_at=payload.get('sale_expires_at'))
        children.append({"job_id": f"{job['job_id']}:{uid}", "kind": "text", "payload": child,
                         "parent_id": job["job_id"], "priority": STOCK_NOTICE_PRIORITY if stock_notice else 20, "attempts": 0,
                         "recipient_rank": hashlib.sha256(f"{job['job_id']}:{uid}".encode()).hexdigest() if stock_notice else "",
                         "status": ("preparing" if stock_notice else "queued") if text else "cancelled", "created_at": created,
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

    if stock_notice and len(users) < 100:
        # Persist all candidates before releasing a stock campaign. Replays use
        # the same job IDs/ranks, and never requeue delivered/cancelled children.
        db.delivery_jobs.update_many({"parent_id": job["job_id"], "status": "preparing"},
                                    {"$set": {"status": "queued"}})
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
    if job['kind'] == 'delete':
        from web_admin.notification_cleanup import expired, discard_expired_targets
        discard_expired_targets(db, job)
        if expired(payload) and not job.get('acknowledged_at'):
            return _send_delivery(app, shop, job)
    if not job.get('acknowledged_at') and _sale_unavailable(db, payload):
        _finish(db, job, {'status': 'cancelled', 'error': 'Sale ended or was replaced before delivery'})
        return
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
    if payload.get('api_status_version') and not job.get('acknowledged_at'):
        from web_admin.api_maintenance import notice_is_current
        if not notice_is_current(db, payload, snapshot):
            _finish(db, job, {'status': 'cancelled', 'error': 'API availability changed again'})
            return
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
        if payload.get('api_audience') and (user.get('api_enabled') is False or not user.get('api_key_hash')):
            _finish(db, job, {'status': 'cancelled', 'error': 'API access no longer enabled for this account'})
            return
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
        snapshot = getattr(g, "delivery_snapshot", None)
        with notification_slot(db, payload["chat_id"], group=bool(payload.get("group_recipient")),
                               pause_loader=snapshot.pause if snapshot else None, reservation=job.get('_chat_reservation')):
            return _send_delivery(app, shop, job)
    except NotificationDeferred as error:
        _finish(db, job, {"status": "queued", "next_attempt_at": now() + timedelta(seconds=error.retry_after)})


def _send_delivery(app, shop, job):
    db, payload = app.db, job["payload"]
    from web_admin.notification_cleanup import expired, record_delete_outcome
    if job['kind'] == 'delete':
        from web_admin.notification_cleanup import discard_expired_targets
        discard_expired_targets(db, job)
    if job['kind'] == 'delete' and not job.get('acknowledged_at') and expired(payload):
        error = 'Skipped: message is outside Telegram deletion window.'
        record_delete_outcome(db, payload, skipped=True, error=error)
        _finish(db, job, {'status': 'cancelled', 'cleanup_unavailable': True, 'error': error})
        return
    meta = payload.get("delivery_meta") or {}
    group_post = job["kind"] == "text" and bool(payload.get("group_recipient"))
    # Persist the fact that sending began. A process crash with no Telegram
    # acknowledgement is flagged for review rather than silently sent again.
    if not job.get("acknowledged_at"):
        started_fields = {"send_started_at": now()}
        if payload.get("cleanup_previous_stock"):
            started_fields["payload"] = payload
        intent = db.delivery_jobs.update_one({"job_id": job["job_id"], "claim_token": job["claim_token"],
            "status": "working", "lease_until": {"$gt": now()}}, {"$set": started_fields})
        if not intent.matched_count:
            return  # A backup delay/recovery may have replaced this claim.
    if job.get("acknowledged_at"):
        status = {"ok": True, "result": job.get("telegram_result") or {}}
    elif job["kind"] == "document":
        status = shop._send_telegram_document_now(payload["chat_id"], payload["filename"], payload["content"], caption=payload.get("caption"))
    elif job["kind"] == "delete":
        status = shop.delete_telegram_messages_status(payload["chat_id"], payload.get('message_ids') or [payload["message_id"]])
    else:
        notification = payload.get("notification", {})
        status = shop._send_telegram_message_now(payload["chat_id"], payload["text"], parse_mode=payload.get("parse_mode"),
                    reply_markup=payload.get("reply_markup"), **notification)
    if status.get("ok"):
        # deleteMessage returns True, while send/forward methods return a
        # Message. Only Message objects have chat and message_id fields.
        result = status.get("result") if isinstance(status.get("result"), dict) else {}
        # Save the acknowledgement before optional bookkeeping/cleanup.
        if not job.get("acknowledged_at"):
            acknowledged = now()
            db.delivery_jobs.update_one({"job_id": job["job_id"], "claim_token": job["claim_token"]},
                {"$set": {"telegram_result": result, "acknowledged_at": acknowledged}})
            job.update(telegram_result=result, acknowledged_at=acknowledged)
        if job.get("stock_subscription_delivery"):
            from web_admin.stock_notifications import acknowledge_delivery
            acknowledge_delivery(db, job)
        snapshot = getattr(g, "delivery_snapshot", None)
        if (snapshot and job['kind'] == 'text' and payload.get('bulk_recipient')
                and not payload.get('group_recipient') and not meta.get('order_id')):
            checked = getattr(g, 'delivery_user_profile', None)
            if checked and checked[0] == payload['chat_id']:
                job['_notice_user'] = checked[1]
            snapshot.defer_bookkeeping(job)
            return
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
            previous_at = shop._parse_utc_dt((previous or {}).get('delivery_sent_at') or (previous or {}).get('updated_at'))
            sent_at = shop._parse_utc_dt(job.get('acknowledged_at')) or now()
            if mid and (not previous_at or previous_at <= sent_at):
                if previous and previous.get("message_id") != mid:
                    from web_admin.notification_cleanup import enqueue_stock_cleanup
                    enqueue_stock_cleanup(db, job, previous)
                shop._save_product_notification(db, payload["chat_id"], payload["product_name"], mid,
                    sent_at=sent_at, telegram_sent_at=result.get('date'))
        if job['kind'] == 'delete':
            record_delete_outcome(db, payload, deleted=True)
        _finish(db, job, {"status": "done", "error": ""})
        return
    attempts = int(job.get("attempts", 0)) + 1
    if job['kind'] == 'delete' and status.get('cleanup_skipped'):
        error = str(status.get('error', 'Telegram does not allow this deletion'))[:500]
        if 'not enough rights' not in error.lower():
            from web_admin.notification_cleanup import split_refused_batch
            if split_refused_batch(db, job):
                _finish(db, job, {'status': 'expanded', 'error': 'Bulk deletion refused; queued individual message checks.'})
                return
        record_delete_outcome(db, payload, skipped=True, error=error)
        _finish(db, job, {'status': 'cancelled', 'cleanup_unavailable': True, 'error': error, 'attempts': attempts})
        return
    delay = status.get("retry_after_seconds")
    permanent = status.get("blocked") or (status.get("status_code") in {400, 401, 403, 404})
    unknown = job['kind'] != 'delete' and status.get("error_type") in {"ReadTimeout", "Timeout"}
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
    if job['kind'] == 'delete' and state == 'failed':
        record_delete_outcome(db, payload, error=status.get('error', 'Deletion failed'))
    if status.get("blocked") and not payload.get('group_recipient'):
        shop.mark_user_delivery_failure(db, payload["chat_id"], source="background_delivery", error=status.get("error", ""))
    if meta.get("order_id"):
        db.orders.update_one({"order_id": meta["order_id"]}, {"$set": {"telegram_delivery_status": state, "telegram_delivery_error": status.get("error", "")}})


def _recover_expired_jobs(db):
    current = now()
    expired = list(db.delivery_jobs.find({"status": "working", "lease_until": {"$lt": current}}).limit(100))
    for job in expired:
        known_group = job.get("channel_source_acknowledged_at") and (job.get("channel_source_result") or {}).get("message_id")
        state = "queued" if job.get("acknowledged_at") or known_group or job['kind'] == 'delete' else "review" if job.get("send_started_at") else "queued"
        db.delivery_jobs.update_one({"job_id": job["job_id"], "status": "working", "lease_until": {"$lt": current}},
            {"$set": {"status": state, "error": "Worker interrupted; review before retrying" if state == "review" else "", "next_attempt_at": current},
             "$unset": {"claim_token": "", "lease_until": ""}})


def process_delivery_jobs(app, shop, *, limit=1, recover=True, exclude_cleanup=False, only_cleanup=False):
    db = app.db
    if recover:
        _recover_expired_jobs(db)
    processed = 0
    for _ in range(limit):
        if only_cleanup:
            from web_admin.notification_cleanup import sending_work_exists
            if sending_work_exists(db):
                break
        token = uuid.uuid4().hex
        query = {"status": "queued", "next_attempt_at": {"$lte": now()}}
        if exclude_cleanup:
            query['kind'] = {'$ne': 'delete'}
        elif only_cleanup:
            query['kind'] = 'delete'
        if g.get("delivery_background_busy") or (g.get("delivery_snapshot") is not None and monitor.background_busy()):
            query["priority"] = {"$lte": 5}
        snapshot = getattr(g, "delivery_snapshot", None)
        pause = snapshot.pause() if snapshot else db.delivery_control.find_one({"key": "telegram_pause"}) or {}
        if pause.get("until") and pause["until"].replace(tzinfo=timezone.utc) > now():
            if only_cleanup:
                break
            query["kind"] = {"$in": ["bulk", "stock_work"]}
        job = db.delivery_jobs.find_one_and_update(query,
            {"$set": {"status": "working", "claim_token": token, "lease_until": now() + timedelta(minutes=5)}},
            sort=JOB_SORT, return_document=ReturnDocument.AFTER)
        if not job:
            break
        processed += 1
        run_claimed_job(app, shop, job)
    return processed


def run_claimed_job(app, shop, job):
    db = app.db
    stop_heartbeat = threading.Event()
    def heartbeat():
        while not stop_heartbeat.wait(30):
            try:
                raw_database(db).delivery_jobs.update_one({"job_id": job["job_id"], "claim_token": job["claim_token"], "status": "working"},
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
        reservation = job.get('_chat_reservation')
        if reservation is not None:
            reservation.release(False)


class DeliveryPool:
    """A rolling pool: one slow recipient never holds up the next whole batch."""
    def __init__(self, app, shop, *, workers=12):
        self.app, self.shop, self.workers = app, shop, workers
        self.executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="telegram-delivery")
        self.pending = set()
        self.last_recovery = 0.0
        self.last_probe = -1.0
        self.poll_interval = 0.25
        self.bookkeeping_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="notice-records")
        self.bookkeeping_future = None
        self.bookkeeping_jobs = []
        self.bookkeeping_releases = {}
        self.last_bookkeeping = 0.0
        self.last_release_flush = 0.0
        self.last_pressure_log = 0.0
        self.last_cleanup_upgrade = -100.0
        self.next_cleanup_at = 0.0
        from web_admin.delivery_snapshot import DeliverySnapshot
        self.snapshot = DeliverySnapshot(app.db, shop)

    def _run_one(self):
        with self.app.app_context(), work_guard(self.app.db):
            g.delivery_snapshot = self.snapshot
            g.delivery_background_busy = monitor.background_busy()
            return process_delivery_jobs(self.app, self.shop, recover=False, limit=3, exclude_cleanup=True)

    def _run_cleanup(self):
        with self.app.app_context(), work_guard(self.app.db):
            g.delivery_snapshot = self.snapshot
            return process_delivery_jobs(self.app, self.shop, recover=False, limit=1, only_cleanup=True)

    def _maybe_cleanup(self, busy):
        if busy or self.pending or time.monotonic() < self.next_cleanup_at:
            return False
        from web_admin.notification_cleanup import adopt_legacy_cleanup, sending_work_exists, upgrade_pending
        if sending_work_exists(self.app.db):
            return False
        if time.monotonic() - self.last_cleanup_upgrade >= 10:
            group_pending, backfill_pending = upgrade_pending(self.app.db)
            if group_pending or backfill_pending:
                try:
                    with self.app.app_context(), work_guard(self.app.db, wait=False):
                        if group_pending:
                            adopt_legacy_cleanup(self.app.db)
                        if backfill_pending:
                            self.shop.backfill_legacy_product_notifications(self.app.db)
                except DatabaseWritesPaused:
                    return False
            self.last_cleanup_upgrade = time.monotonic()
        ready = self.app.db.delivery_jobs.find_one({'kind': 'delete', 'status': 'queued',
            'next_attempt_at': {'$lte': now()}}, {'_id': 1})
        if not ready:
            return False
        self.pending.add(self.executor.submit(self._run_cleanup))
        self.next_cleanup_at = time.monotonic() + .35
        return True

    def _run_notice_slice(self, jobs):
        try:
            with self.app.app_context(), work_guard(self.app.db):
                g.delivery_snapshot = self.snapshot
                for index, job in enumerate(jobs):
                    try:
                        # Refresh the remaining small slice together if a slow
                        # request/rate wait outlived its recipient snapshot.
                        self.snapshot.prefetch_users(jobs[index:])
                    except Exception:
                        # Prefetch is only an optimization. Per-job validation
                        # still handles failure/retry and releases its claim.
                        self.app.logger.exception('Notification prefetch failed; falling back to per-job checks')
                    run_claimed_job(self.app, self.shop, job)
        finally:
            for job in jobs:
                reservation = job.get('_chat_reservation')
                if reservation is not None:
                    reservation.release(False)

    def _record_notices(self, jobs, releases=None):
        from web_admin.notification_bookkeeping import finish_notices
        with self.app.app_context(), work_guard(self.app.db):
            g.delivery_snapshot = self.snapshot
            if releases:
                self.app.db.delivery_control.bulk_write(list(releases.values()), ordered=False)
            if not jobs:
                return
            g.delivery_secret_settings = self.snapshot.settings()
            missing_profiles = [dict(j, payload={'chat_id': j['payload']['chat_id']})
                                for j in jobs if '_notice_user' not in j]
            if missing_profiles:
                self.snapshot.prefetch_users(missing_profiles)
            if jobs:
                finish_notices(self.app.db, self.shop, jobs)

    def _bookkeeping_tick(self):
        if self.bookkeeping_future is not None:
            if not self.bookkeeping_future.done():
                return
            try:
                self.bookkeeping_future.result()
            except Exception:
                for job in self.bookkeeping_jobs:
                    self.snapshot.defer_bookkeeping(job)
                for owner, operation in self.bookkeeping_releases.items():
                    self.snapshot.defer_chat_release(owner, operation)
                self.app.logger.exception("Notification records will retry; acknowledged messages are not resent")
                self.last_bookkeeping = time.monotonic() + 5
            self.bookkeeping_future = None
            self.bookkeeping_jobs = []
            self.bookkeeping_releases = {}
        current_time = time.monotonic()
        records_due = self.snapshot.bookkeeping_count() and current_time - self.last_bookkeeping >= 1.0
        releases_due = self.snapshot.chat_release_count() and current_time - self.last_release_flush >= 1.0
        if records_due or releases_due:
            self.bookkeeping_jobs = self.snapshot.take_bookkeeping() if records_due else []
            self.bookkeeping_releases = self.snapshot.take_chat_releases()
            self.bookkeeping_future = self.bookkeeping_executor.submit(
                self._record_notices, self.bookkeeping_jobs, self.bookkeeping_releases)
            self.last_release_flush = current_time
            if records_due:
                self.last_bookkeeping = current_time

    def tick(self):
        self._bookkeeping_tick()
        for future in list(self.pending):
            if future.done():
                self.pending.remove(future)
                try:
                    future.result()
                except Exception:
                    self.app.logger.exception("Delivery worker failed")
        # Avoid exhausting Atlas Free with background claims/retry churn. All
        # admitted deliveries finish normally; queued jobs remain durable.
        busy = monitor.background_busy() or self.snapshot.bookkeeping_count() >= 200
        if busy and (self.pending or self.snapshot.bookkeeping_count()) and time.monotonic() - self.last_pressure_log >= 30:
            pressure = monitor.pressure()
            self.app.logger.info("Notification queue backoff: %s; database commands=%s/s, interactive=%s/s; configured cap=%s/s",
                pressure['reason'] or 'Pending notification records', pressure['commands_per_second'],
                pressure['interactive_commands_per_second'], effective_rate(self.app.db))
            self.last_pressure_log = time.monotonic()
        if time.monotonic() - self.last_recovery >= 10:
            expired = self.app.db.delivery_jobs.find_one({'status': 'working', 'lease_until': {'$lt': now()}}, {'_id': 1})
            if expired:
                with self.app.app_context(), work_guard(self.app.db, wait=False):
                    _recover_expired_jobs(self.app.db)
            self.last_recovery = time.monotonic()
        room = self.workers - len(self.pending)
        if room <= 0 or time.monotonic() - self.last_probe < (min(.25, self.poll_interval) if busy else self.poll_interval):
            return
        self.last_probe = time.monotonic()
        query = {"status": "queued", "next_attempt_at": {"$lte": now()}, 'kind': {'$ne': 'delete'}}
        if busy:
            # Paid delivery/stock work keeps progressing; routine bulk children
            # wait for database headroom instead of competing with navigation.
            query['priority'] = {'$lte': 5}
        ready = list(self.app.db.delivery_jobs.find(query, {'kind': 1, 'payload.chat_id': 1,
            'payload.product_name': 1, 'payload.cleanup_previous_stock': 1, 'payload.stock_subscription_id': 1,
            'payload.bulk_recipient': 1, 'payload.group_recipient': 1, 'payload.delivery_meta.order_id': 1})
            .sort(JOB_SORT).limit(room * 3))
        if not ready:
            cleaning = self._maybe_cleanup(busy)
            self.poll_interval = .25 if cleaning or busy else min(2.0, self.poll_interval * 2)
            return
        self.poll_interval = 0.25
        pause = self.snapshot.pause()
        if pause.get('until') and pause['until'].replace(tzinfo=timezone.utc) > now():
            ready = [job for job in ready if job['kind'] in {'bulk', 'stock_work'}]
        from web_admin.delivery_claims import eligible, claim_notices
        if ready and all(eligible(row) for row in ready):
            with self.app.app_context(), work_guard(self.app.db):
                claimed = claim_notices(self.app.db, ready, self.snapshot, JOB_SORT)
            # Do not fetch profiles/preferences for jobs another dispatcher owns.
            # One bounded read serves all slices in this accepted batch.
            try:
                self.snapshot.prefetch_users(claimed)
            except Exception:
                self.app.logger.exception('Notification batch prefetch failed; continuing with per-job checks')
            for start in range(0, len(claimed), 3):
                self.pending.add(self.executor.submit(self._run_notice_slice, claimed[start:start+3]))
            return
        for _ in range(min(room, (len(ready) + 2) // 3)):
            self.pending.add(self.executor.submit(self._run_one))

    def shutdown(self):
        self.executor.shutdown(wait=True)
        if self.bookkeeping_future is not None:
            try:
                self.bookkeeping_future.result()
            except Exception:
                for job in self.bookkeeping_jobs:
                    self.snapshot.defer_bookkeeping(job)
                for owner, operation in self.bookkeeping_releases.items():
                    self.snapshot.defer_chat_release(owner, operation)
        self.bookkeeping_executor.shutdown(wait=True)
        while self.snapshot.bookkeeping_count() or self.snapshot.chat_release_count():
            try:
                self._record_notices(self.snapshot.take_bookkeeping(), self.snapshot.take_chat_releases())
            except Exception:
                self.app.logger.exception("Notification records remain in durable acknowledged jobs for recovery")
                break


def start_delivery_worker(app, shop):
    if app.config.get("DELIVERY_WORKER_STARTED"):
        return
    app.config["DELIVERY_WORKER_STARTED"] = True
    pool = DeliveryPool(app, shop)
    app.delivery_pool = pool
    app.logger.info("Notification delivery build47 enabled; shared cap=%s messages/second", effective_rate(app.db))
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
        parent_ids = [job["job_id"] for job in jobs if job["kind"] in {'bulk', 'cleanup'}]
        counts = {parent: {state: 0 for state in ("preparing", "queued", "working", "done", "failed", "review", "cancelled", "acknowledged")} for parent in parent_ids}
        windows = {}
        for row in app.db.delivery_jobs.aggregate([
                {"$match": {"parent_id": {"$in": parent_ids}}},
                {"$group": {"_id": {"parent": "$parent_id", "state": "$status"}, "count": {"$sum": 1},
                    "acknowledged": {"$sum": {"$cond": [{"$ifNull": ["$acknowledged_at", False]}, 1, 0]}},
                    "first_sent": {"$min": "$acknowledged_at"}, "last_sent": {"$max": "$acknowledged_at"}}}]):
            counts[row["_id"]["parent"]][row["_id"]["state"]] = row["count"]
            counts[row['_id']['parent']]['acknowledged'] += row['acknowledged']
            window = windows.setdefault(row['_id']['parent'], [])
            window.extend(t for value in [row.get('first_sent'), row.get('last_sent')] if (t := shop._parse_utc_dt(value)))
        for job in jobs:
            if job["kind"] in {'bulk', 'cleanup'}:
                values = counts[job['job_id']]
                values['sent'] = max(values['done'], values['acknowledged'])
                values['records_pending'] = max(0, values['acknowledged'] - values['done'])
                values['waiting'] = max(0, values['preparing'] + values['queued'] + values['working'] - values['records_pending'])
                window = windows.get(job['job_id']) or []
                if values['acknowledged'] > 1 and window:
                    first, last = min(window), max(window)
                    end = now() if values['waiting'] else last
                    job['send_rate'] = round((values['acknowledged'] - 1) / max(.001, (end - first).total_seconds()), 1)
                job['counts'] = values
        pressure = monitor.pressure()
        pause = app.db.delivery_control.find_one({'key': 'telegram_pause'}) or {}
        until = shop._parse_utc_dt(pause.get('until'))
        return render_template("delivery_queue.html", jobs=jobs, configured_rate=effective_rate(app.db),
            sending_pause=until if until and until > now() else None, database_pressure=pressure)

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
                 else {"parent_id": job_id} if job["kind"] in {'bulk', 'cleanup'} else {"job_id": job_id})
        total = app.db.delivery_jobs.count_documents(query)
        rows = list(app.db.delivery_jobs.find(query).sort("created_at", 1).skip((page - 1) * 50).limit(50))
        return render_template("delivery_job_detail.html", job=job, rows=rows, total=total, page=page)

    @app.post("/delivery-queue/<job_id>/retry")
    @shop.login_required
    @shop.owner_required
    @shop.csrf_required
    def retry_delivery_job(job_id):
        job = app.db.delivery_jobs.find_one({"job_id": job_id})
        if job and job['kind'] == 'delete':
            from web_admin.notification_cleanup import permanent_refusal
            if job.get('cleanup_unavailable') or permanent_refusal(job.get('error')):
                app.db.delivery_jobs.update_one({'job_id': job_id, 'status': {'$in': ['failed', 'review']}},
                    {'$set': {'status': 'cancelled', 'cleanup_unavailable': True, 'expires_at': now() + timedelta(days=30)}})
                flash('Telegram does not allow this deletion. It was skipped and will not be retried.', 'info')
                return redirect(url_for('delivery_queue'))
        if job and job["kind"] in {'bulk', 'cleanup'}:
            query = {"parent_id": job_id, "status": {"$in": ["failed", "review"]}}
        else:
            query = {"job_id": job_id, "status": {"$in": ["failed", "review"]}}
        query['cleanup_unavailable'] = {'$ne': True}
        app.db.delivery_jobs.update_many(query, {"$set": {"status": "queued", "attempts": 0, "next_attempt_at": now()},
            "$unset": {"send_started_at": "", "error": ""}})
        flash("Failed deliveries queued for retry. Completed deliveries are not resent.", "info")
        return redirect(url_for("delivery_queue"))
