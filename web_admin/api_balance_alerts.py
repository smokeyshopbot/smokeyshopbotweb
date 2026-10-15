"""Targeted shared-wallet warnings; durable enqueue outside purchase requests."""
import uuid
from datetime import datetime, timezone, timedelta
from decimal import Decimal, InvalidOperation

from pymongo import ReturnDocument
from web_admin.delivery_queue import enqueue_job

DEFAULTS = {"api_low_balance_alerts_enabled": "1", "api_low_balance_threshold_usdt": "10",
            "api_low_balance_cooldown_minutes": "1440"}


def validate_settings(settings):
    result = {key: str(settings.get(key, default)).strip() for key, default in DEFAULTS.items()}
    if result["api_low_balance_alerts_enabled"] not in {"0", "1"}:
        raise ValueError("API low balance alerts must be enabled or disabled")
    try:
        threshold = Decimal(result["api_low_balance_threshold_usdt"])
        if not threshold.is_finite() or not Decimal("0.000001") <= threshold <= Decimal("1000000000") or threshold != threshold.quantize(Decimal("0.000001")):
            raise ValueError
    except (InvalidOperation, ValueError):
        raise ValueError("API low balance threshold must be between 0.000001 and 1000000000 USDT, with at most 6 decimal places") from None
    try:
        minutes = int(result["api_low_balance_cooldown_minutes"])
        if not 1 <= minutes <= 10080:
            raise ValueError
    except ValueError:
        raise ValueError("API low balance cooldown must be between 1 and 10080 minutes") from None
    result["api_low_balance_threshold_usdt"] = format(threshold, "f")
    result["api_low_balance_cooldown_minutes"] = str(minutes)
    return result


def eligible(user, settings):
    try:
        balance = Decimal(str((user or {}).get("wallet_usdt") or 0))
        if not balance.is_finite(): return False
    except InvalidOperation:
        return False
    return bool(user and settings["api_low_balance_alerts_enabled"] == "1"
                and user.get("api_enabled") is not False and user.get("api_key_hash")
                and user.get("api_last_used_at") and int(user.get("api_request_count") or 0) > 0
                and not user.get("blocked") and user.get("api_low_balance_alerts_enabled") is not False
                and balance < Decimal(settings["api_low_balance_threshold_usdt"]))


def money(value):
    return format(Decimal(str(value or 0)).quantize(Decimal("0.000001")), "f").rstrip("0").rstrip(".") or "0"


def alert_text(user, settings):
    balance, threshold = money(user.get("wallet_usdt", 0)), money(settings["api_low_balance_threshold_usdt"])
    if user.get("language") == "es":
        return ("⚠️ <b>Saldo bajo para compras API</b>\n\n"
                f"Saldo de tu billetera principal: <b>{balance} USDT</b>\nUmbral de aviso: {threshold} USDT\n\n"
                "Recarga con /wallet para evitar que fallen tus compras API. Las compras del bot y de la API usan esta misma billetera.\n\n"
                "Puedes desactivar estos avisos en /api.")
    return ("⚠️ <b>Low balance for API purchases</b>\n\n"
            f"Main wallet balance: <b>{balance} USDT</b>\nAlert threshold: {threshold} USDT\n\n"
            "Top up with /wallet to avoid failed API purchases. Bot purchases and API orders use this same wallet.\n\n"
            "You can turn these alerts off in /api.")


def process_alerts(app, shop, settings, limit=50):
    """A per-user outbox and deterministic job ID survive restarts and duplicate workers."""
    settings = validate_settings(settings)
    if settings["api_low_balance_alerts_enabled"] != "1":
        return
    db = app.db
    current = datetime.now(timezone.utc)
    # Recover an interrupted enqueue before claiming any new notification.
    for user in db.users.find({"api_low_balance_outbox.job_id": {"$exists": True}}).limit(limit):
        outbox = user["api_low_balance_outbox"]
        if eligible(user, settings):
            enqueue_job(db, "text", {"chat_id": user["user_id"], "text": alert_text(user, settings),
                "parse_mode": "HTML", "api_low_balance_alert": True,
                "notification": {"notification_type": "api_low_balance"}}, job_id=outbox["job_id"], priority=8)
        db.users.update_one({"_id": user["_id"], "api_low_balance_outbox.job_id": outbox["job_id"]},
                            {"$unset": {"api_low_balance_outbox": ""}})
    due = {"api_enabled": {"$ne": False}, "api_key_hash": {"$type": "string", "$ne": ""},
           "api_last_used_at": {"$exists": True}, "api_request_count": {"$gt": 0},
           "blocked": {"$ne": True}, "api_low_balance_alerts_enabled": {"$ne": False},
           "wallet_usdt": {"$lt": float(settings["api_low_balance_threshold_usdt"])},
           "api_low_balance_outbox": {"$exists": False},
           "$or": [{"api_low_balance_next_at": {"$exists": False}}, {"api_low_balance_next_at": {"$lte": current}}]}
    for _ in range(limit):
        job_id = "api-low-balance:" + uuid.uuid4().hex
        user = db.users.find_one_and_update(due, {"$set": {
            "api_low_balance_next_at": current + timedelta(minutes=int(settings["api_low_balance_cooldown_minutes"])),
            "api_low_balance_outbox": {"job_id": job_id, "created_at": current}}}, return_document=ReturnDocument.AFTER)
        if not user:
            break
        # Leave the outbox intact if Mongo/queue writes fail; the next tick resumes it.
        if eligible(user, settings):
            enqueue_job(db, "text", {"chat_id": user["user_id"], "text": alert_text(user, settings),
                "parse_mode": "HTML", "api_low_balance_alert": True,
                "notification": {"notification_type": "api_low_balance"}}, job_id=job_id, priority=8)
        db.users.update_one({"_id": user["_id"], "api_low_balance_outbox.job_id": job_id},
                            {"$unset": {"api_low_balance_outbox": ""}})
