"""Shared Telegram retry-after pause for all background senders."""
from datetime import datetime, timezone, timedelta


async def notifications_paused(database):
    row = await database.delivery_control.find_one({"key": "telegram_pause"}) or {}
    until = row.get("until")
    return bool(until and until.replace(tzinfo=timezone.utc) > datetime.now(timezone.utc))


async def pause_notifications(database, seconds):
    seconds = seconds.total_seconds() if isinstance(seconds, timedelta) else float(seconds)
    await database.delivery_control.update_one({"key": "telegram_pause"},
        {"$max": {"until": datetime.now(timezone.utc) + timedelta(seconds=max(1, seconds))}}, upsert=True)
    return max(1, seconds)
