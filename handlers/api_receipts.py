"""Retryable Telegram receipts, independent of API purchase responses."""
import asyncio
import logging
from datetime import datetime, timezone, timedelta
from pymongo import ReturnDocument
from telegram.error import RetryAfter
import database as db
from utils.api_receipts import purchase_receipt
from utils.notification_limits import notifications_paused, pause_notifications
from utils.data_guard import guarded_async

logger = logging.getLogger(__name__)


@guarded_async(lambda: db.get_db())
async def process_api_receipts(bot, limit=10):
    database = db.get_db()
    now = datetime.now(timezone.utc)
    if await notifications_paused(database):
        return
    await database.orders.update_many({"api_receipt_status": "sending", "api_receipt_claimed_at": {"$lt": now - timedelta(minutes=2)}},
        {"$set": {"api_receipt_status": "pending", "api_receipt_next_attempt": now}})
    for _ in range(limit):
        order = await database.orders.find_one_and_update({
            "api_purchase": True, "status": "delivered", "api_receipt_status": "pending",
            "$or": [{"api_receipt_next_attempt": {"$exists": False}}, {"api_receipt_next_attempt": {"$lte": now}}],
        }, {"$set": {"api_receipt_status": "sending", "api_receipt_claimed_at": now}}, return_document=ReturnDocument.AFTER)
        if not order:
            break
        rate_limited = False
        try:
            user = await database.users.find_one({"user_id": order["user_id"]}) or {}
            product = await database.products.find_one({"name": order.get("product_name", "")}) or {}
            result = await bot.send_message(chat_id=order["user_id"], text=purchase_receipt(order, user.get("language", "en"), product))
            fields = {"api_receipt_status": "sent", "api_receipt_sent_at": datetime.now(timezone.utc), "api_receipt_message_id": result.message_id}
        except RetryAfter as exc:
            delay = await pause_notifications(database, exc.retry_after)
            fields = {"api_receipt_status": "pending", "api_receipt_next_attempt": now + timedelta(seconds=delay)}
            rate_limited = True
        except Exception as exc:
            attempts = int(order.get("api_receipt_attempts", 0)) + 1
            fields = {"api_receipt_status": "failed" if attempts >= 5 else "pending", "api_receipt_attempts": attempts,
                      "api_receipt_error": str(exc)[:300], "api_receipt_next_attempt": now + timedelta(seconds=min(3600, 60 * 2 ** (attempts - 1)))}
        await database.orders.update_one({"_id": order["_id"]}, {"$set": fields})
        if rate_limited:
            return
        await asyncio.sleep(0.1)


async def start_api_receipt_watcher(application):
    async def worker():
        while True:
            try:
                await process_api_receipts(application.bot)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("API receipt queue processing failed")
            await asyncio.sleep(3)
    application.bot_data["api_receipt_task"] = asyncio.create_task(worker())


async def stop_background_services(application):
    stop_lookups = getattr(db, 'stop_supplier_lookups', None)
    if stop_lookups:
        await stop_lookups()
    stop_runtime = getattr(db, 'stop_runtime_cache_refresh', None)
    if stop_runtime:
        await stop_runtime()
    tasks = [application.bot_data.get(key) for key in ("api_receipt_task", "poll_task", "channel_membership_refresh_task")]
    for task in tasks:
        if task:
            task.cancel()
    await asyncio.gather(*(task for task in tasks if task), return_exceptions=True)
