"""Scheduled encrypted Telegram exports and wallet reconciliation. No restore routes."""
import base64
import hashlib
import json
import os
import re
import shutil
import tarfile
import tempfile
import threading
import time
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from bson import BSON, json_util
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from flask import Response, flash, redirect, render_template, url_for
from pymongo import ReturnDocument
from utils.data_guard import freeze_writes, raw_database, work_guard, as_utc
from utils.wallet_audit import reconcile_user
from web_admin.delivery_queue import enqueue_job
from web_admin.api_balance_alerts import DEFAULTS as API_BALANCE_DEFAULTS, validate_settings as validate_api_balance_settings, process_alerts
from utils.stock_reservations import sweep_reservations
from utils.wallet_credits import recover_credits
from web_admin.api_recovery import process_recovery
from utils.stock_subscriptions import DEFAULTS as STOCK_NOTICE_DEFAULTS
from utils.buyer_api_settings import DEFAULTS as BUYER_API_DEFAULTS, normalize_endpoint_url
from web_admin.stock_notifications import process_subscriptions

DEFAULTS = {
    "stock_reservations_enabled": "1",
    **STOCK_NOTICE_DEFAULTS,
    **BUYER_API_DEFAULTS,
    **API_BALANCE_DEFAULTS,
    "api_key_requests_per_minute": "60", "api_ip_requests_per_minute": "180", "api_allow_legacy_query_keys": "1",
    "backup_enabled": "0", "backup_chat_id": "", "backup_schedule": "daily", "backup_time": "02:00",
    "backup_timezone": "Asia/Kolkata", "backup_interval_hours": "24", "backup_chunk_mb": "18",
    "backup_timeout_minutes": "10", "backup_max_mb": "512", "backup_retry_minutes": "30",
    "backup_temp_retention_hours": "24", "backup_encryption_key": "",
    "wallet_reconcile_enabled": "1", "wallet_reconcile_interval_minutes": "60", "wallet_reconcile_alert_chat_id": "",
}
BOOL_KEYS = {"backup_enabled", "wallet_reconcile_enabled", "api_allow_legacy_query_keys", "api_low_balance_alerts_enabled", "stock_reservations_enabled"}
INTEGER_LIMITS = {
    "stock_available_again_delay_seconds": (0, 600),
    "stock_available_again_cooldown_minutes": (1, 10080),
    "api_key_requests_per_minute": (1, 10000), "api_ip_requests_per_minute": (1, 100000),
    "backup_interval_hours": (1, 720), "backup_chunk_mb": (1, 18), "backup_timeout_minutes": (1, 30),
    "backup_max_mb": (1, 2048), "backup_retry_minutes": (5, 1440), "backup_temp_retention_hours": (1, 168),
    "wallet_reconcile_interval_minutes": (5, 10080),
}
MAGIC = b"SHOPBACKUP1"
TRANSIENT = {"operational_control", "api_rate_limits", "api_purchase_locks", "runtime_locks"}


def now(): return datetime.now(timezone.utc)


def backup_temp_prefix(db):
    public_connection = os.getenv("MONGO_URI", "").rsplit("@", 1)[-1]
    namespace = hashlib.sha256((raw_database(db).name + public_connection).encode()).hexdigest()[:12]
    return "shop-backup-" + namespace + "-"


def is_backup_directory(db, directory):
    root = Path(tempfile.gettempdir()).resolve()
    return directory.parent.resolve() == root and directory.name.startswith(backup_temp_prefix(db)) and not directory.is_symlink()


