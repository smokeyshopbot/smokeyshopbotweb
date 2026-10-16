"""Shared background pacing across WebAdmin and the Telegram bot.

Interactive commands do not use this budget. No paid broadcasts are enabled.
"""
import asyncio
import os
import threading
from collections import deque
import time
import uuid
from contextlib import contextmanager, asynccontextmanager
from datetime import datetime, timezone, timedelta

from pymongo.errors import DuplicateKeyError

# Conservative default leaves MongoDB Free headroom for purchases and commands.
try:
    BACKGROUND_MESSAGES_PER_SECOND = max(1, min(25, int(os.getenv("BACKGROUND_MESSAGES_PER_SECOND", "5"))))
except ValueError:
    BACKGROUND_MESSAGES_PER_SECOND = 5
_ticket_lock = threading.Lock()
_ticket_pools = {}
_async_ticket_pools = {}
CHAT_INTERVAL_SECONDS = 1.05
GROUP_INTERVAL_SECONDS = 3.1
GLOBAL_KEY = "background_send_slot"


def now():
    return datetime.now(timezone.utc)


def utc(value):
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


class NotificationDeferred(Exception):
    """No HTTP send was attempted; retry later without consuming an attempt."""
    def __init__(self, seconds):
        self.retry_after = max(0.01, float(seconds))
        super().__init__("Background delivery is waiting for a sending slot")


def pause_delay(row):
    until = (row or {}).get("until")
    return max(0.0, (utc(until) - now()).total_seconds()) if until else 0.0


async def notifications_paused(database):
    return bool(pause_delay(await database.delivery_control.find_one({"key": "telegram_pause"})))


async def pause_notifications(database, seconds):
    seconds = seconds.total_seconds() if isinstance(seconds, timedelta) else float(seconds)
    await database.delivery_control.update_one({"key": "telegram_pause"},
        {"$max": {"until": now() + timedelta(seconds=max(1, seconds))}}, upsert=True)
    return max(1, seconds)


def _chat_key(chat_id):
    return "background_chat:" + str(chat_id).lower()


def _chat_query(key, current):
    return {"key": key, "$and": [
        {"$or": [{"lease_until": {"$lte": current}}, {"lease_until": {"$exists": False}}]},
        {"$or": [{"next_at": {"$lte": current}}, {"next_at": {"$exists": False}}]}]}


def _chat_fields(owner, current):
    return {"$set": {"owner": owner, "lease_until": current + timedelta(minutes=5),
                     "expires_at": current + timedelta(hours=24)}}


def _chat_delay(row):
    current = now()
    # A busy in-flight request may finish soon. Never postpone it for five minutes.
    retry = current + timedelta(seconds=0.1)
    if row.get("next_at"):
        retry = max(retry, utc(row["next_at"]))
    return max(0.01, (retry - current).total_seconds())


def _global_reservation(row, count=1):
    current = now()
    previous = row.get("next_at")
    slot = max(current, utc(previous)) if previous else current
    if slot > current + timedelta(seconds=5):
        raise NotificationDeferred(0.1)
    query = {"key": GLOBAL_KEY, "next_at": previous} if previous else {
        "key": GLOBAL_KEY, "next_at": {"$exists": False}}
    return query, {"$set": {"next_at": slot + timedelta(seconds=count / BACKGROUND_MESSAGES_PER_SECOND)}}, slot


def _cooldown(chat_id, group):
    return GROUP_INTERVAL_SECONDS if group or str(chat_id).startswith(("-", "@")) else CHAT_INTERVAL_SECONDS



def _tickets(database):
    from utils.data_guard import raw_database
    key = id(raw_database(database))
    if len(_ticket_pools) > 64:
        _ticket_pools.clear()
    existing = _ticket_pools.get(key)
    raw = raw_database(database)
    if existing is None or existing[0] is not raw:
        existing = (raw, deque())
        _ticket_pools[key] = existing
    return existing[1]


def _reserve_start(database):
    with _ticket_lock:
        tickets = _tickets(database)
        while tickets and tickets[0] < now():
            tickets.popleft()  # Unused old tickets cannot turn into a later burst.
        if tickets:
            return tickets.popleft()
        for _ in range(50):
            row = database.delivery_control.find_one({"key": GLOBAL_KEY}) or {}
            query, update, slot = _global_reservation(row, 4)
            try:
                database.delivery_control.update_one(query, update, upsert=True)
                tickets.extend(slot + timedelta(seconds=i / BACKGROUND_MESSAGES_PER_SECOND) for i in range(1, 4))
                return slot
            except DuplicateKeyError:
                continue
        raise NotificationDeferred(0.1)