def validate_settings(settings):
    result = {key: str(settings.get(key, default) or "").strip() for key, default in DEFAULTS.items()}
    result.update(validate_api_balance_settings(result))
    result["buyer_api_endpoint_url"] = normalize_endpoint_url(result["buyer_api_endpoint_url"])
    for key in BOOL_KEYS:
        if result[key] not in {"0", "1"}: raise ValueError(key.replace("_", " ") + " must be enabled or disabled")
    for key, (minimum, maximum) in INTEGER_LIMITS.items():
        try: value = int(result[key])
        except ValueError: raise ValueError(key.replace("_", " ") + " must be a whole number") from None
        if not minimum <= value <= maximum: raise ValueError(f"{key.replace('_', ' ')} must be between {minimum} and {maximum}")
    if result["backup_schedule"] not in {"daily", "interval"}: raise ValueError("Choose daily or interval backup schedule")
    try:
        datetime.strptime(result["backup_time"], "%H:%M")
        ZoneInfo(result["backup_timezone"])
    except (ValueError, KeyError): raise ValueError("Enter a valid backup time and timezone, e.g. 02:00 and Asia/Kolkata") from None
    for key in ("backup_chat_id", "wallet_reconcile_alert_chat_id"):
        if result[key]:
            try: chat_id = int(result[key])
            except ValueError: raise ValueError(key.replace("_", " ") + " must be a numeric Telegram chat ID") from None
            if not chat_id: raise ValueError("Telegram chat ID cannot be zero")
            if key == "backup_chat_id" and chat_id >= 0: raise ValueError("Backups require a private group ID (a negative number)")
    if result["backup_encryption_key"]:
        try: key = base64.urlsafe_b64decode(result["backup_encryption_key"])
        except Exception: raise ValueError("Backup encryption key must be a URL-safe base64 encoded 32-byte key") from None
        if len(key) != 32: raise ValueError("Backup encryption key must decode to exactly 32 bytes")
    if result["backup_enabled"] == "1" and (not result["backup_chat_id"] or not result["backup_encryption_key"]):
        raise ValueError("Set the private backup group and generate/download an encryption key before enabling backups")
    return result


def next_backup(settings, current):
    zone = ZoneInfo(settings["backup_timezone"])
    hour, minute = map(int, settings["backup_time"].split(":"))
    local = current.astimezone(zone)
    anchor = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
    step = timedelta(days=1) if settings["backup_schedule"] == "daily" else timedelta(hours=int(settings["backup_interval_hours"]))
    while anchor <= local: anchor += step
    return anchor.astimezone(timezone.utc)


def export_archive(db, directory, settings):
    database = raw_database(db)
    directory = Path(directory)
    archive = directory / "database.tar.gz"
    manifest = {"format": "shop-bson-tar-v1", "database": database.name, "exported_at": now().isoformat(), "collections": {},
        "excluded_transient_collections": sorted(TRANSIENT), "encryption_key_excluded": True}
    limit = int(settings["backup_max_mb"]) * 1024 * 1024
    with freeze_writes(db, int(settings["backup_timeout_minutes"]) * 60) as ensure_valid:
        with tarfile.open(archive, "w:gz") as tar:
            for name in sorted(database.list_collection_names()):
                if name in TRANSIENT or name.startswith("system."): continue
                ensure_valid()
                count = 0
                digest = hashlib.sha256()
                data_file = directory / "collection.bson"
                with data_file.open("wb") as target:
                    for doc in database[name].find({}).batch_size(100):
                        if name == "settings" and doc.get("key") == "secret_settings":
                            doc = dict(doc, value=dict(doc.get("value") or {}))
                            doc["value"].pop("backup_encryption_key", None)
                        data = BSON.encode(doc)
                        target.write(data); digest.update(data); count += 1
                        if target.tell() > limit: raise ValueError("Backup exceeds configured size limit")
                        if count % 100 == 0:
                            ensure_valid()
                            if archive.stat().st_size > limit or target.tell() > limit: raise ValueError("Backup exceeds configured size limit")
                tar.add(data_file, arcname=database.name + "/" + name + ".bson")
                data_file.unlink()
                metadata = directory / "metadata.json"
                indexes = [dict(value, key=dict(value["key"]), name=key) for key, value in database[name].index_information().items()]
                metadata.write_text(json_util.dumps({"indexes": indexes, "options": database[name].options()}))
                tar.add(metadata, arcname=database.name + "/" + name + ".metadata.json")
                metadata.unlink()
                manifest["collections"][name] = {"documents": count, "sha256": digest.hexdigest()}
            info = directory / "manifest.json"
            info.write_text(json.dumps(manifest, indent=2))
            tar.add(info, arcname="manifest.json")
        ensure_valid()
    if archive.stat().st_size > limit: raise ValueError("Backup exceeds configured size limit")
    return archive, manifest


def encrypt_and_split(archive, directory, key_text, part_bytes):
    key = base64.urlsafe_b64decode(key_text)
    nonce = os.urandom(12)
    encryptor = Cipher(algorithms.AES(key), modes.GCM(nonce)).encryptor()
    encryptor.authenticate_additional_data(MAGIC)
    output = Path(directory) / "backup.aes"
    with open(archive, "rb") as source, output.open("wb") as target:
        target.write(MAGIC + nonce)
        while chunk := source.read(1024 * 1024): target.write(encryptor.update(chunk))
        target.write(encryptor.finalize()); target.write(encryptor.tag)
    Path(archive).unlink()
    parts = []
    with output.open("rb") as source:
        while chunk := source.read(part_bytes):
            part = Path(directory) / f"shop-backup-{Path(directory).name}.aes.part{len(parts) + 1:03d}"
            part.write_bytes(chunk); parts.append(part)
    digest = hashlib.sha256()
    with output.open("rb") as source:
        while chunk := source.read(1024 * 1024): digest.update(chunk)
    output.unlink()
    return parts, digest.hexdigest()


def telegram_call(db, token, method, *, data, files=None):
    from utils.premium_emojis import transform_payload, emoji_rejected, multipart_data, file_positions, rewind_files
    settings = (raw_database(db).settings.find_one({'key': 'secret_settings'}) or {}).get('value') or {}
    rendered, changed = transform_payload(method, data, settings)
    positions = file_positions(files)
    if files and positions is None:
        rendered, changed = data, False
    for _ in range(4):
        pause = raw_database(db).delivery_control.find_one({"key": "telegram_pause"}) or {}
        until = pause.get("until")
        if until:
            if until.tzinfo is None: until = until.replace(tzinfo=timezone.utc)
            delay = (until - now()).total_seconds()
            if delay > 0: raise RuntimeError("Telegram is rate limited; backup will retry later")
        result = requests.post(f"https://api.telegram.org/bot{token}/{method}", data=multipart_data(rendered), files=files, timeout=(10, 120))
        payload = result.json()
        if changed and result.status_code == 400 and emoji_rejected(payload.get('description', '')):
            rewind_files(positions or [])
            result = requests.post(f"https://api.telegram.org/bot{token}/{method}", data=multipart_data(data), files=files, timeout=(10, 120))
            payload = result.json()
        if payload.get("ok"): return payload["result"]
        if payload.get("error_code") == 429:
            seconds = int((payload.get("parameters") or {}).get("retry_after", 60))
            raw_database(db).delivery_control.update_one({"key": "telegram_pause"}, {"$set": {"until": now() + timedelta(seconds=seconds)}}, upsert=True)
            raise RuntimeError("Telegram is rate limited; backup will retry later")
        raise RuntimeError(f"Telegram rejected backup operation ({payload.get('error_code', result.status_code)}). Check bot permissions and group ID.")
    raise RuntimeError("Telegram backup upload failed")