async def _reserve_start_async(database):
    # Separate local ticket blocks share the same cross-service MongoDB counter.
    from utils.data_guard import raw_database
    key = (id(raw_database(database)), asyncio.get_running_loop())
    if len(_async_ticket_pools) > 64:
        _async_ticket_pools.clear()
    lock, tickets = _async_ticket_pools.setdefault(key, (asyncio.Lock(), deque()))
    async with lock:
        while tickets and tickets[0] < now():
            tickets.popleft()
        if tickets:
            return tickets.popleft()
        for _ in range(50):
            row = await database.delivery_control.find_one({"key": GLOBAL_KEY}) or {}
            query, update, slot = _global_reservation(row, 4)
            try:
                await database.delivery_control.update_one(query, update, upsert=True)
                tickets.extend(slot + timedelta(seconds=i / BACKGROUND_MESSAGES_PER_SECOND) for i in range(1, 4))
                return slot
            except DuplicateKeyError:
                continue
        raise NotificationDeferred(0.1)

@contextmanager
def notification_slot(database, chat_id, *, group=False):
    """Keep one delivery in flight per chat; reserve a globally paced start."""
    delay = pause_delay(database.delivery_control.find_one({"key": "telegram_pause"}))
    if delay:
        raise NotificationDeferred(delay)
    key, owner, entered = _chat_key(chat_id), uuid.uuid4().hex, False
    try:
        database.delivery_control.update_one(_chat_query(key, now()), _chat_fields(owner, now()), upsert=True)
    except DuplicateKeyError:
        row = database.delivery_control.find_one({"key": key}) or {}
        raise NotificationDeferred(_chat_delay(row)) from None
    try:
        slot = _reserve_start(database)
        delay = max(0.0, (slot - now()).total_seconds())
        if delay:
            time.sleep(delay)
        delay = pause_delay(database.delivery_control.find_one({"key": "telegram_pause"}))
        if delay:
            raise NotificationDeferred(delay)
        entered = True
        yield
    finally:
        fields = {"$unset": {"owner": "", "lease_until": ""}}
        if entered:
            fields["$set"] = {"next_at": now() + timedelta(seconds=_cooldown(chat_id, group))}
        database.delivery_control.update_one({"key": key, "owner": owner}, fields)


@asynccontextmanager
async def notification_slot_async(database, chat_id, *, group=False):
    """Async counterpart using the same MongoDB keys and allocation rules."""
    key, owner, entered = _chat_key(chat_id), uuid.uuid4().hex, False
    waiting_since, backoff = time.monotonic(), 0.25
    while True:
        delay = pause_delay(await database.delivery_control.find_one({"key": "telegram_pause"}))
        if delay:
            raise NotificationDeferred(delay)
        try:
            await database.delivery_control.update_one(_chat_query(key, now()), _chat_fields(owner, now()), upsert=True)
            break
        except DuplicateKeyError:
            row = await database.delivery_control.find_one({"key": key}) or {}
            if row.get("lease_until") and utc(row["lease_until"]) > now():
                if time.monotonic() - waiting_since >= 2:
                    raise NotificationDeferred(_chat_delay(row)) from None
                await asyncio.sleep(backoff)
                backoff = min(1.0, backoff * 2)
            else:
                # A known cooldown needs one wait, not four MongoDB probes/second.
                await asyncio.sleep(_chat_delay(row))
    try:
        slot = await _reserve_start_async(database)
        delay = max(0.0, (slot - now()).total_seconds())
        if delay:
            await asyncio.sleep(delay)
        delay = pause_delay(await database.delivery_control.find_one({"key": "telegram_pause"}))
        if delay:
            raise NotificationDeferred(delay)
        entered = True
        yield
    finally:
        fields = {"$unset": {"owner": "", "lease_until": ""}}
        if entered:
            fields["$set"] = {"next_at": now() + timedelta(seconds=_cooldown(chat_id, group))}
        await database.delivery_control.update_one({"key": key, "owner": owner}, fields)


class BackgroundBot:
    """Pace background calls only; callbacks and normal bot replies stay immediate."""
    def __init__(self, bot, database):
        self.bot, self.database = bot, database

    def __getattr__(self, name):
        target = getattr(self.bot, name)
        if name not in {"send_message", "send_poll", "edit_message_text", "stop_poll", "send_document"}:
            return target
        async def call(*args, **kwargs):
            chat_id = kwargs.get("chat_id", args[0] if args else None)
            async with notification_slot_async(self.database, chat_id):
                return await target(*args, **kwargs)
        return call