def run_backup(app, shop, settings):
    db = app.db
    state = raw_database(db).operational_control
    token = shop.get_bot_token(db)
    if not token: raise ValueError("Configure the bot token first")
    if not settings["backup_chat_id"] or not settings["backup_encryption_key"]: raise ValueError("Configure the private group and encryption key first")
    chat = telegram_call(db, token, "getChat", data={"chat_id": settings["backup_chat_id"]})
    if chat.get("type") not in {"group", "supergroup"} or chat.get("username") or chat.get("active_usernames"):
        raise ValueError("Backup destination must be a private Telegram group")
    old = state.find_one({"_id": "backup"}) or {}
    directory = Path(old.get("temp_directory") or "")
    safe_directory = bool(old.get("temp_directory")) and is_backup_directory(db, directory)
    parts = sorted(directory.glob("*.part*")) if safe_directory and directory.is_dir() else []
    valid_parts = len(parts) == old.get("parts") and bool(parts)
    if valid_parts:
        digest = hashlib.sha256()
        for path in parts:
            with path.open("rb") as file:
                while chunk := file.read(1024 * 1024): digest.update(chunk)
        valid_parts = digest.hexdigest() == old.get("sha256")
    if not valid_parts or old.get("destination") != settings["backup_chat_id"] or old.get("key_fingerprint") != hashlib.sha256(settings["backup_encryption_key"].encode()).hexdigest():
        if safe_directory and directory.is_dir(): shutil.rmtree(directory, ignore_errors=True)
        directory = Path(tempfile.mkdtemp(prefix=backup_temp_prefix(db)))
        state.update_one({"_id": "backup"}, {"$set": {"status": "exporting", "started_at": now(), "temp_directory": str(directory), "message_ids": [], "parts": 0, "uploaded": 0, "sha256": ""}}, upsert=True)
        try:
            archive, manifest = export_archive(db, directory, settings)
            parts, digest = encrypt_and_split(archive, directory, settings["backup_encryption_key"], int(settings["backup_chunk_mb"]) * 1024 * 1024)
        except Exception:
            shutil.rmtree(directory, ignore_errors=True)
            state.update_one({"_id": "backup"}, {"$unset": {"temp_directory": ""}})
            raise
        state.update_one({"_id": "backup"}, {"$set": {"status": "uploading", "sha256": digest, "collection_count": len(manifest["collections"]),
            "exported_at": manifest["exported_at"], "parts": len(parts), "uploaded": 0, "destination": settings["backup_chat_id"],
            "key_fingerprint": hashlib.sha256(settings["backup_encryption_key"].encode()).hexdigest(), "message_ids": []}})
        old = state.find_one({"_id": "backup"})
    message_ids = list(old.get("message_ids", []))
    for index, path in enumerate(parts):
        if index < len(message_ids): continue
        with path.open("rb") as file:
            message = telegram_call(db, token, "sendDocument", data={"chat_id": settings["backup_chat_id"],
                "caption": f"Encrypted shop backup — {old.get('exported_at')}\nPart {index + 1}/{len(parts)}. Keep every part and your separate encryption key."},
                files={"document": (path.name, file, "application/octet-stream")})
        message_ids.append(message["message_id"])
        state.update_one({"_id": "backup"}, {"$set": {"message_ids": message_ids, "uploaded": index + 1}})
        time.sleep(0.3)
    telegram_call(db, token, "sendMessage", data={"chat_id": settings["backup_chat_id"],
        "text": f"✅ Backup complete\nDate: {old.get('exported_at')}\nParts: {len(parts)}\nSHA-256: {old.get('sha256')}\nNo backup archive is stored in MongoDB."})
    state.update_one({"_id": "backup"}, {"$set": {"status": "complete", "finished_at": now(), "error": ""}, "$unset": {"temp_directory": "", "retry_at": ""}})
    shutil.rmtree(directory, ignore_errors=True)
    if directory.exists():
        state.update_one({"_id": "backup"}, {"$set": {"error": "Backup delivered; encrypted temporary files await cleanup"}})


def process_reconciliation(app, shop, settings):
    db = app.db
    state = db.operational_control.find_one({"_id": "reconciliation"}) or {}
    if state.get("history_classification_version") != 1:
        # Once per upgrade, replace the old history-only review reports promptly.
        fields = {"history_classification_version": 1, "running": True, "cursor": None, "next_at": now()}
        db.operational_control.update_one({"_id": "reconciliation"}, {"$set": fields}, upsert=True)
        state.update(fields)
    if not state.get("running") and as_utc(state.get("next_at", now())) > now(): return
    query = {"_id": {"$gt": state["cursor"]}} if state.get("cursor") else {}
    users = list(db.users.find(query).sort("_id", 1).limit(50))
    for user in users:
        if "user_id" not in user: continue
        previous = user.get("wallet_reconciliation", {})
        try:
            report = reconcile_user(db, user)
        except Exception:
            app.logger.error("Wallet audit failed for user %s; inspect wallet records.", user["user_id"])
            report = {"status": "review", "checked_at": now(), "issues": ["Wallet records could not be checked; review ledger and stored amounts"],
                "inr": {"expected": None, "actual": user.get("wallet_inr"), "difference": None},
                "usdt": {"expected": None, "actual": user.get("wallet_usdt"), "difference": None}}
            db.users.update_one({"_id": user["_id"]}, {"$set": {"wallet_reconciliation": report}})
        if report and report["status"] == "ok" and (previous.get("status") == "review" or (report.get("history_notes") and not previous.get("history_notes"))):
            # Includes untagged alerts already queued by the previous release.
            prefix = "⚠️ Wallet review required\nUser: " + str(user["user_id"]) + "\n"
            db.delivery_jobs.update_many({"kind": "text", "status": "queued", "acknowledged_at": {"$exists": False},
                "send_started_at": {"$exists": False}, "$or": [{"payload.wallet_review_user_id": user["user_id"]},
                    {"payload.text": re.compile("^" + re.escape(prefix))}]}, {"$set": {
                        "status": "cancelled", "updated_at": now(), "expires_at": now() + timedelta(days=30),
                        "error": "Wallet now reconciles; old history-only/resolved alert cancelled"}})
        if report and report["status"] == "review" and report["issues"] != previous.get("issues"):
            targets = [int(settings["wallet_reconcile_alert_chat_id"])] if settings["wallet_reconcile_alert_chat_id"] else shop.get_admin_ids(db)
            for target in targets:
                enqueue_job(db, "text", {"chat_id": target, "wallet_review_user_id": user["user_id"],
                    "wallet_review_issues": report["issues"], "text": f"⚠️ Wallet review required\nUser: {user['user_id']}\n" + "\n".join(report["issues"]) + "\nOpen WebAdmin → Wallet reconciliation. Balances were not automatically changed."})
    fields = {"running": bool(users), "checked_at": now()}
    if users: fields["cursor"] = users[-1]["_id"]
    else: fields.update(cursor=None, next_at=now() + timedelta(minutes=int(settings["wallet_reconcile_interval_minutes"])))
    db.operational_control.update_one({"_id": "reconciliation"}, {"$set": fields}, upsert=True)


def service_tick(app, shop):
    db = app.db
    settings = validate_settings(shop.get_secret_settings(db))
    control = db.operational_control
    control.update_one({"_id": "services"}, {"$setOnInsert": {"expires_at": now()}}, upsert=True)
    owner = uuid.uuid4().hex
    if not control.find_one_and_update({"_id": "services", "expires_at": {"$lte": now()}},
            {"$set": {"owner": owner, "expires_at": now() + timedelta(minutes=2)}}, return_document=ReturnDocument.AFTER): return
    stopped = threading.Event()
    def heartbeat():
        while not stopped.wait(20):
            control.update_one({"_id": "services", "owner": owner}, {"$set": {"expires_at": now() + timedelta(minutes=2)}})
    pulse = threading.Thread(target=heartbeat, daemon=True); pulse.start()
    try:
        with work_guard(db):
            recover_credits(db)
            process_recovery(app, shop)
            sweep_reservations(db)
            process_alerts(app, shop, settings)
            process_subscriptions(app, shop, settings)
        state = control.find_one({"_id": "backup"}) or {}
        fingerprint = hashlib.sha256(json.dumps({key: settings[key] for key in ("backup_enabled", "backup_schedule", "backup_time", "backup_timezone", "backup_interval_hours")}, sort_keys=True).encode()).hexdigest()
        if state.get("schedule_fingerprint") != fingerprint:
            control.update_one({"_id": "backup"}, {"$set": {"schedule_fingerprint": fingerprint, "next_at": next_backup(settings, now())}}, upsert=True)
            state = control.find_one({"_id": "backup"})
        due = settings["backup_enabled"] == "1" and as_utc(state.get("retry_at") or state.get("next_at", now())) <= now()
        if state.get("requested") or due:
            control.update_one({"_id": "backup"}, {"$set": {"requested": False}})
            try:
                run_backup(app, shop, settings)
                next_at = now() + timedelta(hours=int(settings["backup_interval_hours"])) if settings["backup_schedule"] == "interval" else next_backup(settings, now())
                control.update_one({"_id": "backup"}, {"$set": {"next_at": next_at}})
            except Exception as error:
                app.logger.error("Telegram backup failed (%s); see Secret Settings status.", type(error).__name__)
                safe_error = str(error) if isinstance(error, (ValueError, RuntimeError, TimeoutError)) else "Backup failed; check server logs, database access and Telegram connectivity"
                control.update_one({"_id": "backup"}, {"$set": {"status": "failed", "error": safe_error[:400], "retry_at": now() + timedelta(minutes=int(settings["backup_retry_minutes"]))}})
        if settings["wallet_reconcile_enabled"] == "1":
            with work_guard(db): process_reconciliation(app, shop, settings)
        retention = now().timestamp() - int(settings["backup_temp_retention_hours"]) * 3600
        for path in Path(tempfile.gettempdir()).glob(backup_temp_prefix(db) + "*"):
            if is_backup_directory(db, path) and path.is_dir() and path.stat().st_mtime < retention:
                shutil.rmtree(path, ignore_errors=True)
    finally:
        stopped.set(); pulse.join(timeout=1)
        control.update_one({"_id": "services", "owner": owner}, {"$set": {"expires_at": now()}})


def start_operations_worker(app, shop):
    if app.config.get("OPERATIONS_WORKER_STARTED"): return
    app.config["OPERATIONS_WORKER_STARTED"] = True
    def worker():
        while True:
            try:
                with app.app_context(): service_tick(app, shop)
            except Exception as error: app.logger.error("Scheduled services unavailable (%s)", type(error).__name__)
            time.sleep(10)
    app.operations_thread = threading.Thread(target=worker, daemon=True, name="scheduled-shop-services")
    app.operations_thread.start()


def register_operations_routes(app, shop):
    @app.post("/backups/run")
    @shop.login_required
    @shop.owner_required
    @shop.csrf_required
    def backup_run():
        settings = validate_settings(shop.get_secret_settings(app.db))
        if not settings["backup_chat_id"] or not settings["backup_encryption_key"]:
            flash("Configure the backup group and encryption key first.", "error")
        else:
            app.db.operational_control.update_one({"_id": "backup"}, {"$set": {"requested": True}}, upsert=True)
            flash("Backup requested. It will export and upload in the background.", "success")
        return redirect(url_for("secret_settings_form"))

    @app.post("/backups/key")
    @shop.login_required
    @shop.owner_required
    @shop.csrf_required
    def backup_key():
        settings = shop.get_secret_settings(app.db)
        if not settings.get("backup_encryption_key"):
            settings["backup_encryption_key"] = base64.urlsafe_b64encode(os.urandom(32)).decode()
            shop.set_secret_settings(app.db, settings, preserve_routing=True)
        return Response(settings["backup_encryption_key"] + "\n", mimetype="text/plain",
            headers={"Content-Disposition": "attachment; filename=shop-backup-encryption-key.txt", "Cache-Control": "no-store"})

    @app.get("/wallet-reconciliation")
    @shop.login_required
    @shop.owner_required
    def wallet_reconciliation():
        users = list(app.db.users.find({"wallet_reconciliation.status": "review"}).limit(200))
        history_users = list(app.db.users.find({"wallet_reconciliation.status": "ok",
            "wallet_reconciliation.history_notes.0": {"$exists": True}}).limit(50))
        state = app.db.operational_control.find_one({"_id": "reconciliation"}) or {}
        return render_template("wallet_reconciliation.html", users=users, history_users=history_users, state=state)

    @app.post("/wallet-reconciliation/run")
    @shop.login_required
    @shop.owner_required
    @shop.csrf_required
    def wallet_reconciliation_run():
        app.db.operational_control.update_one({"_id": "reconciliation"}, {"$set": {"running": False, "cursor": None, "next_at": now()}}, upsert=True)
        flash("Wallet reconciliation requested. The background worker will scan accounts.", "success")
        return redirect(url_for("wallet_reconciliation"))
