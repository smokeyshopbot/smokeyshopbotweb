"""Flask web admin panel for the Telegram shop bot.

Run with:
    python -m web_admin.app

The panel uses the same MongoDB collections as the Telegram bot and is now the only admin panel.
"""

from __future__ import annotations

import csv
import functools
import html
import hmac
import hashlib
import io
import math
import os
import re
import secrets
import time
import uuid
from datetime import datetime, timezone, timedelta
from typing import Any, Callable

import requests
from dotenv import load_dotenv
from flask import (
    Flask,
    Response,
    jsonify,
    abort,
    current_app,
    flash,
    has_request_context,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError
from werkzeug.security import check_password_hash, generate_password_hash
from utils.i18n import tr, lang_from_user, normalize_lang, LANGUAGE_NAMES
from utils.bscscan import public_usdt_error_text, extract_usdt_received_amount_from_error

load_dotenv()

PAGE_SIZE = 10
LOW_STOCK_ALERT_THRESHOLD = int(os.getenv("LOW_STOCK_ALERT_THRESHOLD", "10") or 10)
RESTOCK_BACK_IN_STOCK_COOLDOWN_MINUTES = int(os.getenv("RESTOCK_BACK_IN_STOCK_COOLDOWN_MINUTES", "30") or 30)
RESTOCK_NOTIFICATION_COOLDOWN_MINUTES = int(os.getenv("RESTOCK_NOTIFICATION_COOLDOWN_MINUTES", "60") or 60)
RESTOCK_LONG_NOTIFICATION_COOLDOWN_MINUTES = int(os.getenv("RESTOCK_LONG_NOTIFICATION_COOLDOWN_MINUTES", "360") or 360)
RESTOCK_BIG_ADDITION_THRESHOLD = int(os.getenv("RESTOCK_BIG_ADDITION_THRESHOLD", os.getenv("RESTOCK_HIGH_STOCK_THRESHOLD", "20")) or 20)
RESTOCK_HIGH_STOCK_THRESHOLD = RESTOCK_BIG_ADDITION_THRESHOLD  # Backwards-compatible alias.
PAYMENT_TIMEOUT_MINUTES = int(os.getenv("PAYMENT_TIMEOUT_MINUTES", "30") or 30)
STOCK_MANAGER_MIN_PAYOUT_USDT = 10.0
LOGIN_ATTEMPT_LIMIT = 5
LOGIN_ATTEMPT_WINDOW_SECONDS = 15 * 60
LOGIN_LOCK_SECONDS = 15 * 60
SENSITIVE_SECRET_SETTING_KEYS = {
    "bot_token",
    "bscscan_api_key",
    "polygonscan_api_key",
    "etherscan_api_key",
    "binance_api_key",
    "binance_api_secret",
    "admin_panel_secret_key",
}

ADMIN_ROLE_OWNER = "owner"
ADMIN_ROLE_STOCK_MANAGER = "stock_manager"
ADMIN_ROLE_PAYMENT_MANAGER = "payment_manager"
ADMIN_ROLE_ORDERS_MANAGER = "orders_manager"
ADMIN_SUB_ROLES = {ADMIN_ROLE_STOCK_MANAGER, ADMIN_ROLE_PAYMENT_MANAGER, ADMIN_ROLE_ORDERS_MANAGER}
ADMIN_ROLE_LABELS = {
    ADMIN_ROLE_OWNER: "Owner",
    ADMIN_ROLE_STOCK_MANAGER: "Stock Manager",
    ADMIN_ROLE_PAYMENT_MANAGER: "Payment Manager",
    ADMIN_ROLE_ORDERS_MANAGER: "Orders Manager",
}
ADMIN_ROLE_DESCRIPTIONS = {
    ADMIN_ROLE_OWNER: "Full access to every WebAdmin section.",
    ADMIN_ROLE_STOCK_MANAGER: "Can open My Stock Dashboard and only assigned Products & Stock, add/remove own stock, save payment details, and request payout. Product prices/details/settings stay hidden.",
    ADMIN_ROLE_PAYMENT_MANAGER: "Can open Payment Reviews and approve/reject manual payment submissions.",
    ADMIN_ROLE_ORDERS_MANAGER: "Can open order pages, expire/resend orders, but user IDs/usernames stay hidden.",
}
STOCK_MANAGER_PAYMENT_METHOD_LABELS = {
    "upi": "UPI",
    "bep20": "USDT (BEP20)",
    "binance": "Binance Pay",
}
ROLE_HOME_ENDPOINTS = {
    ADMIN_ROLE_OWNER: "dashboard",
    ADMIN_ROLE_STOCK_MANAGER: "stock_manager_dashboard",
    ADMIN_ROLE_PAYMENT_MANAGER: "payments",
    ADMIN_ROLE_ORDERS_MANAGER: "orders",
}
COMMON_ADMIN_ENDPOINTS = {"static", "login_form", "login_submit", "logout", "live_state"}


def _env_int(name: str, default: int, *, minimum: int = 0, maximum: int | None = None) -> int:
    """Read an integer environment setting without letting bad values crash boot."""
    try:
        value = int(str(os.getenv(name, str(default)) or default).strip())
    except (TypeError, ValueError):
        value = default
    value = max(minimum, value)
    if maximum is not None:
        value = min(maximum, value)
    return value


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


ADMIN_CACHE_TTL_SECONDS = _env_int("ADMIN_CACHE_TTL_SECONDS", 15, minimum=0, maximum=300)
ADMIN_DASHBOARD_CACHE_TTL_SECONDS = _env_int("ADMIN_DASHBOARD_CACHE_TTL_SECONDS", 30, minimum=0, maximum=300)
ADMIN_LIVE_STATE_CACHE_TTL_SECONDS = _env_int("ADMIN_LIVE_STATE_CACHE_TTL_SECONDS", 10, minimum=0, maximum=120)
ADMIN_LIVE_STATE_REFRESH_MS = _env_int("ADMIN_LIVE_STATE_REFRESH_MS", 15000, minimum=5000, maximum=120000)
ADMIN_LIVE_FULL_REFRESH = _env_bool("ADMIN_LIVE_FULL_REFRESH", False)
ADMIN_AUTO_INDEXES = _env_bool("ADMIN_AUTO_INDEXES", False)

_TTL_CACHE: dict[str, tuple[float, Any]] = {}


def cached_value(key: str, ttl_seconds: int, factory: Callable[[], Any]) -> Any:
    """Small per-process TTL cache for expensive sidebar/dashboard queries.

    Render/Railway free instances are tiny, and this admin panel has live polling.
    Caching repeated counts for a few seconds dramatically reduces MongoDB round
    trips without hiding admin changes for long.
    """
    if ttl_seconds <= 0:
        return factory()
    now = time.time()
    cached = _TTL_CACHE.get(key)
    if cached and cached[0] > now:
        return cached[1]
    value = factory()
    _TTL_CACHE[key] = (now + ttl_seconds, value)
    return value


def clear_admin_cache(prefix: str | None = None) -> None:
    if prefix is None:
        _TTL_CACHE.clear()
        return
    for key in list(_TTL_CACHE):
        if key.startswith(prefix):
            _TTL_CACHE.pop(key, None)


def secret_fingerprint(value: str | None) -> str:
    raw = str(value or "").strip()
    if not raw:
        return "missing"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]


def mask_secret_value(value: str | None, *, keep_left: int = 4, keep_right: int = 4) -> str:
    raw = str(value or "").strip()
    if not raw:
        return "missing"
    if len(raw) <= keep_left + keep_right:
        return "saved"
    return f"{raw[:keep_left]}…{raw[-keep_right:]}"


def mask_bot_token(value: str | None) -> str:
    raw = str(value or "").strip()
    if not raw:
        return "missing"
    if ":" not in raw:
        return f"saved / sha256:{secret_fingerprint(raw)}"
    bot_id, token_secret = raw.split(":", 1)
    safe_id = mask_secret_value(bot_id, keep_left=4, keep_right=2)
    safe_secret = mask_secret_value(token_secret, keep_left=0, keep_right=4)
    return f"bot_id {safe_id}:{safe_secret} / sha256:{secret_fingerprint(raw)}"

ROLE_ENDPOINTS: dict[str, set[str]] = {
    ADMIN_ROLE_STOCK_MANAGER: {
        "stock_manager_dashboard", "save_stock_manager_payment_details", "request_stock_manager_payment",
        "products", "product_manage", "add_stock", "remove_stock", "export_stock_csv",
    },
    ADMIN_ROLE_PAYMENT_MANAGER: {
        "payments", "decide_payment", "tx_hash_logs", "legacy_payment_audit_redirect", "telegram_file", "order_detail", "mark_order_refund_paid",
    },
    ADMIN_ROLE_ORDERS_MANAGER: {
        "orders", "order_detail", "pending_orders", "expire_stale_orders_now", "expire_order_now", "resend_order", "revoke_order_delivery", "return_revoked_order_to_stock",
        "cancel_pending_stock_order", "mark_order_refund_paid",
        "replacement_orders", "replacement_order_detail",
    },
}


def _split_csv(value: str) -> list[str]:
    return [part.strip() for part in value.split(",") if part.strip()]


def normalize_stock_manager_payment_method(method: str | None) -> str:
    method = str(method or "").strip().lower()
    return method if method in STOCK_MANAGER_PAYMENT_METHOD_LABELS else "upi"


def _compact_text(value: Any, limit: int = 300) -> str:
    text = str(value or "").strip()
    text = re.sub(r"[\r\n\t]+", " ", text)
    text = re.sub(r"\s{2,}", " ", text)
    return text[:limit].strip()


def _stable_text_hash(value: Any) -> str:
    return hashlib.sha256(str(value or "").encode("utf-8", "ignore")).hexdigest()[:16]


def clean_stock_manager_payment_methods(methods: Any) -> dict[str, dict[str, str]]:
    methods = methods if isinstance(methods, dict) else {}
    upi = methods.get("upi") if isinstance(methods.get("upi"), dict) else {}
    bep20 = methods.get("bep20") if isinstance(methods.get("bep20"), dict) else {}
    binance = methods.get("binance") if isinstance(methods.get("binance"), dict) else {}
    return {
        "upi": {
            "name": _compact_text(upi.get("name"), 120),
            "upi_id": _compact_text(upi.get("upi_id"), 160),
        },
        "bep20": {
            "address": _compact_text(bep20.get("address"), 220),
        },
        "binance": {
            "name": _compact_text(binance.get("name"), 120),
            "binance_id": _compact_text(binance.get("binance_id"), 160),
        },
    }


def stock_manager_method_has_details(method: str | None, methods: Any) -> bool:
    method = normalize_stock_manager_payment_method(method)
    methods = clean_stock_manager_payment_methods(methods)
    if method == "upi":
        return bool(methods["upi"].get("name") and methods["upi"].get("upi_id"))
    if method == "bep20":
        return bool(methods["bep20"].get("address"))
    if method == "binance":
        return bool(methods["binance"].get("name") and methods["binance"].get("binance_id"))
    return False


def format_stock_manager_payment_details(account_or_summary: dict | None) -> str:
    account_or_summary = account_or_summary or {}
    method = normalize_stock_manager_payment_method(account_or_summary.get("payment_method"))
    methods = clean_stock_manager_payment_methods(account_or_summary.get("payment_methods"))
    label = STOCK_MANAGER_PAYMENT_METHOD_LABELS.get(method, method.upper())
    if method == "upi" and stock_manager_method_has_details(method, methods):
        return f"Method: {label}\nName: {methods['upi']['name']}\nUPI ID: {methods['upi']['upi_id']}"
    if method == "bep20" and stock_manager_method_has_details(method, methods):
        return f"Method: {label}\nAddress: {methods['bep20']['address']}"
    if method == "binance" and stock_manager_method_has_details(method, methods):
        return f"Method: {label}\nBinance name: {methods['binance']['name']}\nBinance ID: {methods['binance']['binance_id']}"
    legacy = str(account_or_summary.get("payment_details") or "").strip()
    return legacy


def stock_manager_payment_method_label(method: str | None) -> str:
    return STOCK_MANAGER_PAYMENT_METHOD_LABELS.get(normalize_stock_manager_payment_method(method), "UPI")


def clean_assigned_product_names(product_names: Any) -> list[str]:
    if not isinstance(product_names, list):
        product_names = []
    cleaned: list[str] = []
    seen: set[str] = set()
    for name in product_names:
        clean = str(name or "").strip()
        key = product_name_key(clean)
        if clean and key not in seen:
            cleaned.append(clean)
            seen.add(key)
    return cleaned


# Telegram admin panel was removed. WebAdmin is the only admin panel.
ADMIN_IDS: list[int] = []

SECRET_SETTINGS_KEY = "secret_settings"
RUNTIME_BOT_TOKEN_KEY = "telegram_bot_token"

SECRET_DEFAULTS: dict[str, Any] = {
    "bot_token": "",
    "support_usernames": "",
    "admin_ids": "",
    "bscscan_api_key": "",
    "polygonscan_api_key": "",
    "etherscan_api_key": "",
    "bsc_rpc_url": "https://bsc-rpc.publicnode.com",
    "bsc_rpc_urls": "https://bsc-rpc.publicnode.com,https://bsc.drpc.org,https://rpc.ankr.com/bsc",
    "polygon_rpc_url": "https://polygon-rpc.com",
    "polygon_rpc_urls": "https://polygon-rpc.com,https://polygon-bor-rpc.publicnode.com,https://rpc.ankr.com/polygon",
    "usdt_lookback_seconds": "3600",
    "bsc_rpc_block_chunk_size": "450",
    "polygon_rpc_block_chunk_size": "500",
    "binance_api_key": "",
    "binance_api_secret": "",
    "binance_api_base_url": "https://api.binance.com",
    "binance_recv_window_ms": "5000",
    "binance_pay_history_lookback_seconds": "3600",
    "payment_timeout_minutes": "30",
    "payment_reminder_minutes": "20",
    "usdt_verify_interval_seconds": "30",
    "bep20_required_confirmations": "3",
    "polygon_required_confirmations": "20",
    "usdt_manual_verify_delay_minutes": "5",
    "low_stock_alert_threshold": "10",
    "restock_back_in_stock_cooldown_minutes": "30",
    "restock_notification_cooldown_minutes": "60",
    "restock_long_notification_cooldown_minutes": "360",
    "restock_big_addition_threshold": "20",
    "restock_high_stock_threshold": "20",  # legacy name; kept for older saved settings
    "admin_panel_username": "",
    "admin_panel_password_hash": "",
    "admin_panel_secret_key": "",
    "admin_accounts": [],
}


def normalize_admin_role(role: str | None) -> str:
    role = str(role or "").strip().lower()
    return role if role in {ADMIN_ROLE_OWNER, *ADMIN_SUB_ROLES} else ADMIN_ROLE_OWNER


def clean_admin_accounts(accounts: Any) -> list[dict]:
    cleaned: list[dict] = []
    seen_usernames: set[str] = set()
    if not isinstance(accounts, list):
        return cleaned
    for account in accounts:
        if not isinstance(account, dict):
            continue
        username = str(account.get("username") or "").strip()
        username_key = username.lower()
        password_hash = str(account.get("password_hash") or "").strip()
        role = normalize_admin_role(account.get("role"))
        if role == ADMIN_ROLE_OWNER:
            # Owner uses the main Admin username/password fields only.
            continue
        if not username or not password_hash or username_key in seen_usernames:
            continue
        account_id = str(account.get("id") or secrets.token_urlsafe(8)).strip()
        cleaned.append({
            "id": account_id,
            "username": username,
            "password_hash": password_hash,
            "role": role,
            "enabled": bool(account.get("enabled", True)),
            "created_at": str(account.get("created_at") or utcnow().isoformat()),
            "payment_method": normalize_stock_manager_payment_method(account.get("payment_method")),
            "payment_methods": clean_stock_manager_payment_methods(account.get("payment_methods")),
            "payment_details": str(account.get("payment_details") or "").strip(),  # legacy fallback
            "payment_details_updated_at": str(account.get("payment_details_updated_at") or "").strip(),
            "assigned_products": clean_assigned_product_names(account.get("assigned_products")) if role == ADMIN_ROLE_STOCK_MANAGER else [],
        })
        seen_usernames.add(username_key)
    return cleaned


def get_admin_accounts(settings_or_db=None) -> list[dict]:
    if settings_or_db is None:
        settings = get_secret_settings()
    elif hasattr(settings_or_db, "settings"):
        settings = get_secret_settings(settings_or_db)
    elif isinstance(settings_or_db, dict):
        settings = settings_or_db
    else:
        settings = {}
    return clean_admin_accounts(settings.get("admin_accounts") if isinstance(settings, dict) else [])


def current_admin_role() -> str:
    return normalize_admin_role(session.get("admin_role") or ADMIN_ROLE_OWNER)


def current_admin_username() -> str:
    return str(session.get("admin_username") or "").strip()


def is_owner_role() -> bool:
    return current_admin_role() == ADMIN_ROLE_OWNER


def is_stock_manager_role() -> bool:
    return current_admin_role() == ADMIN_ROLE_STOCK_MANAGER


def get_stock_manager_account(db, username: str | None) -> dict | None:
    username_key = normalize_admin_username(username)
    if not username_key:
        return None
    for account in get_admin_accounts(db):
        if account.get("role") == ADMIN_ROLE_STOCK_MANAGER and normalize_admin_username(account.get("username")) == username_key:
            return account
    return None


def migrate_stock_manager_username_references(db, old_username: str, new_username: str) -> None:
    """Move stock-manager ownership/history rows when the owner renames an account.

    Stock-manager stats are keyed by username. Renaming the login account without
    moving historical keys would make the manager look empty, so keep the old
    stock, payout, and replacement history attached to the new username.
    """
    old_clean = str(old_username or "").strip()
    new_clean = str(new_username or "").strip()
    old_key = normalize_admin_username(old_clean)
    new_key = normalize_admin_username(new_clean)
    if not old_key or not new_key or old_key == new_key:
        return
    try:
        db.stock_manager_stock_events.update_many(
            {"username_key": old_key},
            {"$set": {"username": new_clean, "username_key": new_key}},
        )
        db.stock_manager_payment_requests.update_many(
            {"username_key": old_key},
            {"$set": {"username": new_clean, "username_key": new_key}},
        )
        db.stock_manager_payouts.update_many(
            {"username_key": old_key},
            {"$set": {"username": new_clean, "username_key": new_key}},
        )
        db.stock_manager_replacement_obligations.update_many(
            {"stock_added_by_username_key": old_key},
            {"$set": {"stock_added_by_username": new_clean, "stock_added_by_username_key": new_key}},
        )
        db.stock_manager_replacement_obligations.update_many(
            {"username_key": old_key},
            {"$set": {"username": new_clean, "username_key": new_key}},
        )
        db.stock_manager_replacement_obligations.update_many(
            {"fulfilled_by": old_clean},
            {"$set": {"fulfilled_by": new_clean}},
        )
        db.stock_upload_rejections.update_many(
            {"username_key": old_key},
            {"$set": {"username": new_clean, "username_key": new_key}},
        )
        db.stock_item_ledger.update_many(
            {"first_added_by_username": old_clean},
            {"$set": {"first_added_by_username": new_clean}},
        )
        db.stock_item_ledger.update_many(
            {"last_added_by_username": old_clean},
            {"$set": {"last_added_by_username": new_clean}},
        )
        db.replacement_reports.update_many(
            {"stock_added_by_username": old_clean},
            {"$set": {"stock_added_by_username": new_clean}},
        )
        db.replacement_reports.update_many(
            {"items.stock_added_by_username": old_clean},
            {"$set": {"items.$[item].stock_added_by_username": new_clean}},
            array_filters=[{"item.stock_added_by_username": old_clean}],
        )
        db.products.update_many(
            {"stock_added_by.added_by_username": old_clean},
            {"$set": {"stock_added_by.$[record].added_by_username": new_clean}},
            array_filters=[{"record.added_by_username": old_clean}],
        )
    except Exception as exc:
        current_app.logger.warning("Could not fully migrate stock-manager username %s -> %s: %s", old_clean, new_clean, exc)


def get_stock_manager_assigned_product_names(db, username: str | None) -> list[str]:
    account = get_stock_manager_account(db, username)
    if not account:
        return []
    return clean_assigned_product_names(account.get("assigned_products"))


def get_stock_manager_assigned_product_keys(db, username: str | None) -> set[str]:
    return {product_name_key(name) for name in get_stock_manager_assigned_product_names(db, username)}


def stock_manager_has_assigned_product(db, product_name: str | None, username: str | None) -> bool:
    product_key = product_name_key(product_name)
    if not product_key:
        return False
    return product_key in get_stock_manager_assigned_product_keys(db, username)


def require_stock_manager_product_access(db, product_name: str | None) -> bool:
    if not is_stock_manager_role():
        return True
    return stock_manager_has_assigned_product(db, product_name, current_admin_username())


def replace_assigned_product_name_references(db, old_name: str, new_name: str) -> int:
    settings = get_secret_settings(db)
    accounts = get_admin_accounts(settings)
    old_key = product_name_key(old_name)
    changed = 0
    for account in accounts:
        if account.get("role") != ADMIN_ROLE_STOCK_MANAGER:
            continue
        assigned = []
        seen: set[str] = set()
        account_changed = False
        for product_name in clean_assigned_product_names(account.get("assigned_products")):
            updated_name = new_name if product_name_key(product_name) == old_key else product_name
            if updated_name != product_name:
                account_changed = True
            updated_key = product_name_key(updated_name)
            if updated_name and updated_key not in seen:
                assigned.append(updated_name)
                seen.add(updated_key)
        if account_changed:
            account["assigned_products"] = assigned
            changed += 1
    if changed:
        settings["admin_accounts"] = accounts
        set_secret_settings(db, settings)
    return changed


def role_home_endpoint(role: str | None = None) -> str:
    return ROLE_HOME_ENDPOINTS.get(normalize_admin_role(role or current_admin_role()), "dashboard")


def can_access_endpoint(endpoint: str | None, role: str | None = None) -> bool:
    endpoint = str(endpoint or "")
    role = normalize_admin_role(role or current_admin_role())
    if endpoint in COMMON_ADMIN_ENDPOINTS:
        return True
    if role == ADMIN_ROLE_OWNER:
        return True
    return endpoint in ROLE_ENDPOINTS.get(role, set())


def show_order_user_identity() -> bool:
    return current_admin_role() != ADMIN_ROLE_ORDERS_MANAGER


def show_payment_user_identity() -> bool:
    return current_admin_role() != ADMIN_ROLE_PAYMENT_MANAGER


def current_telegram_username(db, user_id: int | str | None, fallback: str | None = None) -> str:
    try:
        uid = int(user_id or 0)
    except (TypeError, ValueError):
        uid = 0
    username = str(fallback or "").strip().lstrip("@")
    if not uid:
        return username
    try:
        user = db.users.find_one({"user_id": uid}, {"username": 1}) or {}
        current = str(user.get("username") or "").strip().lstrip("@")
        if current:
            return current
    except Exception:
        pass
    return username


def telegram_user_display(db, user_id: int | str | None, fallback_username: str | None = None) -> str:
    try:
        uid_text = str(int(user_id or 0))
    except (TypeError, ValueError):
        uid_text = str(user_id or "").strip() or "Unknown ID"
    username = current_telegram_username(db, user_id, fallback_username)
    if username:
        return f"@{username} ({uid_text})"
    return f"No username ({uid_text})"


def owner_required(func):
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        if not is_owner_role():
            abort(403)
        return func(*args, **kwargs)
    return wrapper


def clean_secret_settings(settings: dict | None) -> dict:
    settings = settings or {}
    cleaned = dict(SECRET_DEFAULTS)
    if isinstance(settings, dict):
        for key in SECRET_DEFAULTS:
            if key not in settings or settings.get(key) is None:
                continue
            if key == "admin_accounts":
                cleaned[key] = clean_admin_accounts(settings.get(key))
            else:
                cleaned[key] = str(settings.get(key) or "").strip()
    cleaned["admin_accounts"] = clean_admin_accounts(cleaned.get("admin_accounts"))
    return cleaned


def get_runtime_bot_token_record(db=None) -> dict:
    if db is None:
        db = current_app.db  # type: ignore[attr-defined]
    doc = db.runtime_config.find_one({"key": RUNTIME_BOT_TOKEN_KEY}) or {}
    return doc if isinstance(doc, dict) else {}


def get_runtime_bot_token(db=None) -> str:
    doc = get_runtime_bot_token_record(db)
    return str(doc.get("value") or "").strip()


def get_secret_settings(db=None) -> dict:
    if db is None:
        db = current_app.db  # type: ignore[attr-defined]
    doc = db.settings.find_one({"key": SECRET_SETTINGS_KEY}) or {}
    value = doc.get("value") if isinstance(doc, dict) else {}
    cleaned = clean_secret_settings(value if isinstance(value, dict) else None)
    runtime_token_doc = get_runtime_bot_token_record(db)
    runtime_token = str(runtime_token_doc.get("value") or "").strip()
    if runtime_token:
        # The dedicated runtime token record is authoritative. This prevents an
        # older token left in the broad settings document from being used.
        cleaned["bot_token"] = runtime_token
        cleaned["bot_token_runtime_updated_at"] = runtime_token_doc.get("updated_at")
    cleaned["secret_settings_updated_at"] = doc.get("updated_at") if isinstance(doc, dict) else None
    return cleaned


def set_secret_settings(db, settings: dict) -> dict:
    cleaned = clean_secret_settings(settings)
    now = utcnow()
    db.settings.update_one(
        {"key": SECRET_SETTINGS_KEY},
        {"$set": {"key": SECRET_SETTINGS_KEY, "value": cleaned, "updated_at": now}},
        upsert=True,
    )
    token = str(cleaned.get("bot_token") or "").strip()
    if token:
        db.runtime_config.update_one(
            {"key": RUNTIME_BOT_TOKEN_KEY},
            {"$set": {"key": RUNTIME_BOT_TOKEN_KEY, "value": token, "updated_at": now}},
            upsert=True,
        )
    return cleaned


def get_bot_token(db=None) -> str:
    runtime_token = get_runtime_bot_token(db)
    if runtime_token:
        return runtime_token
    return str(get_secret_settings(db).get("bot_token") or "").strip()


def get_support_usernames(db=None) -> list[str]:
    return _split_csv(str(get_secret_settings(db).get("support_usernames") or ""))


def _parse_admin_ids(value: str) -> list[int]:
    ids: list[int] = []
    for part in _split_csv(value):
        try:
            ids.append(int(part))
        except (TypeError, ValueError):
            continue
    return ids


def get_admin_ids(db=None) -> list[int]:
    settings = get_secret_settings(db)
    raw = str(settings.get("admin_ids") or os.getenv("ADMIN_IDS", "") or "")
    return _parse_admin_ids(raw)


def is_admin_user_id(db, user_id: int | str | None) -> bool:
    try:
        return int(user_id or 0) in set(get_admin_ids(db))
    except (TypeError, ValueError):
        return False


def get_admin_recipient_users(db) -> list[dict]:
    admin_ids = set(get_admin_ids(db))
    if not admin_ids:
        return []
    recipients: list[dict] = []
    seen: set[int] = set()
    for user in db.users.find({"user_id": {"$in": list(admin_ids)}}, {"user_id": 1, "language": 1, "language_code": 1}):
        try:
            uid = int(user.get("user_id", 0) or 0)
        except Exception:
            uid = 0
        if uid and uid not in seen:
            recipients.append(user)
            seen.add(uid)
    for uid in sorted(admin_ids - seen):
        recipients.append({"user_id": uid, "language": "en"})
    return recipients


def get_runtime_int(db, key: str, default: int) -> int:
    try:
        return int(str(get_secret_settings(db).get(key) or "").strip() or default)
    except ValueError:
        return default


def get_admin_login_config(db) -> tuple[str, str, bool]:
    settings = get_secret_settings(db)
    saved_user = str(settings.get("admin_panel_username") or "").strip()
    saved_hash = str(settings.get("admin_panel_password_hash") or "").strip()
    if saved_user and saved_hash:
        return saved_user, saved_hash, True
    return os.getenv("ADMIN_PANEL_USERNAME", "admin"), os.getenv("ADMIN_PANEL_PASSWORD", ""), False


def admin_role_label_for_username(db, username: Any, fallback_role: str | None = None) -> str:
    """Return a display label for an admin actor without exposing the login name."""
    role = str(fallback_role or "").strip().lower()
    if role in ADMIN_ROLE_LABELS:
        return ADMIN_ROLE_LABELS[role]
    username_key = normalize_admin_username(username)
    if not username_key:
        return ADMIN_ROLE_LABELS[ADMIN_ROLE_OWNER]
    settings = get_secret_settings(db)
    main_username = str(settings.get("admin_panel_username") or os.getenv("ADMIN_PANEL_USERNAME", "admin") or "admin").strip()
    if normalize_admin_username(main_username) == username_key:
        return ADMIN_ROLE_LABELS[ADMIN_ROLE_OWNER]
    for account in get_admin_accounts(settings):
        if normalize_admin_username(account.get("username")) == username_key:
            account_role = normalize_admin_role(account.get("role"))
            return ADMIN_ROLE_LABELS.get(account_role, account_role.replace("_", " ").title())
    # Older payout records only saved the owner username. Keep the UI role-based.
    return ADMIN_ROLE_LABELS[ADMIN_ROLE_OWNER]


def stock_manager_payout_paid_by_label(db, record: dict | None) -> str:
    record = record or {}
    status = str(record.get("status") or "").strip().lower()
    if status and status != "paid":
        return "—"
    explicit_label = str(record.get("paid_by_role_label") or "").strip()
    if explicit_label:
        return explicit_label
    explicit_role = str(record.get("paid_by_role") or "").strip().lower()
    if explicit_role in ADMIN_ROLE_LABELS:
        return ADMIN_ROLE_LABELS[explicit_role]
    return admin_role_label_for_username(db, record.get("paid_by"))


def create_app() -> Flask:
    app = Flask(__name__)
    app.db = _connect_db()  # type: ignore[attr-defined]
    if ADMIN_AUTO_INDEXES:
        try:
            ensure_admin_indexes(app.db)
        except Exception as exc:
            app.logger.warning("Could not ensure MongoDB indexes: %s", exc)
    secret_settings = get_secret_settings(app.db)
    app.config["SECRET_KEY"] = secret_settings.get("admin_panel_secret_key") or os.getenv("ADMIN_PANEL_SECRET_KEY") or os.getenv("PANEL_SECRET_KEY") or secrets.token_hex(32)
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

    # Secret Settings uses these helpers both as normal template functions
    # and as filters. Registering them here keeps the page renderable even
    # when templates use the filter form, e.g. {{ value|fmt_dt }}.
    app.jinja_env.filters["fmt_dt"] = fmt_dt
    app.jinja_env.filters["mask_bot_token"] = mask_bot_token
    if os.getenv("ADMIN_PANEL_COOKIE_SECURE", "0").lower() in {"1", "true", "yes"}:
        app.config["SESSION_COOKIE_SECURE"] = True

    app._last_expiry_cleanup = 0  # type: ignore[attr-defined]
    app._login_attempts = {}  # type: ignore[attr-defined]

    @app.before_request
    def clear_cached_counts_before_mutations():
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            clear_admin_cache()
        return None

    @app.before_request
    def enforce_webadmin_role_access():
        endpoint = request.endpoint or ""
        if endpoint in {"static", "login_form", "login_submit"}:
            return None
        if not _is_logged_in():
            return None
        if endpoint == "dashboard" and not can_access_endpoint(endpoint):
            return redirect(url_for(role_home_endpoint()))
        if not can_access_endpoint(endpoint):
            abort(403)
        return None

    @app.before_request
    def expire_stale_unpaid_request_cleanup():
        if not session.get("admin_logged_in"):
            return
        now = time.time()
        if now - float(getattr(app, "_last_expiry_cleanup", 0) or 0) < 30:
            return
        try:
            result = expire_stale_unpaid_payments_and_orders(app.db)
            if any(result.values()):
                app.logger.info("Expired stale unpaid orders/payments from WebAdmin: %s", result)
        except Exception as exc:
            app.logger.warning("Could not expire stale unpaid orders/payments: %s", exc)
        finally:
            app._last_expiry_cleanup = now  # type: ignore[attr-defined]

    @app.context_processor
    def inject_globals():
        sidebar_state = {
            "pending_review_count": 0,
            "pending_stock_order_count": 0,
            "pending_payout_request_count": 0,
            "pending_refund_request_count": 0,
            "pending_replacement_report_count": 0,
            "product_stock_alert": {"count": 0, "low_stock": 0, "out_of_stock": 0, "severity": ""},
            "stock_upload_rejection_count": 0,
            "active_payment_currencies": set(),
            "admin_ids": [],
            "bot_token_configured": False,
        }

        def build_sidebar_state() -> dict[str, Any]:
            return {
                "pending_review_count": count_pending_payment_reviews(app.db),
                "pending_stock_order_count": app.db.orders.count_documents({"status": "pending_stock"}),
                "pending_payout_request_count": count_pending_stock_manager_payout_requests(app.db),
                "pending_refund_request_count": count_pending_refund_requests(app.db),
                "pending_replacement_report_count": count_pending_replacement_reports(app.db),
                "product_stock_alert": get_product_stock_alert_summary(app.db),
                "stock_upload_rejection_count": count_stock_upload_rejections(app.db),
                "active_payment_currencies": get_active_payment_currencies(app.db),
                "admin_ids": get_admin_ids(app.db),
                "bot_token_configured": bool(get_bot_token(app.db)),
            }

        try:
            sidebar_state = cached_value("sidebar_state", ADMIN_CACHE_TTL_SECONDS, build_sidebar_state)
        except Exception:
            # Keep login/error pages renderable even if the database is temporarily unavailable.
            pass
        user_display_cache: dict[tuple[str, str], str] = {}

        def telegram_user_label(user_id, fallback_username=None):
            cache_key = (str(user_id or ""), str(fallback_username or ""))
            if cache_key not in user_display_cache:
                user_display_cache[cache_key] = telegram_user_display(app.db, user_id, fallback_username)
            return user_display_cache[cache_key]

        return {
            "csrf_token": _csrf_token,
            "fmt_dt": fmt_dt,
            "money_inr": money_inr,
            "money_usdt": money_usdt,
            "money_usdt_exact": money_usdt_exact,
            "money_usdt_price": money_usdt_price,
            "money_inr_price": money_inr_price,
            "status_label": status_label,
            "status_badge_class": status_badge_class,
            "payment_status_label": payment_status_label,
            "payment_status_badge_class": payment_status_badge_class,
            "tx_hash_log_tx_hash": tx_hash_log_tx_hash,
            "tx_hash_external_url": tx_hash_external_url,
            "tx_hash_log_network_label": tx_hash_log_network_label,
            "tx_hash_log_expected_amount": tx_hash_log_expected_amount,
            "tx_hash_log_received_amount": tx_hash_log_received_amount,
            "tx_hash_log_diff_amount": tx_hash_log_diff_amount,
            "tx_hash_log_result_label": tx_hash_log_result_label,
            "payment_auto_check_failed": payment_auto_check_failed,
            "payment_auto_check_reason_display": payment_auto_check_reason_display,
            "user_status_badge_class": user_status_badge_class,
            "method_label": method_label,
            "order_amount_text": order_amount_text,
            "order_refund_amount_text": order_refund_amount_text,
            "admin_ids": sidebar_state.get("admin_ids", []),
            "bot_token_configured": bool(sidebar_state.get("bot_token_configured")),
            "current_admin_role": current_admin_role(),
            "current_admin_username": current_admin_username(),
            "is_stock_manager_role": is_stock_manager_role(),
            "admin_role_labels": ADMIN_ROLE_LABELS,
            "admin_role_descriptions": ADMIN_ROLE_DESCRIPTIONS,
            "can_access": can_access_endpoint,
            "show_order_user_identity": show_order_user_identity(),
            "show_payment_user_identity": show_payment_user_identity(),
            "stock_manager_min_payout_usdt": STOCK_MANAGER_MIN_PAYOUT_USDT,
            "stock_manager_payment_method_labels": STOCK_MANAGER_PAYMENT_METHOD_LABELS,
            "stock_manager_payment_method_label": stock_manager_payment_method_label,
            "format_stock_manager_payment_details": format_stock_manager_payment_details,
            "price_group_label": price_group_label,
            "replacement_status_badge_class": replacement_status_badge_class,
            "replacement_status_label": replacement_status_label,
            "replacement_status_key": replacement_status_key,
            "replacement_report_items": replacement_report_items,
            "replacement_products_label": replacement_products_label,
            "telegram_user_label": telegram_user_label,
            "activity_action_label": activity_action_label,
            "activity_actor_label": activity_actor_label,
            "activity_source_label": activity_source_label,
            "replacement_orders_label": replacement_orders_label,
            "pending_payment_review_count": int(sidebar_state.get("pending_review_count") or 0),
            "pending_stock_order_count": int(sidebar_state.get("pending_stock_order_count") or 0),
            "pending_payout_request_count": int(sidebar_state.get("pending_payout_request_count") or 0),
            "pending_refund_request_count": int(sidebar_state.get("pending_refund_request_count") or 0),
            "pending_replacement_report_count": int(sidebar_state.get("pending_replacement_report_count") or 0),
            "product_stock_alert": sidebar_state.get("product_stock_alert") or {"count": 0, "low_stock": 0, "out_of_stock": 0, "severity": ""},
            "stock_upload_rejection_count": int(sidebar_state.get("stock_upload_rejection_count") or 0),
            "active_payment_currencies": sidebar_state.get("active_payment_currencies") or set(),
            "live_state_refresh_ms": ADMIN_LIVE_STATE_REFRESH_MS,
            "live_full_refresh_enabled": ADMIN_LIVE_FULL_REFRESH,
        }

    register_routes(app)
    return app


def _connect_db():
    mongo_uri = os.getenv("MONGO_URI", "").strip()
    if not mongo_uri:
        raise RuntimeError("Missing MONGO_URI in .env")
    db_name = os.getenv("DB_NAME", "shopbot").strip() or "shopbot"
    client = MongoClient(
        mongo_uri,
        tls=True,
        tlsAllowInvalidCertificates=True,
        serverSelectionTimeoutMS=_env_int("MONGO_SERVER_SELECTION_TIMEOUT_MS", 10000, minimum=3000, maximum=60000),
        connectTimeoutMS=_env_int("MONGO_CONNECT_TIMEOUT_MS", 10000, minimum=3000, maximum=60000),
        socketTimeoutMS=_env_int("MONGO_SOCKET_TIMEOUT_MS", 20000, minimum=5000, maximum=120000),
        maxPoolSize=_env_int("MONGO_MAX_POOL_SIZE", 10, minimum=1, maximum=100),
    )
    return client[db_name]


def ensure_admin_indexes(db) -> None:
    """Create the indexes used by the WebAdmin pages.

    Keep this function idempotent so it can be run manually through
    ``python init_indexes.py`` or automatically with ADMIN_AUTO_INDEXES=1.
    """
    db.settings.create_index([("key", 1)], unique=True)
    db.runtime_config.create_index([("key", 1)], unique=True)

    db.users.create_index([("user_id", 1)])
    db.users.create_index([("username", 1)])
    db.users.create_index([("joined_at", -1)])
    db.users.create_index([("blocked", 1), ("joined_at", -1)])
    db.user_product_prices.create_index([("user_id", 1), ("product_key", 1)], unique=True)
    db.user_product_prices.create_index([("product_key", 1)])

    db.products.create_index([("name", 1)])
    db.products.create_index([("enabled", 1), ("shop_order", 1), ("created_at", -1)])

    db.orders.create_index([("order_id", 1)])
    db.orders.create_index([("status", 1), ("created_at", -1)])
    db.orders.create_index([("is_replacement", 1), ("created_at", -1)])
    db.orders.create_index([("user_id", 1), ("created_at", -1)])
    db.orders.create_index([("product_name", 1), ("status", 1), ("created_at", 1)])
    db.orders.create_index([("delivered_at", -1)])

    db.pending_payments.create_index([("ref_id", 1)])
    db.pending_payments.create_index([("status", 1), ("created_at", -1)])
    db.pending_payments.create_index([("user_id", 1), ("created_at", -1)])
    db.pending_payments.create_index([("reviewed_at", -1)])
    db.pending_payments.create_index([("confirmed_at", -1)])
    db.pending_payments.create_index([("usdt_txn_hash_key", 1)])
    db.pending_payments.create_index([("method", 1), ("created_at", -1)])
    db.pending_payments.create_index([("usdt_manual_auto_check_result", 1), ("created_at", -1)])

    db.admin_activity.create_index([("created_at", -1)])
    db.replacement_reports.create_index([("status", 1), ("created_at", -1)])
    db.replacement_reports.create_index([("items.item_hash", 1), ("status", 1)])
    db.stock_manager_payment_requests.create_index([("status", 1), ("requested_at", -1)])
    db.stock_manager_stock_events.create_index([("username_key", 1), ("created_at", -1)])
    db.stock_item_ledger.create_index([("product_key", 1), ("item_hash", 1)], unique=True)
    db.stock_item_ledger.create_index([("item_search_text", 1)])
    db.stock_item_ledger.create_index([("current_status", 1), ("last_movement_at", -1)])
    db.stock_item_ledger.create_index([("first_added_by_username", 1), ("first_added_at", -1)])
    db.stock_upload_rejections.create_index([("product_name", 1), ("created_at", -1)])
    db.stock_upload_rejections.create_index([("username_key", 1), ("created_at", -1)])
    db.stock_manager_replacement_obligations.create_index([("username_key", 1), ("status", 1)])
    db.stock_manager_replacement_obligations.create_index([("stock_added_by_username_key", 1), ("product_name", 1), ("fulfilled_at", 1)])
    db.stock_manager_replacement_obligations.create_index([("source_order_id", 1), ("item_hash", 1)])


def _login_rate_key(username: str) -> str:
    ip = request.headers.get("X-Forwarded-For", request.remote_addr or "").split(",")[0].strip()
    user = str(username or "").strip().lower() or "<blank>"
    return f"{ip}|{user}"


def _login_blocked_seconds(app: Flask, username: str) -> int:
    attempts = getattr(app, "_login_attempts", {})
    record = attempts.get(_login_rate_key(username)) or {}
    locked_until = float(record.get("locked_until") or 0)
    remaining = int(locked_until - time.time())
    return max(0, remaining)


def _record_failed_login(app: Flask, username: str) -> None:
    attempts = getattr(app, "_login_attempts", None)
    if attempts is None:
        attempts = {}
        app._login_attempts = attempts  # type: ignore[attr-defined]
    key = _login_rate_key(username)
    now = time.time()
    record = attempts.get(key) or {"count": 0, "first_at": now, "locked_until": 0}
    if now - float(record.get("first_at") or now) > LOGIN_ATTEMPT_WINDOW_SECONDS:
        record = {"count": 0, "first_at": now, "locked_until": 0}
    record["count"] = int(record.get("count") or 0) + 1
    if record["count"] >= LOGIN_ATTEMPT_LIMIT:
        record["locked_until"] = now + LOGIN_LOCK_SECONDS
    attempts[key] = record


def _clear_login_attempts(app: Flask, username: str) -> None:
    attempts = getattr(app, "_login_attempts", None)
    if isinstance(attempts, dict):
        attempts.pop(_login_rate_key(username), None)


def _normalize_usdt_tx_hash(txn_hash: Any) -> str:
    raw = str(txn_hash or "").strip()
    match = re.search(r"0x[a-fA-F0-9]{64}", raw)
    return match.group(0).lower() if match else raw.lower()


def _normalize_usdt_network_key(network: Any = None) -> str:
    value = str(network or "").strip().lower()
    return "polygon" if value in {"polygon", "matic", "polygon_pos", "usdt_polygon", "polygon_usdt"} else "bep20"


def _make_usdt_tx_hash_key(network: Any, txn_hash: Any) -> str:
    normalized_hash = _normalize_usdt_tx_hash(txn_hash)
    if not normalized_hash:
        return ""
    return f"{_normalize_usdt_network_key(network)}:{normalized_hash}"


def _usdt_tx_hash_exact_query(txn_hash: Any) -> dict:
    normalized = _normalize_usdt_tx_hash(txn_hash)
    if not normalized:
        return {"$expr": {"$eq": [1, 0]}}
    escaped = re.escape(normalized)
    return {
        "$or": [
            {"usdt_transaction_hash": {"$regex": f"^{escaped}$", "$options": "i"}},
            {"usdt_txn_hash": {"$regex": f"^{escaped}$", "$options": "i"}},
            {"usdt_txn_hash_key": {"$regex": f":{escaped}$", "$options": "i"}},
        ]
    }


def _find_used_usdt_tx_hash(db, txn_hash: Any, *, exclude_ref_id: str | None = None) -> dict | None:
    normalized = _normalize_usdt_tx_hash(txn_hash)
    if not normalized:
        return None
    query = _usdt_tx_hash_exact_query(normalized)
    if exclude_ref_id:
        query = {"$and": [query, {"ref_id": {"$ne": exclude_ref_id}}]}
    return db.pending_payments.find_one(query)




def tx_hash_log_tx_hash(payment: dict) -> str:
    """Return the saved USDT transaction hash for the Tx Hash Logs page."""
    for key in ("usdt_transaction_hash", "usdt_txn_hash"):
        value = str((payment or {}).get(key) or "").strip()
        if value:
            return value
    key_value = str((payment or {}).get("usdt_txn_hash_key") or "").strip()
    if ":" in key_value:
        maybe_hash = key_value.rsplit(":", 1)[-1].strip()
        if maybe_hash:
            return maybe_hash
    return ""


def tx_hash_log_network_key(payment: dict) -> str:
    network = str((payment or {}).get("usdt_network") or "").strip().lower()
    key_value = str((payment or {}).get("usdt_txn_hash_key") or "").strip().lower()
    method = str((payment or {}).get("method") or "").strip().lower()
    if network == "polygon" or key_value.startswith("polygon:") or method in {"polygon", "usdt_polygon"}:
        return "polygon"
    return "bep20"


def tx_hash_log_network_label(payment: dict) -> str:
    return "USDT (POLYGON)" if tx_hash_log_network_key(payment) == "polygon" else "USDT (BEP20)"


def tx_hash_external_url(payment: dict) -> str:
    tx_hash = tx_hash_log_tx_hash(payment)
    if not tx_hash:
        return ""
    if tx_hash_log_network_key(payment) == "polygon":
        return f"https://polygonscan.com/tx/{tx_hash}"
    return f"https://bscscan.com/tx/{tx_hash}"


def tx_hash_log_usdt(value: Any) -> str:
    """Format USDT amounts with 3 decimals only on the Tx Hash Logs page."""
    try:
        return f"${float(value or 0):.3f} USDT"
    except (TypeError, ValueError):
        return "$0.000 USDT"


def tx_hash_log_expected_amount(payment: dict) -> str:
    return tx_hash_log_usdt((payment or {}).get("unique_usdt") or (payment or {}).get("expected_usdt") or (payment or {}).get("load_amount") or 0)


def tx_hash_log_received_amount(payment: dict) -> str:
    value = (payment or {}).get("usdt_transaction_amount") or (payment or {}).get("usdt_manual_auto_check_received_usdt")
    if not str(value or "").strip():
        value = extract_usdt_received_amount_from_error((payment or {}).get("usdt_manual_auto_check_reason"))
    return tx_hash_log_usdt(value) if str(value or "").strip() else "—"


def tx_hash_log_diff_amount(value: Any) -> str:
    """Format Tx Hash Logs difference values with 3 decimals."""
    try:
        raw = str(value or "").strip()
        if not raw:
            return ""
        return f"{float(raw):.3f} USDT"
    except (TypeError, ValueError):
        return ""


def tx_hash_log_result_label(payment: dict) -> str:
    status = str((payment or {}).get("status") or "").strip().lower()

    # Once an admin or auto flow has reached a final payment status, show that
    # final status instead of an older manual-hash failure/check label.
    if status in {"confirmed", "approved", "completed", "rejected", "expired"}:
        return payment_status_label(status)

    manual_result = str((payment or {}).get("usdt_manual_auto_check_result") or "").strip().lower()
    if manual_result == "passed":
        return "Manual TxHash auto-approved"
    if manual_result == "duplicate":
        return "Duplicate TxHash blocked"
    if manual_result in {"failed", "error"}:
        return "Manual TxHash needs review"
    if (payment or {}).get("usdt_auto_verified"):
        return "USDT auto-approved"
    if status == "usdt_manual_submitted":
        return "Waiting for admin review"
    return payment_status_label(status)


def payment_auto_check_failed(payment: dict) -> bool:
    return str((payment or {}).get("usdt_manual_auto_check_result") or "").strip().lower() in {"failed", "error", "duplicate"}


def payment_auto_check_reason_display(reason: str | None) -> str:
    """Compact old and new manual TxHash failure reasons for WebAdmin."""
    return public_usdt_error_text(reason or "") if str(reason or "").strip() else ""

def _count_duplicate_field_values(db, collection_name: str, field_name: str, *, non_empty_string_only: bool = True) -> int:
    match: dict[str, Any] = {field_name: {"$exists": True}}
    if non_empty_string_only:
        match[field_name].update({"$type": "string", "$gt": ""})
    rows = list(db[collection_name].aggregate([
        {"$match": match},
        {"$group": {"_id": f"${field_name}", "count": {"$sum": 1}}},
        {"$match": {"count": {"$gt": 1}}},
        {"$count": "duplicate_values"},
    ]))
    return int((rows[0] if rows else {}).get("duplicate_values") or 0)


def build_system_health(db) -> dict[str, Any]:
    checks: list[dict[str, str]] = []

    try:
        db.command("ping")
        checks.append({"name": "MongoDB", "status": "ok", "detail": "Connected"})
    except Exception as exc:
        checks.append({"name": "MongoDB", "status": "error", "detail": f"Connection problem: {exc}"})

    settings = get_secret_settings(db)
    payment_settings = get_payment_settings(db)
    enabled_methods = [
        label for key, label in [
            ("upi", "UPI"),
            ("usdt", "USDT (BEP20)"),
            ("polygon", "USDT (POLYGON)"),
            ("binance", "Binance Pay"),
            ("wallet_inr", "INR Wallet"),
            ("wallet_usdt", "USDT Wallet"),
        ]
        if payment_method_enabled(payment_settings, key)
    ]

    def _int_setting(key: str, default: int) -> int:
        try:
            return int(str(settings.get(key) or SECRET_DEFAULTS.get(key) or default).strip())
        except (TypeError, ValueError):
            return 0

    def _configured_rpc_count(list_key: str, single_key: str) -> int:
        return len(_split_csv(str(settings.get(list_key) or settings.get(single_key) or "")))

    saved_bot_token = get_bot_token(db)
    checks.append({
        "name": "Bot token",
        "status": "ok" if saved_bot_token else "error",
        "detail": f"Configured: {mask_bot_token(saved_bot_token)}. Source: runtime_config/{RUNTIME_BOT_TOKEN_KEY}. Bot service must use this same MONGO_URI and DB_NAME={os.getenv('DB_NAME', 'shopbot').strip() or 'shopbot'}." if saved_bot_token else "Missing — save it in Secret Settings",
    })
    checks.append({
        "name": "WebAdmin secret key",
        "status": "ok" if settings.get("admin_panel_secret_key") or os.getenv("ADMIN_PANEL_SECRET_KEY") or os.getenv("PANEL_SECRET_KEY") else "warning",
        "detail": "Configured" if settings.get("admin_panel_secret_key") or os.getenv("ADMIN_PANEL_SECRET_KEY") or os.getenv("PANEL_SECRET_KEY") else "Not saved; sessions may reset after restart",
    })
    checks.append({
        "name": "Payment methods",
        "status": "ok" if enabled_methods else "warning",
        "detail": ", ".join(enabled_methods) if enabled_methods else "No payment methods enabled",
    })

    usdt_networks = [
        {
            "key": "usdt_bep20",
            "label": "USDT (BEP20)",
            "rpc_list_key": "bsc_rpc_urls",
            "rpc_single_key": "bsc_rpc_url",
            "confirmations_key": "bep20_required_confirmations",
            "scan_key": "bscscan_api_key",
            "scan_label": "BscScan API",
            "default_confirmations": 3,
        },
        {
            "key": "usdt_polygon",
            "label": "USDT (POLYGON)",
            "rpc_list_key": "polygon_rpc_urls",
            "rpc_single_key": "polygon_rpc_url",
            "confirmations_key": "polygon_required_confirmations",
            "scan_key": "polygonscan_api_key",
            "scan_label": "PolygonScan API",
            "default_confirmations": 20,
        },
    ]
    for network in usdt_networks:
        cfg = payment_settings.get(network["key"], {}) if isinstance(payment_settings, dict) else {}
        enabled = bool(cfg.get("enabled"))
        wallet_saved = bool(str(cfg.get("wallet_address") or "").strip())
        if enabled:
            payment_status = "ok" if wallet_saved else "error"
            payment_detail = "Enabled, wallet address saved" if wallet_saved else "Enabled but wallet address missing"
        else:
            payment_status = "ok"
            payment_detail = "Disabled; wallet address saved" if wallet_saved else "Disabled"
        checks.append({"name": f"{network['label']} payment", "status": payment_status, "detail": payment_detail})

        rpc_count = _configured_rpc_count(network["rpc_list_key"], network["rpc_single_key"])
        checks.append({
            "name": f"{network['label']} RPC providers",
            "status": "ok" if rpc_count else ("error" if enabled else "warning"),
            "detail": f"{rpc_count} RPC provider(s) configured" if rpc_count else "No RPC providers configured",
        })

        confirmations = _int_setting(network["confirmations_key"], int(network["default_confirmations"]))
        checks.append({
            "name": f"{network['label']} confirmations",
            "status": "ok" if confirmations >= 1 else ("error" if enabled else "warning"),
            "detail": f"Requires {confirmations} confirmation(s)" if confirmations >= 1 else "Invalid confirmation setting",
        })

        api_key_saved = bool(str(settings.get(network["scan_key"]) or "").strip())
        checks.append({
            "name": f"{network['label']} {network['scan_label']}",
            "status": "ok",
            "detail": "API key saved" if api_key_saved else "Optional API key not saved; RPC verification can still run",
        })

    binance_enabled = bool((payment_settings.get("binance", {}) if isinstance(payment_settings, dict) else {}).get("enabled"))
    if binance_enabled:
        binance_ready = bool(settings.get("binance_api_key") and settings.get("binance_api_secret"))
        checks.append({
            "name": "Binance API",
            "status": "ok" if binance_ready else "warning",
            "detail": "API key and secret saved" if binance_ready else "Binance Pay is enabled but API key/secret are incomplete",
        })

    duplicate_order_ids = _count_duplicate_field_values(db, "orders", "order_id", non_empty_string_only=True)
    duplicate_usdt_hashes = _count_duplicate_field_values(db, "pending_payments", "usdt_transaction_hash", non_empty_string_only=True)
    duplicate_usdt_manual_hashes = _count_duplicate_field_values(db, "pending_payments", "usdt_txn_hash", non_empty_string_only=True)
    duplicate_usdt_key_hashes = _count_duplicate_field_values(db, "pending_payments", "usdt_txn_hash_key", non_empty_string_only=True)
    duplicate_usdt_hashes += duplicate_usdt_manual_hashes + duplicate_usdt_key_hashes
    checks.append({
        "name": "Order ID uniqueness",
        "status": "ok" if duplicate_order_ids == 0 else "warning",
        "detail": "No duplicate order IDs found" if duplicate_order_ids == 0 else f"{duplicate_order_ids} duplicate value(s) found; clean before unique index can be enforced",
    })
    checks.append({
        "name": "USDT transaction reuse",
        "status": "ok" if duplicate_usdt_hashes == 0 else "warning",
        "detail": "No duplicate USDT transaction hashes found" if duplicate_usdt_hashes == 0 else f"{duplicate_usdt_hashes} duplicate transaction hash value(s) found",
    })

    product_alert = get_product_stock_alert_summary(db)
    tx_hash_present = {"$nin": [None, ""]}
    counters = {
        "pending_payment_reviews": count_pending_payment_reviews(db),
        "manual_txhash_reviews": db.pending_payments.count_documents({
            "status": "usdt_manual_submitted",
            "usdt_manual_auto_check_result": {"$in": ["failed", "error", "duplicate"]},
        }),
        "tx_hash_records": db.pending_payments.count_documents({"$or": [
            {"usdt_transaction_hash": tx_hash_present},
            {"usdt_txn_hash": tx_hash_present},
            {"usdt_txn_hash_key": tx_hash_present},
        ]}),
        "pending_stock_orders": db.orders.count_documents({"status": "pending_stock"}),
        "waiting_payments": db.pending_payments.count_documents({"status": "waiting"}),
        "pending_payout_requests": count_pending_stock_manager_payout_requests(db),
        "pending_replacement_reports": count_pending_replacement_reports(db),
        "rejected_stock_uploads": db.stock_upload_rejections.count_documents({}),
        "low_or_out_of_stock_products": int(product_alert.get("count") or 0),
    }
    return {"checks": checks, "counters": counters, "generated_at": utcnow()}


def register_routes(app: Flask) -> None:
    @app.get("/login")
    def login_form():
        if _is_logged_in():
            return redirect(url_for("dashboard"))
        return render_template("login.html")

    @app.post("/login")
    def login_submit():
        username = request.form.get("username", "")
        password = request.form.get("password", "")
        locked_for = _login_blocked_seconds(app, username)
        if locked_for:
            minutes = max(1, math.ceil(locked_for / 60))
            flash(f"Too many failed login attempts. Try again in about {minutes} minute(s).", "error")
            return redirect(url_for("login_form"))

        expected_user, expected_secret, uses_db_password = get_admin_login_config(app.db)
        accounts = get_admin_accounts(app.db)
        if not expected_secret and not accounts:
            flash("Set ADMIN_PANEL_PASSWORD in .env for initial login, then move credentials to Secret Settings.", "error")
            return redirect(url_for("login_form"))

        password_ok = bool(expected_secret) and (check_password_hash(expected_secret, password) if uses_db_password else hmac.compare_digest(password, expected_secret))
        if expected_secret and hmac.compare_digest(username, expected_user) and password_ok:
            _clear_login_attempts(app, username)
            session.clear()
            session["admin_logged_in"] = True
            session["admin_role"] = ADMIN_ROLE_OWNER
            session["admin_username"] = expected_user
            session["csrf"] = secrets.token_urlsafe(32)
            flash("Logged in as Owner.", "success")
            return redirect(url_for(role_home_endpoint(ADMIN_ROLE_OWNER)))

        for account in accounts:
            if not account.get("enabled", True):
                continue
            if not hmac.compare_digest(username.lower(), str(account.get("username") or "").lower()):
                continue
            if check_password_hash(str(account.get("password_hash") or ""), password):
                _clear_login_attempts(app, username)
                role = normalize_admin_role(account.get("role"))
                session.clear()
                session["admin_logged_in"] = True
                session["admin_role"] = role
                session["admin_username"] = account.get("username")
                session["csrf"] = secrets.token_urlsafe(32)
                flash(f"Logged in as {ADMIN_ROLE_LABELS.get(role, role)}.", "success")
                return redirect(url_for(role_home_endpoint(role)))

        _record_failed_login(app, username)
        flash("Invalid username or password.", "error")
        return redirect(url_for("login_form"))

    @app.post("/logout")
    @login_required
    @csrf_required
    def logout():
        session.clear()
        flash("Logged out.", "success")
        return redirect(url_for("login_form"))

    def _attach_order_usernames(rows: list[dict]) -> list[dict]:
        """Fill missing order usernames from the users collection for display/search results."""
        user_ids = []
        for row in rows:
            try:
                user_id = int(row.get("user_id"))
            except (TypeError, ValueError):
                continue
            user_ids.append(user_id)
        if not user_ids:
            return rows
        users = app.db.users.find({"user_id": {"$in": sorted(set(user_ids))}}, {"user_id": 1, "username": 1})
        username_by_id = {int(u.get("user_id")): (u.get("username") or "") for u in users if u.get("user_id") is not None}
        for row in rows:
            try:
                row["username"] = username_by_id.get(int(row.get("user_id")), row.get("username") or "")
            except (TypeError, ValueError):
                row["username"] = row.get("username") or ""
        return rows

    def _attach_order_display_status(rows: list[dict]) -> list[dict]:
        """Add a UI-only display status for orders awaiting manual review.

        The database order stays ``pending`` until admin approval/rejection, but
        showing ``Needs Review`` in order tables makes it clear why it did not
        expire after the payment window.
        """
        rows = _attach_order_usernames(rows)
        refs = [str(row.get("order_id") or "") for row in rows if row.get("status") == "pending" and row.get("order_id")]
        if not refs:
            for row in rows:
                refund_status = str(row.get("refund_status") or "").strip().lower()
                if row.get("delivery_revoked"):
                    row["display_status"] = "revoked"
                elif refund_status == "refund_requested":
                    row["display_status"] = "refund_requested"
                elif refund_status == "waiting_user_choice":
                    row["display_status"] = "awaiting_refund_choice"
                elif refund_status in {"wallet_credited", "refund_paid"}:
                    row["display_status"] = refund_status
                else:
                    row["display_status"] = row.get("status")
            return rows
        review_statuses = {"upi_submitted", "binance_submitted", "usdt_manual_submitted"}
        payment_rows = app.db.pending_payments.find({"ref_id": {"$in": refs}}, {"ref_id": 1, "status": 1})
        payment_status_by_ref = {str(row.get("ref_id") or ""): row.get("status") for row in payment_rows}
        for row in rows:
            status = row.get("status")
            refund_status = str(row.get("refund_status") or "").strip().lower()
            if row.get("delivery_revoked"):
                row["display_status"] = "revoked"
            elif refund_status == "refund_requested":
                row["display_status"] = "refund_requested"
            elif refund_status == "waiting_user_choice":
                row["display_status"] = "awaiting_refund_choice"
            elif refund_status in {"wallet_credited", "refund_paid"}:
                row["display_status"] = refund_status
            elif status == "pending" and payment_status_by_ref.get(str(row.get("order_id") or "")) in review_statuses:
                row["display_status"] = "needs_review"
            else:
                row["display_status"] = status
        return rows

    @app.get("/")
    @login_required
    def dashboard():
        expire_stale_unpaid_payments_and_orders(app.db)

        def build_dashboard_state() -> dict[str, Any]:
            stats = get_bot_stats(app.db)
            low_stock_rows = get_low_stock_products(app.db, limit=8)
            recent_orders = _attach_order_display_status(
                recent_created_rows(
                    app.db.orders.find({"is_replacement": {"$ne": True}}),
                    limit=5,
                )
            )
            return {
                "stats": stats,
                "maintenance": bool(get_setting(app.db, "maintenance_mode", False)),
                "pending_payments": count_pending_payment_reviews(app.db),
                "active_payment_currencies": get_active_payment_currencies(app.db),
                "pending_stock": app.db.orders.count_documents({"status": "pending_stock"}),
                "pending_refunds": count_pending_refund_requests(app.db),
                "low_stock": low_stock_rows,
                "recent_orders": recent_orders,
            }

        state = cached_value("dashboard_state", ADMIN_DASHBOARD_CACHE_TTL_SECONDS, build_dashboard_state)
        return render_template(
            "dashboard.html",
            stats=state.get("stats") or {},
            maintenance=bool(state.get("maintenance")),
            pending_payments=int(state.get("pending_payments") or 0),
            active_payment_currencies=state.get("active_payment_currencies") or set(),
            pending_stock=int(state.get("pending_stock") or 0),
            pending_refunds=int(state.get("pending_refunds") or 0),
            low_stock=state.get("low_stock") or [],
            recent_orders=state.get("recent_orders") or [],
        )


    @app.post("/language-settings")
    @login_required
    @csrf_required
    def language_settings_submit():
        enabled = ["en"]
        if request.form.get("spanish_enabled") == "on":
            enabled.append("es")
        set_language_settings(app.db, enabled)
        flash("Language settings saved.", "success")
        return redirect(request.referrer or url_for("dashboard"))


    @app.post("/maintenance")
    @login_required
    @csrf_required
    def set_maintenance():
        mode = request.form.get("mode", "status")
        if mode == "on":
            set_setting(app.db, "maintenance_mode", True)
            flash("Maintenance mode is now ON.", "success")
        elif mode == "off":
            set_setting(app.db, "maintenance_mode", False)
            flushed = flush_maintenance_notifications(app.db)
            flash(
                f"Maintenance mode is now OFF. Delivered {flushed['processed']} queued product/stock/price notification event(s) "
                f"({flushed['sent']} message(s) sent).",
                "success",
            )
        else:
            flash(f"Maintenance mode is {'ON' if get_setting(app.db, 'maintenance_mode', False) else 'OFF'}.", "info")
        return redirect(request.referrer or url_for("dashboard"))


    @app.get("/secret-settings")
    @login_required
    def secret_settings_form():
        settings = get_secret_settings(app.db)
        language_settings = get_language_settings(app.db)
        return render_template(
            "secret_settings.html",
            settings=settings,
            language_settings=language_settings,
        )

    @app.post("/secret-settings")
    @login_required
    @csrf_required
    def secret_settings_submit():
        existing = get_secret_settings(app.db)
        settings = dict(existing)
        enabled_languages = ["en"]
        if request.form.get("spanish_enabled") == "1":
            enabled_languages.append("es")
        plain_secret_form_keys = [
            "support_usernames", "admin_ids", "bsc_rpc_url", "bsc_rpc_urls", "polygon_rpc_url", "polygon_rpc_urls", "usdt_lookback_seconds", "bsc_rpc_block_chunk_size", "polygon_rpc_block_chunk_size",
            "binance_api_base_url", "binance_recv_window_ms", "binance_pay_history_lookback_seconds",
            "payment_timeout_minutes", "payment_reminder_minutes", "usdt_verify_interval_seconds",
            "bep20_required_confirmations", "polygon_required_confirmations", "usdt_manual_verify_delay_minutes", "low_stock_alert_threshold",
            "restock_back_in_stock_cooldown_minutes", "restock_notification_cooldown_minutes",
            "restock_long_notification_cooldown_minutes", "restock_big_addition_threshold", "admin_panel_username",
        ]
        for key in plain_secret_form_keys:
            settings[key] = request.form.get(key, "").strip()
        for key in SENSITIVE_SECRET_SETTING_KEYS:
            incoming = request.form.get(key, "").strip()
            # Blank means keep the existing saved secret. This prevents accidental
            # erasing and keeps secrets out of the rendered HTML.
            settings[key] = incoming if incoming else str(existing.get(key) or "").strip()
        new_password = request.form.get("admin_panel_password", "")
        if new_password.strip():
            settings["admin_panel_password_hash"] = generate_password_hash(new_password.strip())
        validation_errors = []
        for int_key, label, minimum in [
            ("payment_timeout_minutes", "Payment timeout minutes", 1),
            ("payment_reminder_minutes", "Payment reminder minutes", 1),
            ("usdt_verify_interval_seconds", "USDT verify interval seconds", 5),
            ("bep20_required_confirmations", "BEP20 required confirmations", 1),
            ("polygon_required_confirmations", "Polygon required confirmations", 1),
            ("usdt_manual_verify_delay_minutes", "Manual verification delay minutes", 0),
            ("low_stock_alert_threshold", "Low stock alert threshold", 1),
            ("restock_back_in_stock_cooldown_minutes", "Back-in-stock notification cooldown minutes", 1),
            ("restock_notification_cooldown_minutes", "Low-stock recovery notification cooldown minutes", 1),
            ("restock_long_notification_cooldown_minutes", "Big restock notification cooldown minutes", 1),
            ("restock_big_addition_threshold", "Big restock minimum added items", 1),
        ]:
            try:
                value = int(str(settings.get(int_key) or "").strip())
            except ValueError:
                validation_errors.append(f"{label} must be a whole number.")
                continue
            if value < minimum:
                validation_errors.append(f"{label} must be at least {minimum}.")
        for part in _split_csv(settings.get("admin_ids", "")):
            try:
                int(part)
            except ValueError:
                validation_errors.append("Telegram admin/tester IDs must be numeric IDs separated by commas.")
                break
        if validation_errors:
            for error in validation_errors:
                flash(error, "error")
            return render_template("secret_settings.html", settings=settings, language_settings={"default_language": "en", "enabled_languages": enabled_languages}), 400
        if not settings.get("bot_token"):
            flash("Telegram bot token is required before the bot can start.", "error")
            return render_template("secret_settings.html", settings=settings, language_settings={"default_language": "en", "enabled_languages": enabled_languages}), 400
        if not settings.get("admin_panel_username") and not os.getenv("ADMIN_PANEL_USERNAME"):
            settings["admin_panel_username"] = "admin"
        set_secret_settings(app.db, settings)
        set_language_settings(app.db, enabled_languages)
        saved_token_preview = mask_bot_token(settings.get("bot_token"))
        flash(f"Secret settings saved. Bot token now saved as {saved_token_preview}. Restart the bot after changing bot token/timing settings.", "success")
        return redirect(url_for("secret_settings_form"))


    @app.get("/admins")
    @login_required
    @owner_required
    def admins_panel():
        settings = get_secret_settings(app.db)
        admin_accounts = get_admin_accounts(settings)
        stock_manager_rows = build_stock_manager_admin_rows(app.db, admin_accounts)
        stock_summary_by_id = {str(row.get("account", {}).get("id") or ""): row.get("summary", {}) for row in stock_manager_rows}
        payout_requests = [
            _enrich_stock_manager_payout_request(app.db, doc)
            for doc in app.db.stock_manager_payment_requests.find().sort("requested_at", -1).limit(50)
        ]
        return render_template(
            "admins.html",
            admin_accounts=admin_accounts,
            admin_sub_roles=sorted(ADMIN_SUB_ROLES),
            stock_summary_by_id=stock_summary_by_id,
            payout_requests=payout_requests,
        )

    @app.get("/admins/<account_id>")
    @login_required
    @owner_required
    def admin_account_detail(account_id: str):
        settings = get_secret_settings(app.db)
        accounts = get_admin_accounts(settings)
        account = next((a for a in accounts if str(a.get("id") or "") == str(account_id or "")), None)
        if not account:
            flash("Admin account not found.", "error")
            return redirect(url_for("admins_panel"))
        stats = None
        summary = {}
        payment_details_text = ""
        payment_method_label = ""
        payout_history = []
        payout_requests = []
        all_products = []
        if account.get("role") == ADMIN_ROLE_STOCK_MANAGER:
            username = str(account.get("username") or "").strip()
            stats = build_stock_manager_dashboard(app.db, username)
            summary = stats.get("summary", {})
            payment_details_text = format_stock_manager_payment_details(account)
            payment_method_label = stock_manager_payment_method_label(account.get("payment_method"))
            payout_history = stats.get("payout_history", [])
            payout_requests = stats.get("payout_requests", [])
            all_products = list(app.db.products.find({}, {"name": 1}).sort("name", 1))
        return render_template(
            "admin_detail.html",
            account=account,
            stats=stats,
            summary=summary,
            payment_details_text=payment_details_text,
            payment_method_label=payment_method_label,
            payout_history=payout_history,
            payout_requests=payout_requests,
            all_products=all_products,
        )

    @app.post("/admins/<account_id>/assign-products")
    @login_required
    @owner_required
    @csrf_required
    def assign_stock_manager_products(account_id: str):
        settings = get_secret_settings(app.db)
        accounts = get_admin_accounts(settings)
        account = next((a for a in accounts if str(a.get("id") or "") == str(account_id or "")), None)
        if not account or account.get("role") != ADMIN_ROLE_STOCK_MANAGER:
            flash("Stock manager account not found.", "error")
            return redirect(url_for("admins_panel"))

        valid_products = list(app.db.products.find({}, {"name": 1}).sort("name", 1))
        valid_by_key = {product_name_key(product.get("name")): str(product.get("name") or "").strip() for product in valid_products}
        selected_names = request.form.getlist("assigned_products")
        assigned: list[str] = []
        seen: set[str] = set()
        for name in selected_names:
            key = product_name_key(name)
            if not key or key not in valid_by_key or key in seen:
                continue
            assigned.append(valid_by_key[key])
            seen.add(key)

        account["assigned_products"] = assigned
        settings["admin_accounts"] = accounts
        set_secret_settings(app.db, settings)
        log_admin_action(app.db, "stock_manager_products_assigned", f"{account.get('username')}: {len(assigned)} product(s)")
        flash(f"Assigned {len(assigned)} product(s) to {account.get('username')}.", "success")
        return redirect(url_for("admin_account_detail", account_id=account_id))


    @app.post("/admins/<account_id>/credentials")
    @login_required
    @owner_required
    @csrf_required
    def update_admin_account_credentials(account_id: str):
        settings = get_secret_settings(app.db)
        accounts = get_admin_accounts(settings)
        account = next((a for a in accounts if str(a.get("id") or "") == str(account_id or "")), None)
        if not account:
            flash("Admin account not found.", "error")
            return redirect(url_for("admins_panel"))

        old_username = str(account.get("username") or "").strip()
        new_username = (request.form.get("admin_username") or "").strip()
        new_password = (request.form.get("admin_password") or "").strip()
        if not new_username:
            flash("Username is required.", "error")
            return redirect(url_for("admin_account_detail", account_id=account_id))
        owner_username, _, _ = get_admin_login_config(app.db)
        new_key = new_username.lower()
        if new_key == str(owner_username or "").strip().lower():
            flash("That WebAdmin username is already used by the owner account.", "error")
            return redirect(url_for("admin_account_detail", account_id=account_id))
        for other in accounts:
            if str(other.get("id") or "") == str(account_id or ""):
                continue
            if new_key == str(other.get("username") or "").strip().lower():
                flash("That WebAdmin username already exists.", "error")
                return redirect(url_for("admin_account_detail", account_id=account_id))

        changed_parts: list[str] = []
        if new_username != old_username:
            if account.get("role") == ADMIN_ROLE_STOCK_MANAGER:
                migrate_stock_manager_username_references(app.db, old_username, new_username)
            account["username"] = new_username
            changed_parts.append("username")
        if new_password:
            account["password_hash"] = generate_password_hash(new_password)
            changed_parts.append("password")
        if not changed_parts:
            flash("No account changes were made.", "info")
            return redirect(url_for("admin_account_detail", account_id=account_id))

        settings["admin_accounts"] = accounts
        set_secret_settings(app.db, settings)
        log_admin_action(app.db, "webadmin_account_credentials_updated", f"{old_username} -> {new_username}: {', '.join(changed_parts)}")
        flash(f"Updated {', '.join(changed_parts)} for {new_username}.", "success")
        return redirect(url_for("admin_account_detail", account_id=account_id))


    @app.post("/admins/<account_id>/manual-replacement-due")
    @login_required
    @owner_required
    @csrf_required
    def add_owner_manual_replacement_due(account_id: str):
        settings = get_secret_settings(app.db)
        accounts = get_admin_accounts(settings)
        account = next((a for a in accounts if str(a.get("id") or "") == str(account_id or "")), None)
        if not account or account.get("role") != ADMIN_ROLE_STOCK_MANAGER:
            flash("Stock manager account not found.", "error")
            return redirect(url_for("admins_panel"))
        product_name = (request.form.get("product_name") or "").strip()
        try:
            quantity = int(str(request.form.get("quantity") or "0").strip())
        except ValueError:
            quantity = 0
        note = (request.form.get("note") or "").strip()
        if quantity <= 0:
            flash("Replacement quantity must be at least 1.", "error")
            return redirect(url_for("admin_account_detail", account_id=account_id))
        if quantity > 500:
            flash("You can add at most 500 manual replacements at once.", "error")
            return redirect(url_for("admin_account_detail", account_id=account_id))
        product = app.db.products.find_one({"name": name_regex(product_name)}, {"name": 1}) if product_name else None
        if not product:
            flash("Select a valid product for the replacement due.", "error")
            return redirect(url_for("admin_account_detail", account_id=account_id))
        created = create_owner_manual_replacement_obligations(
            app.db,
            username=str(account.get("username") or "").strip(),
            product_name=str(product.get("name") or product_name).strip(),
            quantity=quantity,
            note=note,
            added_by=current_admin_username() or "owner",
        )
        if created <= 0:
            flash("Could not add manual replacement due.", "error")
            return redirect(url_for("admin_account_detail", account_id=account_id))
        log_admin_action(app.db, "manual_replacement_due_assigned", f"{account.get('username')}: {created} x {product.get('name') or product_name}")
        flash(f"Added {created} manual replacement due item(s) for {account.get('username')}.", "success")
        return redirect(url_for("admin_account_detail", account_id=account_id))


    @app.post("/admins/<account_id>/clear-payout")
    @login_required
    @owner_required
    @csrf_required
    def clear_stock_manager_payout(account_id: str):
        settings = get_secret_settings(app.db)
        accounts = get_admin_accounts(settings)
        account = next((a for a in accounts if str(a.get("id") or "") == str(account_id or "")), None)
        if not account or account.get("role") != ADMIN_ROLE_STOCK_MANAGER:
            flash("Stock manager account not found.", "error")
            return redirect(url_for("admins_panel"))
        username = str(account.get("username") or "").strip()
        username_key = normalize_admin_username(username)
        stats = build_stock_manager_dashboard(app.db, username)
        current_due = round(float(stats.get("summary", {}).get("payable_due_usdt", 0.0) or 0.0), 2)
        pending_request = app.db.stock_manager_payment_requests.find_one(
            {"username_key": username_key, "status": "pending"},
            sort=[("requested_at", 1)],
        )
        requested_amount = round(max(0.0, safe_float((pending_request or {}).get("amount_usdt"), 0.0)), 2)
        clear_amount = requested_amount if pending_request and requested_amount > 0 else current_due
        if current_due > 0:
            clear_amount = min(clear_amount, current_due)
        clear_amount = round(max(0.0, clear_amount), 2)
        if clear_amount <= 0:
            flash(f"No payout due to clear for {username}.", "info")
            return redirect(url_for("admin_account_detail", account_id=account_id))
        note = (request.form.get("note") or "").strip()
        paid_at = utcnow()
        paid_by_role = current_admin_role()
        paid_by_role_label = ADMIN_ROLE_LABELS.get(paid_by_role, paid_by_role.replace("_", " ").title())
        payout_doc = {
            "username": username,
            "username_key": username_key,
            "amount_usdt": clear_amount,
            "payment_method": normalize_stock_manager_payment_method(account.get("payment_method")),
            "payment_method_label": stock_manager_payment_method_label(account.get("payment_method")),
            "payment_details": format_stock_manager_payment_details(account),
            "note": note,
            "paid_by": current_admin_username() or "owner",
            "paid_by_role": paid_by_role,
            "paid_by_role_label": paid_by_role_label,
            "created_at": paid_at,
        }
        if pending_request:
            payout_doc["payment_request_id"] = str(pending_request.get("_id"))
            payout_doc["requested_amount_usdt"] = requested_amount
        app.db.stock_manager_payouts.insert_one(payout_doc)
        if pending_request:
            app.db.stock_manager_payment_requests.update_one(
                {"_id": pending_request.get("_id")},
                {"$set": {
                    "status": "paid",
                    "paid_at": paid_at,
                    "paid_amount_usdt": clear_amount,
                    "paid_by": current_admin_username() or "owner",
                    "paid_by_role": paid_by_role,
                    "paid_by_role_label": paid_by_role_label,
                    "note": note,
                }},
            )
        log_admin_action(app.db, "stock_manager_payout_cleared", f"{username}: {clear_amount:.2f} USDT")
        flash(f"Cleared {money_usdt(clear_amount)} payout due for {username}.", "success")
        return redirect(url_for("admin_account_detail", account_id=account_id))

    @app.post("/admins/add")
    @login_required
    @owner_required
    @csrf_required
    def add_admin_account():
        username = (request.form.get("admin_username") or "").strip()
        password = (request.form.get("admin_password") or "").strip()
        role = normalize_admin_role(request.form.get("admin_role"))
        if role == ADMIN_ROLE_OWNER:
            flash("Use the main Admin username/password fields for Owner access.", "error")
            return redirect(url_for("admins_panel"))
        if not username or not password:
            flash("Admin username and password are required.", "error")
            return redirect(url_for("admins_panel"))
        owner_username, _, _ = get_admin_login_config(app.db)
        settings = get_secret_settings(app.db)
        accounts = get_admin_accounts(settings)
        if username.lower() == str(owner_username or "").lower() or any(username.lower() == str(a.get("username") or "").lower() for a in accounts):
            flash("That WebAdmin username already exists.", "error")
            return redirect(url_for("admins_panel"))
        accounts.append({
            "id": secrets.token_urlsafe(8),
            "username": username,
            "password_hash": generate_password_hash(password),
            "role": role,
            "enabled": True,
            "created_at": utcnow().isoformat(),
            "payment_method": "upi",
            "payment_methods": clean_stock_manager_payment_methods({}),
            "payment_details": "",
            "payment_details_updated_at": "",
            "assigned_products": [],
        })
        settings["admin_accounts"] = accounts
        set_secret_settings(app.db, settings)
        log_admin_action(app.db, "webadmin_account_added", f"{username} role={role}")
        flash(f"Added {ADMIN_ROLE_LABELS.get(role, role)} account {username}.", "success")
        return redirect(url_for("admins_panel"))

    @app.post("/admins/<account_id>/delete")
    @login_required
    @owner_required
    @csrf_required
    def delete_admin_account(account_id: str):
        settings = get_secret_settings(app.db)
        accounts = get_admin_accounts(settings)
        kept = [a for a in accounts if str(a.get("id") or "") != str(account_id or "")]
        if len(kept) == len(accounts):
            flash("Admin account not found.", "error")
            return redirect(url_for("admins_panel"))
        removed = next((a for a in accounts if str(a.get("id") or "") == str(account_id or "")), {})
        settings["admin_accounts"] = kept
        set_secret_settings(app.db, settings)
        log_admin_action(app.db, "webadmin_account_deleted", f"{removed.get('username')} role={removed.get('role')}")
        flash("Admin account removed.", "success")
        return redirect(url_for("admins_panel"))


    @app.get("/replacements")
    @login_required
    @owner_required
    def replacements():
        status = (request.args.get("status") or "pending").strip().lower()
        allowed_statuses = {"pending", "replaced", "cancelled"}
        if status not in allowed_statuses:
            status = "pending"
        status_queries = {
            "pending": {"status": {"$in": ["pending", "reviewing", "approved", "replacement_ready"]}},
            "replaced": {"status": {"$in": ["replaced", "replacement_sent"]}},
            "cancelled": {"status": {"$in": ["cancelled", "closed", "rejected"]}},
        }
        query: dict[str, Any] = status_queries[status]
        page = int_arg("page", 1, minimum=1)
        total = app.db.replacement_reports.count_documents(query)
        reports = recent_created_rows(
            app.db.replacement_reports.find(query),
            skip=(page - 1) * PAGE_SIZE,
            limit=PAGE_SIZE,
        )
        total_pages = max(1, math.ceil(total / PAGE_SIZE)) if total else 1
        counts = {key: app.db.replacement_reports.count_documents(q) for key, q in status_queries.items()}
        return render_template(
            "replacements.html",
            reports=reports,
            status=status,
            counts=counts,
            page=page,
            total_pages=total_pages,
            total=total,
            manual_lookup=None,
            manual_report_text="",
        )

    @app.post("/replacements/manual-lookup")
    @login_required
    @owner_required
    @csrf_required
    def manual_replacement_lookup():
        manual_report_text = (request.form.get("manual_report_text") or "").strip()
        status = (request.form.get("status") or "pending").strip().lower()
        allowed_statuses = {"pending", "replaced", "cancelled"}
        if status not in allowed_statuses:
            status = "pending"
        status_queries = {
            "pending": {"status": {"$in": ["pending", "reviewing", "approved", "replacement_ready"]}},
            "replaced": {"status": {"$in": ["replaced", "replacement_sent"]}},
            "cancelled": {"status": {"$in": ["cancelled", "closed", "rejected"]}},
        }
        query: dict[str, Any] = status_queries[status]
        page = 1
        total = app.db.replacement_reports.count_documents(query)
        reports = recent_created_rows(app.db.replacement_reports.find(query), limit=PAGE_SIZE)
        total_pages = max(1, math.ceil(total / PAGE_SIZE)) if total else 1
        counts = {key: app.db.replacement_reports.count_documents(q) for key, q in status_queries.items()}
        manual_lookup = None
        if len(normalize_report_match_text(manual_report_text)) < 3:
            flash("Paste the user's reported account/code before searching.", "error")
        else:
            manual_lookup = build_manual_replacement_lookup_results(app.db, manual_report_text)
            if not manual_lookup.get("matches"):
                flash("No delivered stock matched this manual report text.", "info")
        return render_template(
            "replacements.html",
            reports=reports,
            status=status,
            counts=counts,
            page=page,
            total_pages=total_pages,
            total=total,
            manual_lookup=manual_lookup,
            manual_report_text=manual_report_text,
        )

    @app.post("/replacements/manual-lookup/add-due")
    @login_required
    @owner_required
    @csrf_required
    def add_manual_replacement_due():
        order_id = (request.form.get("order_id") or "").strip().upper()
        item_hash = (request.form.get("item_hash") or "").strip()
        submitted_text = (request.form.get("manual_report_text") or "").strip()
        note = (request.form.get("manual_note") or "").strip()
        report_id, error = create_manual_replacement_due_from_order_item(
            app.db,
            order_id=order_id,
            item_hash=item_hash,
            submitted_text=submitted_text,
            admin_note=note,
            added_by=current_admin_username() or "owner",
        )
        if error:
            flash(error, "error")
            return redirect(url_for("replacements"))
        log_admin_action(app.db, "manual_replacement_due_added", f"{report_id} order={order_id}")
        flash(f"Manual replacement due added to the original stock manager. Report {report_id} was created.", "success")
        return redirect(url_for("replacement_detail", report_id=report_id))

    @app.get("/replacements/<report_id>")
    @login_required
    @owner_required
    def replacement_detail(report_id: str):
        report = app.db.replacement_reports.find_one({"report_id": str(report_id or "").strip().upper()})
        if not report:
            flash("Replacement report not found.", "error")
            return redirect(url_for("replacements"))
        product_names = []
        for item in replacement_report_items(report):
            product_name = str(item.get("product_name") or "").strip()
            if product_name and product_name not in product_names:
                product_names.append(product_name)
        product = app.db.products.find_one({"name": name_regex(product_names[0])}) if product_names else None
        stock_count = 0
        for product_name in product_names:
            stock_product = app.db.products.find_one({"name": name_regex(product_name)}, {"stock": 1})
            stock_count += len(stock_product.get("stock", []) or []) if stock_product else 0
        related_order = app.db.orders.find_one({"order_id": str(report.get("order_id") or "")})
        return render_template("replacement_detail.html", report=report, product=product, stock_count=stock_count, related_order=related_order)

    @app.post("/replacements/<report_id>/approve")
    @login_required
    @owner_required
    @csrf_required
    def approve_replacement_report(report_id: str):
        report = app.db.replacement_reports.find_one({"report_id": str(report_id or "").strip().upper()})
        if not report:
            flash("Replacement report not found.", "error")
            return redirect(url_for("replacements"))
        if replacement_status_key(report.get("status")) in {"replaced", "cancelled"}:
            flash("This report is already finished and cannot be approved again.", "info")
            return redirect(url_for("replacement_detail", report_id=report.get("report_id")))
        if report.get("approved_at"):
            flash("This replacement request is already approved.", "info")
            return redirect(url_for("replacement_detail", report_id=report.get("report_id")))
        now = utcnow()
        admin_note = (request.form.get("admin_note") or "").strip()[:1000]
        create_stock_manager_replacement_obligations(app.db, report, approved_by=current_admin_username() or "owner", approved_at=now)
        app.db.replacement_reports.update_one(
            {"_id": report["_id"]},
            {"$set": {
                "status": "pending",
                "approved_at": now,
                "approved_by": current_admin_username() or "owner",
                "replacement_queued_at": now,
                "replacement_admin_note": admin_note,
            }, "$unset": {
                "replacement_required_by_username": "",
                "replacement_required_by_username_key": "",
                "replacement_required_quantity": "",
                "replacement_stock_uploaded_at": "",
                "replacement_stock_uploaded_by": "",
            }},
        )
        report = app.db.replacement_reports.find_one({"_id": report["_id"]}) or report
        result = send_replacement_from_stock(app.db, report, current_admin_username() or "owner", admin_note=admin_note)
        log_admin_action(app.db, "replacement_approved", f"{report.get('report_id')} result={result.get('status')}")
        if result.get("status") == "sent":
            flash("Replacement approved and sent to the user.", "success")
        elif result.get("status") == "no_stock":
            notify_replacement_approved_waiting(int(report.get("user_id", 0) or 0), report)
            flash("Replacement approved. It will be delivered automatically once stock is added.", "success")
        else:
            flash(result.get("message") or "Replacement approved, but it could not be sent automatically.", "error")
        return redirect(url_for("replacement_detail", report_id=report.get("report_id")))


    @app.post("/replacements/<report_id>/send-replacement")
    @login_required
    @owner_required
    @csrf_required
    def send_report_replacement(report_id: str):
        report = app.db.replacement_reports.find_one({"report_id": str(report_id or "").strip().upper()})
        if not report:
            flash("Replacement report not found.", "error")
            return redirect(url_for("replacements"))
        if replacement_status_key(report.get("status")) == "replaced":
            flash("Replacement already sent for this report.", "info")
            return redirect(url_for("replacement_detail", report_id=report.get("report_id")))
        if replacement_status_key(report.get("status")) == "cancelled":
            flash("Cancelled reports cannot be replaced.", "error")
            return redirect(url_for("replacement_detail", report_id=report.get("report_id")))
        if not report.get("approved_at"):
            flash("Approve this replacement request before sending replacement stock.", "error")
            return redirect(url_for("replacement_detail", report_id=report.get("report_id")))
        admin_note = (request.form.get("admin_note") or report.get("replacement_admin_note") or "").strip()[:1000]
        if admin_note != str(report.get("replacement_admin_note") or ""):
            app.db.replacement_reports.update_one({"_id": report["_id"]}, {"$set": {"replacement_admin_note": admin_note}})
            report = app.db.replacement_reports.find_one({"_id": report["_id"]}) or report
        result = send_replacement_from_stock(app.db, report, current_admin_username() or "owner", admin_note=admin_note)
        status = result.get("status")
        if status == "sent":
            flash("Replacement sent to user and report marked as replaced.", "success")
        elif status == "no_stock":
            flash("No stock is available for this product. It will be delivered automatically once stock is added.", "error")
        else:
            flash(result.get("message") or "Could not send replacement.", "error")
        return redirect(url_for("replacement_detail", report_id=report.get("report_id")))

    @app.post("/replacements/<report_id>/close")
    @login_required
    @owner_required
    @csrf_required
    def close_replacement_report(report_id: str):
        report = app.db.replacement_reports.find_one({"report_id": str(report_id or "").strip().upper()})
        if not report:
            flash("Replacement report not found.", "error")
            return redirect(url_for("replacements"))
        note = (request.form.get("note") or "").strip()
        app.db.replacement_reports.update_one(
            {"_id": report["_id"]},
            {"$set": {"status": "cancelled", "cancelled_at": utcnow(), "cancelled_by": current_admin_username() or "owner", "cancel_note": note, "closed_at": utcnow(), "closed_by": current_admin_username() or "owner", "close_note": note}},
        )
        notify_replacement_cancelled(int(report.get("user_id", 0) or 0), report, note)
        log_admin_action(app.db, "replacement_cancelled", f"{report.get('report_id')} note={note[:80]}")
        flash("Replacement report cancelled and user was notified.", "success")
        return redirect(url_for("replacement_detail", report_id=report.get("report_id")))

    @app.get("/payment-settings")
    @login_required
    def payment_settings_form():
        settings = get_payment_settings(app.db)
        return render_template("payment_settings.html", settings=settings)

    @app.post("/payment-settings")
    @login_required
    @csrf_required
    def payment_settings_submit():
        settings = {
            "usdt_bep20": {
                "enabled": request.form.get("usdt_bep20_enabled") == "1",
                "wallet_address": request.form.get("usdt_wallet_address", "").strip(),
            },
            "usdt_polygon": {
                "enabled": request.form.get("usdt_polygon_enabled") == "1",
                "wallet_address": request.form.get("usdt_polygon_wallet_address", "").strip(),
            },
            "upi": {
                "enabled": request.form.get("upi_enabled") == "1",
                "upi_id": request.form.get("upi_id", "").strip(),
                "upi_name": request.form.get("upi_name", "").strip(),
            },
            "binance": {
                "enabled": request.form.get("binance_enabled") == "1",
                "binance_pay_id": request.form.get("binance_pay_id", "").strip(),
                "binance_pay_name": request.form.get("binance_pay_name", "").strip(),
            },
            "wallet_limits": {
                "min_inr": request.form.get("wallet_min_inr", "50").strip(),
                "min_usdt": request.form.get("wallet_min_usdt", "1").strip(),
            },
        }
        errors = []
        if settings["usdt_bep20"]["enabled"] and not settings["usdt_bep20"]["wallet_address"]:
            errors.append("USDT (BEP20) wallet address is required when USDT (BEP20) is enabled.")
        if settings["usdt_polygon"]["enabled"] and not settings["usdt_polygon"]["wallet_address"]:
            errors.append("USDT (POLYGON) wallet address is required when USDT (POLYGON) is enabled.")
        if settings["upi"]["enabled"] and not settings["upi"]["upi_id"]:
            errors.append("UPI ID is required when UPI is enabled.")
        if settings["binance"]["enabled"] and not settings["binance"]["binance_pay_id"]:
            errors.append("Binance Pay ID is required when Binance Pay is enabled.")
        wallet_limit_checks = []
        if settings["upi"].get("enabled"):
            wallet_limit_checks.append(("min_inr", "Minimum INR wallet top-up"))
        if settings["usdt_bep20"].get("enabled") or settings["usdt_polygon"].get("enabled") or settings["binance"].get("enabled"):
            wallet_limit_checks.append(("min_usdt", "Minimum USDT wallet top-up"))
        for key, label in wallet_limit_checks:
            try:
                value = float(settings["wallet_limits"].get(key) or 0)
            except ValueError:
                errors.append(f"{label} must be a valid number.")
                continue
            if value <= 0:
                errors.append(f"{label} must be greater than zero.")
        if errors:
            for error in errors:
                flash(error, "error")
            return render_template("payment_settings.html", settings=settings), 400
        set_payment_settings(app.db, settings)
        flash("Payment settings saved. The bot will use these website settings for new payment instructions.", "success")
        return redirect(url_for("payment_settings_form"))

    @app.get("/help")
    @login_required
    def help_page():
        return render_template("help.html")

    @app.get("/system-health")
    @login_required
    @owner_required
    def system_health():
        return render_template("system_health.html", health=build_system_health(app.db))

    @app.get("/stock-manager-dashboard")
    @login_required
    def stock_manager_dashboard():
        username = current_admin_username()
        if not username:
            session.clear()
            flash("Admin username not found in this session. Please log in again.", "error")
            return redirect(url_for("login_form"))
        stats = build_stock_manager_dashboard(app.db, username)
        return render_template("stock_manager_dashboard.html", stats=stats)


    @app.post("/stock-manager-dashboard/payment-details")
    @login_required
    @csrf_required
    def save_stock_manager_payment_details():
        if current_admin_role() != ADMIN_ROLE_STOCK_MANAGER:
            abort(403)
        username = current_admin_username()
        payment_method = normalize_stock_manager_payment_method(request.form.get("payment_method"))
        payment_methods = clean_stock_manager_payment_methods({
            "upi": {
                "name": request.form.get("upi_name"),
                "upi_id": request.form.get("upi_id"),
            },
            "bep20": {
                "address": request.form.get("bep20_address"),
            },
            "binance": {
                "name": request.form.get("binance_name"),
                "binance_id": request.form.get("binance_id"),
            },
        })
        if not stock_manager_method_has_details(payment_method, payment_methods):
            flash("Complete the required fields for your selected payout method before saving.", "error")
            return redirect(url_for("stock_manager_dashboard"))
        settings = get_secret_settings(app.db)
        accounts = get_admin_accounts(settings)
        changed = False
        for account in accounts:
            if normalize_admin_username(account.get("username")) == normalize_admin_username(username):
                account["payment_method"] = payment_method
                account["payment_methods"] = payment_methods
                account["payment_details"] = format_stock_manager_payment_details({
                    "payment_method": payment_method,
                    "payment_methods": payment_methods,
                })
                account["payment_details_updated_at"] = utcnow().isoformat()
                changed = True
                break
        if not changed:
            flash("Could not find your stock manager account. Please ask the owner to recreate it.", "error")
            return redirect(url_for("stock_manager_dashboard"))
        settings["admin_accounts"] = accounts
        set_secret_settings(app.db, settings)
        log_admin_action(app.db, "stock_manager_payment_details_saved", f"{username}: method={payment_method}")
        flash("Payment details saved.", "success")
        return redirect(url_for("stock_manager_dashboard"))

    @app.post("/stock-manager-dashboard/request-payment")
    @login_required
    @csrf_required
    def request_stock_manager_payment():
        if current_admin_role() != ADMIN_ROLE_STOCK_MANAGER:
            abort(403)
        username = current_admin_username()
        stats = build_stock_manager_dashboard(app.db, username)
        due = round(float(stats.get("summary", {}).get("payable_due_usdt", 0.0) or 0.0), 2)
        if due < STOCK_MANAGER_MIN_PAYOUT_USDT:
            flash(f"Minimum payout request is {money_usdt(STOCK_MANAGER_MIN_PAYOUT_USDT)}. Your current due is {money_usdt(due)}.", "error")
            return redirect(url_for("stock_manager_dashboard"))
        settings = get_secret_settings(app.db)
        account = next((a for a in get_admin_accounts(settings) if normalize_admin_username(a.get("username")) == normalize_admin_username(username)), {})
        payment_method = normalize_stock_manager_payment_method(account.get("payment_method"))
        payment_details = format_stock_manager_payment_details(account)
        if not stock_manager_method_has_details(payment_method, account.get("payment_methods")) or not payment_details:
            flash("Add complete payment details before requesting payment.", "error")
            return redirect(url_for("stock_manager_dashboard"))
        existing = app.db.stock_manager_payment_requests.find_one({"username_key": normalize_admin_username(username), "status": "pending"})
        if existing:
            flash("You already have a pending payment request. Wait for the owner to clear it.", "info")
            return redirect(url_for("stock_manager_dashboard"))
        request_doc = {
            "username": username,
            "username_key": normalize_admin_username(username),
            "amount_usdt": due,
            "payment_method": payment_method,
            "payment_method_label": stock_manager_payment_method_label(payment_method),
            "payment_details": payment_details,
            "status": "pending",
            "requested_at": utcnow(),
        }
        app.db.stock_manager_payment_requests.insert_one(request_doc)
        log_admin_action(app.db, "stock_manager_payment_requested", f"{username}: {due:.2f} USDT")
        flash(f"Payment request submitted for {money_usdt(due)}.", "success")
        return redirect(url_for("stock_manager_dashboard"))

    @app.get("/products")
    @login_required
    def products():
        q = request.args.get("q", "").strip()
        lookup_q = request.args.get("lookup_q", "").strip()
        stock_lookup = None
        if lookup_q and is_owner_role():
            stock_lookup = build_stock_lookup_results(app.db, lookup_q)
        elif lookup_q:
            flash("Stock lookup is available only to the owner account.", "error")
        page = int_arg("page", 1, minimum=1)
        assigned_product_names: list[str] = []
        product_query = None
        include_stock_items = False
        if is_stock_manager_role():
            username = current_admin_username()
            assigned_product_names = get_stock_manager_assigned_product_names(app.db, username)
            include_stock_items = True
            if assigned_product_names:
                product_query = {"$or": [{"name": name_regex(name)} for name in assigned_product_names]}
            else:
                product_query = {"_id": {"$exists": False}}
        products = get_products_with_availability(
            app.db,
            include_disabled=True,
            include_stock_items=include_stock_items,
            product_query=product_query,
        )
        if q:
            q_lower = q.lower()
            products = [p for p in products if q_lower in str(p.get("name", "")).lower()]
        if is_stock_manager_role():
            username = current_admin_username()
            replacement_summary = get_stock_manager_replacement_summary(app.db, username)
            pending_replacements_by_product = {
                product_name_key(row.get("product_name")): row
                for row in replacement_summary.get("pending_by_product", [])
            }
            for product in products:
                visibility = get_stock_visibility_summary(product, username)
                product["visible_stock_count"] = visibility["visible"]
                product["hidden_stock_count"] = visibility["hidden"]
                pending_replacement_row = pending_replacements_by_product.get(product_name_key(product.get("name"))) or {}
                product["pending_replacement_count"] = int(pending_replacement_row.get("count") or 0)
                product["pending_replacement_report_ids"] = list(pending_replacement_row.get("report_ids") or [])[:4]

        rows, total_pages = paginate(products, page, PAGE_SIZE)
        if is_owner_role():
            for product_row in rows:
                product_row["rejected_upload_count"] = app.db.stock_upload_rejections.count_documents({"product_name": name_regex(product_row.get("name", ""))})

        payment_currencies = get_active_payment_currencies(app.db)
        return render_template(
            "products.html",
            products=rows,
            page=page,
            total_pages=total_pages,
            q=q,
            total=len(products),
            payment_currencies=payment_currencies,
            low_stock_threshold=get_runtime_int(app.db, "low_stock_alert_threshold", LOW_STOCK_ALERT_THRESHOLD),
            assigned_product_names=assigned_product_names if is_stock_manager_role() else [],
            lookup_q=lookup_q,
            stock_lookup=stock_lookup,
        )

    @app.get("/products/<path:name>/manage")
    @login_required
    def product_manage(name: str):
        product = app.db.products.find_one({"name": name_regex(name)})
        if not product:
            flash("Product not found.", "error")
            return redirect(url_for("products"))
        if not require_stock_manager_product_access(app.db, product.get("name")):
            flash("This product is not assigned to your stock-manager account.", "error")
            return redirect(url_for("products"))
        product["total_stock"] = get_stock_count(app.db, product["name"])
        product["available_stock"] = get_available_stock_count(app.db, product["name"])
        product["pending_stock_quantity"] = get_pending_stock_quantity(app.db, product["name"])
        product["min_order_quantity"] = parse_positive_int(product.get("min_order_quantity"), 1, minimum=1)
        product["max_order_quantity"] = parse_positive_int(product.get("max_order_quantity"), 100, minimum=1)
        product["low_stock_threshold"] = parse_positive_int(product.get("low_stock_threshold"), get_runtime_int(app.db, "low_stock_alert_threshold", LOW_STOCK_ALERT_THRESHOLD), minimum=1)
        active_preorders = get_active_preorder_backorder_quantity(app.db, product["name"])
        product["preorder_enabled"] = product_preorder_enabled(product)
        product["preorder_max_quantity"] = get_product_preorder_max_quantity(product)
        product["preorder_total_limit"] = get_product_preorder_total_limit(product)
        product["active_preorder_quantity"] = active_preorders
        product["paid_preorder_quantity"] = get_paid_preorder_quantity(app.db, product["name"])
        product["preorder_capacity_remaining"] = get_preorder_capacity_remaining(product, active_preorders)
        product["shop_order"] = parse_product_shop_order(product.get("shop_order"), default=None)
        position_products = sort_products_for_shop(list(app.db.products.find({}, {"name": 1, "shop_order": 1, "created_at": 1})))
        product["total_product_positions"] = len(position_products)
        product["current_shop_position"] = next(
            (index for index, row in enumerate(position_products, start=1) if row.get("_id") == product.get("_id")),
            product["shop_order"] or len(position_products) or 1,
        )
        product["stock_manager_earning_rate_usdt"] = safe_float(product.get("stock_manager_earning_rate_usdt"), 0.0)
        product["stock_manager_owner_rate_usdt"] = safe_float(product.get("stock_manager_owner_rate_usdt"), 0.0)
        legacy_description = str(product.get("description") or "")
        product["description_en"] = str(product.get("description_en") or legacy_description)
        product["description_es"] = str(product.get("description_es") or "")
        product["warranty_days"] = parse_positive_int(product.get("warranty_days"), 0, minimum=0)
        product["price_group_rows"] = build_price_group_rows(product)
        if product["max_order_quantity"] < product["min_order_quantity"]:
            product["max_order_quantity"] = product["min_order_quantity"]
        stock_view_items = build_current_stock_view(
            product,
            viewer_username=current_admin_username() if is_stock_manager_role() else None,
        )
        visible_stock_text = [row["text"] for row in stock_view_items if row.get("can_view") and row.get("text")]
        visible_stock_count = len(visible_stock_text)
        hidden_stock_count = sum(1 for row in stock_view_items if not row.get("can_view"))
        approved_pool_stats = approved_stock_pool_stats(app.db, product) if is_owner_role() else {"total": 0, "current": 0, "sold_or_used": 0, "remaining": 0}
        recent_rejected_stock_uploads = []
        if is_owner_role():
            recent_rejected_stock_uploads = list(
                app.db.stock_upload_rejections.find({"product_name": name_regex(product["name"])}).sort("created_at", -1).limit(5)
            )
        return render_template(
            "product_manage.html",
            product=product,
            low_stock_threshold=product["low_stock_threshold"],
            stock_preview=None,
            stock_view_items=stock_view_items,
            visible_stock_text=visible_stock_text,
            visible_stock_count=visible_stock_count,
            hidden_stock_count=hidden_stock_count,
            approved_pool_stats=approved_pool_stats,
            recent_rejected_stock_uploads=recent_rejected_stock_uploads,
            payment_currencies=get_active_payment_currencies(app.db),
            price_groups=PRICE_GROUPS,
        )

    @app.post("/products/<path:name>/approved-stock")
    @login_required
    @owner_required
    @csrf_required
    def update_approved_stock_pool(name: str):
        product = app.db.products.find_one({"name": name_regex(name)})
        if not product:
            flash("Product not found.", "error")
            return redirect(url_for("products"))

        action = str(request.form.get("pool_action") or "settings").strip().lower()
        restrict_enabled = request.form.get("restrict_stock_managers") == "1"
        raw_items = request.form.get("approved_stock_items", "")
        submitted_items = []
        seen_submitted: set[str] = set()
        for item in split_stock(raw_items):
            clean = normalize_approved_stock_item(item)
            if clean and clean not in seen_submitted:
                submitted_items.append(clean)
                seen_submitted.add(clean)

        existing_items = approved_stock_pool_items(product)
        existing_seen = set(existing_items)
        new_pool = list(existing_items)

        if action == "append":
            added = 0
            for item in submitted_items:
                if item not in existing_seen:
                    new_pool.append(item)
                    existing_seen.add(item)
                    added += 1
            if submitted_items:
                flash(f"Added {added} approved stock item(s) to the pool. Skipped {len(submitted_items) - added} duplicate pool item(s).", "success")
            else:
                flash("Approved stock pool settings saved. No new approved stock items were submitted.", "info")
        elif action == "replace":
            if not submitted_items:
                flash("Paste at least one approved stock item to replace the pool.", "error")
                return redirect(url_for("product_manage", name=product["name"]))
            new_pool = submitted_items
            flash(f"Approved stock pool replaced with {len(new_pool)} item(s).", "success")
        elif action == "remove":
            if not submitted_items:
                flash("Paste at least one approved stock item to remove from the pool.", "error")
                return redirect(url_for("product_manage", name=product["name"]))
            remove_set = set(submitted_items)
            before_count = len(new_pool)
            new_pool = [item for item in new_pool if item not in remove_set]
            removed = before_count - len(new_pool)
            missing = len(remove_set) - removed
            flash(f"Removed {removed} approved stock item(s) from the pool. Skipped {max(missing, 0)} item(s) that were not in the pool.", "success" if removed else "info")
        elif action == "clear":
            new_pool = []
            flash("Approved stock pool cleared.", "success")
        else:
            flash("Approved stock restriction setting saved.", "success")

        app.db.products.update_one(
            {"_id": product["_id"]},
            {
                "$set": {
                    "approved_stock_restriction_enabled": bool(restrict_enabled),
                    "approved_stock_pool": new_pool,
                    "approved_stock_pool_updated_at": utcnow(),
                    "approved_stock_pool_updated_by": current_admin_username() or "owner",
                }
            },
        )
        if restrict_enabled and not new_pool:
            flash("Warning: restriction is enabled but the approved stock pool is empty, so stock managers cannot add stock for this product yet.", "warning")
        log_admin_action(
            app.db,
            "approved_stock_pool_saved",
            f"{product['name']}: restrict={restrict_enabled} action={action} pool={len(new_pool)}",
        )
        return redirect(url_for("product_manage", name=product["name"]))

    @app.post("/products/<path:name>/rejected-stock-uploads/clear")
    @login_required
    @owner_required
    @csrf_required
    def clear_rejected_stock_uploads(name: str):
        product = app.db.products.find_one({"name": name_regex(name)})
        if not product:
            flash("Product not found.", "error")
            return redirect(url_for("products"))
        try:
            result = app.db.stock_upload_rejections.delete_many({"product_name": name_regex(product["name"])})
            deleted = int(getattr(result, "deleted_count", 0) or 0)
        except Exception:
            deleted = 0
            flash("Unable to clear rejected stock upload logs right now.", "error")
            return redirect(url_for("product_manage", name=product["name"]))
        log_admin_action(
            app.db,
            "approved_stock_rejections_cleared",
            f"{product['name']}: cleared={deleted} by={current_admin_username() or 'owner'}",
        )
        flash(f"Cleared {deleted} rejected stock upload log(s) for this product.", "success")
        return redirect(url_for("product_manage", name=product["name"]))

    @app.post("/products/add")
    @login_required
    @csrf_required
    def add_product():
        name = request.form.get("name", "").strip()
        payment_currencies = get_active_payment_currencies(app.db)
        price_inr = parse_price_for_currency(request.form.get("price_inr"), "INR", "inr" in payment_currencies)
        price_usdt = parse_price_for_currency(request.form.get("price_usdt"), "USDT", "usdt" in payment_currencies)
        try:
            warranty_days = int(str(request.form.get("warranty_days", "0") or "0").strip())
        except ValueError:
            flash("Warranty days must be a whole number.", "error")
            return redirect(url_for("products"))
        if warranty_days < 0:
            flash("Warranty days cannot be negative.", "error")
            return redirect(url_for("products"))
        if not payment_currencies:
            flash("Configure and enable at least one payment method before adding product prices.", "error")
            return redirect(url_for("products"))
        if not name or price_inr is None or price_usdt is None:
            return redirect(url_for("products"))
        if app.db.products.find_one({"name": name_regex(name)}):
            flash(f"Product {name} already exists.", "error")
            return redirect(url_for("products"))
        product_doc = {
            "name": name,
            "price_inr": price_inr,
            "price_usdt": price_usdt,
            "stock": [],
            "description": "",
            "description_en": "",
            "description_es": "",
            "enabled": True,
            "min_order_quantity": 1,
            "max_order_quantity": 100,
            "low_stock_threshold": get_runtime_int(app.db, "low_stock_alert_threshold", LOW_STOCK_ALERT_THRESHOLD),
            "low_stock_alert_sent": False,
            "preorder_enabled": False,
            "preorder_max_quantity": 10,
            "preorder_total_limit": 50,
            "warranty_days": warranty_days,
            "stock_manager_earning_rate_usdt": 0.0,
            "stock_manager_owner_rate_usdt": 0.0,
            "created_at": utcnow(),
        }
        app.db.products.insert_one(product_doc)
        inserted_product = app.db.products.find_one({"name": name_regex(name)}) or {"name": name, "price_inr": price_inr, "price_usdt": price_usdt, "enabled": True}
        notified = notify_users_new_product(app.db, inserted_product)
        log_admin_action(app.db, "product_added", f"{name} notified={notified}")
        flash(f"Product {name} added. Notified {notified} user(s).", "success")
        return redirect(url_for("products"))

    @app.post("/products/<path:name>/rename")
    @login_required
    @csrf_required
    def rename_product(name: str):
        product = app.db.products.find_one({"name": name_regex(name)})
        if not product:
            flash("Product not found.", "error")
            return redirect(url_for("products"))
        old_name = str(product.get("name") or name).strip()
        new_name = (request.form.get("name") or "").strip()
        if not new_name:
            flash("Product name cannot be empty.", "error")
            return redirect(url_for("product_manage", name=old_name))
        duplicate = app.db.products.find_one({"name": name_regex(new_name), "_id": {"$ne": product["_id"]}})
        if duplicate:
            flash(f"Product {new_name} already exists.", "error")
            return redirect(url_for("product_manage", name=old_name))

        app.db.products.update_one({"_id": product["_id"]}, {"$set": {"name": new_name, "updated_at": utcnow()}})
        updated_refs = rename_product_references(app.db, old_name, new_name)
        assignment_updates = replace_assigned_product_name_references(app.db, old_name, new_name)
        log_admin_action(app.db, "product_renamed", f"{old_name} -> {new_name}; {updated_refs}; assigned_accounts={assignment_updates}")
        flash(f"Product renamed from {old_name} to {new_name}.", "success")
        return redirect(url_for("product_manage", name=new_name))

    @app.post("/products/<path:name>/description")
    @login_required
    @csrf_required
    def update_product_description(name: str):
        product = app.db.products.find_one({"name": name_regex(name)})
        if not product:
            flash("Product not found.", "error")
            return redirect(url_for("products"))
        description_en = (request.form.get("description_en", "") or request.form.get("description", "") or "").strip()
        description_es = (request.form.get("description_es", "") or "").strip()
        try:
            warranty_days = int(str(request.form.get("warranty_days", "0") or "0").strip())
        except ValueError:
            flash("Warranty days must be a whole number.", "error")
            return redirect(url_for("product_manage", name=product["name"]))
        if warranty_days < 0:
            flash("Warranty days cannot be negative.", "error")
            return redirect(url_for("product_manage", name=product["name"]))
        app.db.products.update_one(
            {"_id": product["_id"]},
            {"$set": {
                "description": description_en,  # legacy/backward-compatible English description
                "description_en": description_en,
                "description_es": description_es,
                "warranty_days": warranty_days,
            }},
        )
        log_admin_action(app.db, "product_description_saved", f"{product['name']}: en={len(description_en)} chars es={len(description_es)} chars warranty_days={warranty_days}")
        flash(f"Product details saved for {product['name']}.", "success")
        return redirect(url_for("product_manage", name=product["name"]))

    @app.post("/products/<path:name>/limits")
    @login_required
    @csrf_required
    def update_product_limits(name: str):
        product = app.db.products.find_one({"name": name_regex(name)})
        if not product:
            flash("Product not found.", "error")
            return redirect(url_for("products"))
        try:
            min_qty = int(str(request.form.get("min_order_quantity", "1")).strip())
            max_qty = int(str(request.form.get("max_order_quantity", "100")).strip())
            low_stock_threshold = int(str(request.form.get("low_stock_threshold", "10")).strip())
            preorder_max_quantity = int(str(request.form.get("preorder_max_quantity", "10")).strip())
            preorder_total_limit = int(str(request.form.get("preorder_total_limit", "50")).strip())
        except ValueError:
            flash("Order limits, low-stock threshold, and preorder limits must be whole numbers.", "error")
            return redirect(url_for("product_manage", name=product["name"]))
        preorder_enabled = request.form.get("preorder_enabled") == "1"
        if min_qty < 1:
            flash("Minimum order quantity must be at least 1.", "error")
            return redirect(url_for("product_manage", name=product["name"]))
        if max_qty < min_qty:
            flash("Maximum order quantity must be greater than or equal to the minimum.", "error")
            return redirect(url_for("product_manage", name=product["name"]))
        if low_stock_threshold < 1:
            flash("Low-stock alert threshold must be at least 1.", "error")
            return redirect(url_for("product_manage", name=product["name"]))
        if preorder_max_quantity < 1:
            flash("Maximum preorder quantity must be at least 1.", "error")
            return redirect(url_for("product_manage", name=product["name"]))
        if preorder_total_limit < 1:
            flash("Total active preorder limit must be at least 1.", "error")
            return redirect(url_for("product_manage", name=product["name"]))
        app.db.products.update_one(
            {"_id": product["_id"]},
            {"$set": {
                "min_order_quantity": min_qty,
                "max_order_quantity": max_qty,
                "low_stock_threshold": low_stock_threshold,
                "preorder_enabled": preorder_enabled,
                "preorder_max_quantity": preorder_max_quantity,
                "preorder_total_limit": preorder_total_limit,
            }},
        )
        log_admin_action(app.db, "product_limits_saved", f"{product['name']} min={min_qty} max={max_qty} low_stock={low_stock_threshold} preorder={preorder_enabled} preorder_max={preorder_max_quantity} preorder_total={preorder_total_limit}")
        flash(f"Product settings saved for {product['name']}.", "success")
        return redirect(url_for("product_manage", name=product["name"]))

    def move_product_to_display_position(product_id: Any, requested_position: int) -> dict[str, int] | None:
        """Move one product to a 1-based display position and re-number all products.

        The same shop_order field is used by the WebAdmin Products page and the
        Telegram bot shop, so this keeps both places in the exact same order.
        """
        products = sort_products_for_shop(list(app.db.products.find({}, {"name": 1, "shop_order": 1, "created_at": 1})))
        if not products:
            return None
        current_index = next((index for index, row in enumerate(products) if row.get("_id") == product_id), None)
        if current_index is None:
            return None

        target_index = max(0, min(int(requested_position) - 1, len(products) - 1))
        moving = products.pop(current_index)
        products.insert(target_index, moving)

        now = utcnow()
        changed = 0
        for position, row in enumerate(products, start=1):
            if parse_product_shop_order(row.get("shop_order"), default=None) == position:
                continue
            app.db.products.update_one(
                {"_id": row["_id"]},
                {"$set": {"shop_order": position, "updated_at": now}},
            )
            changed += 1
        return {"position": target_index + 1, "total": len(products), "changed": changed}

    @app.post("/products/<path:name>/shop-order")
    @login_required
    @owner_required
    @csrf_required
    def update_product_shop_order(name: str):
        product = app.db.products.find_one({"name": name_regex(name)})
        if not product:
            flash("Product not found.", "error")
            return redirect(url_for("products"))

        requested_position = parse_product_shop_order_form(
            request.form.get("shop_order"),
            field_label="Product display position",
            allow_blank=False,
        )
        if requested_position is False:
            return redirect(url_for("product_manage", name=product["name"]))

        result = move_product_to_display_position(product["_id"], requested_position)
        if not result:
            flash("Could not update product display position.", "error")
            return redirect(url_for("product_manage", name=product["name"]))

        log_admin_action(
            app.db,
            "product_display_position_saved",
            f"{product.get('name', name)}: #{result['position']} of {result['total']}; changed={result['changed']}",
        )
        flash(
            f"Display position saved for {product.get('name', name)}. It is now #{result['position']} on the website and Telegram bot.",
            "success",
        )
        return redirect(url_for("product_manage", name=product["name"]))


    @app.post("/products/<path:name>/stock-manager-rates")
    @login_required
    @owner_required
    @csrf_required
    def update_stock_manager_rates(name: str):
        product = app.db.products.find_one({"name": name_regex(name)})
        if not product:
            flash("Product not found.", "error")
            return redirect(url_for("products"))
        earning_rate = parse_float(request.form.get("stock_manager_earning_rate_usdt"), "Stock manager earning rate")
        if earning_rate is None:
            return redirect(url_for("product_manage", name=product["name"]))
        if earning_rate < 0:
            flash("Stock manager earning rate cannot be negative.", "error")
            return redirect(url_for("product_manage", name=product["name"]))
        earning_rate = round(float(earning_rate), 3)
        owner_due_rate = 0.0
        app.db.products.update_one(
            {"_id": product["_id"]},
            {"$set": {
                "stock_manager_earning_rate_usdt": earning_rate,
                "stock_manager_owner_rate_usdt": owner_due_rate,
                "updated_at": utcnow(),
            }},
        )
        log_admin_action(app.db, "stock_manager_rates_saved", f"{product['name']}: earning={earning_rate}")
        flash(f"Stock manager rates saved for {product['name']}.", "success")
        return redirect(url_for("product_manage", name=product["name"]))

    @app.post("/products/<path:name>/price")
    @login_required
    @csrf_required
    def update_product_price(name: str):
        product = app.db.products.find_one({"name": name_regex(name)})
        if not product:
            flash("Product not found.", "error")
            return redirect(url_for("products"))
        payment_currencies = get_active_payment_currencies(app.db)
        updates: dict[str, float] = {}
        if "inr" in payment_currencies:
            price_inr = parse_price_for_currency(request.form.get("price_inr"), "INR", True)
            if price_inr is None:
                return redirect(url_for("products"))
            updates["price_inr"] = price_inr
        if "usdt" in payment_currencies:
            price_usdt = parse_price_for_currency(request.form.get("price_usdt"), "USDT", True)
            if price_usdt is None:
                return redirect(url_for("products"))
            updates["price_usdt"] = price_usdt
        if not updates:
            flash("No enabled payment methods have price fields to update.", "error")
            return redirect(url_for("products"))
        old_inr = product.get("price_inr")
        old_usdt = product.get("price_usdt")
        res = app.db.products.update_one({"_id": product["_id"]}, {"$set": updates})
        if res.matched_count:
            updated_product = dict(product)
            updated_product.update(updates)
            notified = notify_users_price_drop(
                app.db,
                updated_product,
                old_inr=old_inr,
                new_inr=updates.get("price_inr", old_inr),
                old_usdt=old_usdt,
                new_usdt=updates.get("price_usdt", old_usdt),
            )
            log_admin_action(app.db, "product_price_updated", f"{product.get('name', name)} notified={notified}")
        flash("Price updated." if res.matched_count else "Product not found.", "success" if res.matched_count else "error")
        return redirect(url_for("products", q=request.args.get("q", "")))



    @app.post("/products/<path:name>/group-prices")
    @login_required
    @owner_required
    @csrf_required
    def update_product_group_prices(name: str):
        product = app.db.products.find_one({"name": name_regex(name)})
        if not product:
            flash("Product not found.", "error")
            return redirect(url_for("products"))
        updated: dict[str, dict[str, float]] = {}
        for group in PRICE_GROUPS:
            key = group["key"]
            if key == DEFAULT_PRICE_GROUP:
                continue
            inr_raw = str(request.form.get(f"{key}_price_inr", "") or "").strip()
            usdt_raw = str(request.form.get(f"{key}_price_usdt", "") or "").strip()
            group_prices: dict[str, float] = {}
            if inr_raw:
                price_inr = parse_price_for_currency(inr_raw, f"{group['label']} INR", True)
                if price_inr is None:
                    return redirect(url_for("product_manage", name=product["name"]))
                group_prices["price_inr"] = price_inr
            if usdt_raw:
                price_usdt = parse_price_for_currency(usdt_raw, f"{group['label']} USDT", True)
                if price_usdt is None:
                    return redirect(url_for("product_manage", name=product["name"]))
                group_prices["price_usdt"] = price_usdt
            if group_prices:
                updated[key] = group_prices
        res = app.db.products.update_one(
            {"_id": product["_id"]},
            {"$set": {"price_group_prices": updated, "price_group_prices_updated_at": utcnow()}},
        )
        changed_groups = ", ".join(price_group_label(key) for key in updated) or "none"
        log_admin_action(app.db, "product_group_prices_updated", f"{product.get('name', name)} groups={changed_groups}")
        flash("Custom group prices updated.", "success" if res.matched_count else "error")
        return redirect(url_for("product_manage", name=product["name"]))

    @app.post("/products/<path:name>/toggle")
    @login_required
    @csrf_required
    def toggle_product(name: str):
        enabled = request.form.get("enabled") == "1"
        res = app.db.products.update_one({"name": name_regex(name)}, {"$set": {"enabled": enabled}})
        if res.matched_count:
            log_admin_action(app.db, "product_enabled" if enabled else "product_disabled", name)
        flash(("Product enabled." if enabled else "Product disabled.") if res.matched_count else "Product not found.", "success" if res.matched_count else "error")
        return redirect(url_for("products"))

    @app.post("/products/<path:name>/stock/notify")
    @login_required
    @owner_required
    @csrf_required
    def resend_fresh_stock_notification(name: str):
        product = app.db.products.find_one({"name": name_regex(name)})
        if not product:
            flash("Product not found.", "error")
            return redirect(url_for("products"))
        available = get_available_stock_count(app.db, product["name"])
        if product.get("enabled", True) is False:
            flash("This product is disabled. Enable it before sending a fresh stock notification.", "error")
            return redirect(url_for("product_manage", name=product["name"]))
        if available <= 0:
            flash("No available stock to notify users about.", "error")
            return redirect(url_for("product_manage", name=product["name"]))

        notified = notify_users_new_stock(app.db, product["name"], available)
        log_admin_action(
            app.db,
            "fresh_stock_notification_resent",
            f"{product['name']}: available={available} notified={notified}",
        )
        flash(
            f"Fresh stock notification resent for {product['name']} to {notified} user(s). Available stock: {available}.",
            "success",
        )
        return redirect(url_for("product_manage", name=product["name"]))


    @app.post("/products/<path:name>/stock/add")
    @login_required
    @csrf_required
    def add_stock(name: str):
        raw = request.form.get("items", "")
        upload_kind = (request.form.get("stock_upload_kind") or "normal").strip().lower()
        is_replacement_upload = upload_kind == "replacement"
        blocks = split_stock(raw)
        if not blocks:
            flash("No valid stock items found.", "error")
            return redirect(url_for("product_manage", name=name))
        product = app.db.products.find_one({"name": name_regex(name)})
        if not product:
            flash("Product not found.", "error")
            return redirect(url_for("products"))
        if not require_stock_manager_product_access(app.db, product.get("name")):
            flash("You can add stock only to products assigned to your stock-manager account.", "error")
            return redirect(url_for("products"))

        approved_pool_rejected_blocks: list[str] = []
        if is_stock_manager_role() and bool(product.get("approved_stock_restriction_enabled")):
            approved_blocks, approved_pool_rejected_blocks = filter_stock_against_approved_pool(product, blocks)
            if approved_pool_rejected_blocks:
                rejection_doc = record_rejected_stock_upload(
                    app.db,
                    product["name"],
                    approved_pool_rejected_blocks,
                    accepted_count=len(approved_blocks),
                    upload_kind="replacement" if is_replacement_upload else "normal",
                    source="webadmin",
                    username=current_admin_username(),
                    role=current_admin_role(),
                )
                webpanel_notified = notify_owner_stock_upload_rejection(app.db, rejection_doc)
                log_admin_action(
                    app.db,
                    "stock_upload_rejected_lines",
                    f"{product['name']}: by={current_admin_username()} accepted={len(approved_blocks)} rejected={len(approved_pool_rejected_blocks)} webpanel_notified={webpanel_notified}",
                )
            blocks = approved_blocks
            if not blocks:
                flash("No stock added. All submitted item(s) were rejected because they are not in the owner-approved stock pool.", "error")
                return redirect(url_for("product_manage", name=product["name"]))

        previous_available = get_available_stock_count(app.db, product["name"])

        protected_stock = list(product.get("stock", []) or []) + get_used_stock_items(app.db, product["name"])
        fresh_blocks, duplicate_blocks = filter_fresh_stock_items(protected_stock, blocks)
        if not fresh_blocks:
            rejected_text = f" Rejected by approved-stock pool: {len(approved_pool_rejected_blocks)}." if approved_pool_rejected_blocks else ""
            flash(f"No fresh stock added. Skipped {len(duplicate_blocks)} duplicate item(s).{rejected_text}", "error")
            return redirect(url_for("product_manage", name=product["name"]))

        earning_rate_for_upload = 0.0 if is_replacement_upload else safe_float(product.get("stock_manager_earning_rate_usdt"), 0.0)
        stock_meta_records = build_stock_added_by_records(
            fresh_blocks,
            username=current_admin_username() or "owner",
            role=current_admin_role(),
            manager_earning_rate_usdt=earning_rate_for_upload,
            owner_due_rate_usdt=0.0,
            stock_upload_kind="replacement" if is_replacement_upload else "normal",
        )
        app.db.products.update_one(
            {"_id": product["_id"]},
            {"$push": {"stock": {"$each": fresh_blocks}, "stock_added_by": {"$each": stock_meta_records}}},
        )
        record_stock_ledger_add(
            app.db,
            product["name"],
            fresh_blocks,
            username=current_admin_username() or "owner",
            role=current_admin_role(),
            source="webadmin",
            stock_upload_kind="replacement" if is_replacement_upload else "normal",
            manager_earning_rate_usdt=earning_rate_for_upload,
            owner_due_rate_usdt=0.0,
        )
        record_stock_manager_stock_event(
            app.db,
            "add",
            product["name"],
            fresh_blocks,
            username=current_admin_username() or "owner",
            role=current_admin_role(),
            manager_earning_rate_usdt=earning_rate_for_upload,
            owner_due_rate_usdt=0.0,
            stock_upload_kind="replacement" if is_replacement_upload else "normal",
        )
        fulfilled_replacement_owed_count = 0
        if is_replacement_upload and is_stock_manager_role():
            fulfilled_replacement_owed_count = fulfill_stock_manager_replacement_uploads(
                app.db,
                current_admin_username() or "",
                product["name"],
                len(fresh_blocks),
            )
        replacement_summary = process_pending_replacement_reports(app.db, product["name"])
        summary = process_pending_stock_orders(app.db, product["name"])
        total = get_stock_count(app.db, product["name"])
        available = get_available_stock_count(app.db, product["name"])
        skipped = len(duplicate_blocks)
        rejected_by_pool = len(approved_pool_rejected_blocks)
        skipped_text = f" Skipped {skipped} duplicate item(s)." if skipped else ""
        rejected_text = f" Rejected {rejected_by_pool} item(s) not in owner-approved stock pool." if rejected_by_pool else ""
        should_notify_restock = (
            not is_replacement_upload
            and claim_restock_notification_slot(
                app.db,
                product["name"],
                previous_available,
                available,
                cooldown_minutes=get_runtime_int(app.db, "restock_notification_cooldown_minutes", RESTOCK_NOTIFICATION_COOLDOWN_MINUTES),
                back_in_stock_cooldown_minutes=get_runtime_int(app.db, "restock_back_in_stock_cooldown_minutes", RESTOCK_BACK_IN_STOCK_COOLDOWN_MINUTES),
                long_cooldown_minutes=get_runtime_int(app.db, "restock_long_notification_cooldown_minutes", RESTOCK_LONG_NOTIFICATION_COOLDOWN_MINUTES),
                big_restock_quantity=get_runtime_int(app.db, "restock_big_addition_threshold", get_runtime_int(app.db, "restock_high_stock_threshold", RESTOCK_BIG_ADDITION_THRESHOLD)),
                added_stock_count=len(fresh_blocks),
            )
        )
        notified = notify_users_new_stock(app.db, product["name"], available) if should_notify_restock else 0
        pending_orders_sent = int(summary.get("orders_delivered", 0) or 0)
        pending_units_sent = int(summary.get("items_delivered", 0) or 0)
        log_action_name = "replacement_stock_added" if is_replacement_upload else "stock_added"
        log_admin_action(
            app.db,
            log_action_name,
            f"{product['name']}: added={len(fresh_blocks)} skipped={skipped} rejected_by_pool={rejected_by_pool} "
            f"notified={notified} replacement_sent={replacement_summary['replacements_sent']} "
            f"pending_orders_sent={pending_orders_sent} pending_units_sent={pending_units_sent}",
        )
        flash(
            f"Added {len(fresh_blocks)} {'replacement ' if is_replacement_upload else ''}fresh stock item(s).{skipped_text}{rejected_text}",
            "success",
        )
        return redirect(url_for("product_manage", name=product["name"]))

    @app.post("/products/<path:name>/stock/remove")
    @login_required
    @csrf_required
    def remove_stock(name: str):
        raw = request.form.get("items", "")
        blocks = split_stock(raw)
        if not blocks:
            flash("No valid stock items found.", "error")
            return redirect(url_for("product_manage", name=name))
        if is_stock_manager_role() and not stock_manager_has_assigned_product(app.db, name, current_admin_username()):
            flash("You can remove stock only from products assigned to your stock-manager account.", "error")
            return redirect(url_for("products"))
        result = remove_stock_items(
            app.db,
            name,
            blocks,
            allowed_added_by=current_admin_username() if is_stock_manager_role() else None,
        )
        if result.get("removed"):
            record_stock_ledger_status(
                app.db,
                name,
                result.get("removed", []),
                "removed",
                username=current_admin_username() or "owner",
                role=current_admin_role(),
                source="webadmin",
                note="Removed from current stock",
            )
            record_stock_manager_stock_event(
                app.db,
                "remove",
                name,
                result.get("removed", []),
                username=current_admin_username() or "owner",
                role=current_admin_role(),
            )
        notify_low_stock_if_needed(app.db, name)
        log_admin_action(
            app.db,
            "stock_removed",
            f"{name}: removed={len(result['removed'])} not_found={len(result['not_found'])} not_allowed={len(result.get('not_allowed', []))}",
        )
        blocked_text = f" Not added by you: {len(result.get('not_allowed', []))}." if result.get("not_allowed") else ""
        flash(f"Removed {len(result['removed'])} item(s). Not found: {len(result['not_found'])}.{blocked_text} Remaining: {result['remaining']}.", "success")
        return redirect(url_for("product_manage", name=name))

    @app.post("/products/<path:name>/stock/clear")
    @login_required
    @csrf_required
    def clear_stock(name: str):
        product_before = app.db.products.find_one({"name": name_regex(name)}, {"stock": 1, "name": 1})
        current_items = list((product_before or {}).get("stock", []) or [])
        res = app.db.products.update_one({"name": name_regex(name)}, {"$set": {"stock": []}})
        if res.matched_count and current_items:
            record_stock_ledger_status(
                app.db,
                (product_before or {}).get("name") or name,
                current_items,
                "removed",
                username=current_admin_username() or "owner",
                role=current_admin_role(),
                source="webadmin",
                note="Product stock cleared",
            )
        notify_low_stock_if_needed(app.db, name)
        if res.matched_count:
            log_admin_action(app.db, "stock_cleared", name)
        flash("Stock cleared." if res.matched_count else "Product not found.", "success" if res.matched_count else "error")
        return redirect(url_for("product_manage", name=name) if res.matched_count else url_for("products"))

    @app.post("/products/<path:name>/delete")
    @login_required
    @csrf_required
    def delete_product(name: str):
        res = app.db.products.delete_one({"name": name_regex(name)})
        if res.deleted_count:
            log_admin_action(app.db, "product_deleted", name)
        flash("Product removed." if res.deleted_count else "Product not found.", "success" if res.deleted_count else "error")
        return redirect(url_for("products"))

    @app.get("/users")
    @login_required
    def users():
        q = request.args.get("q", "").strip()
        query: dict[str, Any] = {}
        if q:
            if q.isdigit():
                query = {"$or": [{"user_id": int(q)}, {"username": {"$regex": re.escape(q), "$options": "i"}}]}
            else:
                query = {"username": {"$regex": re.escape(q.lstrip("@")), "$options": "i"}}
        page = int_arg("page", 1, minimum=1)
        total = app.db.users.count_documents(query)
        rows = list(app.db.users.find(query).sort("joined_at", -1).skip((page - 1) * PAGE_SIZE).limit(PAGE_SIZE))
        total_pages = max(1, math.ceil(total / PAGE_SIZE)) if total else 1
        return render_template("users.html", users=rows, page=page, total_pages=total_pages, total=total, q=q)

    @app.get("/users/<int:user_id>")
    @login_required
    def user_detail(user_id: int):
        user = app.db.users.find_one({"user_id": user_id})
        if not user:
            flash("User not found.", "error")
            return redirect(url_for("users"))
        stats = get_user_order_stats(app.db, user_id)

        per_page = PAGE_SIZE
        orders_page = int_arg("orders_page", 1, minimum=1)
        replacements_page = int_arg("replacements_page", 1, minimum=1)
        wallet_page = int_arg("wallet_page", 1, minimum=1)

        orders_query = {"user_id": user_id, "is_replacement": {"$ne": True}}
        replacements_query = {"user_id": user_id, "is_replacement": True}
        wallet_logs_query = {"user_id": user_id, "pay_type": "wallet"}

        orders_total = app.db.orders.count_documents(orders_query)
        replacements_total = app.db.orders.count_documents(replacements_query)
        wallet_logs_total = app.db.pending_payments.count_documents(wallet_logs_query)

        orders_total_pages = max(1, math.ceil(orders_total / per_page)) if orders_total else 1
        replacements_total_pages = max(1, math.ceil(replacements_total / per_page)) if replacements_total else 1
        wallet_logs_total_pages = max(1, math.ceil(wallet_logs_total / per_page)) if wallet_logs_total else 1

        orders_page = min(orders_page, orders_total_pages)
        replacements_page = min(replacements_page, replacements_total_pages)
        wallet_page = min(wallet_page, wallet_logs_total_pages)

        orders = recent_created_rows(
            app.db.orders.find(orders_query),
            skip=(orders_page - 1) * per_page,
            limit=per_page,
        )
        recent_replacements = recent_created_rows(
            app.db.orders.find(replacements_query),
            skip=(replacements_page - 1) * per_page,
            limit=per_page,
        )
        logs = recent_created_rows(
            app.db.pending_payments.find(wallet_logs_query),
            skip=(wallet_page - 1) * per_page,
            limit=per_page,
        )

        user_language = lang_from_user(user)
        user["pricing_group"] = normalize_price_group(user.get("pricing_group"))
        products_for_send = get_products_with_availability(app.db, include_disabled=True)
        user_price_overrides = build_user_price_override_rows(app.db, user, products_for_send)
        send_stock_duplicate_warning = session.pop("send_stock_duplicate_warning", None)
        return render_template(
            "user_detail.html",
            user=user,
            stats=stats,
            orders=orders,
            orders_page=orders_page,
            orders_total_pages=orders_total_pages,
            orders_total=orders_total,
            recent_replacements=recent_replacements,
            replacements_page=replacements_page,
            replacements_total_pages=replacements_total_pages,
            replacements_total=replacements_total,
            logs=logs,
            wallet_page=wallet_page,
            wallet_logs_total_pages=wallet_logs_total_pages,
            wallet_logs_total=wallet_logs_total,
            user_detail_page_size=per_page,
            products_for_send=products_for_send,
            price_groups=PRICE_GROUPS,
            user_price_overrides=user_price_overrides,
            user_language=user_language,
            language_settings=get_language_settings(app.db),
            language_names=LANGUAGE_NAMES,
            send_stock_duplicate_warning=send_stock_duplicate_warning,
        )



    @app.post("/users/<int:user_id>/pricing")
    @login_required
    @owner_required
    @csrf_required
    def update_user_pricing(user_id: int):
        user = app.db.users.find_one({"user_id": user_id})
        if not user:
            flash("User not found.", "error")
            return redirect(url_for("users"))
        action = str(request.form.get("pricing_action") or "group").strip().lower()
        user_label = telegram_user_display(app.db, user_id, user.get("username"))
        if action == "group":
            group_key = normalize_price_group(request.form.get("pricing_group"))
            app.db.users.update_one(
                {"user_id": user_id},
                {"$set": {"pricing_group": group_key, "pricing_group_updated_at": utcnow()}},
            )
            log_admin_action(app.db, "user_pricing_group_updated", f"user={user_label} group={group_key}")
            flash(f"Pricing group updated to {price_group_label(group_key)} for {user_label}.", "success")
            return redirect(url_for("user_detail", user_id=user_id))

        if action == "clear_override":
            product_name = str(request.form.get("product_name") or "").strip()
            key = product_name_key(request.form.get("product_key") or product_name)
            if not key:
                flash("Select a product override to clear.", "error")
                return redirect(url_for("user_detail", user_id=user_id))
            app.db.user_product_prices.delete_one({"user_id": user_id, "product_key": key})
            log_admin_action(app.db, "user_custom_price_cleared", f"user={user_label} product_key={key}")
            flash("Custom user price cleared.", "success")
            return redirect(url_for("user_detail", user_id=user_id))

        if action == "override":
            product_name = str(request.form.get("product_name") or "").strip()
            product = app.db.products.find_one({"name": name_regex(product_name)})
            if not product:
                flash("Select a valid product before saving a custom price.", "error")
                return redirect(url_for("user_detail", user_id=user_id))
            inr_raw = str(request.form.get("price_inr", "") or "").strip()
            usdt_raw = str(request.form.get("price_usdt", "") or "").strip()
            fields: dict[str, Any] = {
                "user_id": user_id,
                "product_key": product_name_key(product.get("name")),
                "product_name": product.get("name"),
                "updated_at": utcnow(),
            }
            if inr_raw:
                price_inr = parse_price_for_currency(inr_raw, "custom INR", True)
                if price_inr is None:
                    return redirect(url_for("user_detail", user_id=user_id))
                fields["price_inr"] = price_inr
            if usdt_raw:
                price_usdt = parse_price_for_currency(usdt_raw, "custom USDT", True)
                if price_usdt is None:
                    return redirect(url_for("user_detail", user_id=user_id))
                fields["price_usdt"] = price_usdt
            if "price_inr" not in fields and "price_usdt" not in fields:
                flash("Enter at least one custom price, or clear an existing override.", "error")
                return redirect(url_for("user_detail", user_id=user_id))
            app.db.user_product_prices.update_one(
                {"user_id": user_id, "product_key": fields["product_key"]},
                {"$set": fields, "$setOnInsert": {"created_at": utcnow()}},
                upsert=True,
            )
            log_admin_action(app.db, "user_custom_price_updated", f"user={user_label} product={product.get('name')}")
            flash(f"Custom price saved for {user_label}.", "success")
            return redirect(url_for("user_detail", user_id=user_id))

        flash("Unknown pricing action.", "error")
        return redirect(url_for("user_detail", user_id=user_id))

    def create_admin_wallet_adjustment_log(user: dict, action: str, currency: str, amount: float, balance_after: float, note: str = "") -> str:
        """Store manual admin wallet credits/removals in the same wallet history used by users and WebAdmin."""
        now = utcnow()
        normalized_action = "remove" if action == "remove" else "add"
        normalized_currency = "usdt" if currency == "usdt" else "inr"
        decimals = 2
        amount = round(float(amount or 0), decimals)
        user_id_value = int(user.get("user_id", 0) or 0)
        ref_id = ""
        for _ in range(5):
            candidate = f"admin_wallet_{uuid.uuid4().hex[:8].upper()}"
            if not app.db.pending_payments.find_one({"ref_id": candidate}):
                ref_id = candidate
                break
        if not ref_id:
            ref_id = f"admin_wallet_{uuid.uuid4().hex.upper()}"

        doc = {
            "ref_id": ref_id,
            "user_id": user_id_value,
            "username": user.get("username", ""),
            "pay_type": "wallet",
            "method": f"admin_{normalized_action}",
            "currency": normalized_currency,
            "load_amount": amount,
            "expected_inr": amount if normalized_currency == "inr" else 0.0,
            "expected_usdt": amount if normalized_currency == "usdt" else 0.0,
            "unique_usdt": 0.0,
            "status": "completed",
            "created_at": now,
            "completed_at": now,
            "reviewed_at": now,
            "admin_wallet_adjustment": True,
            "admin_wallet_action": normalized_action,
            "wallet_balance_after": round(float(balance_after or 0), decimals),
            "notes": str(note or "").strip(),
            "admin_username": current_admin_username(),
            "admin_role": current_admin_role(),
            "effective_price_group": priced_product.get("effective_price_group"),
            "effective_price_source": priced_product.get("effective_price_source"),
        }
        doc["wallet_adjusted_at"] = now
        if normalized_action == "add":
            doc["wallet_credited_at"] = now
        else:
            doc["wallet_removed_at"] = now
        try:
            app.db.pending_payments.insert_one(doc)
            return ref_id
        except Exception as exc:
            current_app.logger.warning("Could not save admin wallet adjustment history for user %s: %s", user_id_value, exc)
            return ""

    @app.post("/users/<int:user_id>/balance")
    @login_required
    @csrf_required
    def adjust_balance(user_id: int):
        action = request.form.get("action")
        currency = request.form.get("currency", "").lower()
        amount = parse_float(request.form.get("amount"), "Amount")
        note = request.form.get("note", "").strip()
        user = app.db.users.find_one({"user_id": user_id})
        if not user:
            flash("User not found. Ask them to press /start first.", "error")
            return redirect(url_for("users"))
        active_currencies = get_active_payment_currencies(app.db)
        if currency not in {"inr", "usdt"} or amount is None or amount <= 0:
            flash("Currency must be an active wallet currency and amount must be greater than zero.", "error")
            return redirect(url_for("user_detail", user_id=user_id))
        if currency not in active_currencies:
            flash("That wallet currency is disabled in Payment Settings. Enable its payment method before adjusting that balance.", "error")
            return redirect(url_for("user_detail", user_id=user_id))
        field = "wallet_inr" if currency == "inr" else "wallet_usdt"
        decimals = 2
        amount = round(amount, decimals)
        if action == "remove" and float(user.get(field, 0) or 0) < amount:
            flash("Cannot remove more than the current wallet balance.", "error")
            return redirect(url_for("user_detail", user_id=user_id))
        delta = amount if action == "add" else -amount
        app.db.users.update_one({"user_id": user_id}, {"$inc": {field: delta}})
        updated = app.db.users.find_one({"user_id": user_id}) or {}
        new_balance = float(updated.get(field, 0) or 0)
        label = "INR" if currency == "inr" else "USDT"
        amount_text = money_inr(amount) if currency == "inr" else money_usdt(amount)
        balance_text = money_inr(new_balance) if currency == "inr" else money_usdt(new_balance)
        verb = "added to" if action == "add" else "removed from"
        user_label = telegram_user_display(app.db, user_id, user.get("username"))
        wallet_log_ref = create_admin_wallet_adjustment_log(user, action or "add", currency, amount, new_balance, note)
        log_details = f"user={user_label} {label} {action} {amount_text}"
        if wallet_log_ref:
            log_details += f" ref={wallet_log_ref}"
        log_admin_action(app.db, "wallet_adjusted", log_details)
        history_text = f" Wallet history ref: {wallet_log_ref}." if wallet_log_ref else " Wallet changed, but the history log could not be saved."
        flash(f"{amount_text} {verb} user {user_label}. New {label} balance: {balance_text}.{history_text}", "success" if wallet_log_ref else "warning")
        notify_user_balance_adjustment(user_id, action or "add", amount_text, balance_text, label, note)
        return redirect(url_for("user_detail", user_id=user_id))

    def create_admin_stock_order(user: dict, product: dict, items: list[str], source: str = "manual", admin_note: str = "") -> tuple[dict | None, str | None]:
        """Create a delivered order for stock sent from WebAdmin.

        Manual sends use the stock pasted by the admin. Inventory sends use items
        already removed from product stock before this order is created. In both
        cases this creates normal delivered order history for the selected user.
        """
        now = utcnow()
        user_id_value = int(user.get("user_id", 0) or 0)
        username = str(user.get("username") or "").strip().lstrip("@")
        product_name = str(product.get("name") or "").strip()
        quantity = len(items)
        clean_note = str(admin_note or "").strip()[:1000]
        priced_product = effective_product_for_user(app.db, product, user) or product
        display_amount_usdt = round(max(0.0, safe_float(priced_product.get("price_usdt"), 0.0)) * quantity, 2)
        order_base = {
            "user_id": user_id_value,
            "username": username,
            "product_name": product_name,
            "quantity": quantity,
            "items": items,
            "payment_method": "admin_stock",
            "amount_inr": 0.0,
            "amount_usdt": display_amount_usdt,
            "admin_stock_value_usdt": display_amount_usdt,
            "status": "delivered",
            "created_at": now,
            "delivered_at": now,
            "admin_stock_delivery": True,
            "admin_stock_source": "inventory" if str(source or "").strip().lower() == "inventory" else "manual",
            "admin_stock_note": clean_note,
            "admin_username": current_admin_username(),
            "admin_role": current_admin_role(),
            "effective_price_group": priced_product.get("effective_price_group"),
            "effective_price_source": priced_product.get("effective_price_source"),
        }
        for attempt in range(25):
            order_id_len = 8 if attempt < 20 else 12
            order_id = uuid.uuid4().hex[:order_id_len].upper()
            if app.db.orders.find_one({"order_id": order_id}, {"_id": 1}):
                continue
            order_doc = {**order_base, "order_id": order_id}
            try:
                app.db.orders.insert_one(order_doc)
                return order_doc, None
            except DuplicateKeyError:
                continue
            except Exception as exc:
                return None, str(exc)
        return None, "Could not generate a unique order ID. Please try again."

    def create_admin_created_order(user: dict, product: dict, quantity: int, admin_note: str = "") -> tuple[dict | None, str | None]:
        """Create a normal user order from WebAdmin.

        The order is treated as already approved by admin. After creation the
        normal delivery flow decides whether it can be delivered immediately or
        must wait in the paid pending-stock queue. This keeps older paid
        pending-stock orders ahead of newly-created admin orders.
        """
        now = utcnow()
        user_id_value = int(user.get("user_id", 0) or 0)
        username = str(user.get("username") or "").strip().lstrip("@")
        product_name = str(product.get("name") or "").strip()
        try:
            quantity_value = max(1, int(quantity or 1))
        except Exception:
            quantity_value = 1
        clean_note = str(admin_note or "").strip()[:1000]
        priced_product = effective_product_for_user(app.db, product, user) or product
        amount_usdt = round(max(0.0, safe_float(priced_product.get("price_usdt"), 0.0)) * quantity_value, 2)
        amount_inr = round(max(0.0, safe_float(priced_product.get("price_inr"), 0.0)) * quantity_value, 2)
        order_base = {
            "user_id": user_id_value,
            "username": username,
            "product_name": product_name,
            "quantity": quantity_value,
            "items": [],
            "payment_method": "admin_created_order",
            "amount_inr": amount_inr,
            "amount_usdt": amount_usdt,
            "status": "pending",
            "created_at": now,
            "paid_at": now,
            "payment_verified_at": now,
            "admin_created_order": True,
            "admin_created_order_at": now,
            "admin_created_order_note": clean_note,
            "admin_username": current_admin_username(),
            "admin_role": current_admin_role(),
        }
        for attempt in range(25):
            order_id_len = 8 if attempt < 20 else 12
            order_id = uuid.uuid4().hex[:order_id_len].upper()
            if app.db.orders.find_one({"order_id": order_id}, {"_id": 1}):
                continue
            order_doc = {**order_base, "order_id": order_id}
            try:
                app.db.orders.insert_one(order_doc)
                return order_doc, None
            except DuplicateKeyError:
                continue
            except Exception as exc:
                return None, str(exc)
        return None, "Could not generate a unique order ID. Please try again."

    def create_manual_replacement_order(user: dict, product: dict, items: list[str], source: str = "manual", admin_note: str = "") -> tuple[dict | None, str | None]:
        """Create a delivered replacement order from WebAdmin without a bot report.

        This is for cases where the customer reported an issue outside the bot.
        The replacement is saved with normal replacement history so the user can
        see it under /replacements and fetch it again with /getorder.
        """
        now = utcnow()
        user_id_value = int(user.get("user_id", 0) or 0)
        username = str(user.get("username") or "").strip().lstrip("@")
        product_name = str(product.get("name") or "").strip()
        quantity = len(items)
        clean_note = str(admin_note or "").strip()[:1000]
        order_base = {
            "user_id": user_id_value,
            "username": username,
            "product_name": product_name,
            "quantity": quantity,
            "items": items,
            "payment_method": "replacement",
            "amount_inr": 0.0,
            "amount_usdt": 0.0,
            "status": "delivered",
            "created_at": now,
            "delivered_at": now,
            "is_replacement": True,
            "manual_replacement_delivery": True,
            "manual_replacement_source": "inventory" if str(source or "").strip().lower() == "inventory" else "manual",
            "replacement_sent_by": current_admin_username() or "owner",
            "replacement_admin_note": clean_note,
            "original_order_id": "Manual Telegram report",
            "original_order_ids": [],
            "admin_username": current_admin_username(),
            "admin_role": current_admin_role(),
        }
        for attempt in range(25):
            order_id_len = 8 if attempt < 20 else 12
            order_id = uuid.uuid4().hex[:order_id_len].upper()
            if app.db.orders.find_one({"order_id": order_id}, {"_id": 1}):
                continue
            order_doc = {
                **order_base,
                "order_id": order_id,
                "replacement_report_id": f"MANUAL-{order_id}",
            }
            try:
                app.db.orders.insert_one(order_doc)
                return order_doc, None
            except DuplicateKeyError:
                continue
            except Exception as exc:
                return None, str(exc)
        return None, "Could not generate a unique replacement ID. Please try again."

    def create_transferred_delivery_order(original_order: dict, target_user: dict, admin_note: str = "") -> tuple[dict | None, str | None]:
        """Create a new delivered history row for stock transferred from a revoked delivery.

        This intentionally bypasses normal duplicate-stock blocking only for this
        one exact revoked order. The old order stays revoked for audit/history,
        while the new order is the only one the correct user can fetch with
        /getorder.
        """
        now = utcnow()
        original_id = str(original_order.get("order_id") or "").strip().upper()
        items = [str(item).strip() for item in (original_order.get("items") or []) if str(item).strip()]
        if not original_id or not items:
            return None, "Original delivery has no saved stock/items to transfer."

        target_user_id = int(target_user.get("user_id", 0) or 0)
        target_username = str(target_user.get("username") or "").strip().lstrip("@")
        product_name = str(original_order.get("product_name") or "").strip()
        quantity = len(items)
        transferred_by = current_admin_username() or current_admin_role() or "owner"
        clean_note = str(admin_note or "").strip()[:1000]
        is_replacement = bool(original_order.get("is_replacement"))
        common_transfer_fields = {
            "transferred_from_order_id": original_id,
            "delivery_transfer": True,
            "delivery_transferred_at": now,
            "delivery_transferred_by": transferred_by,
            "delivery_transferred_from_user_id": original_order.get("user_id"),
            "delivery_transferred_from_username": str(original_order.get("username") or "").strip().lstrip("@"),
            "delivery_transfer_note": clean_note,
            "admin_username": current_admin_username(),
            "admin_role": current_admin_role(),
        }

        if is_replacement:
            original_ids = []
            for value in original_order.get("original_order_ids") or []:
                value = str(value or "").strip()
                if value and value not in original_ids:
                    original_ids.append(value)
            original_order_id = str(original_order.get("original_order_id") or "").strip()
            if original_order_id and original_order_id not in original_ids:
                original_ids.append(original_order_id)
            if original_id and original_id not in original_ids:
                original_ids.append(original_id)

            order_base = {
                "user_id": target_user_id,
                "username": target_username,
                "product_name": product_name,
                "quantity": quantity,
                "items": items,
                "payment_method": "replacement",
                "amount_inr": 0.0,
                "amount_usdt": 0.0,
                "status": "delivered",
                "created_at": now,
                "delivered_at": now,
                "is_replacement": True,
                "manual_replacement_delivery": True,
                "manual_replacement_source": "transfer",
                "replacement_sent_by": transferred_by,
                "replacement_admin_note": "",
                "original_order_id": original_order_id or f"Transferred from {original_id}",
                "original_order_ids": original_ids,
                **common_transfer_fields,
            }
        else:
            product = app.db.products.find_one({"name": name_regex(product_name)}) if product_name else None
            display_amount_usdt = safe_float(original_order.get("admin_stock_value_usdt"), 0.0)
            if display_amount_usdt <= 0:
                display_amount_usdt = safe_float(original_order.get("amount_usdt"), 0.0)
            if display_amount_usdt <= 0 and product:
                display_amount_usdt = max(0.0, safe_float(product.get("price_usdt"), 0.0)) * quantity
            display_amount_usdt = round(max(0.0, display_amount_usdt), 2)
            order_base = {
                "user_id": target_user_id,
                "username": target_username,
                "product_name": product_name,
                "quantity": quantity,
                "items": items,
                "payment_method": "admin_stock",
                "amount_inr": 0.0,
                "amount_usdt": display_amount_usdt,
                "admin_stock_value_usdt": display_amount_usdt,
                "status": "delivered",
                "created_at": now,
                "delivered_at": now,
                "admin_stock_delivery": True,
                "admin_stock_source": "transfer",
                "admin_stock_note": "",
                **common_transfer_fields,
            }

        for attempt in range(25):
            order_id_len = 8 if attempt < 20 else 12
            order_id = uuid.uuid4().hex[:order_id_len].upper()
            if app.db.orders.find_one({"order_id": order_id}, {"_id": 1}):
                continue
            order_doc = {**order_base, "order_id": order_id}
            if is_replacement:
                order_doc["replacement_report_id"] = f"TRANSFER-{order_id}"
            try:
                app.db.orders.insert_one(order_doc)
                return order_doc, None
            except DuplicateKeyError:
                continue
            except Exception as exc:
                return None, str(exc)
        label = "replacement ID" if is_replacement else "order ID"
        return None, f"Could not generate a unique transfer {label}. Please try again."

    def build_stock_duplicate_report(product_name: str, items: list[str], *, existing_stock: list[str] | None = None) -> dict[str, Any]:
        """Find duplicate/already-used stock before admin sends it to a user.

        This protects against accidentally delivering the same account/code twice,
        including stock already delivered by normal paid orders, replacements, or
        previous admin sends. It also protects manual sends from duplicating items
        still sitting in product stock, because those could later be sold again.
        """
        normalized_items = [str(item).strip() for item in (items or []) if str(item).strip()]
        seen: set[str] = set()
        duplicate_in_send: list[str] = []
        for item in normalized_items:
            if item in seen and item not in duplicate_in_send:
                duplicate_in_send.append(item)
            seen.add(item)

        item_set = set(normalized_items)
        existing_set = {str(item).strip() for item in (existing_stock or []) if str(item).strip()}
        duplicate_in_current_stock = sorted(item_set & existing_set)

        already_delivered = get_used_stock_items(app.db, product_name)
        delivered_set = {str(item).strip() for item in already_delivered if str(item).strip()}
        duplicate_already_sent = sorted(item_set & delivered_set)

        return {
            "duplicate_in_send": duplicate_in_send,
            "duplicate_in_current_stock": duplicate_in_current_stock,
            "duplicate_already_sent": duplicate_already_sent,
        }

    def has_stock_duplicate_report(report: dict[str, Any]) -> bool:
        return any(report.get(key) for key in ("duplicate_in_send", "duplicate_in_current_stock", "duplicate_already_sent"))

    def queue_stock_duplicate_warning(product_name: str, report: dict[str, Any], *, source: str, flow: str = "stock") -> None:
        duplicates: list[str] = []
        for key in ("duplicate_in_send", "duplicate_in_current_stock", "duplicate_already_sent"):
            for item in report.get(key, []) or []:
                clean = str(item).strip()
                if clean and clean not in duplicates:
                    duplicates.append(clean)

        is_replacement_flow = flow == "replacement"
        is_manual_source = source != "inventory"
        title = "Send replacement?" if is_replacement_flow else "Send stock?"
        if is_manual_source:
            entry_label = "manual replacement entry" if is_replacement_flow else "manual entry"
        else:
            entry_label = "selected product stock"
        message = (
            f"Duplicate stock detected in the {entry_label} ({len(duplicates)} duplicate item(s)). "
            "Nothing will be sent. Remove every duplicate block shown below and try again."
        )
        session["send_stock_duplicate_warning"] = {
            "title": title,
            "message": message,
            "product_name": product_name,
            "source": "product stock" if source == "inventory" else "manual entry",
            "duplicate_in_send": len(report.get("duplicate_in_send", []) or []),
            "duplicate_in_current_stock": len(report.get("duplicate_in_current_stock", []) or []),
            "duplicate_already_sent": len(report.get("duplicate_already_sent", []) or []),
            "duplicates": duplicates,
            # Keep this for compatibility with any old template/session state.
            "sample": duplicates,
        }

    @app.post("/users/<int:user_id>/create-order")
    @login_required
    @csrf_required
    def create_order_for_user(user_id: int):
        user = app.db.users.find_one({"user_id": user_id})
        if not user:
            flash("User not found. Ask them to press /start first.", "error")
            return redirect(url_for("users"))
        if user.get("blocked"):
            flash("This user is blocked. Unblock them before creating an order.", "error")
            return redirect(url_for("user_detail", user_id=user_id))

        product_name = (request.form.get("product_name", "") or "").strip()
        admin_note = str(request.form.get("order_note", "") or "").strip()[:1000]
        try:
            quantity = int(str(request.form.get("order_quantity", "") or "").strip())
        except (TypeError, ValueError):
            quantity = 0
        if not product_name:
            flash("Select a product before creating an order.", "error")
            return redirect(url_for("user_detail", user_id=user_id))
        if quantity < 1:
            flash("Enter an order quantity of at least 1.", "error")
            return redirect(url_for("user_detail", user_id=user_id))
        if quantity > 1000:
            flash("Too many items in one order. Create 1000 items or fewer at a time.", "error")
            return redirect(url_for("user_detail", user_id=user_id))

        product = app.db.products.find_one({"name": name_regex(product_name)})
        if not product:
            flash("Selected product was not found.", "error")
            return redirect(url_for("user_detail", user_id=user_id))

        user_label = telegram_user_display(app.db, user_id, user.get("username"))
        preorder_lock_token = acquire_product_preorder_lock(app.db, product["name"])
        if not preorder_lock_token:
            flash("This product is busy processing another preorder/order right now. Please try again in a few seconds.", "warning")
            return redirect(url_for("user_detail", user_id=user_id))

        try:
            # First clear any older paid pending-stock orders if stock already exists.
            # New admin-created orders must never jump ahead of the existing queue.
            # Admin-created orders intentionally bypass preorder limits, but this
            # lock makes their quantity visible to simultaneous user preorders.
            process_pending_stock_orders(app.db, product["name"])

            order, err = create_admin_created_order(user, product, quantity, admin_note=admin_note)
            if not order:
                flash(f"Could not create order for {user_label}: {err or 'unknown error'}", "error")
                return redirect(url_for("user_detail", user_id=user_id))

            complete_order(app.db, user_id, order["order_id"])
            updated_order = app.db.orders.find_one({"order_id": order["order_id"]}) or order
            status = str(updated_order.get("status") or "").lower()
            log_admin_action(
                app.db,
                "admin_order_created",
                f"user={user_label} order={order['order_id']} product={product.get('name')} quantity={quantity} status={status}",
            )
            if status == "delivered":
                flash(f"Created and delivered order {order['order_id']} for {user_label}.", "success")
            elif status == "pending_stock":
                flash(f"Created order {order['order_id']} for {user_label} and added it to Pending Stock Orders.", "warning")
            else:
                flash(f"Created order {order['order_id']} for {user_label}. Current status: {status_label(status)}.", "success")
        finally:
            release_product_preorder_lock(app.db, product["name"], preorder_lock_token)
        return redirect(url_for("user_detail", user_id=user_id))

    @app.post("/users/<int:user_id>/send-stock")
    @login_required
    @csrf_required
    def send_stock_to_user(user_id: int):
        user = app.db.users.find_one({"user_id": user_id})
        if not user:
            flash("User not found. Ask them to press /start first.", "error")
            return redirect(url_for("users"))
        if user.get("blocked") and user.get("blocked_manually"):
            flash("This user is manually blocked. Unblock them before sending stock.", "error")
            return redirect(url_for("user_detail", user_id=user_id))

        product_name = (request.form.get("product_name", "") or "").strip()
        admin_note = str(request.form.get("stock_note", "") or "").strip()[:1000]
        source = (request.form.get("stock_source") or "manual").strip().lower()
        if source not in {"inventory", "manual"}:
            source = "manual"
        if not product_name:
            flash("Select a product before sending stock.", "error")
            return redirect(url_for("user_detail", user_id=user_id))
        product = app.db.products.find_one({"name": name_regex(product_name)})
        if not product:
            flash("Selected product was not found.", "error")
            return redirect(url_for("user_detail", user_id=user_id))

        if source == "inventory":
            try:
                quantity = int(str(request.form.get("stock_quantity", "")).strip())
            except (TypeError, ValueError):
                quantity = 0
            if quantity < 1:
                flash("Enter how many stock item(s) to send from product inventory.", "error")
                return redirect(url_for("user_detail", user_id=user_id))
            if quantity > 1000:
                flash("Too many stock items in one send. Send 1000 items or fewer at a time.", "error")
                return redirect(url_for("user_detail", user_id=user_id))
            total_stock = get_stock_count(app.db, product["name"])
            if total_stock < quantity:
                flash(f"Not enough product stock for {product.get('name')}. In stock: {total_stock}. Pending paid orders still remain queued.", "error")
                return redirect(url_for("user_detail", user_id=user_id))

            product_stock = [str(item).strip() for item in (product.get("stock", []) or []) if str(item).strip()]
            candidate_items = product_stock[:quantity]
            if len(candidate_items) != quantity:
                flash("Stock changed before it could be sent. Please refresh the user page and try again.", "error")
                return redirect(url_for("user_detail", user_id=user_id))
            other_stock = product_stock[quantity:]
            duplicate_report = build_stock_duplicate_report(product["name"], candidate_items, existing_stock=other_stock)
            if has_stock_duplicate_report(duplicate_report):
                queue_stock_duplicate_warning(product["name"], duplicate_report, source=source)
                flash("Duplicate stock detected. Nothing was sent. Review the popup warning and clean the stock before trying again.", "error")
                return redirect(url_for("user_detail", user_id=user_id))

            items = pop_available_stock_for_admin_send(app.db, product["name"], quantity, reserve_pending=False)
            if len(items) != quantity:
                flash("Stock changed before it could be sent. Please refresh the user page and try again.", "error")
                return redirect(url_for("user_detail", user_id=user_id))
            duplicate_report = build_stock_duplicate_report(product["name"], items, existing_stock=[])
            if has_stock_duplicate_report(duplicate_report):
                # Race-safety fallback: put the removed items back at the front of available stock.
                restore_records = build_stock_added_by_records(
                    items,
                    username=current_admin_username() or "owner",
                    role=current_admin_role(),
                    manager_earning_rate_usdt=0.0,
                    owner_due_rate_usdt=0.0,
                    stock_upload_kind="admin_send_duplicate_rollback",
                )
                app.db.products.update_one(
                    {"_id": product["_id"]},
                    {"$push": {"stock": {"$each": items, "$position": 0}, "stock_added_by": {"$each": restore_records}}},
                )
                record_stock_ledger_add(
                    app.db,
                    product["name"],
                    items,
                    username=current_admin_username() or "owner",
                    role=current_admin_role(),
                    source="webadmin",
                    stock_upload_kind="admin_send_duplicate_rollback",
                    note="Restored after duplicate check rollback",
                )
                queue_stock_duplicate_warning(product["name"], duplicate_report, source=source)
                flash("Duplicate stock detected after stock changed. Nothing was sent. Please try again after cleaning the stock.", "error")
                return redirect(url_for("user_detail", user_id=user_id))
        else:
            raw_stock = request.form.get("stock_items", "") or ""
            items = split_stock(raw_stock)
            if not items:
                flash("Enter at least one stock item/account to send, or choose Send from product stock.", "error")
                return redirect(url_for("user_detail", user_id=user_id))
            if len(items) > 1000:
                flash("Too many stock items in one send. Send 1000 items or fewer at a time.", "error")
                return redirect(url_for("user_detail", user_id=user_id))
            duplicate_report = build_stock_duplicate_report(product["name"], items, existing_stock=product.get("stock", []) or [])
            if has_stock_duplicate_report(duplicate_report):
                queue_stock_duplicate_warning(product["name"], duplicate_report, source=source)
                flash("Duplicate stock detected. Nothing was sent. Review the popup warning and enter only fresh stock.", "error")
                return redirect(url_for("user_detail", user_id=user_id))

        order, err = create_admin_stock_order(user, product, items, source=source, admin_note=admin_note)
        user_label = telegram_user_display(app.db, user_id, user.get("username"))
        if not order:
            flash(f"Could not create order history for {user_label}: {err or 'unknown error'}", "error")
            return redirect(url_for("user_detail", user_id=user_id))

        if source == "manual":
            record_stock_ledger_add(
                app.db,
                product["name"],
                items,
                username=current_admin_username() or "owner",
                role=current_admin_role(),
                source="webadmin",
                stock_upload_kind="manual_admin_send",
                note=f"Sent directly to user as {order['order_id']}",
            )
        record_order_items_delivered_in_ledger(app.db, order, items, source="webadmin_admin_stock")
        lang = get_user_language_sync(app.db, user_id)
        filename = delivery_txt_filename(order)
        sent = send_telegram_document(
            user_id,
            filename,
            delivery_txt_content(order, items, lang=lang),
            caption=delivery_caption(order, lang=lang),
        )
        if sent:
            mark_user_delivery_success(app.db, user_id, source="admin_stock_document")
            record_order_delivery_message(
                app.db,
                order["order_id"],
                user_id,
                sent,
                filename=filename,
                sent_by="webadmin_admin_stock",
                resent=False,
            )
        app.db.orders.update_one(
            {"order_id": order["order_id"]},
            {"$set": {"admin_stock_sent_at": utcnow(), "admin_stock_telegram_sent": bool(sent)}},
        )
        log_admin_action(
            app.db,
            "admin_stock_sent",
            f"user={user_label} order={order['order_id']} product={product.get('name')} quantity={len(items)} source={source} sent={bool(sent)}",
        )
        if sent:
            flash(f"Sent {len(items)} item(s) of {product.get('name')} to {user_label}. Order {order['order_id']} was added to their history.", "success")
        else:
            flash(f"Order {order['order_id']} was added to {user_label}'s history, but Telegram delivery failed. Check bot token/user chat and use Resend from the order page.", "warning")
        return redirect(url_for("user_detail", user_id=user_id))

    @app.post("/users/<int:user_id>/send-replacement")
    @login_required
    @csrf_required
    def send_replacement_to_user(user_id: int):
        user = app.db.users.find_one({"user_id": user_id})
        if not user:
            flash("User not found. Ask them to press /start first.", "error")
            return redirect(url_for("users"))
        if user.get("blocked") and user.get("blocked_manually"):
            flash("This user is manually blocked. Unblock them before sending a replacement.", "error")
            return redirect(url_for("user_detail", user_id=user_id))

        product_name = (request.form.get("product_name", "") or "").strip()
        source = (request.form.get("replacement_source") or "manual").strip().lower()
        if source not in {"inventory", "manual"}:
            source = "manual"
        admin_note = str(request.form.get("replacement_note", "") or "").strip()
        if not product_name:
            flash("Select a product before sending a replacement.", "error")
            return redirect(url_for("user_detail", user_id=user_id))
        product = app.db.products.find_one({"name": name_regex(product_name)})
        if not product:
            flash("Selected product was not found.", "error")
            return redirect(url_for("user_detail", user_id=user_id))

        if source == "inventory":
            try:
                quantity = int(str(request.form.get("replacement_quantity", "")).strip())
            except (TypeError, ValueError):
                quantity = 0
            if quantity < 1:
                flash("Enter how many replacement item(s) to send from product inventory.", "error")
                return redirect(url_for("user_detail", user_id=user_id))
            if quantity > 1000:
                flash("Too many replacement items in one send. Send 1000 items or fewer at a time.", "error")
                return redirect(url_for("user_detail", user_id=user_id))
            total_stock = get_stock_count(app.db, product["name"])
            if total_stock < quantity:
                flash(f"Not enough product stock for {product.get('name')}. In stock: {total_stock}. Pending paid orders still remain queued.", "error")
                return redirect(url_for("user_detail", user_id=user_id))

            product_stock = [str(item).strip() for item in (product.get("stock", []) or []) if str(item).strip()]
            candidate_items = product_stock[:quantity]
            if len(candidate_items) != quantity:
                flash("Stock changed before the replacement could be sent. Please refresh the user page and try again.", "error")
                return redirect(url_for("user_detail", user_id=user_id))
            other_stock = product_stock[quantity:]
            duplicate_report = build_stock_duplicate_report(product["name"], candidate_items, existing_stock=other_stock)
            if has_stock_duplicate_report(duplicate_report):
                queue_stock_duplicate_warning(product["name"], duplicate_report, source=source, flow="replacement")
                flash("Duplicate stock detected. Nothing was sent. Review the popup warning and clean the stock before trying again.", "error")
                return redirect(url_for("user_detail", user_id=user_id))

            items = pop_available_stock_for_admin_send(app.db, product["name"], quantity, reserve_pending=False)
            if len(items) != quantity:
                flash("Stock changed before the replacement could be sent. Please refresh the user page and try again.", "error")
                return redirect(url_for("user_detail", user_id=user_id))
            duplicate_report = build_stock_duplicate_report(product["name"], items, existing_stock=[])
            if has_stock_duplicate_report(duplicate_report):
                restore_records = build_stock_added_by_records(
                    items,
                    username=current_admin_username() or "owner",
                    role=current_admin_role(),
                    manager_earning_rate_usdt=0.0,
                    owner_due_rate_usdt=0.0,
                    stock_upload_kind="manual_replacement_duplicate_rollback",
                )
                app.db.products.update_one(
                    {"_id": product["_id"]},
                    {"$push": {"stock": {"$each": items, "$position": 0}, "stock_added_by": {"$each": restore_records}}},
                )
                record_stock_ledger_add(
                    app.db,
                    product["name"],
                    items,
                    username=current_admin_username() or "owner",
                    role=current_admin_role(),
                    source="webadmin",
                    stock_upload_kind="manual_replacement_duplicate_rollback",
                    note="Restored after duplicate check rollback",
                )
                queue_stock_duplicate_warning(product["name"], duplicate_report, source=source, flow="replacement")
                flash("Duplicate stock detected after stock changed. Nothing was sent. Please try again after cleaning the stock.", "error")
                return redirect(url_for("user_detail", user_id=user_id))
        else:
            raw_stock = request.form.get("replacement_items", "") or ""
            items = split_stock(raw_stock)
            if not items:
                flash("Enter at least one replacement item/account, or choose Send from product stock.", "error")
                return redirect(url_for("user_detail", user_id=user_id))
            if len(items) > 1000:
                flash("Too many replacement items in one send. Send 1000 items or fewer at a time.", "error")
                return redirect(url_for("user_detail", user_id=user_id))
            duplicate_report = build_stock_duplicate_report(product["name"], items, existing_stock=product.get("stock", []) or [])
            if has_stock_duplicate_report(duplicate_report):
                queue_stock_duplicate_warning(product["name"], duplicate_report, source=source, flow="replacement")
                flash("Duplicate stock detected. Nothing was sent. Review the popup warning and enter only fresh replacement stock.", "error")
                return redirect(url_for("user_detail", user_id=user_id))

        order, err = create_manual_replacement_order(user, product, items, source=source, admin_note=admin_note)
        user_label = telegram_user_display(app.db, user_id, user.get("username"))
        if not order:
            flash(f"Could not create replacement history for {user_label}: {err or 'unknown error'}", "error")
            return redirect(url_for("user_detail", user_id=user_id))

        if source == "manual":
            record_stock_ledger_add(
                app.db,
                product["name"],
                items,
                username=current_admin_username() or "owner",
                role=current_admin_role(),
                source="webadmin",
                stock_upload_kind="manual_replacement_send",
                note=f"Sent directly as replacement {order['order_id']}",
            )
        record_order_items_delivered_in_ledger(app.db, order, items, source="webadmin_manual_replacement")
        lang = get_user_language_sync(app.db, user_id)
        filename = delivery_txt_filename(order)
        sent = send_telegram_document(
            user_id,
            filename,
            delivery_txt_content(order, items, lang=lang),
            caption=delivery_caption(order, lang=lang),
        )
        if sent:
            mark_user_delivery_success(app.db, user_id, source="manual_replacement_document")
            record_order_delivery_message(
                app.db,
                order["order_id"],
                user_id,
                sent,
                filename=filename,
                sent_by="webadmin_manual_replacement",
                resent=False,
            )
        app.db.orders.update_one(
            {"order_id": order["order_id"]},
            {"$set": {"manual_replacement_sent_at": utcnow(), "manual_replacement_telegram_sent": bool(sent)}},
        )
        log_admin_action(
            app.db,
            "manual_replacement_sent",
            f"user={user_label} order={order['order_id']} product={product.get('name')} quantity={len(items)} source={source} sent={bool(sent)}",
        )
        if sent:
            flash(f"Sent {len(items)} replacement item(s) of {product.get('name')} to {user_label}. Replacement {order['order_id']} was added to their history.", "success")
        else:
            flash(f"Replacement {order['order_id']} was added to {user_label}'s history, but Telegram delivery failed. Check bot token/user chat and use Resend from the replacement order page.", "warning")
        return redirect(url_for("user_detail", user_id=user_id))

    @app.post("/users/<int:user_id>/message")
    @login_required
    @csrf_required
    def message_user(user_id: int):
        user = app.db.users.find_one({"user_id": user_id})
        if not user:
            flash("User not found. Ask them to press /start first.", "error")
            return redirect(url_for("users"))
        if user.get("blocked") and user.get("blocked_manually"):
            flash("This user is manually blocked. Unblock them before sending a message.", "error")
            return redirect(url_for("user_detail", user_id=user_id))
        lang = lang_from_user(user)
        normalized_lang = normalize_lang(lang)
        message = (request.form.get("message", "") or "").strip()
        if not message:
            # Backward-compatible fallback for older forms/bookmarks.
            if normalized_lang == "es":
                message = (request.form.get("message_es", "") or "").strip()
            else:
                message = (request.form.get("message_en", "") or "").strip()
        if not message:
            label = LANGUAGE_NAMES.get(normalized_lang, "English")
            flash(f"{label} message cannot be empty.", "error")
            return redirect(url_for("user_detail", user_id=user_id))
        if get_setting(app.db, "maintenance_mode", False) and not is_admin_user_id(app.db, user_id):
            log_admin_action(app.db, "direct_message_skipped_maintenance", f"user={telegram_user_display(app.db, user_id, user.get('username'))}")
            flash("Maintenance mode is ON. Direct user message skipped for this normal user and not queued.", "info")
            return redirect(url_for("user_detail", user_id=user_id))
        prefix = admin_panel_message_prefix("direct", lang)
        status = send_telegram_message_status(user_id, f"{prefix}\n\n{message}")
        ok = bool(status.get("ok"))
        if ok:
            mark_user_delivery_success(app.db, user_id, source="direct_message")
        elif status.get("blocked"):
            mark_user_delivery_failure(app.db, user_id, source="direct_message", error=str(status.get("error") or ""))
        user_label = telegram_user_display(app.db, user_id, user.get("username"))
        flash(f"Message sent to {user_label}." if ok else f"Message could not be sent to {user_label}. Check Secret Settings → Bot token and whether the user has started the bot.", "success" if ok else "error")
        return redirect(url_for("user_detail", user_id=user_id))


    @app.post("/users/<int:user_id>/block")
    @login_required
    @csrf_required
    def block_user(user_id: int):
        blocked = request.form.get("blocked") == "1"
        user = app.db.users.find_one({"user_id": user_id}) or {}
        user_label = telegram_user_display(app.db, user_id, user.get("username"))
        set_user_manual_block(app.db, user_id, blocked)
        send_telegram_message(user_id, "🚫 You have been blocked from this bot." if blocked else "✅ You have been unblocked. You can use the bot again.")
        flash(f"{user_label} blocked." if blocked else f"{user_label} unblocked.", "success")
        return redirect(request.referrer or url_for("users"))

    @app.get("/orders")
    @login_required
    def orders():
        expire_stale_unpaid_payments_and_orders(app.db)
        query: dict[str, Any] = {"is_replacement": {"$ne": True}}
        q = request.args.get("q", "").strip()
        combined_filter = request.args.get("filter", "").strip().lower()
        status = request.args.get("status", "").strip().lower()
        method = request.args.get("method", "").strip().lower()
        if combined_filter.startswith("status:"):
            status = combined_filter.split(":", 1)[1].strip()
            method = ""
        elif combined_filter.startswith("method:"):
            method = combined_filter.split(":", 1)[1].strip()
            status = ""
        if q:
            ors: list[dict] = [
                {"order_id": {"$regex": re.escape(q), "$options": "i"}},
                {"product_name": {"$regex": re.escape(q), "$options": "i"}},
            ]
            if show_order_user_identity():
                username_q = q.lstrip("@").strip()
                if username_q:
                    username_regex = {"$regex": re.escape(username_q), "$options": "i"}
                    ors.append({"username": username_regex})
                    matching_user_ids = [
                        int(user.get("user_id"))
                        for user in app.db.users.find({"username": username_regex}, {"user_id": 1})
                        if user.get("user_id") is not None
                    ]
                    if matching_user_ids:
                        ors.append({"user_id": {"$in": sorted(set(matching_user_ids))}})
                if q.isdigit():
                    ors.append({"user_id": int(q)})
            query["$or"] = ors
        valid_statuses = {"pending", "delivered", "pending_stock", "expired", "failed", "cancelled", "refund_requested", "awaiting_refund_choice", "wallet_credited", "refund_paid"}
        if status not in valid_statuses:
            status = ""
        if status:
            if status == "refund_requested":
                query["refund_status"] = "refund_requested"
            elif status == "awaiting_refund_choice":
                query["refund_status"] = "waiting_user_choice"
            elif status in {"wallet_credited", "refund_paid"}:
                query["refund_status"] = status
            else:
                query["status"] = status
        payment_settings = get_payment_settings(app.db)
        methods = sorted([
            m for m in app.db.orders.distinct("payment_method", {"is_replacement": {"$ne": True}})
            if m and payment_method_enabled(payment_settings, m)
        ])
        if method and (method not in methods or not payment_method_enabled(payment_settings, method)):
            method = ""
        if method:
            query["payment_method"] = method
        page = int_arg("page", 1, minimum=1)
        total = app.db.orders.count_documents(query)
        rows = _attach_order_display_status(
            recent_created_rows(
                app.db.orders.find(query),
                skip=(page - 1) * PAGE_SIZE,
                limit=PAGE_SIZE,
            )
        )
        total_pages = max(1, math.ceil(total / PAGE_SIZE)) if total else 1
        filter_value = f"status:{status}" if status else (f"method:{method}" if method else "")
        return render_template("orders.html", orders=rows, page=page, total_pages=total_pages, total=total, q=q, status=status, method=method, methods=methods, filter_value=filter_value)

    @app.post("/orders/expire-stale")
    @login_required
    @csrf_required
    def expire_stale_orders_now():
        result = expire_stale_unpaid_payments_and_orders(app.db)
        total_changed = int(result.get("orders_expired", 0) or 0) + int(result.get("payments_expired", 0) or 0) + int(result.get("failed_orders_marked", 0) or 0)
        if total_changed:
            flash(
                f"Cleanup complete: expired {result.get('orders_expired', 0)} order(s), expired {result.get('payments_expired', 0)} payment(s), marked {result.get('failed_orders_marked', 0)} failed.",
                "success",
            )
        else:
            flash("No overdue unpaid pending orders were found.", "info")
        return redirect(url_for("orders", status=request.args.get("status", ""), q=request.args.get("q", "")))

    @app.post("/orders/<order_id>/expire")
    @login_required
    @csrf_required
    def expire_order_now(order_id: str):
        ref_id = str(order_id or "").strip().upper()
        order_before = app.db.orders.find_one({"order_id": ref_id}) if ref_id else None
        ok, message = expire_single_pending_order(app.db, ref_id, force=True)
        if ok and order_before:
            delete_payment_message(app.db, ref_id)
            notify_order_expired(order_before, admin_expired=True)
            log_admin_action(app.db, "order_expired", f"{ref_id} for {order_before.get('user_id')}")
        flash(message, "success" if ok else "error")
        return redirect(url_for("orders", filter=request.args.get("filter", ""), q=request.args.get("q", "")))

    @app.post("/orders/<order_id>/cancel-pending-stock")
    @login_required
    @csrf_required
    def cancel_pending_stock_order(order_id: str):
        if current_admin_role() not in {ADMIN_ROLE_OWNER, ADMIN_ROLE_ORDERS_MANAGER}:
            flash("Only owner/orders admin can cancel paid pending-stock orders.", "error")
            return redirect(url_for("orders"))

        ref_id = str(order_id or "").strip().upper()
        order = app.db.orders.find_one({"order_id": ref_id}) if ref_id else None
        if not order:
            flash("Order not found.", "error")
            return redirect(url_for("orders"))
        if order.get("is_replacement"):
            flash("Replacement orders cannot be cancelled here.", "error")
            return redirect(url_for("replacement_order_detail", order_id=ref_id))
        if order.get("status") != "pending_stock":
            flash("Only Paid — Waiting for Stock orders can be cancelled here.", "error")
            return redirect(url_for("order_detail", order_id=ref_id))

        mode = str(request.form.get("cancel_mode") or "no_stock").strip().lower()
        if mode not in {"no_stock", "customer_request", "created_by_mistake", "other"}:
            mode = "other"
        note = str(request.form.get("cancel_note") or "").strip()[:1000]
        if not note:
            flash("Add a cancellation note/reason.", "error")
            return redirect(url_for("order_detail", order_id=ref_id))

        offer_wallet_credit = bool(request.form.get("offer_wallet_credit"))
        offer_refund_request = bool(request.form.get("offer_refund_request"))
        refund_currency, refund_amount = order_refund_currency_amount(order)
        has_refundable_amount = refund_currency in {"inr", "usdt"} and refund_amount > 0
        if not has_refundable_amount:
            offer_wallet_credit = False
            offer_refund_request = False
        refund_choice_enabled = bool(offer_wallet_credit or offer_refund_request)
        now = utcnow()
        cancelled_by = current_admin_username() or current_admin_role() or "owner"
        update_set = {
            "status": "cancelled",
            "cancelled_at": now,
            "cancelled_by": cancelled_by,
            "cancelled_by_role": current_admin_role(),
            "cancel_mode": mode,
            "cancel_note": note,
            "pending_stock_cancelled": True,
            "refund_currency": refund_currency,
            "refund_amount": round(refund_amount, 2),
            "refund_wallet_enabled": bool(offer_wallet_credit),
            "refund_external_enabled": bool(offer_refund_request),
            "refund_updated_at": now,
        }
        if refund_choice_enabled:
            update_set.update({
                "refund_status": "waiting_user_choice",
                "refund_choice_sent_at": now,
            })
        else:
            update_set.update({
                "refund_status": "not_eligible",
                "refund_not_eligible_reason": "no_refund_options_selected" if has_refundable_amount else "no_refundable_amount",
            })

        updated = app.db.orders.find_one_and_update(
            {"order_id": ref_id, "status": "pending_stock"},
            {
                "$set": update_set,
                "$unset": {"delivery_lock_token": "", "delivery_lock_at": ""},
            },
            return_document=ReturnDocument.AFTER,
        )
        if not updated:
            flash("Order could not be cancelled. It may already have been delivered/cancelled by another process.", "error")
            return redirect(url_for("order_detail", order_id=ref_id))

        delete_payment_message(app.db, ref_id)
        sent = False
        user_id_value = int(updated.get("user_id", 0) or 0)
        if refund_choice_enabled:
            sent = send_cancelled_order_refund_choice(user_id_value, updated, note)
        else:
            sent = send_cancelled_order_notice(user_id_value, updated, note)
        log_admin_action(app.db, "pending_stock_order_cancelled", f"{ref_id} mode={mode} refund_status={updated.get('refund_status')} wallet={bool(updated.get('refund_wallet_enabled'))} refund={bool(updated.get('refund_external_enabled'))} sent={bool(sent)} by={cancelled_by}")
        if refund_choice_enabled:
            enabled_labels = []
            if updated.get("refund_wallet_enabled"):
                enabled_labels.append("wallet credit")
            if updated.get("refund_external_enabled"):
                enabled_labels.append("refund request")
            label_text = " and ".join(enabled_labels) or "refund"
            flash(f"Order {ref_id} cancelled. User was sent {label_text} option(s)." if sent else f"Order {ref_id} cancelled, but Telegram notice could not be sent.", "success" if sent else "warning")
        else:
            flash(f"Order {ref_id} cancelled without user refund/wallet buttons." if sent else f"Order {ref_id} cancelled, but Telegram notice could not be sent.", "success" if sent else "warning")
        return redirect(url_for("order_detail", order_id=ref_id))

    @app.post("/orders/<order_id>/mark-refund-paid")
    @login_required
    @csrf_required
    def mark_order_refund_paid(order_id: str):
        if current_admin_role() not in {ADMIN_ROLE_OWNER, ADMIN_ROLE_PAYMENT_MANAGER, ADMIN_ROLE_ORDERS_MANAGER}:
            flash("You do not have permission to close refund requests.", "error")
            return redirect(url_for("orders"))
        ref_id = str(order_id or "").strip().upper()
        note = str(request.form.get("refund_paid_note") or "").strip()[:1000]
        now = utcnow()
        paid_by = current_admin_username() or current_admin_role() or "owner"
        updated = app.db.orders.find_one_and_update(
            {"order_id": ref_id, "refund_status": "refund_requested"},
            {"$set": {
                "refund_status": "refund_paid",
                "refund_paid_at": now,
                "refund_completed_at": now,
                "refund_paid_by": paid_by,
                "refund_paid_by_role": current_admin_role(),
                "refund_paid_note": note,
            }},
            return_document=ReturnDocument.AFTER,
        )
        if not updated:
            flash("Refund request not found or already closed.", "error")
            return redirect(url_for("order_detail", order_id=ref_id))
        send_refund_paid_notice(int(updated.get("user_id", 0) or 0), updated, note)
        log_admin_action(app.db, "refund_marked_paid", f"{ref_id} by={paid_by} note={note[:80]}")
        flash(f"Refund for order {ref_id} marked as paid.", "success")
        return redirect(url_for("order_detail", order_id=ref_id))

    @app.get("/orders/<order_id>")
    @login_required
    def order_detail(order_id: str):
        order = app.db.orders.find_one({"order_id": order_id.upper()})
        if not order:
            flash("Order not found.", "error")
            return redirect(url_for("orders"))
        if order.get("is_replacement"):
            return redirect(url_for("replacement_order_detail", order_id=order.get("order_id")))
        return render_template("order_detail.html", order=order)

    @app.post("/orders/<order_id>/resend")
    @login_required
    @csrf_required
    def resend_order(order_id: str):
        order = app.db.orders.find_one({"order_id": order_id.upper()})
        if not order:
            flash("Order not found.", "error")
            return redirect(url_for("orders"))
        items = order.get("items", []) or []
        next_url = str(request.form.get("next") or "").strip()
        if not (next_url.startswith("/") and not next_url.startswith("//")):
            next_url = url_for("order_detail", order_id=order.get("order_id", order_id))
        if order.get("delivery_revoked"):
            flash("This delivery was revoked. It cannot be resent to the user.", "error")
            return redirect(next_url)
        if order.get("status") != "delivered" or not items:
            flash("Only delivered orders with saved items can be resent.", "error")
            return redirect(next_url)
        send_order_items(int(order.get("user_id", 0) or 0), order, items, from_pending=False, resent_by_admin=True)
        action_name = "replacement_resent" if order.get("is_replacement") else "order_resent"
        log_admin_action(app.db, action_name, f"{order.get('order_id')} to {order.get('user_id')}")
        label = "Replacement" if order.get("is_replacement") else "Order"
        flash(f"{label} {order.get('order_id', order_id)} resent to the user.", "success")
        return redirect(next_url)

    @app.post("/orders/<order_id>/revoke-telegram")
    @login_required
    @csrf_required
    def revoke_order_delivery(order_id: str):
        ref_id = str(order_id or "").strip().upper()
        order = app.db.orders.find_one({"order_id": ref_id}) if ref_id else None
        if not order:
            flash("Order not found.", "error")
            return redirect(url_for("orders"))

        next_url = str(request.form.get("next") or "").strip()
        if not (next_url.startswith("/") and not next_url.startswith("//")):
            if order.get("is_replacement"):
                next_url = url_for("replacement_order_detail", order_id=order.get("order_id", ref_id))
            else:
                next_url = url_for("order_detail", order_id=order.get("order_id", ref_id))

        if order.get("delivery_revoked"):
            flash("This delivery is already revoked. Users cannot fetch it again from the bot.", "info")
            return redirect(next_url)

        items = order.get("items") or []
        if order.get("status") != "delivered" or not items:
            flash("Only delivered orders with saved stock/items can be revoked.", "error")
            return redirect(next_url)

        refs = order_delivery_messages(order)
        results: list[dict[str, Any]] = []
        deleted_count = 0
        for ref in refs:
            ok, detail = delete_telegram_message(int(ref["chat_id"]), int(ref["message_id"]))
            if ok:
                deleted_count += 1
            results.append({
                "chat_id": ref["chat_id"],
                "message_id": ref["message_id"],
                "ok": bool(ok),
                "detail": detail,
                "attempted_at": utcnow(),
            })

        now = utcnow()
        revoked_by = current_admin_username() or current_admin_role() or "owner"
        app.db.orders.update_one(
            {"order_id": ref_id},
            {"$set": {
                "delivery_revoked": True,
                "delivery_revoked_at": now,
                "delivery_revoked_by": revoked_by,
                "delivery_telegram_delete_attempted_at": now,
                "delivery_telegram_deleted": bool(refs and deleted_count == len(refs)),
                "delivery_telegram_deleted_count": deleted_count,
                "delivery_telegram_delete_results": results,
            }},
        )
        record_stock_ledger_status(
            app.db,
            str(order.get("product_name") or ""),
            [str(item).strip() for item in (order.get("items") or []) if str(item).strip()],
            "revoked",
            order=order,
            username=revoked_by,
            role=current_admin_role(),
            source="webadmin",
            note="Delivery revoked/deleted from Telegram",
        )
        action_name = "replacement_revoked" if order.get("is_replacement") else "order_revoked"
        log_admin_action(app.db, action_name, f"{ref_id} user={order.get('user_id')} deleted={deleted_count}/{len(refs)} by={revoked_by}")

        label = "Replacement" if order.get("is_replacement") else "Order"
        if refs and deleted_count == len(refs):
            flash(f"{label} {ref_id} revoked. Deleted {deleted_count} Telegram file message(s), and /getorder is disabled.", "success")
        elif refs:
            flash(f"{label} {ref_id} revoked and /getorder is disabled. Deleted {deleted_count}/{len(refs)} Telegram message(s). Telegram may refuse deletion after about 48 hours.", "warning")
        else:
            flash(f"{label} {ref_id} revoked and /getorder is disabled. No saved Telegram message ID was found, so old sent files could not be deleted.", "warning")
        return redirect(next_url)

    @app.post("/orders/<order_id>/transfer")
    @login_required
    @csrf_required
    def transfer_revoked_order(order_id: str):
        ref_id = str(order_id or "").strip().upper()
        order = app.db.orders.find_one({"order_id": ref_id}) if ref_id else None
        if not order:
            flash("Order not found.", "error")
            return redirect(url_for("orders"))

        old_detail_url = url_for("replacement_order_detail" if order.get("is_replacement") else "order_detail", order_id=order.get("order_id", ref_id))

        if not order.get("delivery_revoked"):
            flash("Revoke/delete the mistaken Telegram delivery first, then transfer it to the correct user.", "error")
            return redirect(old_detail_url)

        if order.get("delivery_transferred_to_order_id"):
            new_id = str(order.get("delivery_transferred_to_order_id") or "").strip().upper()
            flash(f"This revoked delivery was already transferred to {new_id}.", "info")
            if new_id:
                new_order = app.db.orders.find_one({"order_id": new_id}, {"is_replacement": 1, "order_id": 1})
                if new_order:
                    endpoint = "replacement_order_detail" if new_order.get("is_replacement") else "order_detail"
                    return redirect(url_for(endpoint, order_id=new_id))
            return redirect(old_detail_url)

        if order.get("delivery_returned_to_stock"):
            flash("This revoked delivery was already added back to stock, so it cannot be transferred.", "info")
            return redirect(old_detail_url)

        items = [str(item).strip() for item in (order.get("items") or []) if str(item).strip()]
        if order.get("status") != "delivered" or not items:
            flash("Only revoked delivered orders with saved stock/items can be transferred.", "error")
            return redirect(old_detail_url)

        raw_target = str(request.form.get("target_user_id") or "").strip()
        if not raw_target:
            flash("Enter the correct user's Telegram ID or @username.", "error")
            return redirect(old_detail_url)

        target_user = None
        if re.fullmatch(r"\d+", raw_target):
            target_user = app.db.users.find_one({"user_id": int(raw_target)})
        else:
            username_query = raw_target.lstrip("@").strip()
            if username_query:
                target_user = app.db.users.find_one({"username": {"$regex": f"^{re.escape(username_query)}$", "$options": "i"}})

        if not target_user:
            flash("Correct user was not found. Ask them to press /start in the bot first, then try the transfer again.", "error")
            return redirect(old_detail_url)

        target_user_id = int(target_user.get("user_id", 0) or 0)
        if target_user_id <= 0:
            flash("Correct user has an invalid Telegram user ID.", "error")
            return redirect(old_detail_url)
        if target_user_id == int(order.get("user_id", 0) or 0):
            flash("Choose a different user. This revoked delivery already belongs to that user.", "error")
            return redirect(old_detail_url)
        if target_user.get("blocked"):
            flash("The correct user is blocked. Unblock them before transferring stock.", "error")
            return redirect(old_detail_url)

        transfer_note = str(request.form.get("transfer_note") or "").strip()[:1000]
        transferred_by = current_admin_username() or current_admin_role() or "owner"
        new_order, err = create_transferred_delivery_order(order, target_user, admin_note=transfer_note)
        if not new_order:
            flash(f"Could not create transfer order: {err or 'unknown error'}", "error")
            return redirect(old_detail_url)

        sent = send_order_items(target_user_id, new_order, items, from_pending=False, resent_by_admin=False)
        now = utcnow()
        app.db.orders.update_one(
            {"order_id": new_order["order_id"]},
            {"$set": {
                "delivery_transfer_sent_at": now,
                "delivery_transfer_telegram_sent": bool(sent),
            }},
        )
        app.db.orders.update_one(
            {"order_id": ref_id},
            {"$set": {
                "delivery_transferred": True,
                "delivery_transferred_at": now,
                "delivery_transferred_by": transferred_by,
                "delivery_transferred_to_user_id": target_user_id,
                "delivery_transferred_to_username": str(target_user.get("username") or "").strip().lstrip("@"),
                "delivery_transferred_to_order_id": new_order["order_id"],
                "delivery_transfer_note": transfer_note,
            }},
        )
        record_stock_ledger_status(
            app.db,
            str(order.get("product_name") or ""),
            items,
            "transferred",
            order=order,
            username=transferred_by,
            role=current_admin_role(),
            source="webadmin",
            note=f"Transferred to {target_user_id} as {new_order['order_id']}",
        )
        record_stock_ledger_status(
            app.db,
            str(new_order.get("product_name") or ""),
            items,
            "transferred_delivered",
            order=new_order,
            username=transferred_by,
            role=current_admin_role(),
            source="webadmin",
            note=f"Transferred from {ref_id}",
        )

        label = "Replacement" if order.get("is_replacement") else "Order"
        target_label = telegram_user_display(app.db, target_user_id, target_user.get("username"))
        action_name = "replacement_transferred" if order.get("is_replacement") else "order_transferred"
        log_admin_action(app.db, action_name, f"from={ref_id} to_order={new_order['order_id']} to_user={target_label} sent={bool(sent)} by={transferred_by}")
        if sent:
            flash(f"{label} {ref_id} transferred to {target_label}. New delivery ID: {new_order['order_id']}.", "success")
        else:
            flash(f"Transfer history {new_order['order_id']} was created for {target_label}, but Telegram delivery failed. Use Resend on the new detail page after checking the user/chat.", "warning")

        endpoint = "replacement_order_detail" if new_order.get("is_replacement") else "order_detail"
        return redirect(url_for(endpoint, order_id=new_order["order_id"]))

    @app.post("/orders/<order_id>/return-to-stock")
    @login_required
    @csrf_required
    def return_revoked_order_to_stock(order_id: str):
        ref_id = str(order_id or "").strip().upper()
        order = app.db.orders.find_one({"order_id": ref_id}) if ref_id else None
        if not order:
            flash("Order not found.", "error")
            return redirect(url_for("orders"))

        detail_url = url_for("replacement_order_detail" if order.get("is_replacement") else "order_detail", order_id=order.get("order_id", ref_id))

        if not order.get("delivery_revoked"):
            flash("Only revoked deliveries can be added back to stock.", "error")
            return redirect(detail_url)
        if order.get("delivery_transferred_to_order_id"):
            flash("This revoked delivery was already transferred, so it cannot be added back to stock.", "error")
            return redirect(detail_url)
        if order.get("delivery_returned_to_stock"):
            flash("This revoked delivery was already added back to stock.", "info")
            return redirect(detail_url)

        product_name = str(order.get("product_name") or "").strip()
        product = app.db.products.find_one({"name": name_regex(product_name)}) if product_name else None
        if not product:
            flash("Product not found, so the revoked stock could not be added back.", "error")
            return redirect(detail_url)

        returned_items: list[str] = []
        current_keys = {
            normalize_approved_stock_item(item)
            for item in (product.get("stock") or [])
            if normalize_approved_stock_item(item)
        }
        seen_keys: set[str] = set()
        for raw_item in (order.get("items") or []):
            clean_item = normalize_approved_stock_item(raw_item)
            if not clean_item:
                continue
            if clean_item in current_keys or clean_item in seen_keys:
                continue
            returned_items.append(clean_item)
            seen_keys.add(clean_item)

        if not returned_items:
            flash("No stock was added back. These item(s) are already in current stock or no saved item text was found.", "info")
            return redirect(detail_url)

        returned_by = current_admin_username() or current_admin_role() or "owner"
        stock_meta_records = build_stock_added_by_records(
            returned_items,
            username=returned_by,
            role=current_admin_role(),
            manager_earning_rate_usdt=0.0,
            owner_due_rate_usdt=0.0,
            stock_upload_kind="returned_revoked_delivery",
        )
        app.db.products.update_one(
            {"_id": product["_id"]},
            {"$push": {"stock": {"$each": returned_items}, "stock_added_by": {"$each": stock_meta_records}}},
        )
        record_stock_ledger_add(
            app.db,
            product_name,
            returned_items,
            username=returned_by,
            role=current_admin_role(),
            source="webadmin",
            stock_upload_kind="returned_revoked_delivery",
            note=f"Added back from revoked delivery {ref_id}",
        )
        pending_summary = process_pending_stock_orders(app.db, product_name)
        now = utcnow()
        app.db.orders.update_one(
            {"order_id": ref_id},
            {"$set": {
                "delivery_returned_to_stock": True,
                "delivery_returned_to_stock_at": now,
                "delivery_returned_to_stock_by": returned_by,
                "delivery_returned_to_stock_count": len(returned_items),
                "delivery_returned_to_stock_pending_orders_delivered": int(pending_summary.get("orders_delivered", 0) or 0),
                "delivery_returned_to_stock_pending_items_delivered": int(pending_summary.get("items_delivered", 0) or 0),
            }},
        )

        label = "Replacement" if order.get("is_replacement") else "Order"
        log_action = "revoked_replacement_returned_to_stock" if order.get("is_replacement") else "revoked_order_returned_to_stock"
        log_admin_action(
            app.db,
            log_action,
            f"{ref_id}: product={product_name} added={len(returned_items)} pending_orders_delivered={pending_summary.get('orders_delivered', 0)} by={returned_by}",
        )
        delivered_count = int(pending_summary.get("orders_delivered", 0) or 0)
        if delivered_count:
            flash(f"{label} {ref_id} stock added back. Auto-delivered {delivered_count} pending order(s).", "success")
        else:
            flash(f"{label} {ref_id} stock added back to current stock.", "success")
        return redirect(detail_url)

    @app.get("/replacement-orders")
    @login_required
    def replacement_orders():
        q = request.args.get("q", "").strip()
        query: dict[str, Any] = {"is_replacement": True}
        if q:
            ors: list[dict] = [
                {"order_id": {"$regex": re.escape(q), "$options": "i"}},
                {"replacement_report_id": {"$regex": re.escape(q), "$options": "i"}},
                {"original_order_id": {"$regex": re.escape(q), "$options": "i"}},
                {"original_order_ids": {"$regex": re.escape(q), "$options": "i"}},
                {"product_name": {"$regex": re.escape(q), "$options": "i"}},
            ]
            if show_order_user_identity():
                username_q = q.lstrip("@").strip()
                if username_q:
                    username_regex = {"$regex": re.escape(username_q), "$options": "i"}
                    ors.append({"username": username_regex})
                    matching_user_ids = [
                        int(user.get("user_id"))
                        for user in app.db.users.find({"username": username_regex}, {"user_id": 1})
                        if user.get("user_id") is not None
                    ]
                    if matching_user_ids:
                        ors.append({"user_id": {"$in": sorted(set(matching_user_ids))}})
                if q.isdigit():
                    ors.append({"user_id": int(q)})
            query["$or"] = ors
        page = int_arg("page", 1, minimum=1)
        total = app.db.orders.count_documents(query)
        rows = recent_created_rows(
            app.db.orders.find(query),
            skip=(page - 1) * PAGE_SIZE,
            limit=PAGE_SIZE,
        )
        total_pages = max(1, math.ceil(total / PAGE_SIZE)) if total else 1
        return render_template("replacement_orders.html", orders=rows, page=page, total_pages=total_pages, total=total, q=q)

    @app.get("/replacement-orders/<order_id>")
    @login_required
    def replacement_order_detail(order_id: str):
        order = app.db.orders.find_one({"order_id": str(order_id or "").strip().upper(), "is_replacement": True})
        if not order:
            flash("Replacement delivery not found.", "error")
            return redirect(url_for("replacement_orders"))
        report = None
        report_id = str(order.get("replacement_report_id") or "").strip().upper()
        if report_id:
            report = app.db.replacement_reports.find_one({"report_id": report_id})
        return render_template("replacement_order_detail.html", order=order, report=report)

    @app.get("/pending-orders")
    @login_required
    def pending_orders():
        rows = list(app.db.orders.find({"status": "pending_stock"}).sort("created_at", 1).limit(100))
        return render_template("pending_orders.html", orders=rows)

    @app.get("/ranking")
    @login_required
    def ranking():
        page = int_arg("page", 1, minimum=1)
        total = count_ranked_buyers(app.db)
        rows = get_buyer_ranking(app.db, limit=PAGE_SIZE, skip=(page - 1) * PAGE_SIZE)
        total_pages = max(1, math.ceil(total / PAGE_SIZE)) if total else 1
        return render_template("ranking.html", rows=rows, page=page, total_pages=total_pages, total=total)

    @app.get("/payments")
    @login_required
    def payments():
        status = request.args.get("status", "needs_review")
        allowed_filters = {"all", "needs_review", "approved", "rejected"}
        if status not in allowed_filters:
            status = "needs_review"
        if status == "needs_review":
            query = {"status": {"$in": ["upi_submitted", "binance_submitted", "usdt_manual_submitted"]}}
        elif status == "approved":
            query = {
                "status": {"$in": ["approved", "confirmed", "completed"]},
                "$or": manual_review_query(),
            }
        elif status == "rejected":
            query = {
                "status": "rejected",
                "$or": manual_review_query(),
            }
        else:
            query = {"$or": manual_review_query()} if current_admin_role() == ADMIN_ROLE_PAYMENT_MANAGER else {}
        page = int_arg("page", 1, minimum=1)
        total = app.db.pending_payments.count_documents(query)
        rows = recent_created_rows(
            app.db.pending_payments.find(query),
            skip=(page - 1) * PAGE_SIZE,
            limit=PAGE_SIZE,
        )
        order_refs = [
            str(p.get("ref_id") or "").upper()
            for p in rows
            if p.get("pay_type") == "order" and p.get("ref_id")
        ]
        if order_refs:
            order_rows = app.db.orders.find(
                {"order_id": {"$in": order_refs}},
                {"order_id": 1, "product_name": 1, "quantity": 1},
            )
            orders_by_id = {str(order.get("order_id") or "").upper(): order for order in order_rows}
            for payment in rows:
                if payment.get("pay_type") != "order":
                    continue
                order = orders_by_id.get(str(payment.get("ref_id") or "").upper())
                if not order:
                    continue
                payment["order_product_name"] = order.get("product_name")
                payment["order_quantity"] = order.get("quantity")
        total_pages = max(1, math.ceil(total / PAGE_SIZE)) if total else 1
        return render_template("payments.html", payments=rows, status=status, page=page, total_pages=total_pages, total=total)


    @app.get("/payment-audit")
    @login_required
    def legacy_payment_audit_redirect():
        return redirect(url_for("tx_hash_logs"))

    @app.get("/tx-hash-logs")
    @login_required
    def tx_hash_logs():
        network = request.args.get("network", "all")
        result = request.args.get("result", "all")
        q = str(request.args.get("q", "") or "").strip()
        allowed_networks = {"all", "bep20", "polygon"}
        allowed_results = {"all", "auto_approved", "needs_review", "failed", "rejected", "expired"}
        if network not in allowed_networks:
            network = "all"
        if result not in allowed_results:
            result = "all"

        tx_hash_filter = {"$or": [
            {"usdt_transaction_hash": {"$exists": True, "$type": "string", "$gt": ""}},
            {"usdt_txn_hash": {"$exists": True, "$type": "string", "$gt": ""}},
            {"usdt_txn_hash_key": {"$regex": r":0x[a-fA-F0-9]{64}$"}},
        ]}
        filters: list[dict[str, Any]] = [tx_hash_filter]

        if network == "polygon":
            filters.append({"$or": [
                {"method": {"$in": ["polygon", "usdt_polygon"]}},
                {"usdt_network": "polygon"},
                {"usdt_txn_hash_key": {"$regex": "^polygon:", "$options": "i"}},
            ]})
        elif network == "bep20":
            filters.append({"$or": [
                {"method": {"$in": ["usdt", "bep20"]}},
                {"usdt_network": {"$in": ["bep20", "bsc", "bscscan"]}},
                {"usdt_txn_hash_key": {"$regex": "^bep20:", "$options": "i"}},
                {"usdt_txn_hash_key": {"$regex": "^bsc:", "$options": "i"}},
            ]})

        if result == "auto_approved":
            filters.append({"$or": [
                {"usdt_auto_verified": True},
                {"usdt_manual_auto_check_result": "passed"},
            ]})
        elif result == "needs_review":
            filters.append({"status": "usdt_manual_submitted"})
        elif result == "failed":
            filters.append({"$and": [
                {"usdt_manual_auto_check_result": {"$in": ["failed", "error", "duplicate"]}},
                {"status": {"$nin": ["confirmed", "approved", "completed", "rejected", "expired"]}},
            ]})
        elif result == "rejected":
            filters.append({"status": "rejected"})
        elif result == "expired":
            filters.append({"status": "expired"})

        if q:
            escaped = re.escape(q)
            search_filter: dict[str, Any] = {"$or": [
                {"ref_id": {"$regex": escaped, "$options": "i"}},
                {"usdt_transaction_hash": {"$regex": escaped, "$options": "i"}},
                {"usdt_txn_hash": {"$regex": escaped, "$options": "i"}},
                {"usdt_txn_hash_key": {"$regex": escaped, "$options": "i"}},
            ]}
            if q.isdigit():
                search_filter["$or"].append({"user_id": int(q)})
            if show_payment_user_identity():
                username_q = q.lstrip("@").strip()
                if username_q:
                    username_regex = {"$regex": re.escape(username_q), "$options": "i"}
                    search_filter["$or"].append({"username": username_regex})
                    matching_user_ids = [
                        int(user.get("user_id"))
                        for user in app.db.users.find({"username": username_regex}, {"user_id": 1})
                        if user.get("user_id") is not None
                    ]
                    if matching_user_ids:
                        search_filter["$or"].append({"user_id": {"$in": sorted(set(matching_user_ids))}})
            filters.append(search_filter)

        query = {"$and": filters} if len(filters) > 1 else filters[0]
        page = int_arg("page", 1, minimum=1)
        total = app.db.pending_payments.count_documents(query)
        rows = recent_created_rows(
            app.db.pending_payments.find(query),
            skip=(page - 1) * PAGE_SIZE,
            limit=PAGE_SIZE,
        )
        order_refs = [str(p.get("ref_id") or "").upper() for p in rows if p.get("pay_type") == "order" and p.get("ref_id")]
        if order_refs:
            order_rows = app.db.orders.find({"order_id": {"$in": order_refs}}, {"order_id": 1, "product_name": 1, "quantity": 1})
            orders_by_id = {str(order.get("order_id") or "").upper(): order for order in order_rows}
            for payment in rows:
                order = orders_by_id.get(str(payment.get("ref_id") or "").upper())
                if order:
                    payment["order_detail_exists"] = True
                    payment["order_product_name"] = order.get("product_name")
                    payment["order_quantity"] = order.get("quantity")
        total_pages = max(1, math.ceil(total / PAGE_SIZE)) if total else 1
        return render_template(
            "tx_hash_logs.html",
            payments=rows,
            network=network,
            result=result,
            q=q,
            page=page,
            total_pages=total_pages,
            total=total,
        )


    @app.get("/api/live-state")
    @login_required
    def live_state():
        """Small, reliable live-update endpoint for the admin panel.

        The previous updater fetched and parsed a full page on every interval.
        That made live updates feel intermittent on Railway when requests overlapped,
        a page had focused controls, or multiple tabs were open. This endpoint is
        intentionally lightweight: it returns counts plus latest timestamps so the
        browser can decide when to refresh the visible page and when to show a
        notification.
        """
        try:
            state = cached_value("live_admin_state", ADMIN_LIVE_STATE_CACHE_TTL_SECONDS, lambda: get_live_admin_state(app.db))
            return jsonify(state)
        except Exception as exc:
            app.logger.warning("Could not build live admin state: %s", exc)
            return jsonify({"ok": False, "error": "live_state_failed", "ts": time.time()}), 500

    @app.post("/payments/<ref_id>/<action>")
    @login_required
    @csrf_required
    def decide_payment(ref_id: str, action: str):
        pending = app.db.pending_payments.find_one({"ref_id": ref_id})
        if not pending:
            flash("Payment request not found.", "error")
            return redirect(url_for("payments"))
        if action not in {"approve", "reject"}:
            abort(404)
        current_status = pending.get("status")
        valid_statuses = {"upi_submitted", "binance_submitted", "usdt_manual_submitted"}
        if current_status not in valid_statuses:
            flash("This payment request is no longer active.", "error")
            return redirect(url_for("payments"))
        if action == "reject":
            app.db.pending_payments.update_one({"ref_id": ref_id}, {"$set": {"status": "rejected", "reviewed_at": utcnow()}})
            if pending.get("pay_type") == "order":
                update_order_status(app.db, ref_id, "failed")
            send_telegram_message(int(pending.get("user_id", 0) or 0), "❌ Your payment could not be verified. Please contact support if you believe this is a mistake.")
            log_admin_action(app.db, "payment_rejected", ref_id)
            flash(f"Payment {ref_id} rejected.", "success")
            return redirect(url_for("payments"))

        if current_status == "usdt_manual_submitted":
            txn_hash = _normalize_usdt_tx_hash(pending.get("usdt_txn_hash"))
            if _find_used_usdt_tx_hash(app.db, txn_hash, exclude_ref_id=ref_id):
                flash("This USDT Tx hash/ID has already been used before. Ask the user to submit the correct one.", "error")
                return redirect(url_for("payments"))

        # Match the bot's semantics: UPI/Binance become approved; manual USDT becomes confirmed.
        approved_status = "confirmed" if current_status == "usdt_manual_submitted" else "approved"
        update_fields = {"status": approved_status, "reviewed_at": utcnow()}
        if current_status == "usdt_manual_submitted" and txn_hash:
            network = pending.get("usdt_network") or pending.get("method")
            update_fields.update({
                "usdt_txn_hash": txn_hash,
                "usdt_txn_hash_key": _make_usdt_tx_hash_key(network, txn_hash),
                "usdt_network": _normalize_usdt_network_key(network),
            })
        app.db.pending_payments.update_one({"ref_id": ref_id}, {"$set": update_fields})
        if pending.get("pay_type") == "order":
            complete_order(app.db, int(pending.get("user_id", 0) or 0), ref_id)
        else:
            complete_wallet_load(app.db, int(pending.get("user_id", 0) or 0), pending)
        log_admin_action(app.db, "payment_approved", ref_id)
        flash(f"Payment {ref_id} approved and completed.", "success")
        return redirect(url_for("payments"))

    @app.get("/broadcast")
    @login_required
    def broadcast_form():
        active_users = app.db.users.count_documents({"blocked": {"$ne": True}})
        language_settings = get_language_settings(app.db)
        active_users_by_language = {
            "en": app.db.users.count_documents({"blocked": {"$ne": True}, "$or": [{"language": {"$in": ["en", None, ""]}}, {"language": {"$exists": False}}]}),
            "es": app.db.users.count_documents({"blocked": {"$ne": True}, "language": "es"}),
        }
        return render_template(
            "broadcast.html",
            active_users=active_users,
            active_users_by_language=active_users_by_language,
            language_settings=language_settings,
        )

    @app.post("/broadcast")
    @login_required
    @csrf_required
    def broadcast_submit():
        message_en = (request.form.get("message_en", "") or request.form.get("message", "")).strip()
        message_es = request.form.get("message_es", "").strip()
        if not message_en and not message_es:
            flash("Enter an English message, a Spanish message, or both.", "error")
            return redirect(url_for("broadcast_form"))

        maintenance_on = get_setting(app.db, "maintenance_mode", False)
        if maintenance_on:
            users = get_admin_recipient_users(app.db)
        else:
            users = list(app.db.users.find({"blocked": {"$ne": True}}))

        sent = 0
        failed = 0
        skipped = 0
        sent_by_language = {"en": 0, "es": 0}
        skipped_by_language = {"en": 0, "es": 0}
        seen: set[int] = set()
        for user in users:
            try:
                user_id = int(user.get("user_id", 0) or 0)
            except Exception:
                user_id = 0
            if not user_id or user_id in seen:
                continue
            seen.add(user_id)
            lang = lang_from_user(user)
            normalized_lang = normalize_lang(lang)
            message = choose_admin_text_for_language(message_en, message_es, lang)
            if not message:
                skipped += 1
                skipped_by_language[normalized_lang] = skipped_by_language.get(normalized_lang, 0) + 1
                continue
            prefix = admin_panel_message_prefix("broadcast", lang)
            ok = send_telegram_message(user_id, f"{prefix}\n\n{message}", parse_mode="Markdown")
            if ok:
                sent += 1
                sent_by_language[normalized_lang] = sent_by_language.get(normalized_lang, 0) + 1
            else:
                failed += 1

        if maintenance_on:
            log_admin_action(
                app.db,
                "broadcast_sent_admins_maintenance",
                f"sent={sent} failed={failed} skipped={skipped}; normal users not queued",
            )
            flash(f"Maintenance mode is ON. Broadcast sent only to admin/tester IDs and not queued for normal users. Sent: {sent}, failed: {failed}, skipped: {skipped}.", "info")
            return redirect(url_for("broadcast_form"))

        log_admin_action(
            app.db,
            "broadcast_sent",
            f"sent={sent} failed={failed} skipped={skipped} en={sent_by_language.get('en', 0)} es={sent_by_language.get('es', 0)} skipped_en={skipped_by_language.get('en', 0)} skipped_es={skipped_by_language.get('es', 0)}",
        )
        flash(f"Broadcast complete. Sent: {sent}, failed: {failed}, skipped: {skipped}.", "success")
        return redirect(url_for("broadcast_form"))


    @app.get("/activity")
    @login_required
    def activity_log():
        page = int_arg("page", 1, minimum=1)
        total = app.db.admin_activity.count_documents({})
        rows = list(app.db.admin_activity.find().sort("created_at", -1).skip((page - 1) * PAGE_SIZE).limit(PAGE_SIZE))
        total_pages = max(1, math.ceil(total / PAGE_SIZE)) if total else 1
        return render_template("activity.html", rows=rows, page=page, total_pages=total_pages, total=total)

    @app.get("/exports/orders.csv")
    @login_required
    def export_orders_csv():
        rows = app.db.orders.find().sort("created_at", -1)
        return csv_response(
            "orders.csv",
            ["order_id", "user_id", "username", "product_name", "quantity", "payment_method", "amount_inr", "amount_usdt", "status", "created_at", "delivered_at"],
            rows,
        )

    @app.get("/exports/users.csv")
    @login_required
    def export_users_csv():
        rows = app.db.users.find().sort("joined_at", -1)
        return csv_response(
            "users.csv",
            ["user_id", "username", "blocked", "wallet_inr", "wallet_usdt", "joined_at"],
            rows,
        )

    @app.get("/exports/stock.csv")
    @login_required
    def export_stock_csv():
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["product_name", "enabled", "stock_number", "stock_item"])
        stock_manager_username = current_admin_username() if is_stock_manager_role() else None
        assigned_product_keys = get_stock_manager_assigned_product_keys(app.db, stock_manager_username) if stock_manager_username else set()
        for product in app.db.products.find().sort("name", 1):
            if stock_manager_username and product_name_key(product.get("name")) not in assigned_product_keys:
                continue
            rows = build_current_stock_view(product, viewer_username=stock_manager_username)
            visible_index = 0
            for row in rows:
                if stock_manager_username and not row.get("can_view"):
                    continue
                visible_index += 1
                writer.writerow([product.get("name"), product.get("enabled", True), visible_index, row.get("text", "")])
        return Response(output.getvalue(), mimetype="text/csv", headers={"Content-Disposition": "attachment; filename=stock.csv"})

    @app.get("/telegram-file/<path:file_id>")
    @login_required
    def telegram_file(file_id: str):
        bot_token = get_bot_token(app.db)
        if not bot_token:
            abort(404)
        meta = requests.get(f"https://api.telegram.org/bot{bot_token}/getFile", params={"file_id": file_id}, timeout=15).json()
        if not meta.get("ok"):
            abort(404)
        file_path = meta.get("result", {}).get("file_path")
        if not file_path:
            abort(404)
        file_resp = requests.get(f"https://api.telegram.org/file/bot{bot_token}/{file_path}", timeout=30)
        if file_resp.status_code != 200:
            abort(404)
        return Response(file_resp.content, content_type=file_resp.headers.get("content-type", "application/octet-stream"))


def _max_mongo_value(db, collection: str, field: str, query: dict | None = None):
    try:
        row = db[collection].find(query or {}, {field: 1}).sort(field, -1).limit(1)
        rows = list(row)
        if rows:
            return rows[0].get(field)
    except Exception:
        pass
    return None


def _count_safe(db, collection: str, query: dict | None = None) -> int:
    try:
        return int(db[collection].count_documents(query or {}))
    except Exception:
        return 0


def _dt_or_number_signature(value) -> str:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat()
    if isinstance(value, (int, float)):
        return str(float(value))
    return str(value or "")


def count_stock_upload_rejections(db) -> int:
    return _count_safe(db, "stock_upload_rejections", {})


def get_live_admin_state(db) -> dict:
    review_query = {"status": {"$in": ["upi_submitted", "binance_submitted", "usdt_manual_submitted"]}}
    pending_stock_query = {"status": "pending_stock"}
    active_order_query = {"status": {"$in": ["pending", "pending_stock"]}}

    normal_order_query = {"is_replacement": {"$ne": True}}
    latest_order_created = _max_mongo_value(db, "orders", "created_at", normal_order_query)
    latest_order_delivered = _max_mongo_value(
        db,
        "orders",
        "delivered_at",
        {"is_replacement": {"$ne": True}, "delivered_at": {"$exists": True, "$ne": None}},
    )
    latest_payment_created = _max_mongo_value(db, "pending_payments", "created_at")
    latest_payment_review = _max_mongo_value(db, "pending_payments", "reviewed_at", {"reviewed_at": {"$exists": True}})
    latest_payment_confirmed = _max_mongo_value(db, "pending_payments", "confirmed_at", {"confirmed_at": {"$exists": True}})
    latest_payout_request = _max_mongo_value(db, "stock_manager_payment_requests", "requested_at", {"status": "pending"})
    latest_refund_request = _max_mongo_value(db, "orders", "refund_requested_at", {"refund_status": "refund_requested", "is_replacement": {"$ne": True}})
    latest_replacement_report = _max_mongo_value(db, "replacement_reports", "created_at", {"status": {"$in": ["pending", "reviewing", "approved", "replacement_ready"]}})
    latest_stock_upload_rejection = _max_mongo_value(db, "stock_upload_rejections", "created_at")

    stock_alert = get_product_stock_alert_summary(db)
    counts = {
        "orders_total": _count_safe(db, "orders", normal_order_query),
        "orders_pending": _count_safe(db, "orders", {"status": "pending", "is_replacement": {"$ne": True}}),
        "orders_pending_stock": _count_safe(db, "orders", {"status": "pending_stock", "is_replacement": {"$ne": True}}),
        "orders_delivered": _count_safe(db, "orders", {"status": "delivered", "is_replacement": {"$ne": True}}),
        "active_orders": _count_safe(db, "orders", {"status": {"$in": ["pending", "pending_stock"]}, "is_replacement": {"$ne": True}}),
        "payment_reviews": _count_safe(db, "pending_payments", review_query),
        "stock_manager_payout_requests": _count_safe(db, "stock_manager_payment_requests", {"status": "pending"}),
        "refund_requests_pending": count_pending_refund_requests(db),
        "replacement_reports_pending": _count_safe(db, "replacement_reports", {"status": {"$in": ["pending", "reviewing", "approved", "replacement_ready"]}}),
        "replacement_reports_open": _count_safe(db, "replacement_reports", {"status": {"$in": ["pending", "reviewing", "approved", "replacement_ready"]}}),
        "waiting_payments": _count_safe(db, "pending_payments", {"status": "waiting"}),
        "products_low_stock": int(stock_alert.get("low_stock", 0) or 0),
        "products_out_of_stock": int(stock_alert.get("out_of_stock", 0) or 0),
        "product_stock_alerts": int(stock_alert.get("count", 0) or 0),
        "stock_upload_rejections": count_stock_upload_rejections(db),
    }
    latest = {
        "order_created": _dt_or_number_signature(latest_order_created),
        "order_delivered": _dt_or_number_signature(latest_order_delivered),
        "payment_created": _dt_or_number_signature(latest_payment_created),
        "payment_reviewed": _dt_or_number_signature(latest_payment_review),
        "payment_confirmed": _dt_or_number_signature(latest_payment_confirmed),
        "stock_manager_payout_request": _dt_or_number_signature(latest_payout_request),
        "refund_request": _dt_or_number_signature(latest_refund_request),
        "replacement_report": _dt_or_number_signature(latest_replacement_report),
        "stock_upload_rejection": _dt_or_number_signature(latest_stock_upload_rejection),
    }
    signature = "|".join([f"{k}:{counts[k]}" for k in sorted(counts)] + [f"{k}:{latest[k]}" for k in sorted(latest)])
    return {
        "ok": True,
        "ts": time.time(),
        "counts": counts,
        "latest": latest,
        "stock_alert": stock_alert,
        "signature": signature,
    }


# ───────────────────────── Auth / CSRF ─────────────────────────


def _is_logged_in() -> bool:
    return bool(session.get("admin_logged_in"))


def login_required(func):
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        if not _is_logged_in():
            return redirect(url_for("login_form"))
        return func(*args, **kwargs)
    return wrapper


def _csrf_token() -> str:
    token = session.get("csrf")
    if not token:
        token = secrets.token_urlsafe(32)
        session["csrf"] = token
    return token


def csrf_required(func):
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        sent = request.form.get("csrf_token", "")
        expected = session.get("csrf", "")
        if not sent or not expected or not hmac.compare_digest(sent, expected):
            abort(400, "Invalid CSRF token")
        return func(*args, **kwargs)
    return wrapper


# ───────────────────────── DB helpers ─────────────────────────


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def name_regex(name: str) -> dict[str, str]:
    return {"$regex": f"^{re.escape(name.strip())}$", "$options": "i"}


def split_stock(raw: str) -> list[str]:
    return [block.strip() for block in (raw or "").split("---") if block.strip()]


def get_used_stock_items(db, product_name: str) -> list[str]:
    """Return stock items that were already delivered for this product."""
    used: list[str] = []
    for order in db.orders.find(
        {"product_name": name_regex(product_name), "items.0": {"$exists": True}},
        {"items": 1},
    ):
        used.extend([normalize_approved_stock_item(item) for item in (order.get("items", []) or []) if normalize_approved_stock_item(item)])
    return used


def filter_fresh_stock_items(existing_stock: list[str], new_items: list[str]) -> tuple[list[str], list[str]]:
    """Return only stock items that are fresh for this product.

    Exact duplicate stock is rejected only within the same product. This checks
    current stock, already delivered stock, and duplicates repeated in the same
    upload. The same stock text can still be used for a different product.
    """
    existing = {
        normalize_approved_stock_item(item)
        for item in (existing_stock or [])
        if normalize_approved_stock_item(item)
    }
    seen_in_upload: set[str] = set()
    fresh: list[str] = []
    duplicates: list[str] = []
    for item in new_items:
        clean = normalize_approved_stock_item(item)
        if not clean:
            continue
        if clean in existing or clean in seen_in_upload:
            duplicates.append(clean)
            continue
        fresh.append(clean)
        seen_in_upload.add(clean)
    return fresh, duplicates


def normalize_approved_stock_item(item: Any) -> str:
    """Normalize stock text for owner-approved pool matching.

    Leading/trailing spaces around the whole item and around each line are
    ignored so harmless copy/paste spaces do not reject otherwise valid stock.
    Other characters must still match exactly.
    """
    text = str(item or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        return ""
    return "\n".join(line.strip() for line in text.split("\n")).strip()


def approved_stock_pool_items(product: dict | None) -> list[str]:
    seen: set[str] = set()
    items: list[str] = []
    for item in ((product or {}).get("approved_stock_pool") or []):
        clean = normalize_approved_stock_item(item)
        if clean and clean not in seen:
            items.append(clean)
            seen.add(clean)
    return items


def filter_stock_against_approved_pool(product: dict, items: list[str]) -> tuple[list[str], list[str]]:
    approved = set(approved_stock_pool_items(product))
    accepted: list[str] = []
    rejected: list[str] = []
    for item in items:
        clean = normalize_approved_stock_item(item)
        if not clean:
            continue
        if clean in approved:
            accepted.append(clean)
        else:
            rejected.append(clean)
    return accepted, rejected


def approved_stock_pool_stats(db, product: dict) -> dict[str, int]:
    pool = approved_stock_pool_items(product)
    pool_set = set(pool)
    current = {normalize_approved_stock_item(item) for item in (product.get("stock", []) or []) if normalize_approved_stock_item(item)}
    used = {normalize_approved_stock_item(item) for item in get_used_stock_items(db, product.get("name", "")) if normalize_approved_stock_item(item)}
    blocked = current | used
    return {
        "total": len(pool),
        "current": len(pool_set & current),
        "sold_or_used": len(pool_set & used),
        "remaining": len(pool_set - blocked),
    }


def record_rejected_stock_upload(
    db,
    product_name: str,
    rejected_items: list[str],
    *,
    accepted_count: int,
    duplicate_count: int = 0,
    upload_kind: str = "normal",
    source: str = "webadmin",
    username: str | None = None,
    role: str | None = None,
) -> dict | None:
    rejected = [normalize_approved_stock_item(item) for item in rejected_items if normalize_approved_stock_item(item)]
    if not rejected:
        return None
    doc = {
        "product_name": str(product_name or "").strip(),
        "username": str(username or current_admin_username() or "").strip(),
        "username_key": normalize_admin_username(username or current_admin_username()),
        "role": normalize_admin_role(role or current_admin_role()),
        "source": str(source or "webadmin").strip(),
        "upload_kind": "replacement" if str(upload_kind or "").strip().lower() == "replacement" else "normal",
        "accepted_count": int(accepted_count or 0),
        "duplicate_count": int(duplicate_count or 0),
        "rejected_count": len(rejected),
        "rejected_items": rejected[:200],
        "rejected_preview": rejected[:5],
        "reason": "Not in owner-approved stock pool",
        "created_at": utcnow(),
    }
    try:
        db.stock_upload_rejections.insert_one(doc)
    except Exception:
        pass
    return doc


def notify_owner_stock_upload_rejection(db, rejection_doc: dict | None) -> int:
    """Mark a rejected stock-manager upload for WebAdmin live notification.

    Admin Telegram DMs are intentionally not sent from here because WebAdmin is
    the admin surface. The owner sees a sidebar badge/live toast, and the
    product manage page shows the rejected upload details.
    """
    if not rejection_doc:
        return 0
    try:
        db.stock_upload_rejections.update_one(
            {"_id": rejection_doc.get("_id")},
            {"$set": {"webadmin_notification_created_at": utcnow()}},
        )
    except Exception:
        pass
    return 1


def stock_item_hash(item: str) -> str:
    return hashlib.sha256(str(item or "").strip().encode("utf-8")).hexdigest()


def stock_ledger_product_key(product_name: Any) -> str:
    return str(product_name or "").strip().lower()


def stock_ledger_item_hash(item: Any) -> str:
    clean = normalize_approved_stock_item(item)
    return hashlib.sha256(clean.encode("utf-8")).hexdigest() if clean else ""


def stock_ledger_search_text(item: Any) -> str:
    return re.sub(r"\s+", " ", normalize_approved_stock_item(item).lower()).strip()


def stock_ledger_movement(
    movement_type: str,
    *,
    username: str = "",
    role: str = "",
    user_id: int | None = None,
    source: str = "webadmin",
    order_id: str = "",
    note: str = "",
) -> dict[str, Any]:
    movement: dict[str, Any] = {
        "type": str(movement_type or "").strip().lower(),
        "at": utcnow(),
        "username": str(username or "").strip().lstrip("@"),
        "role": str(role or "").strip(),
        "source": str(source or "webadmin").strip(),
        "order_id": str(order_id or "").strip().upper(),
        "note": str(note or "").strip()[:1000],
    }
    if user_id is not None:
        try:
            movement["user_id"] = int(user_id)
        except Exception:
            pass
    return movement


def record_stock_ledger_add(
    db,
    product_name: str,
    items: list[str],
    *,
    username: str = "",
    role: str = "",
    user_id: int | None = None,
    source: str = "webadmin",
    stock_upload_kind: str = "normal",
    note: str = "",
    manager_earning_rate_usdt: float | None = None,
    owner_due_rate_usdt: float | None = None,
) -> None:
    clean_product = str(product_name or "").strip()
    product_key = stock_ledger_product_key(clean_product)
    if not product_key:
        return
    now = utcnow()
    clean_username = str(username or "").strip().lstrip("@")
    clean_role = normalize_admin_role(role) if role else str(role or "").strip()
    upload_kind = str(stock_upload_kind or "normal").strip().lower() or "normal"
    set_fields_common: dict[str, Any] = {}
    if manager_earning_rate_usdt is not None:
        set_fields_common["stock_manager_earning_rate_usdt"] = round(max(0.0, safe_float(manager_earning_rate_usdt, 0.0)), 3)
    if owner_due_rate_usdt is not None:
        set_fields_common["stock_manager_owner_rate_usdt"] = round(max(0.0, safe_float(owner_due_rate_usdt, 0.0)), 3)
    movement = stock_ledger_movement(
        "added", username=clean_username, role=clean_role, user_id=user_id, source=source, note=note
    )
    for item in items or []:
        clean_item = normalize_approved_stock_item(item)
        item_hash = stock_ledger_item_hash(clean_item)
        if not clean_item or not item_hash:
            continue
        try:
            db.stock_item_ledger.update_one(
                {"product_key": product_key, "item_hash": item_hash},
                {
                    "$setOnInsert": {
                        "product_key": product_key,
                        "item_hash": item_hash,
                        "first_added_at": now,
                        "first_added_by_username": clean_username,
                        "first_added_by_role": clean_role,
                        "first_added_by_user_id": int(user_id) if user_id is not None else None,
                        "first_added_source": str(source or "webadmin").strip(),
                    },
                    "$set": {
                        "product_name": clean_product,
                        "item_text": clean_item,
                        "item_search_text": stock_ledger_search_text(clean_item),
                        "current_status": "available",
                        "current_status_at": now,
                        "last_movement_at": now,
                        "last_added_at": now,
                        "last_added_by_username": clean_username,
                        "last_added_by_role": clean_role,
                        "last_added_by_user_id": int(user_id) if user_id is not None else None,
                        "last_added_source": str(source or "webadmin").strip(),
                        "stock_upload_kind": upload_kind,
                        "current_order_id": "",
                        "current_user_id": None,
                        "current_username": "",
                        "updated_at": now,
                        **set_fields_common,
                    },
                    "$push": {"movements": {"$each": [movement], "$slice": -80}},
                },
                upsert=True,
            )
        except Exception:
            pass


def record_stock_ledger_status(
    db,
    product_name: str,
    items: list[str],
    status: str,
    *,
    order: dict | None = None,
    username: str = "",
    role: str = "",
    user_id: int | None = None,
    source: str = "webadmin",
    note: str = "",
) -> None:
    clean_product = str(product_name or "").strip()
    product_key = stock_ledger_product_key(clean_product)
    clean_status = str(status or "").strip().lower() or "unknown"
    if not product_key or not items:
        return
    now = utcnow()
    order = order or {}
    order_id = str(order.get("order_id") or "").strip().upper()
    current_user_id = user_id
    if current_user_id is None and order.get("user_id") is not None:
        try:
            current_user_id = int(order.get("user_id"))
        except Exception:
            current_user_id = None
    current_username = str(order.get("username") or "").strip().lstrip("@")
    movement = stock_ledger_movement(
        clean_status, username=username, role=role, user_id=current_user_id, source=source, order_id=order_id, note=note
    )
    set_fields: dict[str, Any] = {
        "product_name": clean_product,
        "current_status": clean_status,
        "current_status_at": now,
        "last_movement_at": now,
        "updated_at": now,
    }
    if order_id:
        set_fields["current_order_id"] = order_id
    if current_user_id is not None:
        set_fields["current_user_id"] = current_user_id
    if current_username:
        set_fields["current_username"] = current_username
    for item in items or []:
        clean_item = normalize_approved_stock_item(item)
        item_hash = stock_ledger_item_hash(clean_item)
        if not clean_item or not item_hash:
            continue
        try:
            db.stock_item_ledger.update_one(
                {"product_key": product_key, "item_hash": item_hash},
                {
                    "$setOnInsert": {
                        "product_key": product_key,
                        "item_hash": item_hash,
                        "first_added_at": now,
                        "first_added_by_username": str(username or "").strip().lstrip("@"),
                        "first_added_by_role": str(role or "").strip(),
                        "first_added_source": str(source or "webadmin").strip(),
                    },
                    "$set": {**set_fields, "item_text": clean_item, "item_search_text": stock_ledger_search_text(clean_item)},
                    "$push": {"movements": {"$each": [movement], "$slice": -80}},
                },
                upsert=True,
            )
        except Exception:
            pass


def record_order_items_delivered_in_ledger(db, order: dict, items: list[str], *, source: str = "webadmin") -> None:
    if not order or not items:
        return
    if order.get("is_replacement"):
        status = "replacement_delivered"
    elif order.get("admin_stock_delivery") or order.get("payment_method") == "admin_stock":
        status = "admin_sent"
    else:
        status = "delivered"
    record_stock_ledger_status(
        db,
        str(order.get("product_name") or ""),
        items,
        status,
        order=order,
        source=source,
        note="Attached to delivered order history",
    )


def stock_ledger_status_label(status: Any) -> tuple[str, str]:
    clean = str(status or "").strip().lower()
    labels = {
        "available": ("Available in inventory", "available"),
        "delivered": ("Sold / delivered order", "delivered"),
        "replacement_delivered": ("Replacement delivered", "delivered"),
        "admin_sent": ("Admin-sent order", "delivered"),
        "removed": ("Removed from stock", "removed"),
        "popped": ("Removed from live stock", "other"),
        "revoked": ("Revoked delivery", "other"),
        "transferred": ("Transferred delivery", "delivered"),
        "transferred_delivered": ("Transferred delivery", "delivered"),
    }
    return labels.get(clean, (clean.replace("_", " ").title() or "Ledger record", "other"))


REPORT_EMAIL_RE = re.compile(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", re.IGNORECASE)


def normalize_report_match_text(value: Any) -> str:
    text = str(value or "").strip().lower()
    text = re.sub(r"\s+", " ", text)
    return text


def report_emails_in_text(value: Any) -> set[str]:
    return {m.group(0).lower() for m in REPORT_EMAIL_RE.finditer(str(value or ""))}


def significant_report_tokens(value: Any) -> set[str]:
    text = normalize_report_match_text(value)
    email_domains = {email.split("@", 1)[1] for email in report_emails_in_text(text) if "@" in email}
    tokens = set(report_emails_in_text(text))
    ignored = {"mail", "email", "pass", "password", "login", "username", "user"}
    for token in re.findall(r"[a-z0-9._%+\-]{5,}", text, flags=re.IGNORECASE):
        cleaned = token.strip("._-+% ").lower()
        if len(cleaned) < 5 or cleaned in ignored:
            continue
        if cleaned in email_domains:
            continue
        tokens.add(cleaned)
    return tokens


def report_submitted_item_matches(submitted: Any, delivered_item: Any) -> bool:
    submitted_norm = normalize_report_match_text(submitted)
    delivered_norm = normalize_report_match_text(delivered_item)
    if not submitted_norm or not delivered_norm:
        return False
    submitted_emails = report_emails_in_text(submitted_norm)
    delivered_emails = report_emails_in_text(delivered_norm)
    if submitted_emails and delivered_emails:
        return bool(submitted_emails.intersection(delivered_emails))
    submitted_tokens = significant_report_tokens(submitted_norm)
    delivered_tokens = significant_report_tokens(delivered_norm)
    if submitted_tokens and delivered_tokens and submitted_tokens.intersection(delivered_tokens):
        return True
    if len(submitted_norm) >= 6 and submitted_norm in delivered_norm:
        return True
    if len(delivered_norm) >= 6 and delivered_norm in submitted_norm:
        return True
    return submitted_norm == delivered_norm


def report_submitted_match_snippet(submitted: Any, delivered_item: Any) -> str:
    submitted_raw = str(submitted or "").strip()
    delivered_raw = str(delivered_item or "").strip()
    submitted_norm = normalize_report_match_text(submitted_raw)
    delivered_norm = normalize_report_match_text(delivered_raw)
    submitted_emails = report_emails_in_text(submitted_norm)
    delivered_emails = report_emails_in_text(delivered_norm)
    common_emails = submitted_emails.intersection(delivered_emails)
    if common_emails:
        email = sorted(common_emails, key=len, reverse=True)[0]
        for line in submitted_raw.splitlines():
            if email.lower() in line.lower():
                return line.strip() or email
        return email
    if submitted_emails and delivered_emails:
        return submitted_raw[:1000]
    common_tokens = significant_report_tokens(submitted_norm).intersection(significant_report_tokens(delivered_norm))
    if common_tokens:
        token = sorted(common_tokens, key=len, reverse=True)[0]
        for line in submitted_raw.splitlines():
            if token.lower() in line.lower():
                return line.strip() or token
        return token
    for line in submitted_raw.splitlines():
        clean_line = line.strip()
        if clean_line and report_submitted_item_matches(clean_line, delivered_raw):
            return clean_line
    return submitted_raw[:1000]


def replacement_item_already_due(db, *, user_id: int, order_id: str, item_hash: str) -> dict | None:
    clean_hash = str(item_hash or "").strip()
    clean_order_id = str(order_id or "").strip().upper()
    if not clean_hash:
        return None
    active_report = db.replacement_reports.find_one(
        {
            "user_id": int(user_id or 0),
            "$or": [{"item_hash": clean_hash}, {"items.item_hash": clean_hash}],
            "status": {"$nin": ["cancelled", "rejected", "closed"]},
        },
        {"report_id": 1, "status": 1, "created_at": 1},
        sort=[("created_at", -1)],
    )
    if active_report:
        return {"kind": "report", "id": str(active_report.get("report_id") or ""), "status": str(active_report.get("status") or "")}
    active_obligation_query = {
        "item_hash": clean_hash,
        "$or": [
            {"source_order_id": clean_order_id},
            {"order_id": clean_order_id},
            {"source_order_id": {"$exists": False}},
        ],
        "$and": [{"$or": [{"fulfilled_at": {"$exists": False}}, {"fulfilled_at": None}, {"fulfilled_at": ""}]}],
    }
    active_obligation = db.stock_manager_replacement_obligations.find_one(
        active_obligation_query,
        {"report_id": 1, "created_at": 1},
        sort=[("created_at", -1)],
    )
    if active_obligation:
        return {"kind": "obligation", "id": str(active_obligation.get("report_id") or ""), "status": "pending"}
    return None


def build_manual_replacement_lookup_results(db, submitted_text: str, *, limit: int = 80) -> dict[str, Any]:
    lookup = str(submitted_text or "").strip()
    result: dict[str, Any] = {"query": lookup, "matches": [], "total_matches": 0, "limited": False, "searched_emails": sorted(report_emails_in_text(lookup))}
    if len(normalize_report_match_text(lookup)) < 3:
        return result
    seen: set[tuple[str, str]] = set()
    projection = {
        "order_id": 1,
        "product_name": 1,
        "items": 1,
        "status": 1,
        "user_id": 1,
        "username": 1,
        "created_at": 1,
        "delivered_at": 1,
        "is_replacement": 1,
        "manual_replacement_delivery": 1,
        "admin_stock_delivery": 1,
        "payment_method": 1,
        "replacement_report_id": 1,
    }
    for order in db.orders.find({"status": "delivered", "items.0": {"$exists": True}}, projection).sort("delivered_at", -1):
        order_id = str(order.get("order_id") or "").strip().upper()
        product_name = str(order.get("product_name") or "Unknown product").strip() or "Unknown product"
        for item in order.get("items", []) or []:
            item_text = str(item or "").strip()
            if not item_text or not report_submitted_item_matches(lookup, item_text):
                continue
            item_hash = stock_item_hash(item_text)
            seen_key = (order_id, item_hash)
            if seen_key in seen:
                continue
            seen.add(seen_key)
            result["total_matches"] = int(result.get("total_matches") or 0) + 1
            if len(result["matches"]) >= limit:
                result["limited"] = True
                continue
            info = stock_upload_info_for_item(db, product_name, item_text)
            uploaded_by = str(info.get("uploaded_by") or "").strip()
            uploaded_role = str(info.get("uploaded_role") or "").strip()
            existing_due = replacement_item_already_due(db, user_id=int(order.get("user_id", 0) or 0), order_id=order_id, item_hash=item_hash)
            is_replacement = bool(order.get("is_replacement"))
            try:
                endpoint = "replacement_order_detail" if is_replacement else "order_detail"
                order_url = url_for(endpoint, order_id=order_id)
            except Exception:
                order_url = ""
            can_add_due = (
                bool(order_id)
                and bool(item_hash)
                and uploaded_by
                and uploaded_by.lower() != "unknown"
                and not existing_due
            )
            result["matches"].append({
                "order_id": order_id,
                "order_url": order_url,
                "product_name": product_name,
                "user_id": int(order.get("user_id", 0) or 0),
                "username": str(order.get("username") or ""),
                "user_label": telegram_user_display(db, order.get("user_id"), order.get("username")),
                "item": item_text,
                "submitted_item": report_submitted_match_snippet(lookup, item_text),
                "item_hash": item_hash,
                "delivered_at": order.get("delivered_at") or order.get("created_at"),
                "uploaded_by": uploaded_by or "Unknown",
                "uploaded_role": uploaded_role,
                "uploaded_at": info.get("uploaded_at"),
                "source": "Replacement delivery" if is_replacement else ("Admin stock send" if order.get("admin_stock_delivery") else "Paid order"),
                "existing_due": existing_due,
                "can_add_due": can_add_due,
                "cannot_add_reason": "" if can_add_due else ("Already added to pending replacements" if existing_due else "Original stock manager was not found"),
            })
    return result


def new_manual_replacement_report_id(db) -> str:
    for _ in range(40):
        report_id = "MANUAL-" + secrets.token_hex(4).upper()
        if not db.replacement_reports.find_one({"report_id": report_id}, {"_id": 1}):
            return report_id
    return "MANUAL-" + secrets.token_hex(6).upper()


def create_manual_replacement_due_from_order_item(db, *, order_id: str, item_hash: str, submitted_text: str = "", admin_note: str = "", added_by: str = "owner") -> tuple[str | None, str | None]:
    clean_order_id = str(order_id or "").strip().upper()
    clean_hash = str(item_hash or "").strip()
    if not clean_order_id or not clean_hash:
        return None, "Missing order/item details. Search the manual report again and retry."
    order = db.orders.find_one({"order_id": clean_order_id, "status": "delivered", "items.0": {"$exists": True}})
    if not order:
        return None, "Delivered order was not found."
    matched_item = ""
    for item in order.get("items", []) or []:
        item_text = str(item or "").strip()
        if item_text and stock_item_hash(item_text) == clean_hash:
            matched_item = item_text
            break
    if not matched_item:
        return None, "That stock item was not found inside the selected order."
    user_id = int(order.get("user_id", 0) or 0)
    existing_due = replacement_item_already_due(db, user_id=user_id, order_id=clean_order_id, item_hash=clean_hash)
    if existing_due:
        return None, f"This item is already pending as {existing_due.get('id') or 'a replacement due'}."
    product_name = str(order.get("product_name") or "").strip()
    if not product_name:
        return None, "Order has no product name."
    upload_info = stock_upload_info_for_item(db, product_name, matched_item)
    stock_manager = str(upload_info.get("uploaded_by") or "").strip()
    stock_manager_key = normalize_admin_username(stock_manager)
    if not stock_manager or stock_manager.lower() == "unknown" or not stock_manager_key:
        return None, "Original stock manager could not be found for this item."
    now = utcnow()
    report_id = new_manual_replacement_report_id(db)
    submitted_item = report_submitted_match_snippet(submitted_text, matched_item) if submitted_text else matched_item[:1000]
    clean_note = str(admin_note or "").strip()[:1000]
    report_doc = {
        "report_id": report_id,
        "user_id": user_id,
        "username": str(order.get("username") or "").strip(),
        "product_name": product_name,
        "order_id": clean_order_id,
        "payment_method": str(order.get("payment_method") or ""),
        "submitted_item": submitted_item,
        "delivered_item": matched_item,
        "item_hash": clean_hash,
        "sold_at": order.get("delivered_at") or order.get("created_at"),
        "order_created_at": order.get("created_at"),
        "stock_added_by_username": stock_manager,
        "stock_added_by_role": str(upload_info.get("uploaded_role") or ""),
        "stock_added_at": upload_info.get("uploaded_at"),
        "status": "pending",
        "issue_text": clean_note or "Manual report added by owner from pasted user report.",
        "screenshot_file_id": "",
        "created_at": now,
        "manual_report_lookup": True,
        "manual_report_created_by": added_by or "owner",
        "approved_at": now,
        "approved_by": added_by or "owner",
        "replacement_queued_at": now,
        "replacement_admin_note": clean_note,
        "items": [{
            "order_id": clean_order_id,
            "product_name": product_name,
            "payment_method": str(order.get("payment_method") or ""),
            "submitted_item": submitted_item,
            "delivered_item": matched_item,
            "item_hash": clean_hash,
            "sold_at": order.get("delivered_at") or order.get("created_at"),
            "order_created_at": order.get("created_at"),
            "stock_added_by_username": stock_manager,
            "stock_added_by_role": str(upload_info.get("uploaded_role") or ""),
            "stock_added_at": upload_info.get("uploaded_at"),
            "stock_metadata_product_name": product_name,
        }],
        "product_names": [product_name],
        "order_ids": [clean_order_id],
        "item_count": 1,
        "replacement_obligation_count": 1,
        "replacement_obligations_created_at": now,
    }
    obligation_key = f"{report_id}:0:{clean_hash}"
    try:
        db.replacement_reports.insert_one(report_doc)
        db.stock_manager_replacement_obligations.insert_one({
            "obligation_key": obligation_key,
            "report_id": report_id,
            "report_item_index": 0,
            "product_name": product_name,
            "submitted_item": submitted_item,
            "delivered_item": matched_item,
            "item_hash": clean_hash,
            "stock_added_by_username": stock_manager,
            "stock_added_by_username_key": stock_manager_key,
            "stock_added_at": upload_info.get("uploaded_at"),
            "source": "manual_report_lookup",
            "manual_report_lookup": True,
            "source_order_id": clean_order_id,
            "source_user_id": user_id,
            "source_username": str(order.get("username") or "").strip(),
            "approved_at": now,
            "approved_by": added_by or "owner",
            "created_at": now,
            "fulfilled_at": None,
            "fulfilled_by": "",
            "fulfilled_product_name": "",
        })
    except DuplicateKeyError:
        return None, "This manual replacement due was already created."
    return report_id, None


def normalize_admin_username(username: str | None) -> str:
    return str(username or "").strip().lower()


def build_stock_added_by_records(
    items: list[str],
    username: str,
    role: str,
    manager_earning_rate_usdt: float = 0.0,
    owner_due_rate_usdt: float = 0.0,
    stock_upload_kind: str = "normal",
) -> list[dict[str, Any]]:
    added_at = utcnow()
    clean_username = str(username or "").strip()
    clean_role = normalize_admin_role(role)
    earning_rate = round(safe_float(manager_earning_rate_usdt, 0.0), 3)
    owner_due_rate = round(safe_float(owner_due_rate_usdt, 0.0), 3)
    upload_kind = "replacement" if str(stock_upload_kind or "").strip().lower() == "replacement" else "normal"
    return [
        {
            "item_hash": stock_item_hash(item),
            "added_by_username": clean_username,
            "added_by_role": clean_role,
            "added_at": added_at,
            "stock_manager_earning_rate_usdt": 0.0 if upload_kind == "replacement" else earning_rate,
            "stock_manager_owner_rate_usdt": 0.0 if upload_kind == "replacement" else owner_due_rate,
            "stock_upload_kind": upload_kind,
        }
        for item in items
        if str(item or "").strip()
    ]


def stock_owner_lookup(product: dict) -> dict[str, dict]:
    lookup: dict[str, dict] = {}
    records = product.get("stock_added_by", []) or []
    if not isinstance(records, list):
        return lookup
    for record in records:
        if not isinstance(record, dict):
            continue
        item_hash = str(record.get("item_hash") or "").strip()
        if item_hash:
            lookup[item_hash] = record
    return lookup


def stock_item_is_owned_by(product: dict, item: str, username: str | None) -> bool:
    username_key = normalize_admin_username(username)
    if not username_key:
        return False
    record = stock_owner_lookup(product).get(stock_item_hash(item))
    if not record:
        return False
    return normalize_admin_username(record.get("added_by_username")) == username_key


def build_current_stock_view(product: dict, viewer_username: str | None = None) -> list[dict[str, Any]]:
    stock = [str(item) for item in (product.get("stock", []) or [])]
    owner_lookup = stock_owner_lookup(product)
    viewer_key = normalize_admin_username(viewer_username)
    rows: list[dict[str, Any]] = []
    for index, item in enumerate(stock, 1):
        record = owner_lookup.get(stock_item_hash(item)) or {}
        added_by_username = str(record.get("added_by_username") or "").strip()
        can_view = not viewer_key or normalize_admin_username(added_by_username) == viewer_key
        rows.append({
            "number": index,
            "text": item if can_view else "",
            "can_view": can_view,
            "added_by_username": added_by_username,
        })
    return rows


def get_stock_visibility_summary(product: dict, username: str | None) -> dict[str, int]:
    rows = build_current_stock_view(product, viewer_username=username)
    visible = sum(1 for row in rows if row.get("can_view"))
    hidden = max(0, len(rows) - visible)
    return {"visible": visible, "hidden": hidden}


def normalize_stock_lookup_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip().lower())


def stock_lookup_terms(query: str) -> list[str]:
    clean = normalize_stock_lookup_text(query)
    return [part for part in re.split(r"\s+", clean) if len(part) >= 2]


def stock_item_matches_query(item: Any, query: str) -> bool:
    item_text = normalize_stock_lookup_text(item)
    query_text = normalize_stock_lookup_text(query)
    if not item_text or not query_text:
        return False
    if query_text in item_text:
        return True
    terms = stock_lookup_terms(query_text)
    return bool(terms) and all(term in item_text for term in terms)


def first_stock_upload_event(db, product_name: str, item_hash: str) -> dict | None:
    clean_hash = str(item_hash or "").strip()
    if not clean_hash:
        return None
    queries: list[dict[str, Any]] = []
    clean_product = str(product_name or "").strip()
    if clean_product:
        queries.append({"type": "add", "item_hashes": clean_hash, "product_name": name_regex(clean_product)})
    queries.append({"type": "add", "item_hashes": clean_hash})
    for query in queries:
        try:
            event = db.stock_manager_stock_events.find_one(query, sort=[("created_at", 1)])
        except Exception:
            event = None
        if event:
            return event
    return None


def stock_upload_info_for_item(db, product_name: str, item: Any, current_product: dict | None = None) -> dict[str, Any]:
    clean_item = str(item or "").strip()
    item_hash = stock_item_hash(clean_item)
    record: dict[str, Any] = {}
    if isinstance(current_product, dict):
        record = stock_owner_lookup(current_product).get(item_hash) or {}
    if not record:
        event = first_stock_upload_event(db, product_name, item_hash) or {}
        if event:
            record = {
                "added_by_username": event.get("username"),
                "added_by_role": event.get("role"),
                "added_at": event.get("created_at"),
                "stock_upload_kind": event.get("stock_upload_kind"),
            }
    if not record:
        ledger_query = {"item_hash": stock_ledger_item_hash(clean_item)}
        product_key = stock_ledger_product_key(product_name)
        if product_key:
            ledger_query = {"product_key": product_key, "item_hash": stock_ledger_item_hash(clean_item)}
        ledger = db.stock_item_ledger.find_one(ledger_query, sort=[("first_added_at", 1)]) or {}
        if ledger:
            record = {
                "added_by_username": ledger.get("first_added_by_username") or ledger.get("last_added_by_username"),
                "added_by_role": ledger.get("first_added_by_role") or ledger.get("last_added_by_role"),
                "added_at": ledger.get("first_added_at") or ledger.get("last_added_at"),
                "stock_upload_kind": ledger.get("stock_upload_kind"),
            }
    return {
        "uploaded_by": str(record.get("added_by_username") or "Unknown").strip() or "Unknown",
        "uploaded_role": str(record.get("added_by_role") or "").strip(),
        "uploaded_at": record.get("added_at"),
        "upload_kind": str(record.get("stock_upload_kind") or "normal").strip().lower(),
    }


def build_stock_lookup_results(db, query: str, *, limit: int = 200) -> dict[str, Any]:
    lookup = str(query or "").strip()
    result: dict[str, Any] = {"query": lookup, "matches": [], "total_matches": 0, "limited": False}
    if len(normalize_stock_lookup_text(lookup)) < 2:
        return result

    seen_ledger_keys: set[tuple[str, str]] = set()

    def remember_ledger_item(product_name: Any, item: Any) -> None:
        product_key = stock_ledger_product_key(product_name)
        item_hash = stock_ledger_item_hash(item)
        if product_key and item_hash:
            seen_ledger_keys.add((product_key, item_hash))

    def add_match(row: dict[str, Any]) -> None:
        result["total_matches"] = int(result.get("total_matches") or 0) + 1
        if len(result["matches"]) < limit:
            result["matches"].append(row)
        else:
            result["limited"] = True

    # Current inventory lookup. This helps verify whether a reported item is still unsold.
    for product in db.products.find({}, {"name": 1, "stock": 1, "stock_added_by": 1}):
        product_name = str(product.get("name") or "Unknown product")
        for index, item in enumerate(product.get("stock", []) or [], 1):
            if not stock_item_matches_query(item, lookup):
                continue
            info = stock_upload_info_for_item(db, product_name, item, current_product=product)
            remember_ledger_item(product_name, item)
            add_match({
                "status": "Available in inventory",
                "status_class": "available",
                "product_name": product_name,
                "item": str(item or "").strip(),
                "stock_position": index,
                "uploaded_by": info.get("uploaded_by"),
                "uploaded_role": info.get("uploaded_role"),
                "uploaded_at": info.get("uploaded_at"),
                "delivered_at": None,
                "order_id": "",
                "order_url": "",
                "user_label": "Not delivered",
                "source": "Current product stock",
            })

    order_projection = {
        "order_id": 1,
        "product_name": 1,
        "items": 1,
        "status": 1,
        "user_id": 1,
        "username": 1,
        "created_at": 1,
        "delivered_at": 1,
        "is_replacement": 1,
        "manual_replacement_delivery": 1,
        "admin_stock_delivery": 1,
        "admin_stock_source": 1,
        "replacement_report_id": 1,
        "payment_method": 1,
    }
    for order in db.orders.find({"items.0": {"$exists": True}}, order_projection).sort("created_at", -1):
        product_name = str(order.get("product_name") or "Unknown product")
        for item in order.get("items", []) or []:
            if not stock_item_matches_query(item, lookup):
                continue
            info = stock_upload_info_for_item(db, product_name, item)
            remember_ledger_item(product_name, item)
            is_replacement = bool(order.get("is_replacement"))
            is_admin_stock = bool(order.get("admin_stock_delivery"))
            if is_replacement:
                status = "Replacement delivered"
                source = "Manual replacement" if order.get("manual_replacement_delivery") else "Replacement delivery"
                endpoint = "replacement_order_detail"
            elif is_admin_stock:
                status = "Admin-sent order"
                source = "Admin stock send"
                endpoint = "order_detail"
            else:
                status = "Sold / delivered order" if order.get("status") == "delivered" else str(order.get("status") or "Order")
                source = "Paid order"
                endpoint = "order_detail"
            try:
                order_url = url_for(endpoint, order_id=str(order.get("order_id") or ""))
            except Exception:
                order_url = ""
            add_match({
                "status": status,
                "status_class": "delivered" if order.get("status") == "delivered" else "other",
                "product_name": product_name,
                "item": str(item or "").strip(),
                "stock_position": None,
                "uploaded_by": info.get("uploaded_by"),
                "uploaded_role": info.get("uploaded_role"),
                "uploaded_at": info.get("uploaded_at"),
                "delivered_at": order.get("delivered_at") or order.get("created_at"),
                "order_id": str(order.get("order_id") or ""),
                "order_url": order_url,
                "user_label": telegram_user_display(db, order.get("user_id"), order.get("username")),
                "source": source,
                "replacement_report_id": str(order.get("replacement_report_id") or ""),
            })

    # Permanent item ledger lookup. This keeps stock searchable even after it was
    # removed from live stock, revoked, transferred, or hit an unusual failed delivery path.
    terms = stock_lookup_terms(lookup)
    ledger_query: dict[str, Any] = {}
    if terms:
        # Use the longest useful term to reduce the scan, then apply the same
        # Python matcher used for current/order stock so multi-line stock still works.
        strongest = max(terms, key=len)
        ledger_query = {"item_search_text": {"$regex": re.escape(strongest), "$options": "i"}}
    try:
        ledger_cursor = db.stock_item_ledger.find(ledger_query).sort("last_movement_at", -1).limit(1000)
    except Exception:
        ledger_cursor = []
    for ledger in ledger_cursor:
        item_text = str(ledger.get("item_text") or "").strip()
        product_name = str(ledger.get("product_name") or "Unknown product").strip() or "Unknown product"
        if not stock_item_matches_query(item_text, lookup):
            continue
        ledger_key = (stock_ledger_product_key(product_name), stock_ledger_item_hash(item_text))
        if ledger_key in seen_ledger_keys:
            continue
        seen_ledger_keys.add(ledger_key)
        status_label_text, status_class = stock_ledger_status_label(ledger.get("current_status"))
        order_id = str(ledger.get("current_order_id") or "").strip().upper()
        order_url = ""
        if order_id:
            try:
                order_row = db.orders.find_one({"order_id": order_id}, {"is_replacement": 1}) or {}
                endpoint = "replacement_order_detail" if order_row.get("is_replacement") else "order_detail"
                order_url = url_for(endpoint, order_id=order_id)
            except Exception:
                order_url = ""
        user_id = ledger.get("current_user_id")
        user_label = "Not delivered"
        if user_id:
            user_label = telegram_user_display(db, user_id, ledger.get("current_username"))
        add_match({
            "status": status_label_text,
            "status_class": status_class,
            "product_name": product_name,
            "item": item_text,
            "stock_position": None,
            "uploaded_by": str(ledger.get("first_added_by_username") or ledger.get("last_added_by_username") or "Unknown").strip() or "Unknown",
            "uploaded_role": str(ledger.get("first_added_by_role") or ledger.get("last_added_by_role") or "").strip(),
            "uploaded_at": ledger.get("first_added_at") or ledger.get("last_added_at"),
            "delivered_at": ledger.get("current_status_at"),
            "order_id": order_id,
            "order_url": order_url,
            "user_label": user_label,
            "source": "Stock item ledger",
            "replacement_report_id": "",
        })

    return result


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or str(value).strip() == "":
            return float(default)
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def record_stock_manager_stock_event(
    db,
    event_type: str,
    product_name: str,
    items: list[str],
    *,
    username: str,
    role: str,
    manager_earning_rate_usdt: float | None = None,
    owner_due_rate_usdt: float | None = None,
    stock_upload_kind: str = "normal",
) -> None:
    clean_items = [str(item).strip() for item in (items or []) if str(item or "").strip()]
    clean_username = str(username or "").strip()
    if not clean_items or not clean_username:
        return
    try:
        event_doc = {
            "type": str(event_type or "").strip().lower(),
            "product_name": str(product_name or "").strip(),
            "username": clean_username,
            "username_key": normalize_admin_username(clean_username),
            "role": normalize_admin_role(role),
            "quantity": len(clean_items),
            "item_hashes": [stock_item_hash(item) for item in clean_items],
            "created_at": utcnow(),
            "stock_upload_kind": "replacement" if str(stock_upload_kind or "").strip().lower() == "replacement" else "normal",
        }
        if manager_earning_rate_usdt is not None:
            event_doc["stock_manager_earning_rate_usdt"] = round(max(0.0, safe_float(manager_earning_rate_usdt, 0.0)), 3)
        if owner_due_rate_usdt is not None:
            event_doc["stock_manager_owner_rate_usdt"] = round(max(0.0, safe_float(owner_due_rate_usdt, 0.0)), 3)
        db.stock_manager_stock_events.insert_one(event_doc)
    except Exception:
        pass


def product_name_key(name: Any) -> str:
    return str(name or "").strip().lower()



# ─────────────────────── CUSTOM PRICING ───────────────────────
PRICE_GROUPS = [
    {"key": "normal", "label": "Normal"},
    {"key": "vip", "label": "VIP"},
    {"key": "reseller", "label": "Reseller"},
    {"key": "wholesale", "label": "Wholesale"},
]
PRICE_GROUP_KEYS = {row["key"] for row in PRICE_GROUPS}
DEFAULT_PRICE_GROUP = "normal"


def normalize_price_group(value: Any) -> str:
    key = str(value or "").strip().lower().replace(" ", "_")
    return key if key in PRICE_GROUP_KEYS else DEFAULT_PRICE_GROUP


def price_group_label(value: Any) -> str:
    key = normalize_price_group(value)
    for row in PRICE_GROUPS:
        if row["key"] == key:
            return row["label"]
    return "Normal"


def clean_price_value(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return None
    if amount < 0:
        return None
    return round(amount, 2)


def price_row_value(row: Any, field: str) -> float | None:
    if not isinstance(row, dict) or field not in row or row.get(field) is None:
        return None
    return clean_price_value(row.get(field))


def clean_price_group_prices(value: Any) -> dict[str, dict[str, float]]:
    value = value if isinstance(value, dict) else {}
    cleaned: dict[str, dict[str, float]] = {}
    for group in PRICE_GROUPS:
        key = group["key"]
        if key == DEFAULT_PRICE_GROUP:
            continue
        row = value.get(key) if isinstance(value.get(key), dict) else {}
        prices: dict[str, float] = {}
        inr = price_row_value(row, "price_inr")
        usdt = price_row_value(row, "price_usdt")
        if inr is not None:
            prices["price_inr"] = inr
        if usdt is not None:
            prices["price_usdt"] = usdt
        if prices:
            cleaned[key] = prices
    return cleaned


def build_price_group_rows(product: dict) -> list[dict[str, Any]]:
    group_prices = clean_price_group_prices((product or {}).get("price_group_prices"))
    rows = []
    for group in PRICE_GROUPS:
        key = group["key"]
        if key == DEFAULT_PRICE_GROUP:
            continue
        prices = group_prices.get(key, {})
        rows.append({
            "key": key,
            "label": group["label"],
            "price_inr": prices.get("price_inr"),
            "price_usdt": prices.get("price_usdt"),
        })
    return rows


def product_effective_price(product: dict, user: dict | None = None, custom_price: dict | None = None) -> dict:
    product = dict(product or {})
    group_key = normalize_price_group((user or {}).get("pricing_group"))
    group_prices = clean_price_group_prices(product.get("price_group_prices")).get(group_key) if group_key != DEFAULT_PRICE_GROUP else None
    default_inr = clean_price_value(product.get("price_inr")) or 0.0
    default_usdt = clean_price_value(product.get("price_usdt")) or 0.0
    price_inr = default_inr
    price_usdt = default_usdt
    source = "default"
    group_inr = price_row_value(group_prices, "price_inr")
    group_usdt = price_row_value(group_prices, "price_usdt")
    if group_inr is not None:
        price_inr = group_inr
        source = f"group:{group_key}"
    if group_usdt is not None:
        price_usdt = group_usdt
        source = f"group:{group_key}"
    custom_inr = price_row_value(custom_price, "price_inr")
    custom_usdt = price_row_value(custom_price, "price_usdt")
    if custom_inr is not None:
        price_inr = custom_inr
        source = "custom"
    if custom_usdt is not None:
        price_usdt = custom_usdt
        source = "custom"
    product["default_price_inr"] = default_inr
    product["default_price_usdt"] = default_usdt
    product["price_inr"] = round(max(0.0, price_inr), 2)
    product["price_usdt"] = round(max(0.0, price_usdt), 2)
    product["effective_price_group"] = group_key
    product["effective_price_group_label"] = price_group_label(group_key)
    product["effective_price_source"] = source
    return product


def user_product_custom_price_map(db, user_id: int) -> dict[str, dict]:
    try:
        uid = int(user_id)
    except Exception:
        return {}
    rows = list(db.user_product_prices.find({"user_id": uid}))
    result = {}
    for row in rows:
        key = product_name_key(row.get("product_key") or row.get("product_name"))
        if key:
            result[key] = row
    return result


def effective_product_for_user(db, product: dict | None, user: dict | None) -> dict | None:
    if not product:
        return None
    user = user or {}
    try:
        uid = int(user.get("user_id") or 0)
    except Exception:
        uid = 0
    custom = None
    if uid:
        custom = db.user_product_prices.find_one({"user_id": uid, "product_key": product_name_key(product.get("name"))})
    return product_effective_price(product, user, custom)


def build_user_price_override_rows(db, user: dict, products: list[dict]) -> list[dict[str, Any]]:
    product_by_key = {product_name_key(product.get("name")): product for product in products or []}
    rows = []
    try:
        uid = int((user or {}).get("user_id") or 0)
    except Exception:
        uid = 0
    if not uid:
        return rows
    for custom in db.user_product_prices.find({"user_id": uid}).sort("product_name", 1):
        key = product_name_key(custom.get("product_key") or custom.get("product_name"))
        product = product_by_key.get(key) or {"name": custom.get("product_name") or key}
        effective = product_effective_price(product, user, custom)
        rows.append({
            "product_key": key,
            "product_name": custom.get("product_name") or product.get("name") or key,
            "price_inr": custom.get("price_inr"),
            "price_usdt": custom.get("price_usdt"),
            "effective_price_inr": effective.get("price_inr"),
            "effective_price_usdt": effective.get("price_usdt"),
            "updated_at": custom.get("updated_at"),
        })
    return rows


def stock_record_rates(record: dict, product: dict | None = None) -> tuple[float, float]:
    product = product or {}
    earning_rate = safe_float(
        record.get("stock_manager_earning_rate_usdt", product.get("stock_manager_earning_rate_usdt", 0.0)),
        safe_float(product.get("stock_manager_earning_rate_usdt"), 0.0),
    )
    owner_due_rate = safe_float(
        record.get("stock_manager_owner_rate_usdt", product.get("stock_manager_owner_rate_usdt", 0.0)),
        safe_float(product.get("stock_manager_owner_rate_usdt"), 0.0),
    )
    return max(0.0, earning_rate), max(0.0, owner_due_rate)


def make_stock_manager_product_stats(product_name: str) -> dict[str, Any]:
    return {
        "product_name": product_name or "Unknown product",
        "added": 0,
        "current": 0,
        "sold": 0,
        "removed": 0,
        "other_legacy": 0,
        "manager_earning_usdt": 0.0,
        "owner_due_usdt": 0.0,
        "configured_manager_rate_usdt": 0.0,
        "configured_owner_due_rate_usdt": 0.0,
        "last_activity_at": None,
    }


def touch_stock_manager_product_stats(stats: dict[str, Any], at_value: Any) -> None:
    if not at_value:
        return
    current = sort_dt(stats.get("last_activity_at"))
    new = sort_dt(at_value)
    if new >= current:
        stats["last_activity_at"] = at_value


def build_stock_ownership_index(db) -> dict[str, Any]:
    by_product_hash: dict[tuple[str, str], dict] = {}
    by_hash: dict[str, list[dict]] = {}
    metadata_counts: dict[tuple[str, str], int] = {}
    products = list(db.products.find({}, {
        "name": 1,
        "stock": 1,
        "stock_added_by": 1,
        "stock_manager_earning_rate_usdt": 1,
        "stock_manager_owner_rate_usdt": 1,
    }))
    for product in products:
        current_product_name = str(product.get("name") or "Unknown product")
        for record in product.get("stock_added_by", []) or []:
            if not isinstance(record, dict):
                continue
            item_hash = str(record.get("item_hash") or "").strip()
            added_by = str(record.get("added_by_username") or "").strip()
            if not item_hash or not added_by:
                continue
            earning_rate, owner_due_rate = stock_record_rates(record, product)
            enriched = dict(record)
            enriched.update({
                "item_hash": item_hash,
                "added_by_username": added_by,
                "username_key": normalize_admin_username(added_by),
                "product_name": current_product_name,
                "product_name_key": product_name_key(current_product_name),
                "stock_manager_earning_rate_usdt": earning_rate,
                "stock_manager_owner_rate_usdt": owner_due_rate,
                "stock_upload_kind": str(record.get("stock_upload_kind") or "normal").strip().lower(),
                "product": product,
            })
            by_product_hash[(product_name_key(current_product_name), item_hash)] = enriched
            by_hash.setdefault(item_hash, []).append(enriched)
            key = (normalize_admin_username(added_by), current_product_name)
            metadata_counts[key] = metadata_counts.get(key, 0) + 1
    return {
        "products": products,
        "by_product_hash": by_product_hash,
        "by_hash": by_hash,
        "metadata_counts": metadata_counts,
    }


def find_stock_owner_record(index: dict[str, Any], product_name: str, item: str) -> dict | None:
    item_hash = stock_item_hash(item)
    direct = index["by_product_hash"].get((product_name_key(product_name), item_hash))
    if direct:
        return direct
    candidates = index["by_hash"].get(item_hash, [])
    return candidates[0] if len(candidates) == 1 else None


def get_stock_manager_paid_total(db, username: str) -> float:
    username_key = normalize_admin_username(username)
    if not username_key:
        return 0.0
    total = 0.0
    for payout in db.stock_manager_payouts.find({"username_key": username_key}, {"amount_usdt": 1}):
        total += max(0.0, safe_float(payout.get("amount_usdt"), 0.0))
    return round(total, 2)


def get_stock_manager_payment_details(db, username: str) -> dict[str, Any]:
    username_key = normalize_admin_username(username)
    for account in get_admin_accounts(db):
        if normalize_admin_username(account.get("username")) == username_key:
            method = normalize_stock_manager_payment_method(account.get("payment_method"))
            methods = clean_stock_manager_payment_methods(account.get("payment_methods"))
            details = format_stock_manager_payment_details(account)
            return {
                "payment_method": method,
                "payment_method_label": stock_manager_payment_method_label(method),
                "payment_methods": methods,
                "payment_details": details,
                "has_payment_details": stock_manager_method_has_details(method, methods) and bool(details),
                "payment_details_updated_at": str(account.get("payment_details_updated_at") or "").strip(),
            }
    return {
        "payment_method": "upi",
        "payment_method_label": stock_manager_payment_method_label("upi"),
        "payment_methods": clean_stock_manager_payment_methods({}),
        "payment_details": "",
        "has_payment_details": False,
        "payment_details_updated_at": "",
    }


def get_stock_manager_payout_history(db, username: str, limit: int = 50) -> list[dict[str, Any]]:
    username_key = normalize_admin_username(username)
    if not username_key:
        return []
    return list(db.stock_manager_payouts.find({"username_key": username_key}).sort("created_at", -1).limit(limit))


def _enrich_stock_manager_payout_request(db, request_doc: dict[str, Any]) -> dict[str, Any]:
    request_doc = dict(request_doc or {})
    status = str(request_doc.get("status") or "pending").strip().lower()
    if status == "paid":
        request_doc["paid_by_label"] = stock_manager_payout_paid_by_label(db, request_doc)
        if not str(request_doc.get("note") or "").strip() and request_doc.get("_id") is not None:
            try:
                linked_payout = db.stock_manager_payouts.find_one(
                    {"payment_request_id": str(request_doc.get("_id"))},
                    {"note": 1},
                )
            except Exception:
                linked_payout = None
            if linked_payout and str(linked_payout.get("note") or "").strip():
                request_doc["note"] = str(linked_payout.get("note") or "").strip()
    else:
        request_doc["paid_by_label"] = "—"
    request_doc["note_label"] = str(request_doc.get("note") or "").strip() or "—"
    return request_doc


def get_stock_manager_payout_requests(db, username: str, limit: int = 50) -> list[dict[str, Any]]:
    username_key = normalize_admin_username(username)
    if not username_key:
        return []
    request_docs = [
        _enrich_stock_manager_payout_request(db, doc)
        for doc in db.stock_manager_payment_requests.find({"username_key": username_key}).sort("requested_at", -1).limit(limit)
    ]
    linked_request_ids = {str(doc.get("_id")) for doc in request_docs if doc.get("_id") is not None}

    # Older/direct owner clears live in stock_manager_payouts only. Surface them in
    # the single payout request history so the UI no longer needs a separate
    # payment history table. Linked payouts are skipped to avoid duplicates.
    for payout in db.stock_manager_payouts.find({"username_key": username_key}).sort("created_at", -1).limit(limit):
        payment_request_id = str(payout.get("payment_request_id") or "").strip()
        if payment_request_id and payment_request_id in linked_request_ids:
            continue
        synthetic = {
            "username": payout.get("username") or username,
            "username_key": username_key,
            "amount_usdt": payout.get("requested_amount_usdt") or payout.get("amount_usdt"),
            "paid_amount_usdt": payout.get("amount_usdt"),
            "payment_method": payout.get("payment_method"),
            "payment_method_label": payout.get("payment_method_label"),
            "payment_details": payout.get("payment_details"),
            "status": "paid",
            "requested_at": payout.get("created_at"),
            "paid_at": payout.get("created_at"),
            "paid_by": payout.get("paid_by"),
            "paid_by_role": payout.get("paid_by_role"),
            "paid_by_role_label": payout.get("paid_by_role_label"),
            "note": payout.get("note"),
            "source": "payout",
        }
        synthetic["paid_by_label"] = stock_manager_payout_paid_by_label(db, synthetic)
        synthetic["note_label"] = str(synthetic.get("note") or "").strip() or "—"
        request_docs.append(synthetic)

    request_docs.sort(
        key=lambda doc: sort_dt(doc.get("requested_at") or doc.get("paid_at") or doc.get("created_at")),
        reverse=True,
    )
    return request_docs[:limit]


def build_stock_manager_dashboard(db, username: str) -> dict[str, Any]:
    username_key = normalize_admin_username(username)
    assigned_product_names = get_stock_manager_assigned_product_names(db, username)
    assigned_product_keys = {product_name_key(name) for name in assigned_product_names}
    product_rows: dict[str, dict[str, Any]] = {}

    def product_is_assigned(product_name: Any) -> bool:
        return product_name_key(product_name) in assigned_product_keys

    def get_row(product_name: str) -> dict[str, Any]:
        key = str(product_name or "Unknown product")
        if key not in product_rows:
            product_rows[key] = make_stock_manager_product_stats(key)
        return product_rows[key]

    products_for_rates = list(db.products.find({}, {
        "name": 1,
        "stock_manager_earning_rate_usdt": 1,
        "stock_manager_owner_rate_usdt": 1,
    }))
    rate_by_product = {
        product_name_key(product.get("name")): (
            max(0.0, safe_float(product.get("stock_manager_earning_rate_usdt"), 0.0)),
            max(0.0, safe_float(product.get("stock_manager_owner_rate_usdt"), 0.0)),
        )
        for product in products_for_rates
    }
    event_added_counts: dict[str, int] = {}

    # The add-event ledger is the source for lifetime "stock added" earnings.
    # Earnings are based on stock added, not on sold count.
    for event in db.stock_manager_stock_events.find({"username_key": username_key}):
        product_name = str(event.get("product_name") or "Unknown product")
        if not product_is_assigned(product_name):
            continue
        product_key = product_name_key(product_name)
        row = get_row(product_name)
        qty = max(0, int(event.get("quantity") or len(event.get("item_hashes", []) or []) or 0))
        fallback_earning, fallback_owner_due = rate_by_product.get(product_key, (0.0, 0.0))
        event_upload_kind = str(event.get("stock_upload_kind") or "normal").strip().lower()
        if event.get("type") == "add" and event_upload_kind != "replacement":
            earning_rate = max(0.0, safe_float(event.get("stock_manager_earning_rate_usdt"), fallback_earning))
            owner_due_rate = max(0.0, safe_float(event.get("stock_manager_owner_rate_usdt"), fallback_owner_due))
            row["added"] += qty
            row["manager_earning_usdt"] += qty * earning_rate
            row["owner_due_usdt"] += qty * owner_due_rate
            row["configured_manager_rate_usdt"] = fallback_earning or earning_rate
            row["configured_owner_due_rate_usdt"] = fallback_owner_due or owner_due_rate
            event_added_counts[product_key] = event_added_counts.get(product_key, 0) + qty
        elif event.get("type") == "remove":
            row["removed"] += qty
        touch_stock_manager_product_stats(row, event.get("created_at"))

    ownership_index = build_stock_ownership_index(db)

    for product in ownership_index["products"]:
        product_name = str(product.get("name") or "Unknown product")
        if not product_is_assigned(product_name):
            continue
        product_key = product_name_key(product_name)
        stock = [str(item) for item in (product.get("stock", []) or [])]
        current_configured_earning = max(0.0, safe_float(product.get("stock_manager_earning_rate_usdt"), 0.0))
        current_configured_owner_due = max(0.0, safe_float(product.get("stock_manager_owner_rate_usdt"), 0.0))
        metadata_records = [
            record for record in (product.get("stock_added_by", []) or [])
            if isinstance(record, dict)
            and normalize_admin_username(record.get("added_by_username")) == username_key
            and str(record.get("stock_upload_kind") or "normal").strip().lower() != "replacement"
        ]
        metadata_count = len(metadata_records)
        if metadata_count:
            row = get_row(product_name)
            row["configured_manager_rate_usdt"] = current_configured_earning
            row["configured_owner_due_rate_usdt"] = current_configured_owner_due
            # Backward-compatible fallback for projects that have stock metadata but
            # no event ledger yet. Do not double count if add-events already exist.
            missing_from_events = max(0, metadata_count - event_added_counts.get(product_key, 0))
            if missing_from_events:
                row["added"] += missing_from_events
                for record in metadata_records[:missing_from_events]:
                    earning_rate, owner_due_rate = stock_record_rates(record, product)
                    row["manager_earning_usdt"] += earning_rate
                    row["owner_due_usdt"] += owner_due_rate
        for item in stock:
            record = find_stock_owner_record(ownership_index, product_name, item)
            if not record or record.get("username_key") != username_key:
                continue
            if str(record.get("stock_upload_kind") or "normal").strip().lower() == "replacement":
                continue
            row = get_row(str(record.get("product_name") or product_name))
            row["current"] += 1
            row["configured_manager_rate_usdt"] = current_configured_earning
            row["configured_owner_due_rate_usdt"] = current_configured_owner_due
            touch_stock_manager_product_stats(row, record.get("added_at"))

    for order in db.orders.find({
        "status": "delivered",
        "items.0": {"$exists": True},
        "is_replacement": {"$ne": True},
        "payment_method": {"$ne": "replacement"},
        "delivery_revoked": {"$ne": True},
    }, {
        "product_name": 1,
        "items": 1,
        "delivered_at": 1,
        "created_at": 1,
        "is_replacement": 1,
        "payment_method": 1,
    }):
        order_product_name = str(order.get("product_name") or "")
        for item in order.get("items", []) or []:
            clean_item = str(item or "").strip()
            if not clean_item:
                continue
            record = find_stock_owner_record(ownership_index, order_product_name, clean_item)
            if not record or record.get("username_key") != username_key:
                continue
            if str(record.get("stock_upload_kind") or "normal").strip().lower() == "replacement":
                continue
            product_name = str(record.get("product_name") or order_product_name or "Unknown product")
            if not product_is_assigned(product_name):
                continue
            row = get_row(product_name)
            row["sold"] += 1
            earning_rate, owner_due_rate = stock_record_rates(record, record.get("product") or {})
            if not row.get("configured_manager_rate_usdt"):
                row["configured_manager_rate_usdt"] = earning_rate
            if not row.get("configured_owner_due_rate_usdt"):
                row["configured_owner_due_rate_usdt"] = owner_due_rate
            touch_stock_manager_product_stats(row, order.get("delivered_at") or order.get("created_at"))

    products = []
    for row in product_rows.values():
        normal_current = int(row.get("current", 0))
        normal_sold = int(row.get("sold", 0))
        normal_visible_total = normal_current + normal_sold
        lifetime_uploaded_total = max(int(row.get("added", 0)), normal_visible_total)
        row["uploaded_added"] = lifetime_uploaded_total
        # Stock-manager totals are based on normal stock submitted by the manager.
        # Replacement uploads are excluded earlier from add events and metadata.
        row["added"] = lifetime_uploaded_total
        explicit_removed = max(0, int(row.get("removed", 0)))
        # Keep removed/cleared stock separate from older untracked differences.
        # This makes the stock-manager totals explainable instead of forcing every
        # mismatch into the removed bucket. Old records may not have item-level
        # movement history, so their gap is shown as other/legacy.
        row["removed"] = explicit_removed
        tracked_total = normal_current + normal_sold + explicit_removed
        row["other_legacy"] = max(0, lifetime_uploaded_total - tracked_total)
        row["accounted_total"] = normal_current + normal_sold + explicit_removed + int(row.get("other_legacy", 0))
        row["manager_earning_usdt"] = round(float(row.get("manager_earning_usdt", 0.0)), 2)
        row["owner_due_usdt"] = round(float(row.get("owner_due_usdt", 0.0)), 2)
        products.append(row)

    products.sort(key=lambda row: (sort_dt(row.get("last_activity_at")), row.get("product_name", "").lower()), reverse=True)
    paid_usdt = get_stock_manager_paid_total(db, username)
    manager_earning_usdt = round(sum(float(row.get("manager_earning_usdt", 0.0)) for row in products), 2)
    payable_due = round(max(0.0, manager_earning_usdt - paid_usdt), 2)
    pending_request = db.stock_manager_payment_requests.find_one({"username_key": username_key, "status": "pending"}) if username_key else None
    pending_request_amount = round(max(0.0, safe_float((pending_request or {}).get("amount_usdt"), 0.0)), 2)
    clearable_payout = pending_request_amount if pending_request_amount > 0 else payable_due
    if payable_due > 0:
        clearable_payout = min(clearable_payout, payable_due)
    clearable_payout = round(max(0.0, clearable_payout), 2)
    payment_details = get_stock_manager_payment_details(db, username)
    payout_history = get_stock_manager_payout_history(db, username)
    payout_requests = get_stock_manager_payout_requests(db, username)
    replacement_summary = get_stock_manager_replacement_summary(db, username)
    summary = {
        "username": username,
        "total_added": sum(int(row.get("added", 0)) for row in products),
        "current_stock": sum(int(row.get("current", 0)) for row in products),
        "sold_stock": sum(int(row.get("sold", 0)) for row in products),
        "removed_stock": sum(int(row.get("removed", 0)) for row in products),
        "other_legacy_stock": sum(int(row.get("other_legacy", 0)) for row in products),
        "accounted_stock": sum(int(row.get("accounted_total", 0)) for row in products),
        "manager_earning_usdt": manager_earning_usdt,
        "paid_usdt": paid_usdt,
        "payable_due_usdt": payable_due,
        "pending_request_amount_usdt": pending_request_amount,
        "clearable_payout_usdt": clearable_payout,
        "can_request_payment": payable_due >= STOCK_MANAGER_MIN_PAYOUT_USDT and not bool(pending_request),
        "pending_payment_request": pending_request,
        "pending_replacement_count": int(replacement_summary.get("pending_count", 0) or 0),
        "assigned_product_count": len(assigned_product_names),
        "assigned_product_names": assigned_product_names,
        **payment_details,
    }
    return {"summary": summary, "products": products, "payout_history": payout_history, "payout_requests": payout_requests, "replacement_summary": replacement_summary}


def build_stock_manager_admin_rows(db, admin_accounts: list[dict]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for account in admin_accounts:
        if account.get("role") != ADMIN_ROLE_STOCK_MANAGER:
            continue
        username = str(account.get("username") or "").strip()
        stats = build_stock_manager_dashboard(db, username)
        rows.append({
            "account": account,
            "summary": stats.get("summary", {}),
            "products": stats.get("products", []),
            "payout_history": stats.get("payout_history", []),
            "payment_details_text": format_stock_manager_payment_details(account),
            "payment_method_label": stock_manager_payment_method_label(account.get("payment_method")),
        })
    rows.sort(key=lambda row: float(row.get("summary", {}).get("payable_due_usdt", 0.0) or 0.0), reverse=True)
    return rows


def notify_owner_stock_manager_payment_request(db, request_doc: dict) -> int:
    amount = round(float(request_doc.get("amount_usdt", 0.0) or 0.0), 2)
    username = str(request_doc.get("username") or "Stock manager")
    details = str(request_doc.get("payment_details") or "").strip()
    method_label = str(request_doc.get("payment_method_label") or stock_manager_payment_method_label(request_doc.get("payment_method")))
    text = (
        "💸 Stock manager payment request\n\n"
        f"Admin: {username}\n"
        f"Amount: ${amount:.2f} USDT\n"
        f"Payment method: {method_label}\n"
        f"Minimum threshold: ${STOCK_MANAGER_MIN_PAYOUT_USDT:.2f} USDT\n\n"
        "Payment details:\n"
        f"{details or 'No payment details saved.'}\n\n"
        "Open WebAdmin → Admins to clear the payout after payment."
    )
    sent = 0
    for admin_id in get_admin_ids(db):
        if send_telegram_message(int(admin_id), text):
            sent += 1
    return sent


def create_stock_manager_replacement_obligations(db, report: dict, approved_by: str = "owner", approved_at: datetime | None = None) -> int:
    """Record replacement stock owed by the original stock manager.

    One obligation is created per reported delivered item. It is intentionally
    independent of the report status, so the count stays due even after the
    customer receives a replacement from current stock. It is cleared only when
    that stock manager uploads replacement stock for the same product.
    """
    report_id = str((report or {}).get("report_id") or "").strip().upper()
    if not report_id:
        return 0
    now = approved_at or utcnow()
    created = 0
    for index, item in enumerate(replacement_report_items(report)):
        username = str(item.get("stock_added_by_username") or report.get("stock_added_by_username") or "").strip()
        username_key = normalize_admin_username(username)
        if not username_key:
            continue
        product_name = str(item.get("product_name") or report.get("product_name") or "").strip()
        if not product_name:
            continue
        item_hash = str(item.get("item_hash") or "").strip()
        obligation_key = f"{report_id}:{index}:{item_hash or _stable_text_hash(item.get('delivered_item') or item.get('submitted_item') or index)}"
        existing = db.stock_manager_replacement_obligations.find_one({"obligation_key": obligation_key}, {"_id": 1})
        if existing:
            continue
        db.stock_manager_replacement_obligations.insert_one({
            "obligation_key": obligation_key,
            "report_id": report_id,
            "report_item_index": index,
            "product_name": product_name,
            "submitted_item": str(item.get("submitted_item") or ""),
            "delivered_item": str(item.get("delivered_item") or ""),
            "item_hash": item_hash,
            "stock_added_by_username": username,
            "stock_added_by_username_key": username_key,
            "stock_added_at": item.get("stock_added_at") or report.get("stock_added_at"),
            "approved_at": now,
            "approved_by": approved_by or "owner",
            "created_at": now,
            "fulfilled_at": None,
            "fulfilled_by": "",
            "fulfilled_product_name": "",
        })
        created += 1
    if created:
        db.replacement_reports.update_one(
            {"_id": report.get("_id")},
            {"$set": {"replacement_obligations_created_at": now, "replacement_obligation_count": int(report.get("replacement_obligation_count") or 0) + created}},
        )
    return created


def create_owner_manual_replacement_obligations(db, *, username: str, product_name: str, quantity: int, note: str = "", added_by: str = "owner") -> int:
    clean_username = str(username or "").strip()
    username_key = normalize_admin_username(clean_username)
    clean_product = str(product_name or "").strip()
    qty = max(0, int(quantity or 0))
    if not username_key or not clean_product or qty <= 0:
        return 0
    now = utcnow()
    due_id = "OWNER-DUE-" + secrets.token_hex(4).upper()
    clean_note = str(note or "").strip()[:1000]
    docs = []
    for index in range(qty):
        docs.append({
            "obligation_key": f"{due_id}:{index}",
            "manual_due_id": due_id,
            "report_id": "",
            "report_item_index": index,
            "product_name": clean_product,
            "submitted_item": "",
            "delivered_item": "",
            "item_hash": "",
            "stock_added_by_username": clean_username,
            "stock_added_by_username_key": username_key,
            "stock_added_at": None,
            "source": "owner_manual",
            "owner_note": clean_note,
            "approved_at": now,
            "approved_by": added_by or "owner",
            "created_at": now,
            "fulfilled_at": None,
            "fulfilled_by": "",
            "fulfilled_product_name": "",
        })
    if not docs:
        return 0
    try:
        result = db.stock_manager_replacement_obligations.insert_many(docs, ordered=False)
        return len(result.inserted_ids)
    except Exception as exc:
        current_app.logger.warning("Could not add owner manual replacement due: %s", exc)
        return 0


def count_stock_manager_pending_replacements(db, username: str) -> int:
    username_key = normalize_admin_username(username)
    if not username_key:
        return 0
    return db.stock_manager_replacement_obligations.count_documents({
        "stock_added_by_username_key": username_key,
        "$or": [
            {"fulfilled_at": {"$exists": False}},
            {"fulfilled_at": None},
            {"fulfilled_at": ""},
        ],
    })


def get_stock_manager_replacement_summary(db, username: str) -> dict[str, Any]:
    username_key = normalize_admin_username(username)
    if not username_key:
        return {"pending_count": 0, "ready_count": 0, "pending_by_product": [], "pending_reports": []}
    query = {
        "stock_added_by_username_key": username_key,
        "$or": [
            {"fulfilled_at": {"$exists": False}},
            {"fulfilled_at": None},
            {"fulfilled_at": ""},
        ],
    }
    obligations = list(db.stock_manager_replacement_obligations.find(
        query,
        {"report_id": 1, "product_name": 1, "approved_at": 1, "created_at": 1},
    ).sort("approved_at", 1).limit(500))
    by_product: dict[str, dict[str, Any]] = {}
    for obligation in obligations:
        product_name = str(obligation.get("product_name") or "Unknown product")
        row = by_product.setdefault(product_name, {"product_name": product_name, "count": 0, "report_ids": [], "manual_due_ids": [], "manual_count": 0})
        row["count"] += 1
        report_id = str(obligation.get("report_id") or "")
        manual_due_id = str(obligation.get("manual_due_id") or "")
        if report_id and report_id not in row["report_ids"]:
            row["report_ids"].append(report_id)
        if obligation.get("source") == "owner_manual":
            row["manual_count"] += 1
            if manual_due_id and manual_due_id not in row["manual_due_ids"]:
                row["manual_due_ids"].append(manual_due_id)
    pending_by_product = sorted(by_product.values(), key=lambda row: row["product_name"].lower())
    return {
        "pending_count": len(obligations),
        "ready_count": 0,
        "pending_by_product": pending_by_product,
        "pending_reports": obligations,
    }


def fulfill_stock_manager_replacement_uploads(db, username: str, product_name: str, quantity: int) -> int:
    username_key = normalize_admin_username(username)
    if not username_key or quantity <= 0:
        return 0
    obligations = list(db.stock_manager_replacement_obligations.find(
        {
            "stock_added_by_username_key": username_key,
            "product_name": name_regex(product_name),
            "$or": [
                {"fulfilled_at": {"$exists": False}},
                {"fulfilled_at": None},
                {"fulfilled_at": ""},
            ],
        },
        {"_id": 1},
    ).sort("approved_at", 1).limit(int(quantity)))
    if not obligations:
        return 0
    now = utcnow()
    ids = [obligation["_id"] for obligation in obligations]
    db.stock_manager_replacement_obligations.update_many(
        {"_id": {"$in": ids}},
        {"$set": {
            "fulfilled_at": now,
            "fulfilled_by": username,
            "fulfilled_product_name": product_name,
        }},
    )
    return len(ids)


def count_pending_replacement_reports(db) -> int:
    return _count_safe(db, "replacement_reports", {"status": {"$in": ["pending", "reviewing", "approved", "replacement_ready"]}})


def replacement_report_items(report: dict | None) -> list[dict[str, Any]]:
    report = report or {}
    raw_items = report.get("items")
    if isinstance(raw_items, list) and raw_items:
        return [item for item in raw_items if isinstance(item, dict)]
    return [{
        "order_id": str(report.get("order_id") or ""),
        "product_name": str(report.get("product_name") or ""),
        "payment_method": str(report.get("payment_method") or ""),
        "submitted_item": str(report.get("submitted_item") or ""),
        "delivered_item": str(report.get("delivered_item") or ""),
        "item_hash": str(report.get("item_hash") or ""),
        "sold_at": report.get("sold_at"),
        "order_created_at": report.get("order_created_at"),
        "stock_added_by_username": str(report.get("stock_added_by_username") or ""),
        "stock_added_by_role": str(report.get("stock_added_by_role") or ""),
        "stock_added_at": report.get("stock_added_at"),
        "stock_metadata_product_name": str(report.get("stock_metadata_product_name") or ""),
    }]


def replacement_products_label(report: dict | None) -> str:
    names: list[str] = []
    for item in replacement_report_items(report):
        name = str(item.get("product_name") or "").strip()
        if name and name not in names:
            names.append(name)
    if not names:
        return "N/A"
    if len(names) == 1:
        count = len(replacement_report_items(report))
        return names[0] if count <= 1 else f"{names[0]} x{count}"
    return f"Multiple products ({len(names)})"


def replacement_orders_label(report: dict | None) -> str:
    order_ids: list[str] = []
    for item in replacement_report_items(report):
        order_id = str(item.get("order_id") or "").strip()
        if order_id and order_id not in order_ids:
            order_ids.append(order_id)
    if not order_ids:
        return "N/A"
    if len(order_ids) == 1:
        return order_ids[0]
    return f"{len(order_ids)} orders"


def replacement_status_key(status: str | None) -> str:
    status = str(status or "pending").strip().lower()
    if status in {"replaced", "replacement_sent"}:
        return "replaced"
    if status in {"cancelled", "closed", "rejected"}:
        return "cancelled"
    return "pending"


def replacement_status_label(status: str | None) -> str:
    key = replacement_status_key(status)
    if key == "replaced":
        return "Replaced"
    if key == "cancelled":
        return "Cancelled"
    return "Pending"


def replacement_status_badge_class(status: str | None) -> str:
    key = replacement_status_key(status)
    if key == "replaced":
        return "success"
    if key == "cancelled":
        return "muted-badge"
    return "warning"


def _replacement_required_product_counts(report: dict) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in replacement_report_items(report):
        product_name = str(item.get("product_name") or "").strip()
        if product_name:
            counts[product_name] = counts.get(product_name, 0) + 1
    return counts


def _replacement_stock_is_available(db, report: dict) -> tuple[bool, str]:
    counts = _replacement_required_product_counts(report)
    if not counts:
        return False, "Report has no product name."
    for product_name, needed in counts.items():
        product = db.products.find_one({"name": name_regex(product_name)}, {"stock": 1, "name": 1})
        if not product:
            return False, f"Original product {product_name} was not found. Add/rename the product back before sending a replacement."
        available = len(product.get("stock", []) or [])
        if available < needed:
            return False, f"Not enough stock is available for {product_name}. Need {needed}, available {available}."
    return True, ""


def _restore_replacement_stock(db, popped_by_product: dict[str, list[str]]) -> None:
    for product_name, items in popped_by_product.items():
        clean_items = [str(item) for item in (items or []) if str(item).strip()]
        if clean_items:
            db.products.update_one(
                {"name": name_regex(product_name)},
                {"$push": {"stock": {"$each": clean_items, "$position": 0}}},
            )
            record_stock_ledger_add(
                db,
                product_name,
                clean_items,
                username="system",
                role="system",
                source="webadmin",
                stock_upload_kind="replacement_send_rollback",
                note="Restored because replacement Telegram send failed",
            )


def _new_replacement_order_id(db) -> str:
    for _ in range(30):
        order_id = "R" + secrets.token_hex(4).upper()[:7]
        if not db.orders.find_one({"order_id": order_id}, {"_id": 1}):
            return order_id
    return "R" + secrets.token_hex(6).upper()[:11]


def _replacement_order_product_label(report: dict) -> str:
    names: list[str] = []
    for item in replacement_report_items(report):
        name = str(item.get("product_name") or "").strip()
        if name and name not in names:
            names.append(name)
    if len(names) == 1:
        return names[0]
    return f"Replacement - {len(names) or 1} items"


def _replacement_delivery_caption(order: dict, report: dict, admin_note: str = "") -> str:
    caption = (
        "✅ Replacement sent by admin\n\n"
        f"🧾 Order ID: {order.get('order_id', 'N/A')}\n"
        f"🛠 Report ID: {report.get('report_id', 'N/A')}\n"
        f"📦 Product: {order.get('product_name', 'Replacement')}\n"
        f"🔢 Quantity: {int(order.get('quantity', 0) or 0)}"
    )
    note = str(admin_note or report.get("replacement_admin_note") or "").strip()
    if note:
        caption += f"\n\n📝 Admin note: {note[:700]}"
    return caption


def send_replacement_from_stock(db, report: dict, sent_by: str = "owner", admin_note: str = "") -> dict[str, Any]:
    ok, message = _replacement_stock_is_available(db, report)
    if not ok:
        return {"status": "no_stock", "message": message or "No stock is available for this replacement."}

    popped_by_product: dict[str, list[str]] = {}
    replacement_items: list[str] = []
    for item in replacement_report_items(report):
        product_name = str(item.get("product_name") or "").strip()
        popped = pop_stock(db, product_name, 1)
        if not popped:
            _restore_replacement_stock(db, popped_by_product)
            return {"status": "no_stock", "message": f"No stock is available for {product_name}."}
        replacement_item = str(popped[0])
        popped_by_product.setdefault(product_name, []).append(replacement_item)
        replacement_items.append(replacement_item)

    now = utcnow()
    user_id = int(report.get("user_id", 0) or 0)
    order = {
        "order_id": _new_replacement_order_id(db),
        "user_id": user_id,
        "username": str(report.get("username") or ""),
        "product_name": _replacement_order_product_label(report),
        "quantity": len(replacement_items),
        "items": replacement_items,
        "payment_method": "replacement",
        "amount_inr": 0.0,
        "amount_usdt": 0.0,
        "status": "delivered",
        "created_at": now,
        "delivered_at": now,
        "is_replacement": True,
        "replacement_report_id": str(report.get("report_id") or ""),
        "replacement_sent_by": sent_by or "owner",
        "replacement_admin_note": str(admin_note or report.get("replacement_admin_note") or "").strip()[:1000],
        "original_order_id": str(report.get("order_id") or ""),
        "original_order_ids": [str(x) for x in (report.get("order_ids") or []) if str(x).strip()],
    }

    lang = get_user_language_sync(db, user_id)
    filename = delivery_txt_filename(order)
    sent = send_telegram_document(
        user_id,
        filename,
        delivery_txt_content(order, replacement_items, lang=lang),
        caption=_replacement_delivery_caption(order, report, admin_note),
    )
    if not sent:
        _restore_replacement_stock(db, popped_by_product)
        return {"status": "send_failed", "message": "Could not send Telegram replacement message. Stock was restored."}

    message_id = _telegram_result_message_id(sent)
    if message_id:
        sent_at = utcnow()
        order.update({
            "delivery_chat_id": int(user_id),
            "delivery_message_id": message_id,
            "delivery_message_sent_at": sent_at,
            "delivery_filename": filename,
            "delivery_telegram_deleted": False,
            "delivery_telegram_messages": [{
                "chat_id": int(user_id),
                "message_id": message_id,
                "filename": filename,
                "sent_at": sent_at,
                "sent_by": "webadmin_replacement_report",
                "resent": False,
            }],
        })

    db.orders.insert_one(order)
    for product_name, popped_items in popped_by_product.items():
        record_stock_ledger_status(
            db,
            product_name,
            popped_items,
            "replacement_delivered",
            order=order,
            username=sent_by or "owner",
            role="system" if sent_by == "auto-stock" else "owner",
            source="webadmin",
            note=f"Replacement report {report.get('report_id', '')}",
        )
    db.replacement_reports.update_one(
        {"_id": report["_id"]},
        {"$set": {
            "status": "replaced",
            "replacement_item": replacement_items[0] if replacement_items else "",
            "replacement_items": replacement_items,
            "replacement_order_id": order["order_id"],
            "replacement_sent_at": now,
            "replacement_sent_by": sent_by or "owner",
            "replacement_admin_note": str(admin_note or report.get("replacement_admin_note") or "").strip()[:1000],
        }, "$unset": {
            "replacement_queued_at": "",
            "replacement_required_by_username": "",
            "replacement_required_by_username_key": "",
            "replacement_required_quantity": "",
            "replacement_stock_uploaded_at": "",
            "replacement_stock_uploaded_by": "",
        }},
    )
    for product_name in popped_by_product:
        notify_low_stock_if_needed(db, product_name)
    log_admin_action(db, "replacement_sent", f"{report.get('report_id')} order={order['order_id']} user={user_id}")
    return {"status": "sent", "items": replacement_items, "order_id": order["order_id"]}


def process_pending_replacement_reports(db, product_name: str) -> dict[str, int]:
    reports = list(db.replacement_reports.find({
        "$or": [{"product_name": name_regex(product_name)}, {"items.product_name": name_regex(product_name)}],
        "approved_at": {"$exists": True, "$ne": None},
        "status": {"$nin": ["replaced", "replacement_sent", "cancelled", "closed", "rejected"]},
        "replacement_sent_at": {"$exists": False},
    }).sort("approved_at", 1).limit(100))
    sent_count = 0
    for report in reports:
        result = send_replacement_from_stock(db, report, "auto-stock")
        if result.get("status") == "sent":
            sent_count += 1
            continue
        if result.get("status") == "no_stock":
            continue
        break
    return {"replacements_sent": sent_count}


def notify_replacement_approved_waiting(user_id: int, report: dict) -> bool:
    if not user_id:
        return False
    text = (
        "✅ Replacement approved\n\n"
        f"Report ID: {report.get('report_id', 'N/A')}\n"
        f"Product: {replacement_products_label(report)}\n"
        f"Original Order ID: {replacement_orders_label(report)}\n\n"
        "Your replacement will be delivered automatically as soon as stock is added."
    )
    return send_telegram_message(user_id, text)


def notify_replacement_cancelled(user_id: int, report: dict, note: str = "") -> bool:
    if not user_id:
        return False
    reason = str(note or "No reason provided.").strip() or "No reason provided."
    text = (
        "❌ Replacement request cancelled\n\n"
        f"Report ID: {report.get('report_id', 'N/A')}\n"
        f"Product: {replacement_products_label(report)}\n"
        f"Original Order ID: {replacement_orders_label(report)}\n\n"
        f"Reason: {reason}"
    )
    return send_telegram_message(user_id, text)


def replacement_txt_filename(report: dict) -> str:
    report_id = str(report.get("report_id") or "replacement").strip() or "replacement"
    safe = "".join(ch for ch in report_id if ch.isalnum() or ch in ("-", "_")).strip() or "replacement"
    return f"replacement_{safe}.txt"


def replacement_txt_content(report: dict, item: str) -> str:
    return (
        "Replacement Item\n"
        f"Report ID: {report.get('report_id', 'N/A')}\n"
        f"Original Order ID: {replacement_orders_label(report)}\n"
        f"Product: {replacement_products_label(report)}\n"
        "\nItem:\n"
        f"{str(item or '').strip()}\n"
    )


def send_replacement_item(user_id: int, report: dict, item: str) -> bool:
    # Kept for old call sites. New replacement delivery is saved as a normal order
    # through send_replacement_from_stock so /getorder... and WebAdmin resend work.
    report_id = str(report.get("report_id") or "N/A")
    product_name = replacement_products_label(report)
    caption = (
        "✅ Replacement sent by admin\n\n"
        f"Report ID: {report_id}\n"
        f"Product: {product_name}\n"
        f"Original Order ID: {replacement_orders_label(report)}"
    )
    return send_telegram_document(
        int(user_id),
        replacement_txt_filename(report),
        replacement_txt_content(report, item),
        caption=caption,
    )

def parse_float(value: str | None, label: str) -> float | None:
    try:
        result = float(str(value or "").strip())
    except ValueError:
        flash(f"{label} must be a number.", "error")
        return None
    return result


def parse_positive_int(value, default: int, *, minimum: int = 1) -> int:
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError):
        parsed = int(default)
    return max(int(minimum), parsed)


def parse_product_shop_order(value, default=None):
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError):
        return default
    return parsed if parsed >= 1 else default


def parse_product_shop_order_form(value, *, field_label: str = "Bot shop order", allow_blank: bool = True):
    raw = str(value or "").strip()
    if not raw and allow_blank:
        return None
    try:
        parsed = int(raw)
    except ValueError:
        flash(f"{field_label} must be a whole number.", "error")
        return False
    if parsed < 1:
        flash(f"{field_label} must be at least 1, or blank for automatic order.", "error")
        return False
    if parsed > 999999:
        flash(f"{field_label} must be 999999 or lower.", "error")
        return False
    return parsed


def product_shop_sort_key(product: dict) -> tuple:
    order = parse_product_shop_order((product or {}).get("shop_order"), default=None)
    created_sort = -sort_dt((product or {}).get("created_at"))
    name_sort = str((product or {}).get("name") or "").lower()
    if order is not None:
        return (0, order, created_sort, name_sort)
    return (1, 0, created_sort, name_sort)


def sort_products_for_shop(products: list[dict]) -> list[dict]:
    return sorted(products, key=product_shop_sort_key)


def parse_price_for_currency(value: str | None, currency_label: str, required: bool) -> float | None:
    raw = str(value or "").strip()
    if not required and not raw:
        return 0.0
    try:
        result = float(raw)
    except ValueError:
        flash(f"{currency_label} price must be a number.", "error")
        return None
    if result < 0:
        flash(f"{currency_label} price cannot be negative.", "error")
        return None
    return round(result, 3 if currency_label.upper() == "USDT" else 2)


def int_arg(name: str, default: int, minimum: int | None = None) -> int:
    try:
        value = int(request.args.get(name, default))
    except (TypeError, ValueError):
        value = default
    if minimum is not None:
        value = max(minimum, value)
    return value


def paginate(items: list[Any], page: int, page_size: int) -> tuple[list[Any], int]:
    total_pages = max(1, math.ceil(len(items) / page_size)) if items else 1
    page = max(1, min(page, total_pages))
    start = (page - 1) * page_size
    return items[start:start + page_size], total_pages


def get_setting(db, key: str, default=None):
    doc = db.settings.find_one({"key": key})
    return doc.get("value", default) if doc else default


def set_setting(db, key: str, value: Any) -> None:
    db.settings.update_one({"key": key}, {"$set": {"key": key, "value": value, "updated_at": utcnow()}}, upsert=True)


def get_language_settings(db) -> dict:
    value = get_setting(db, "language_settings", {"default_language": "en", "enabled_languages": ["en", "es"]})
    if not isinstance(value, dict):
        value = {}
    enabled = value.get("enabled_languages")
    if not isinstance(enabled, list):
        enabled = ["en", "es"]
    cleaned = []
    for item in enabled:
        code = normalize_lang(str(item))
        if code in LANGUAGE_NAMES and code not in cleaned:
            cleaned.append(code)
    if "en" not in cleaned:
        cleaned.insert(0, "en")
    return {"default_language": "en", "enabled_languages": cleaned or ["en"]}


def set_language_settings(db, enabled_languages: list[str]) -> dict:
    cleaned = []
    for item in enabled_languages or []:
        code = normalize_lang(str(item))
        if code in LANGUAGE_NAMES and code not in cleaned:
            cleaned.append(code)
    if "en" not in cleaned:
        cleaned.insert(0, "en")
    settings = {"default_language": "en", "enabled_languages": cleaned or ["en"]}
    set_setting(db, "language_settings", settings)
    return settings


def get_user_language_sync(db, user_id: int | str | None) -> str:
    try:
        uid = int(user_id or 0)
    except Exception:
        uid = 0
    if not uid:
        return "en"
    user = db.users.find_one({"user_id": uid}, {"language": 1, "language_code": 1}) or {}
    return lang_from_user(user)


def choose_admin_text_for_language(message_en: str, message_es: str, lang: str | None) -> str:
    """Choose only the admin-written text matching the user's language.

    This is used for broadcasts. Leaving a language's field empty means users
    with that selected language are skipped; there is no cross-language fallback.
    The admin's exact text is not machine-translated or modified.
    """
    en = (message_en or "").strip()
    es = (message_es or "").strip()
    if normalize_lang(lang) == "es":
        return es
    return en


def admin_panel_message_prefix(kind: str, lang: str | None) -> str:
    language = normalize_lang(lang)
    if kind == "broadcast":
        return "📢 *Mensaje general:*" if language == "es" else "📢 *Broadcast Message:*"
    return "💬 Mensaje del admin:" if language == "es" else "💬 Message from admin:"


PAYMENT_SETTINGS_KEY = "payment_settings"

DEFAULT_PAYMENT_SETTINGS = {
    "usdt_bep20": {"enabled": False, "wallet_address": ""},
    "usdt_polygon": {"enabled": False, "wallet_address": ""},
    "upi": {"enabled": False, "upi_id": "", "upi_name": ""},
    "binance": {"enabled": False, "binance_pay_id": "", "binance_pay_name": ""},
    "wallet_limits": {"min_inr": "50", "min_usdt": "1"},
}


def clean_payment_settings(settings: dict | None) -> dict:
    settings = settings or {}
    cleaned = {method: dict(values) for method, values in DEFAULT_PAYMENT_SETTINGS.items()}
    for method, defaults in DEFAULT_PAYMENT_SETTINGS.items():
        incoming = settings.get(method) if isinstance(settings, dict) else None
        if not isinstance(incoming, dict):
            continue
        for key in defaults:
            if key == "enabled":
                cleaned[method][key] = bool(incoming.get(key))
            else:
                value = str(incoming.get(key) or "").strip()
                cleaned[method][key] = value if value else str(defaults.get(key, ""))
    for key, default_value in DEFAULT_PAYMENT_SETTINGS["wallet_limits"].items():
        try:
            amount = float(str(cleaned["wallet_limits"].get(key) or default_value).strip())
        except ValueError:
            amount = float(default_value)
        if amount <= 0:
            amount = float(default_value)
        cleaned["wallet_limits"][key] = f"{amount:g}"
    # Be tolerant of older/malformed database documents created during testing,
    # while still never falling back to .env payment details.
    if isinstance(settings, dict):
        legacy_usdt_wallet = str(settings.get("usdt_wallet_address") or settings.get("wallet_address") or settings.get("USDT_WALLET") or "").strip()
        if legacy_usdt_wallet and not cleaned["usdt_bep20"].get("wallet_address"):
            cleaned["usdt_bep20"]["wallet_address"] = legacy_usdt_wallet
        if settings.get("usdt_enabled") is not None and not cleaned["usdt_bep20"].get("enabled"):
            cleaned["usdt_bep20"]["enabled"] = bool(settings.get("usdt_enabled"))
    if not cleaned["usdt_bep20"].get("wallet_address"):
        cleaned["usdt_bep20"]["enabled"] = False
    if not cleaned["usdt_polygon"].get("wallet_address"):
        cleaned["usdt_polygon"]["enabled"] = False
    if not cleaned["upi"].get("upi_id"):
        cleaned["upi"]["enabled"] = False
    if not cleaned["binance"].get("binance_pay_id"):
        cleaned["binance"]["enabled"] = False
    return cleaned


def get_payment_settings(db) -> dict:
    doc = db.settings.find_one({"key": PAYMENT_SETTINGS_KEY})
    value = doc.get("value") if doc else None
    return clean_payment_settings(value if isinstance(value, dict) else None)


def set_payment_settings(db, settings: dict) -> dict:
    cleaned = clean_payment_settings(settings)
    db.settings.update_one(
        {"key": PAYMENT_SETTINGS_KEY},
        {"$set": {"key": PAYMENT_SETTINGS_KEY, "value": cleaned, "updated_at": utcnow()}},
        upsert=True,
    )
    return cleaned


def payment_method_enabled(settings: dict, method: str) -> bool:
    method = (method or "").lower()
    settings = clean_payment_settings(settings)
    if method in {"usdt", "usdt_bep20", "bep20"}:
        return bool(settings["usdt_bep20"].get("enabled") and settings["usdt_bep20"].get("wallet_address"))
    if method in {"polygon", "usdt_polygon", "polygon_usdt"}:
        return bool(settings["usdt_polygon"].get("enabled") and settings["usdt_polygon"].get("wallet_address"))
    if method in {"upi", "inr", "wallet_inr"}:
        return bool(settings["upi"].get("enabled") and settings["upi"].get("upi_id"))
    if method in {"binance", "binance_pay", "binance_usdt"}:
        return bool(settings["binance"].get("enabled") and settings["binance"].get("binance_pay_id"))
    if method == "wallet_usdt":
        return payment_method_enabled(settings, "usdt") or payment_method_enabled(settings, "polygon") or payment_method_enabled(settings, "binance")
    return False


def get_active_payment_currencies(db) -> set[str]:
    settings = get_payment_settings(db)
    currencies: set[str] = set()
    if payment_method_enabled(settings, "upi"):
        currencies.add("inr")
    if payment_method_enabled(settings, "usdt") or payment_method_enabled(settings, "polygon") or payment_method_enabled(settings, "binance"):
        currencies.add("usdt")
    return currencies


def _created_at_ts(value: Any) -> float | None:
    """Return a UTC timestamp for current and older date formats.

    New records store timezone-aware datetimes, pending payment rows may store
    time.time() floats, and some older/local test rows can contain strings like
    ``2026-05-17 08:24 UTC``.  Supporting all of these prevents old unpaid
    orders from getting stuck in Pending forever.
    """
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, datetime):
        dt = value
    else:
        raw = str(value).strip()
        candidates = [
            raw,
            raw.replace("Z", "+00:00"),
            raw.replace(" UTC", "+00:00"),
            raw.replace(" utc", "+00:00"),
        ]
        dt = None
        for candidate in candidates:
            try:
                dt = datetime.fromisoformat(candidate)
                break
            except Exception:
                pass
        if dt is None:
            for fmt in ("%Y-%m-%d %H:%M UTC", "%Y-%m-%d %H:%M:%S UTC", "%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S"):
                try:
                    dt = datetime.strptime(raw, fmt)
                    break
                except Exception:
                    pass
        if dt is None:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def expire_stale_unpaid_payments_and_orders(db) -> dict[str, int]:
    """Expire unpaid waiting payments and matching plain-pending orders.

    Manual-review rows are not expired here because they are waiting for admin
    action. Rejected manual order payments are moved from order Pending to Failed.
    Supports numeric, datetime, and ISO-string created_at values.
    """
    timeout_seconds = max(60, int(get_runtime_int(db, "payment_timeout_minutes", PAYMENT_TIMEOUT_MINUTES) or 30) * 60)
    cutoff_ts = time.time() - timeout_seconds
    now_dt = utcnow()
    result = {"payments_expired": 0, "orders_expired": 0, "orphan_orders_expired": 0, "failed_orders_marked": 0}

    waiting_payments = list(db.pending_payments.find(
        {"status": "waiting"},
        {"ref_id": 1, "pay_type": 1, "created_at": 1},
    ))
    stale_payments = [p for p in waiting_payments if (_created_at_ts(p.get("created_at")) or time.time()) < cutoff_ts]
    stale_refs = [str(p.get("ref_id") or "") for p in stale_payments if p.get("ref_id")]
    if stale_refs:
        payment_update = db.pending_payments.update_many(
            {"ref_id": {"$in": stale_refs}, "status": "waiting"},
            {"$set": {"status": "expired", "expired_at": now_dt}},
        )
        result["payments_expired"] += int(payment_update.modified_count or 0)

        order_refs = [str(p.get("ref_id") or "") for p in stale_payments if p.get("pay_type") == "order" and p.get("ref_id")]
        if order_refs:
            order_update = db.orders.update_many(
                {"order_id": {"$in": order_refs}, "status": "pending"},
                {"$set": {"status": "expired", "expired_at": now_dt}},
            )
            result["orders_expired"] += int(order_update.modified_count or 0)

    review_or_paid_statuses = {
        "upi_submitted", "binance_submitted", "usdt_manual_submitted",
        "approved", "confirmed", "completed",
    }
    pending_orders = list(db.orders.find(
        {"status": "pending"},
        {"order_id": 1, "created_at": 1},
    ))
    for order in pending_orders:
        ref_id = str(order.get("order_id") or "")
        if not ref_id:
            continue
        order_ts = _created_at_ts(order.get("created_at"))
        if order_ts is None or order_ts >= cutoff_ts:
            continue
        pending = db.pending_payments.find_one({"ref_id": ref_id})
        if pending and pending.get("status") in review_or_paid_statuses:
            continue
        if pending and pending.get("status") == "rejected":
            update = db.orders.update_one(
                {"order_id": ref_id, "status": "pending"},
                {"$set": {"status": "failed", "failed_at": now_dt}},
            )
            result["failed_orders_marked"] += int(update.modified_count or 0)
            continue
        if pending and pending.get("status") == "waiting":
            pending_ts = _created_at_ts(pending.get("created_at"))
            # If an older row has an unparsable payment timestamp, do not let it
            # block expiry when the order itself is already older than the timeout.
            if pending_ts is not None and pending_ts >= cutoff_ts:
                continue
            db.pending_payments.update_one(
                {"ref_id": ref_id, "status": "waiting"},
                {"$set": {"status": "expired", "expired_at": now_dt}},
            )
        update = db.orders.update_one(
            {"order_id": ref_id, "status": "pending"},
            {"$set": {"status": "expired", "expired_at": now_dt}},
        )
        changed = int(update.modified_count or 0)
        result["orphan_orders_expired"] += changed
        result["orders_expired"] += changed

    return result


def pending_order_is_review_or_paid(db, order_id: str) -> tuple[bool, str]:
    """Return whether a pending order should stay open because proof is in review or payment is already paid."""
    pending = db.pending_payments.find_one({"ref_id": order_id})
    if not pending:
        return False, ""
    status = str(pending.get("status") or "").strip()
    protected = {
        "upi_submitted", "binance_submitted", "usdt_manual_submitted",
        "approved", "confirmed", "completed",
    }
    return status in protected, status


def expire_single_pending_order(db, order_id: str, force: bool = False) -> tuple[bool, str]:
    """Expire one plain pending order. Force is for admin manual cleanup of old stuck unpaid rows."""
    ref_id = str(order_id or "").strip().upper()
    if not ref_id:
        return False, "Missing order ID."
    order = db.orders.find_one({"order_id": ref_id})
    if not order:
        return False, f"Order {ref_id} was not found."
    if order.get("status") != "pending":
        return False, f"Order {ref_id} is already {status_label(order.get('status'))}."
    protected, payment_status = pending_order_is_review_or_paid(db, ref_id)
    if protected and not force:
        return False, f"Order {ref_id} has payment status {payment_status}; approve or reject it in Payment Reviews."

    now_dt = utcnow()
    db.pending_payments.update_many(
        {"ref_id": ref_id, "status": {"$nin": ["approved", "confirmed", "completed", "rejected"]}},
        {"$set": {"status": "expired", "expired_at": now_dt}},
    )
    update = db.orders.update_one(
        {"order_id": ref_id, "status": "pending"},
        {"$set": {"status": "expired", "expired_at": now_dt, "expired_by_admin": bool(force)}},
    )
    if update.modified_count:
        return True, f"Order {ref_id} was expired."
    return False, f"Order {ref_id} could not be expired."


def count_pending_payment_reviews(db) -> int:
    return db.pending_payments.count_documents({
        "status": {"$in": ["upi_submitted", "binance_submitted", "usdt_manual_submitted"]}
    })


def count_pending_stock_manager_payout_requests(db) -> int:
    return db.stock_manager_payment_requests.count_documents({"status": "pending"})


def count_pending_refund_requests(db) -> int:
    return db.orders.count_documents({
        "refund_status": "refund_requested",
        "is_replacement": {"$ne": True},
    })


def manual_review_query() -> list[dict]:
    """Return conditions that identify payments handled by manual review.

    Auto-detected BEP20 payments can also end in confirmed/completed states, so
    the Payment Reviews history filters should only include rows that were
    submitted for manual review or explicitly marked by an admin.
    """
    not_empty = {"$nin": [None, ""]}
    return [
        {"reviewed_at": {"$exists": True}},
        {"reviewed_by": {"$exists": True}},
        {"upi_txn_id": not_empty},
        {"upi_screenshot_file_id": not_empty},
        {"binance_name": not_empty},
        {"binance_screenshot_file_id": not_empty},
        {"usdt_txn_hash": not_empty},
        {"usdt_screenshot_file_id": not_empty},
    ]


def rename_product_references(db, old_name: str, new_name: str) -> dict[str, int]:
    old_lower = str(old_name or "").strip().lower()
    if not old_lower:
        return {"orders": 0, "favorites": 0}

    orders_result = db.orders.update_many(
        {"product_name": name_regex(old_name), "status": {"$in": ["pending", "pending_stock"]}},
        {"$set": {"product_name": new_name}},
    )

    favorites_changed = 0
    users = db.users.find({"favorite_products": {"$exists": True}}, {"favorite_products": 1})
    for user in users:
        favorites = user.get("favorite_products") or []
        if not isinstance(favorites, list):
            continue
        changed = False
        updated: list[str] = []
        seen: set[str] = set()
        for favorite in favorites:
            favorite_text = str(favorite or "").strip()
            replacement = new_name if favorite_text.lower() == old_lower else favorite_text
            if replacement != favorite_text:
                changed = True
            key = replacement.lower()
            if replacement and key not in seen:
                updated.append(replacement)
                seen.add(key)
        if changed:
            db.users.update_one({"_id": user["_id"]}, {"$set": {"favorite_products": updated}})
            favorites_changed += 1

    custom_price_result = db.user_product_prices.update_many(
        {"product_key": product_name_key(old_name)},
        {"$set": {"product_name": new_name, "product_key": product_name_key(new_name), "updated_at": utcnow()}},
    )

    return {"orders": int(orders_result.modified_count or 0), "favorites": favorites_changed, "custom_prices": int(custom_price_result.modified_count or 0)}


def acquire_product_preorder_lock(db, product_name: str, *, lock_seconds: int = 15, wait_seconds: float = 3.0) -> str | None:
    """Mongo-backed product lock shared by bot and WebAdmin preorder/order flows."""
    clean_name = str(product_name or "").strip()
    if not clean_name:
        return None
    token = uuid.uuid4().hex
    deadline = time.monotonic() + max(0.2, float(wait_seconds or 0))
    while True:
        now = utcnow()
        cutoff = now - timedelta(seconds=max(1, int(lock_seconds or 15)))
        try:
            locked = db.products.find_one_and_update(
                {
                    "name": name_regex(clean_name),
                    "$or": [
                        {"preorder_lock_token": {"$exists": False}},
                        {"preorder_lock_token": None},
                        {"preorder_lock_at": {"$exists": False}},
                        {"preorder_lock_at": None},
                        {"preorder_lock_at": {"$lte": cutoff}},
                    ],
                },
                {"$set": {"preorder_lock_token": token, "preorder_lock_at": now}},
                return_document=ReturnDocument.AFTER,
            )
        except Exception:
            locked = None
        if locked:
            return token
        if time.monotonic() >= deadline:
            return None
        time.sleep(0.08)


def release_product_preorder_lock(db, product_name: str, token: str | None) -> bool:
    if not token:
        return False
    res = db.products.update_one(
        {"name": name_regex(str(product_name or "")), "preorder_lock_token": token},
        {"$unset": {"preorder_lock_token": "", "preorder_lock_at": ""}},
    )
    return bool(res.modified_count)


def get_pending_stock_quantity(db, product_name: str) -> int:
    rows = list(db.orders.aggregate([
        {"$match": {"product_name": name_regex(product_name), "status": "pending_stock"}},
        {"$group": {"_id": None, "total": {"$sum": "$quantity"}}},
    ]))
    return int(rows[0].get("total", 0)) if rows else 0


def restore_popped_stock_items(
    db,
    product_name: str,
    items: list[str],
    *,
    source: str = "webadmin",
    note: str = "Restored after delivery finalization failure",
) -> int:
    """Put stock items back at the front of the live stock queue after rollback."""
    clean_items = [str(item).strip() for item in (items or []) if str(item).strip()]
    if not clean_items:
        return 0
    result = db.products.update_one(
        {"name": name_regex(product_name)},
        {"$push": {"stock": {"$each": clean_items, "$position": 0}}},
    )
    if not result.matched_count:
        return 0
    try:
        record_stock_ledger_status(db, product_name, clean_items, "restored", source=source, note=note)
    except Exception:
        pass
    return len(clean_items)


def get_stock_count(db, product_name: str) -> int:
    product = db.products.find_one({"name": name_regex(product_name)})
    return len(product.get("stock", []) or []) if product else 0


def get_available_stock_count(db, product_name: str) -> int:
    return max(0, get_stock_count(db, product_name) - get_pending_stock_quantity(db, product_name))


def product_preorder_enabled(product: dict | None) -> bool:
    return bool((product or {}).get("preorder_enabled"))


def get_product_preorder_max_quantity(product: dict | None, default: int = 10) -> int:
    try:
        value = int((product or {}).get("preorder_max_quantity") or default or 10)
    except Exception:
        value = default or 10
    return max(1, value)


def get_product_preorder_total_limit(product: dict | None, default: int = 50) -> int:
    try:
        value = int((product or {}).get("preorder_total_limit") or default or 50)
    except Exception:
        value = default or 50
    return max(1, value)


def get_active_preorder_quantity(db, product_name: str) -> int:
    rows = list(db.orders.aggregate([
        {"$match": {
            "product_name": name_regex(product_name),
            "is_preorder": True,
            "status": {"$in": ["pending", "pending_stock"]},
            "is_replacement": {"$ne": True},
        }},
        {"$group": {"_id": None, "total": {"$sum": "$quantity"}}},
    ]))
    return int(rows[0].get("total", 0)) if rows else 0


def get_active_preorder_backorder_quantity(db, product_name: str) -> int:
    """Total active demand that consumes public preorder capacity.

    Counts all pending/pending-stock product orders, including admin-created
    waiting-stock orders, so user preorders stop once the product's backlog
    reaches the configured limit. Admin-created orders may still exceed it.
    """
    rows = list(db.orders.aggregate([
        {"$match": {
            "product_name": name_regex(product_name),
            "status": {"$in": ["pending", "pending_stock"]},
            "is_replacement": {"$ne": True},
        }},
        {"$group": {"_id": None, "total": {"$sum": "$quantity"}}},
    ]))
    return int(rows[0].get("total", 0)) if rows else 0


def get_active_user_preorder_quantity(db, user_id: int, product_name: str) -> int:
    rows = list(db.orders.aggregate([
        {"$match": {
            "user_id": int(user_id),
            "product_name": name_regex(product_name),
            "is_preorder": True,
            "status": {"$in": ["pending", "pending_stock"]},
            "is_replacement": {"$ne": True},
        }},
        {"$group": {"_id": None, "total": {"$sum": "$quantity"}}},
    ]))
    return int(rows[0].get("total", 0)) if rows else 0


def get_paid_preorder_quantity(db, product_name: str) -> int:
    rows = list(db.orders.aggregate([
        {"$match": {
            "product_name": name_regex(product_name),
            "is_preorder": True,
            "status": "pending_stock",
        }},
        {"$group": {"_id": None, "total": {"$sum": "$quantity"}}},
    ]))
    return int(rows[0].get("total", 0)) if rows else 0


def get_preorder_capacity_remaining(product: dict | None, active_quantity: int) -> int:
    if not product_preorder_enabled(product):
        return 0
    return max(0, get_product_preorder_total_limit(product) - int(active_quantity or 0))


def get_user_preorder_capacity_remaining(product: dict | None, active_user_quantity: int) -> int:
    try:
        product_max = parse_positive_int((product or {}).get("max_order_quantity"), 100, minimum=1)
    except Exception:
        product_max = 100
    user_limit = min(get_product_preorder_max_quantity(product), product_max)
    return max(0, user_limit - int(active_user_quantity or 0))


def get_product_restock_threshold(db, product: dict | None) -> int:
    if not product:
        return max(1, get_runtime_int(db, "low_stock_alert_threshold", LOW_STOCK_ALERT_THRESHOLD))
    return max(1, parse_positive_int(
        product.get("low_stock_threshold"),
        get_runtime_int(db, "low_stock_alert_threshold", LOW_STOCK_ALERT_THRESHOLD),
        minimum=1,
    ))


def claim_restock_notification_slot(
    db,
    product_name: str,
    previous_available_stock: int,
    current_available_stock: int,
    *,
    cooldown_minutes: int = RESTOCK_NOTIFICATION_COOLDOWN_MINUTES,
    back_in_stock_cooldown_minutes: int = RESTOCK_BACK_IN_STOCK_COOLDOWN_MINUTES,
    long_cooldown_minutes: int = RESTOCK_LONG_NOTIFICATION_COOLDOWN_MINUTES,
    big_restock_quantity: int = RESTOCK_BIG_ADDITION_THRESHOLD,
    high_stock_threshold: int | None = None,
    added_stock_count: int | None = None,
) -> bool:
    """Return True when a fresh-stock notification should be sent.

    Rules:
    - 0 available -> product threshold or higher: notify after the shorter
      back-in-stock cooldown. Small 0 -> 1/4/9 additions stay silent.
    - low stock -> product threshold or higher: notify after the normal cooldown.
    - already available -> only notify after the long cooldown for a big restock
      (added at least ``big_restock_quantity`` items, or available stock doubled).
    """
    product = db.products.find_one({"name": name_regex(product_name)}, {
        "_id": 1,
        "name": 1,
        "enabled": 1,
        "low_stock_threshold": 1,
    })
    if not product or product.get("enabled", True) is False:
        return False
    try:
        previous_available_stock = max(0, int(previous_available_stock or 0))
        current_available_stock = max(0, int(current_available_stock or 0))
    except Exception:
        return False
    if current_available_stock <= 0:
        return False

    threshold = get_product_restock_threshold(db, product)
    available_increase = max(0, current_available_stock - previous_available_stock)
    try:
        uploaded_count = max(0, int(added_stock_count if added_stock_count is not None else available_increase))
    except Exception:
        uploaded_count = available_increase
    try:
        big_quantity = max(1, int(big_restock_quantity if big_restock_quantity is not None else high_stock_threshold or RESTOCK_BIG_ADDITION_THRESHOLD))
    except Exception:
        big_quantity = RESTOCK_BIG_ADDITION_THRESHOLD

    back_in_stock = previous_available_stock <= 0 and current_available_stock >= threshold
    low_stock_recovered = 0 < previous_available_stock < threshold <= current_available_stock
    stock_doubled = previous_available_stock > 0 and current_available_stock >= previous_available_stock * 2 and available_increase > 0
    big_restock = previous_available_stock >= threshold and current_available_stock >= threshold and available_increase > 0 and (
        uploaded_count >= big_quantity or stock_doubled
    )

    if not back_in_stock and not low_stock_recovered and not big_restock:
        return False

    try:
        back_in_stock_cooldown = max(1, int(back_in_stock_cooldown_minutes or RESTOCK_BACK_IN_STOCK_COOLDOWN_MINUTES))
    except Exception:
        back_in_stock_cooldown = RESTOCK_BACK_IN_STOCK_COOLDOWN_MINUTES
    try:
        normal_cooldown = max(1, int(cooldown_minutes or RESTOCK_NOTIFICATION_COOLDOWN_MINUTES))
    except Exception:
        normal_cooldown = RESTOCK_NOTIFICATION_COOLDOWN_MINUTES
    try:
        long_cooldown = max(normal_cooldown, int(long_cooldown_minutes or RESTOCK_LONG_NOTIFICATION_COOLDOWN_MINUTES))
    except Exception:
        long_cooldown = max(normal_cooldown, RESTOCK_LONG_NOTIFICATION_COOLDOWN_MINUTES)

    if back_in_stock:
        required_cooldown = back_in_stock_cooldown
    elif low_stock_recovered:
        required_cooldown = normal_cooldown
    else:
        required_cooldown = long_cooldown

    now = utcnow()
    cutoff = now - timedelta(minutes=required_cooldown)
    result = db.products.update_one(
        {
            "_id": product["_id"],
            "$or": [
                {"restock_notification_last_sent_at": {"$exists": False}},
                {"restock_notification_last_sent_at": None},
                {"restock_notification_last_sent_at": {"$lte": cutoff}},
            ],
        },
        {"$set": {"restock_notification_last_sent_at": now}},
    )
    return bool(result.modified_count)


def _product_stock_count_expr() -> dict[str, Any]:
    return {"$cond": [{"$isArray": "$stock"}, {"$size": "$stock"}, 0]}


def _pending_stock_quantity_map(db) -> dict[str, int]:
    rows = db.orders.aggregate([
        {"$match": {"status": "pending_stock"}},
        {"$group": {"_id": "$product_name", "total": {"$sum": {"$ifNull": ["$quantity", 1]}}}},
    ])
    totals: dict[str, int] = {}
    for row in rows:
        key = product_name_key(row.get("_id"))
        if key:
            totals[key] = totals.get(key, 0) + int(row.get("total") or 0)
    return totals


def _active_preorder_backorder_quantity_map(db) -> dict[str, int]:
    """Return all active preorder/backorder demand in one DB round trip."""
    rows = db.orders.aggregate([
        {"$match": {
            "status": {"$in": ["pending", "pending_stock"]},
            "is_replacement": {"$ne": True},
        }},
        {"$group": {"_id": "$product_name", "total": {"$sum": {"$ifNull": ["$quantity", 1]}}}},
    ])
    totals: dict[str, int] = {}
    for row in rows:
        key = product_name_key(row.get("_id"))
        if key:
            totals[key] = totals.get(key, 0) + int(row.get("total") or 0)
    return totals


def _products_with_stock_counts(db, query: dict[str, Any], *, include_stock_items: bool = False) -> list[dict]:
    project: dict[str, Any] = {
        "name": 1,
        "price_inr": 1,
        "price_usdt": 1,
        "price_group_prices": 1,
        "enabled": 1,
        "created_at": 1,
        "shop_order": 1,
        "low_stock_threshold": 1,
        "min_order_quantity": 1,
        "max_order_quantity": 1,
        "warranty_days": 1,
        "description": 1,
        "description_en": 1,
        "description_es": 1,
        "preorder_enabled": 1,
        "preorder_max_quantity": 1,
        "preorder_total_limit": 1,
        "stock_manager_earning_rate_usdt": 1,
        "stock_manager_owner_rate_usdt": 1,
        "actual_stock": _product_stock_count_expr(),
    }
    if include_stock_items:
        project["stock"] = 1
        project["stock_added_by"] = 1
    rows = list(db.products.aggregate([{ "$match": query }, { "$project": project }]))
    return rows


def get_product_stock_alert_summary(db) -> dict[str, Any]:
    """Return sidebar stock-alert status for enabled products only.

    This uses MongoDB's ``$size`` projection instead of downloading every stock
    item to Python. That matters when product stock arrays become large.
    """
    def build() -> dict[str, Any]:
        default_threshold = max(1, get_runtime_int(db, "low_stock_alert_threshold", LOW_STOCK_ALERT_THRESHOLD))
        out_of_stock = 0
        low_stock = 0
        query = {"$or": [{"enabled": True}, {"enabled": {"$exists": False}}]}
        rows = db.products.aggregate([
            {"$match": query},
            {"$project": {"low_stock_threshold": 1, "actual_stock": _product_stock_count_expr()}},
        ])
        for product in rows:
            threshold = max(1, parse_positive_int(product.get("low_stock_threshold"), default_threshold, minimum=1))
            stock_count = int(product.get("actual_stock") or 0)
            if stock_count <= 0:
                out_of_stock += 1
            elif stock_count < threshold:
                low_stock += 1
        total = out_of_stock + low_stock
        severity = "danger" if out_of_stock else ("warning" if low_stock else "")
        return {
            "count": total,
            "low_stock": low_stock,
            "out_of_stock": out_of_stock,
            "threshold": default_threshold,
            "severity": severity,
        }

    return cached_value("product_stock_alert_summary", ADMIN_CACHE_TTL_SECONDS, build)


def get_low_stock_products(db, *, limit: int = 8) -> list[dict]:
    default_threshold = max(1, get_runtime_int(db, "low_stock_alert_threshold", LOW_STOCK_ALERT_THRESHOLD))
    query = {"$or": [{"enabled": True}, {"enabled": {"$exists": False}}]}
    products = _products_with_stock_counts(db, query, include_stock_items=False)
    rows: list[dict] = []
    for product in sort_products_for_shop(products):
        threshold = parse_positive_int(product.get("low_stock_threshold"), default_threshold, minimum=1)
        actual = int(product.get("actual_stock") or 0)
        if actual < threshold:
            product["enabled"] = product.get("enabled", True)
            product["low_stock_threshold"] = threshold
            product["available_stock"] = actual
            rows.append(product)
            if len(rows) >= limit:
                break
    return rows


def get_products_with_availability(
    db,
    include_disabled: bool = False,
    *,
    include_stock_items: bool = False,
    product_query: dict[str, Any] | None = None,
) -> list[dict]:
    visibility_query = {} if include_disabled else {"$or": [{"enabled": True}, {"enabled": {"$exists": False}}]}
    if product_query and visibility_query:
        query = {"$and": [visibility_query, product_query]}
    else:
        query = product_query or visibility_query
    products = sort_products_for_shop(_products_with_stock_counts(db, query, include_stock_items=include_stock_items))
    pending_by_product = _pending_stock_quantity_map(db)
    active_backorder_by_product = _active_preorder_backorder_quantity_map(db)
    default_threshold = max(1, get_runtime_int(db, "low_stock_alert_threshold", LOW_STOCK_ALERT_THRESHOLD))
    for product in products:
        key = product_name_key(product.get("name"))
        actual = int(product.get("actual_stock") or 0)
        pending = int(pending_by_product.get(key, 0) or 0)
        active_preorders = int(active_backorder_by_product.get(key, 0) or 0)
        product["enabled"] = product.get("enabled", True)
        product["shop_order"] = parse_product_shop_order(product.get("shop_order"), default=None)
        product["low_stock_threshold"] = parse_positive_int(product.get("low_stock_threshold"), default_threshold, minimum=1)
        product["pending_stock_quantity"] = pending
        product["preorder_enabled"] = product_preorder_enabled(product)
        product["preorder_max_quantity"] = get_product_preorder_max_quantity(product)
        product["preorder_total_limit"] = get_product_preorder_total_limit(product)
        product["active_preorder_quantity"] = active_preorders
        product["preorder_capacity_remaining"] = get_preorder_capacity_remaining(product, active_preorders)
        product["available_stock"] = max(0, actual - pending)
    return products


def remove_stock_items(db, product_name: str, items: list[str], allowed_added_by: str | None = None) -> dict[str, Any]:
    product = db.products.find_one({"name": name_regex(product_name)})
    if not product:
        return {"removed": [], "not_found": items, "not_allowed": [], "remaining": 0}
    stock = list(product.get("stock", []) or [])
    removed: list[str] = []
    not_found: list[str] = []
    not_allowed: list[str] = []

    def find_stock_index(submitted_item: str) -> int | None:
        # Prefer exact matching first, then fall back to normalized line matching.
        # This lets admins remove stock copied from the Current stock cards even if
        # the browser adds CRLF line endings or harmless spaces around lines.
        try:
            return stock.index(submitted_item)
        except ValueError:
            pass
        submitted_key = normalize_approved_stock_item(submitted_item)
        if not submitted_key:
            return None
        for candidate_index, candidate in enumerate(stock):
            if normalize_approved_stock_item(candidate) == submitted_key:
                return candidate_index
        return None

    for item in items:
        index = find_stock_index(item)
        if index is None:
            not_found.append(item)
            continue
        matched_item = stock[index]
        if allowed_added_by and not stock_item_is_owned_by(product, matched_item, allowed_added_by):
            not_allowed.append(item)
            continue
        removed.append(stock.pop(index))
    if removed:
        update: dict[str, Any] = {"$set": {"stock": stock}}
        removed_hashes = [stock_item_hash(item) for item in removed]
        if removed_hashes:
            update["$pull"] = {"stock_added_by": {"item_hash": {"$in": removed_hashes}}}
        db.products.update_one({"_id": product["_id"]}, update)
    return {"removed": removed, "not_found": not_found, "not_allowed": not_allowed, "remaining": len(stock)}


def pop_stock(db, product_name: str, quantity: int) -> list[str]:
    if quantity < 1:
        return []
    stock_expr = {"$ifNull": ["$stock", []]}
    updated = db.products.find_one_and_update(
        {"name": name_regex(product_name), "$expr": {"$gte": [{"$size": stock_expr}, quantity]}},
        [{"$set": {
            "_last_popped": {"$slice": [stock_expr, quantity]},
            "stock": {
                "$cond": [
                    {"$gt": [{"$size": stock_expr}, quantity]},
                    {"$slice": [stock_expr, quantity, {"$max": [1, {"$subtract": [{"$size": stock_expr}, quantity]}]}]},
                    [],
                ]
            },
        }}],
        return_document=ReturnDocument.AFTER,
    )
    if not updated:
        return []
    items = updated.get("_last_popped", []) or []
    db.products.update_one({"_id": updated["_id"]}, {"$unset": {"_last_popped": ""}})
    if items:
        record_stock_ledger_status(db, product_name, items, "popped", source="webadmin", note="Removed from live stock before delivery finalization")
    return items


def pop_available_stock_for_admin_send(db, product_name: str, quantity: int, *, reserve_pending: bool = True) -> list[str]:
    """Remove stock for an admin-to-user delivery.

    By default, paid pending-stock orders reserve the oldest stock items. User
    detail manual sends pass ``reserve_pending=False`` so an owner/admin can
    intentionally send current inventory even while the public shop keeps that
    inventory reserved from normal buyers. The exact stock-array condition keeps
    the operation race-safe: if another process changes stock between the read
    and update, this retries instead of removing the wrong items.
    """
    if quantity < 1:
        return []

    for _attempt in range(5):
        pending_qty = max(0, get_pending_stock_quantity(db, product_name)) if reserve_pending else 0
        product = db.products.find_one({"name": name_regex(product_name)}, {"stock": 1, "stock_added_by": 1})
        if not product:
            return []

        stock = list(product.get("stock", []) or [])
        required_size = pending_qty + quantity
        if len(stock) < required_size:
            return []

        raw_items = stock[pending_qty:required_size]
        items = [str(item).strip() for item in raw_items if str(item).strip()]
        if len(items) != quantity:
            return []
        new_stock = stock[:pending_qty] + stock[required_size:]
        removed_hashes = [stock_item_hash(item) for item in items]
        update: dict[str, Any] = {"$set": {"stock": new_stock}}
        if removed_hashes:
            update["$pull"] = {"stock_added_by": {"item_hash": {"$in": removed_hashes}}}

        try:
            result = db.products.update_one({"_id": product["_id"], "stock": product.get("stock", []) or []}, update)
        except Exception:
            # Let the caller show a normal retry/refresh message instead of a
            # Flask 500 page if the database provider rejects the update.
            try:
                current_app.logger.exception("Failed to remove inventory stock for admin send: product=%s quantity=%s", product_name, quantity)
            except Exception:
                pass
            return []

        if result.modified_count:
            record_stock_ledger_status(db, product_name, items, "popped", source="webadmin", note="Removed from live stock for admin send")
            return items

        # Stock changed between read and write. Retry with a fresh view.
        time.sleep(0.05)

    return []


def has_pending_stock_ahead(db, product_name: str, created_at: Any, order_id: str | None = None) -> bool:
    query: dict[str, Any] = {"product_name": name_regex(product_name), "status": "pending_stock"}
    if created_at:
        query["created_at"] = {"$lt": created_at}
    if order_id:
        query["order_id"] = {"$ne": order_id}
    return db.orders.count_documents(query, limit=1) > 0


def get_bot_stats(db) -> dict[str, Any]:
    def build() -> dict[str, Any]:
        revenue_rows = list(db.orders.aggregate([
            {"$match": {
                "status": {"$in": ["delivered", "pending_stock"]},
                "is_replacement": {"$ne": True},
                "payment_method": {"$nin": ["replacement"]},
                "refund_status": {"$nin": ["wallet_credited", "refund_requested", "refund_paid"]},
            }},
            {"$group": {"_id": None, "inr": {"$sum": paid_inr_expr()}, "usdt": {"$sum": paid_usdt_expr()}}},
        ]))
        revenue = revenue_rows[0] if revenue_rows else {"inr": 0, "usdt": 0}
        stock_rows = list(db.products.aggregate([
            {"$project": {"actual_stock": _product_stock_count_expr()}},
            {"$group": {"_id": None, "total_stock": {"$sum": "$actual_stock"}}},
        ]))
        total_stock = int((stock_rows[0] if stock_rows else {}).get("total_stock") or 0)
        return {
            "users_total": db.users.count_documents({}),
            "users_blocked": db.users.count_documents({"blocked": True}),
            "orders_total": db.orders.count_documents({"is_replacement": {"$ne": True}}),
            "orders_delivered": db.orders.count_documents({"status": "delivered", "is_replacement": {"$ne": True}}),
            "orders_pending_stock": db.orders.count_documents({"status": "pending_stock", "is_replacement": {"$ne": True}}),
            "orders_pending": db.orders.count_documents({"status": "pending", "is_replacement": {"$ne": True}}),
            "orders_failed": db.orders.count_documents({"status": {"$in": ["failed", "expired"]}, "is_replacement": {"$ne": True}}),
            "products_total": db.products.count_documents({}),
            "products_enabled": db.products.count_documents({"$or": [{"enabled": True}, {"enabled": {"$exists": False}}]}),
            "total_stock": total_stock,
            "revenue_inr": float(revenue.get("inr", 0) or 0),
            "revenue_usdt": float(revenue.get("usdt", 0) or 0),
        }

    return cached_value("bot_stats", ADMIN_DASHBOARD_CACHE_TTL_SECONDS, build)


def get_user_order_stats(db, user_id: int) -> dict[str, Any]:
    orders = list(db.orders.find({"user_id": user_id, "is_replacement": {"$ne": True}}))
    paid_orders = [
        o for o in orders
        if o.get("status") in {"delivered", "pending_stock"}
        and o.get("payment_method") != "replacement"
        and o.get("refund_status") not in {"wallet_credited", "refund_requested", "refund_paid"}
    ]
    paid_amounts = [order_paid_amounts(o) for o in paid_orders]
    return {
        "total_orders": len(orders),
        "delivered": sum(1 for o in orders if o.get("status") == "delivered"),
        "pending_stock": sum(1 for o in orders if o.get("status") == "pending_stock"),
        "pending": sum(1 for o in orders if o.get("status") == "pending"),
        "failed": sum(1 for o in orders if o.get("status") in {"failed", "expired"}),
        "total_inr": sum(v[0] for v in paid_amounts),
        "total_usdt": sum(v[1] for v in paid_amounts),
    }


def numeric_amount_expr(field_name: str) -> dict:
    """Mongo expression that safely treats old string amounts as numbers."""
    return {
        "$convert": {
            "input": {"$ifNull": [f"${field_name}", 0]},
            "to": "double",
            "onError": 0,
            "onNull": 0,
        }
    }


def paid_inr_expr() -> dict:
    method = {"$toLower": {"$ifNull": ["$payment_method", ""]}}
    return {"$cond": [
        {"$or": [
            {"$in": [method, ["upi", "wallet_inr", "inr"]]},
            {"$regexMatch": {"input": method, "regex": "upi"}},
            {"$regexMatch": {"input": method, "regex": "_inr$"}},
        ]},
        numeric_amount_expr("amount_inr"),
        0,
    ]}


def paid_usdt_expr() -> dict:
    method = {"$toLower": {"$ifNull": ["$payment_method", ""]}}
    return {"$cond": [
        {"$or": [
            {"$in": [method, ["usdt", "polygon", "usdt_polygon", "wallet_usdt", "binance", "binance_pay", "binance_usdt", "bep20", "admin_created_order", "admin_stock"]]},
            {"$regexMatch": {"input": method, "regex": "usdt"}},
            {"$regexMatch": {"input": method, "regex": "polygon"}},
            {"$regexMatch": {"input": method, "regex": "binance"}},
            {"$regexMatch": {"input": method, "regex": "bep20"}},
            {"$regexMatch": {"input": method, "regex": "admin_created"}},
        ]},
        numeric_amount_expr("amount_usdt"),
        0,
    ]}


def buyer_ranking_base_match() -> dict:
    """Base filter for Buyer Ranking order counts.

    Orders should match the User Details page: every non-replacement sales
    order placed for the user is counted, regardless of whether it later
    delivered, failed, expired, was cancelled, or is still pending. Revenue is
    calculated separately and only includes valid paid delivered/waiting-stock
    value.
    """
    return {
        "is_replacement": {"$ne": True},
        "payment_method": {"$nin": ["replacement"]},
    }


# Backwards-compatible name used by older helper code/tests.
def buyer_ranking_match() -> dict:
    return buyer_ranking_base_match()


def buyer_ranking_revenue_eligible_expr() -> dict:
    return {
        "$and": [
            {"$in": ["$status", ["delivered", "pending_stock"]]},
            {"$ne": ["$pending_stock_cancelled", True]},
            {"$not": [{"$in": [{"$ifNull": ["$refund_status", ""]}, ["wallet_credited", "refund_requested", "refund_paid"]]}]},
            {"$not": [{"$in": [{"$ifNull": ["$status", ""]}, ["cancelled", "refunded", "failed", "expired"]]}]},
        ]
    }


def buyer_ranking_paid_value_stage() -> dict:
    revenue_ok = buyer_ranking_revenue_eligible_expr()
    return {
        "$addFields": {
            "_ranking_paid_inr": {"$cond": [revenue_ok, paid_inr_expr(), 0]},
            "_ranking_paid_usdt": {"$cond": [revenue_ok, paid_usdt_expr(), 0]},
            "_ranking_waiting_paid": {"$cond": [{"$and": [revenue_ok, {"$eq": ["$status", "pending_stock"]}]}, 1, 0]},
        }
    }


def buyer_ranking_has_value_match() -> dict:
    """Kept for older tests/helpers; Buyer Ranking now counts all order rows."""
    return {"$match": {}}


def count_ranked_buyers(db) -> int:
    rows = list(db.orders.aggregate([
        {"$match": buyer_ranking_base_match()},
        {"$group": {"_id": "$user_id"}},
        {"$count": "count"},
    ]))
    return int(rows[0].get("count", 0)) if rows else 0


def get_buyer_ranking(db, limit: int = 10, skip: int = 0) -> list[dict]:
    pipeline = [
        {"$match": buyer_ranking_base_match()},
        buyer_ranking_paid_value_stage(),
        {"$group": {
            "_id": "$user_id",
            "total_orders": {"$sum": 1},
            "delivered_orders": {"$sum": {"$cond": [{"$eq": ["$status", "delivered"]}, 1, 0]}},
            "pending_stock_orders": {"$sum": "$_ranking_waiting_paid"},
            "total_inr": {"$sum": "$_ranking_paid_inr"},
            "total_usdt": {"$sum": "$_ranking_paid_usdt"},
            "last_order_at": {"$max": "$created_at"},
        }},
        {"$lookup": {"from": "users", "localField": "_id", "foreignField": "user_id", "as": "user_doc"}},
        {"$unwind": {"path": "$user_doc", "preserveNullAndEmptyArrays": True}},
        {"$project": {
            "_id": 0,
            "user_id": "$_id",
            "username": "$user_doc.username",
            "total_orders": 1,
            "delivered_orders": 1,
            "pending_stock_orders": 1,
            "total_inr": 1,
            "total_usdt": 1,
            "last_order_at": 1,
        }},
        {"$sort": {"total_usdt": -1, "total_inr": -1, "delivered_orders": -1, "pending_stock_orders": -1, "total_orders": -1, "user_id": 1}},
        {"$skip": skip},
        {"$limit": limit},
    ]
    return list(db.orders.aggregate(pipeline))


def activity_action_label(action: Any) -> str:
    text = str(action or "").strip()
    if not text:
        return "Activity"
    labels = {
        "stock_added": "Stock added",
        "replacement_stock_added": "Replacement stock added",
        "stock_removed": "Stock removed",
        "stock_cleared": "Stock cleared",
        "product_added": "Product added",
        "product_deleted": "Product deleted",
        "product_renamed": "Product renamed",
        "product_price_updated": "Product price updated",
        "product_group_prices_updated": "Product group prices updated",
        "user_pricing_group_updated": "User pricing group updated",
        "user_custom_price_updated": "User custom price updated",
        "user_custom_price_cleared": "User custom price cleared",
        "product_enabled": "Product enabled",
        "product_disabled": "Product disabled",
        "product_description_saved": "Product details saved",
        "product_limits_saved": "Product limits saved",
        "product_display_position_saved": "Product position saved",
        "stock_manager_rates_saved": "Stock manager rate saved",
        "approved_stock_pool_saved": "Approved stock pool updated",
        "approved_stock_rejections_cleared": "Rejected stock logs cleared",
        "stock_upload_rejected_lines": "Rejected stock upload",
        "telegram_stock_added": "Stock added from bot",
        "telegram_stock_removed": "Stock removed from bot",
        "telegram_stock_cleared": "Stock cleared from bot",
        "telegram_product_added": "Product added from bot",
        "telegram_product_removed": "Product removed from bot",
        "telegram_product_price_updated": "Product price updated from bot",
        "telegram_product_enabled": "Product enabled from bot",
        "telegram_product_disabled": "Product disabled from bot",
        "telegram_stock_upload_rejected_lines": "Rejected bot stock upload",
        "stock_manager_payment_details_saved": "Stock manager payout details saved",
    }
    return labels.get(text, text.replace("_", " ").strip().title())


def activity_actor_label(row: dict | None) -> str:
    row = row or {}
    username = str(row.get("admin_username") or row.get("username") or "").strip().lstrip("@")
    user_id = row.get("admin_user_id") or row.get("user_id")
    role = normalize_admin_role(row.get("admin_role")) if row.get("admin_role") else str(row.get("role") or "").strip()
    role_label = ADMIN_ROLE_LABELS.get(role, role.replace("_", " ").title() if role else "")
    if username:
        label = f"@{username}"
        if user_id not in (None, ""):
            try:
                label += f" ({int(user_id)})"
            except Exception:
                label += f" ({user_id})"
        elif role_label:
            label += f" ({role_label})"
        return label
    if user_id not in (None, ""):
        return f"ID {user_id}"
    if role_label:
        return role_label
    return "System"


def activity_source_label(row: dict | None) -> str:
    row = row or {}
    source = str(row.get("admin_source") or row.get("source") or "webadmin").strip().lower()
    if source in {"telegram", "telegram_bot", "bot"}:
        return "Telegram Bot"
    if source in {"system", "automation"}:
        return "System"
    return "WebAdmin"

def log_admin_action(db, action: str, details: str = "") -> None:
    try:
        doc = {
            "action": str(action or "").strip(),
            "details": str(details or "").strip(),
            "created_at": utcnow(),
        }
        if has_request_context():
            doc.update({
                "admin_username": current_admin_username(),
                "admin_role": current_admin_role(),
                "admin_source": "webadmin",
                "ip_address": request.headers.get("X-Forwarded-For", request.remote_addr or "").split(",")[0].strip(),
            })
        else:
            doc.setdefault("admin_source", "system")
        db.admin_activity.insert_one(doc)
    except Exception:
        pass


def csv_response(filename: str, fields: list[str], rows) -> Response:
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(fields)
    for row in rows:
        writer.writerow([row.get(field, "") for field in fields])
    return Response(output.getvalue(), mimetype="text/csv", headers={"Content-Disposition": f"attachment; filename={filename}"})



def claim_order_delivery(db, order_id: str, *, lock_seconds: int = 300) -> dict | None:
    clean_order_id = str(order_id or "").strip().upper()
    if not clean_order_id:
        return None
    now = utcnow()
    try:
        seconds = max(30, int(lock_seconds or 300))
    except Exception:
        seconds = 300
    cutoff = now - timedelta(seconds=seconds)
    token = uuid.uuid4().hex
    return db.orders.find_one_and_update(
        {
            "order_id": clean_order_id,
            "status": {"$in": ["pending", "pending_stock"]},
            "$or": [
                {"delivery_lock_token": {"$exists": False}},
                {"delivery_lock_token": None},
                {"delivery_lock_at": {"$exists": False}},
                {"delivery_lock_at": None},
                {"delivery_lock_at": {"$lte": cutoff}},
            ],
        },
        {"$set": {"delivery_lock_token": token, "delivery_lock_at": now}},
        return_document=ReturnDocument.AFTER,
    )


def clear_order_delivery_lock(db, order_id: str, delivery_token: str | None = None) -> bool:
    query: dict[str, Any] = {"order_id": str(order_id or "").strip().upper()}
    if delivery_token:
        query["delivery_lock_token"] = delivery_token
    res = db.orders.update_one(query, {"$unset": {"delivery_lock_token": "", "delivery_lock_at": ""}})
    return bool(res.modified_count)


# ───────────────────── Telegram mirrored actions ─────────────────────


def complete_order(db, user_id: int, ref_id: str) -> None:
    order = claim_order_delivery(db, ref_id)
    if not order:
        latest = db.orders.find_one({"order_id": str(ref_id or "").strip().upper()}, {"status": 1})
        if not latest or latest.get("status") in {"delivered", "expired", "cancelled", "rejected"}:
            return
        try:
            current_app.logger.info("Order delivery already in progress or not deliverable ref=%s status=%s", ref_id, latest.get("status"))
        except Exception:
            pass
        return

    delivery_token = order.get("delivery_lock_token")
    product_name = order.get("product_name", "")
    quantity = int(order.get("quantity", 1) or 1)
    if order.get("status") != "pending_stock" and has_pending_stock_ahead(db, product_name, order.get("created_at"), order.get("order_id")):
        mark_order_pending_stock(db, ref_id, delivery_token=delivery_token)
        order["status"] = "pending_stock"
        delete_payment_message(db, ref_id)
        try:
            process_pending_stock_orders(db, product_name)
        except Exception:
            current_app.logger.exception("Could not drain pending-stock queue after queuing %s", ref_id)
        latest = db.orders.find_one({"order_id": str(ref_id or "").strip().upper()})
        if latest and latest.get("status") == "delivered":
            return
        send_pending_stock_notice(user_id, order, queued=True)
        send_admin_message(f"⏳ Paid order queued behind older pending stock orders.\nProduct: {product_name}\nQuantity: {quantity}\nUser: {user_id}\nRef: {ref_id}")
        return
    items = pop_stock(db, product_name, quantity)
    if not items:
        mark_order_pending_stock(db, ref_id, delivery_token=delivery_token)
        order["status"] = "pending_stock"
        delete_payment_message(db, ref_id)
        send_pending_stock_notice(user_id, order)
        send_admin_message(
            f"⚠️ Paid order is waiting for stock.\nProduct: {product_name}\nQuantity: {quantity}\nUser: {user_id}\nRef: {ref_id}\n\nAdd stock from the web panel or with /addstock {product_name}."
        )
        return
    was_pending_stock = order.get("status") == "pending_stock"
    updated = update_order_status(db, ref_id, "delivered", items, delivery_token=delivery_token)
    if not updated:
        restore_popped_stock_items(
            db,
            product_name,
            items,
            source="webadmin",
            note=f"Restored because paid order {ref_id} could not be finalized",
        )
        clear_order_delivery_lock(db, ref_id, delivery_token=delivery_token)
        send_admin_message(
            f"🚨 Stock delivery finalization failed after stock was removed, so the stock was restored.\nProduct: {product_name}\nQuantity: {quantity}\nUser: {user_id}\nRef: {ref_id}\nPlease retry delivery after checking this order."
        )
        return
    if is_admin_created_order(order) and not was_pending_stock:
        send_admin_created_order_created_notice(user_id, order)
    send_order_items(user_id, order, items, from_pending=was_pending_stock)
    delete_payment_message(db, ref_id)
    notify_low_stock_if_needed(db, product_name)
    try:
        process_pending_stock_orders(db, product_name)
    except Exception:
        current_app.logger.exception("Could not drain pending-stock queue after delivering %s", ref_id)


def process_pending_stock_orders(db, product_name: str, *, limit: int = 100, max_passes: int = 3) -> dict[str, int]:
    """Deliver paid pending-stock orders oldest-first and retry the queue safely."""
    delivered_orders = 0
    delivered_items = 0
    locked_orders = 0
    blocked_by_stock = 0
    restored_items = 0
    safe_limit = max(1, min(int(limit or 100), 500))
    safe_passes = max(1, min(int(max_passes or 3), 10))

    for _pass in range(safe_passes):
        pending_orders = list(
            db.orders.find({"product_name": name_regex(product_name), "status": "pending_stock"})
            .sort("created_at", 1)
            .limit(safe_limit)
        )
        if not pending_orders:
            break

        pass_delivered = 0
        stop_pass = False
        for order in pending_orders:
            order_id = order.get("order_id")
            qty = int(order.get("quantity", 1) or 1)
            if get_stock_count(db, product_name) < qty:
                blocked_by_stock += 1
                stop_pass = True
                break

            claimed = claim_order_delivery(db, order_id)
            if not claimed:
                # Keep FIFO priority: do not skip an older locked order and
                # deliver younger waiting-stock orders first.
                locked_orders += 1
                stop_pass = True
                break
            delivery_token = claimed.get("delivery_lock_token")

            items = pop_stock(db, product_name, qty)
            if not items:
                mark_order_pending_stock(db, order_id, delivery_token=delivery_token)
                blocked_by_stock += 1
                stop_pass = True
                break

            updated = update_order_status(db, order_id, "delivered", items, delivery_token=delivery_token)
            if not updated:
                restored_items += restore_popped_stock_items(
                    db,
                    product_name,
                    items,
                    source="webadmin",
                    note=f"Restored because pending-stock order {order_id} could not be finalized",
                )
                clear_order_delivery_lock(db, order_id, delivery_token=delivery_token)
                send_admin_message(
                    f"🚨 Pending-stock finalization failed after stock was removed, so the stock was restored.\n"
                    f"Product: {product_name}\nQuantity: {qty}\nRef: {order_id}"
                )
                stop_pass = True
                break

            delivered_orders += 1
            pass_delivered += 1
            delivered_items += len(items)
            send_order_items(int(order.get("user_id", 0) or 0), order, items, from_pending=True)
            delete_payment_message(db, order_id)

        if stop_pass or pass_delivered == 0:
            break

    if delivered_orders:
        send_admin_message(f"✅ Auto-delivered {delivered_orders} pending order(s) for {product_name} using {delivered_items} stock item(s).")
    notify_low_stock_if_needed(db, product_name)
    return {
        "orders_delivered": delivered_orders,
        "items_delivered": delivered_items,
        "locked_orders": locked_orders,
        "blocked_by_stock": blocked_by_stock,
        "restored_items": restored_items,
    }

def complete_wallet_load(db, user_id: int, pending: dict) -> None:
    ref_id = pending.get("ref_id", "")
    credited = db.pending_payments.find_one_and_update(
        {"ref_id": ref_id, "pay_type": "wallet", "$or": [{"wallet_credited_at": None}, {"wallet_credited_at": {"$exists": False}}]},
        {"$set": {"wallet_credited_at": utcnow(), "status": "completed"}},
        return_document=ReturnDocument.AFTER,
    )
    if not credited:
        delete_payment_message(db, ref_id)
        latest = db.pending_payments.find_one({"ref_id": ref_id}) or pending
        send_wallet_load_status(db, user_id, latest, already=True)
        return
    currency = credited.get("currency", "inr")
    amount = float(credited.get("load_amount", 0.0) or 0.0)
    if currency == "inr":
        db.users.update_one({"user_id": user_id}, {"$inc": {"wallet_inr": round(amount, 2)}})
    else:
        db.users.update_one({"user_id": user_id}, {"$inc": {"wallet_usdt": round(amount, 2)}})
    delete_payment_message(db, ref_id)
    send_wallet_load_status(db, user_id, credited, already=False)


def update_order_status(
    db,
    order_id: str,
    status: str,
    items: list[str] | None = None,
    *,
    delivery_token: str | None = None,
) -> bool:
    query: dict[str, Any] = {"order_id": str(order_id or "").strip().upper()}
    if delivery_token:
        query["delivery_lock_token"] = delivery_token
    if status == "pending_stock":
        query["status"] = {"$nin": ["delivered", "expired", "cancelled", "rejected"]}
        if not delivery_token:
            query["$or"] = [
                {"delivery_lock_token": {"$exists": False}},
                {"delivery_lock_token": None},
            ]

    update: dict[str, Any] = {"$set": {"status": status}}
    if items is not None:
        update["$set"]["items"] = items
    if status == "delivered":
        update["$set"]["delivered_at"] = utcnow()
    if status == "pending_stock":
        update["$set"]["pending_stock_at"] = utcnow()
    if status in {"delivered", "pending_stock", "expired", "cancelled", "rejected"}:
        update["$unset"] = {"delivery_lock_token": "", "delivery_lock_at": ""}

    res = db.orders.update_one(query, update)
    if res.matched_count and status == "delivered" and items:
        try:
            order = db.orders.find_one({"order_id": str(order_id or "").strip().upper()}) or {}
            record_order_items_delivered_in_ledger(db, order, items, source="webadmin")
        except Exception:
            pass
    return bool(res.matched_count)


def mark_order_pending_stock(db, order_id: str, *, delivery_token: str | None = None) -> bool:
    return update_order_status(db, order_id, "pending_stock", delivery_token=delivery_token)


def claim_pending_stock_notice(db, order_id: str) -> bool:
    """Claim the one-time pending-stock user notice for this order."""
    clean_order_id = str(order_id or "").strip().upper()
    if not clean_order_id:
        return False
    res = db.orders.update_one(
        {
            "order_id": clean_order_id,
            "$or": [
                {"pending_stock_notice_sent_at": {"$exists": False}},
                {"pending_stock_notice_sent_at": None},
            ],
        },
        {"$set": {"pending_stock_notice_sent_at": utcnow()}},
    )
    return bool(res.modified_count)


def notify_low_stock_if_needed(db, product_name: str) -> None:
    product = db.products.find_one({"name": name_regex(product_name)})
    if not product or product.get("enabled", True) is False:
        return
    stock_count = len(product.get("stock", []) or [])
    threshold = max(1, int(product.get("low_stock_threshold") or get_runtime_int(db, "low_stock_alert_threshold", LOW_STOCK_ALERT_THRESHOLD)))
    if stock_count >= threshold:
        if product.get("low_stock_alert_sent"):
            db.products.update_one({"_id": product["_id"]}, {"$set": {"low_stock_alert_sent": False}})
        return
    if product.get("low_stock_alert_sent"):
        return
    db.products.update_one({"_id": product["_id"]}, {"$set": {"low_stock_alert_sent": True}})
    send_admin_message(f"⚠️ *Low Stock Alert*\n\nProduct: *{product.get('name', product_name)}*\nStock left: *{stock_count}*\nAlert threshold: below *{threshold}* units", parse_mode="Markdown")


def queue_maintenance_notification(db, kind: str, product_name: str, payload: dict | None = None) -> None:
    kind = str(kind or "").strip()
    product_name = str(product_name or "").strip()
    if not kind or not product_name:
        return
    now = utcnow()
    db.maintenance_notifications.update_one(
        {"kind": kind, "product_name": product_name},
        {
            "$set": {
                "kind": kind,
                "product_name": product_name,
                "payload": payload or {},
                "updated_at": now,
            },
            "$setOnInsert": {"created_at": now},
        },
        upsert=True,
    )


def flush_maintenance_notifications(db) -> dict[str, int]:
    rows = list(db.maintenance_notifications.find({}).sort("created_at", 1).limit(500))
    processed = 0
    sent = 0
    for row in rows:
        payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
        kind = str(row.get("kind") or "")
        product_name = str(row.get("product_name") or "").strip()
        try:
            if kind == "new_product":
                product = db.products.find_one({"name": name_regex(product_name)}) or payload or {"name": product_name}
                sent += notify_users_new_product(db, product, include_admins=False)
            elif kind == "new_stock":
                sent += notify_users_new_stock(db, product_name, payload.get("available_stock"), include_admins=False)
            elif kind == "price_drop":
                product = db.products.find_one({"name": name_regex(product_name)})
                if product:
                    sent += notify_users_price_drop(
                        db,
                        product,
                        old_inr=payload.get("old_inr"),
                        new_inr=payload.get("new_inr"),
                        old_usdt=payload.get("old_usdt"),
                        new_usdt=payload.get("new_usdt"),
                        include_admins=False,
                    )
            processed += 1
        finally:
            db.maintenance_notifications.delete_one({"_id": row.get("_id")})
    return {"processed": processed, "sent": sent}


def mark_user_delivery_success(db, user_id: int, *, source: str = "telegram") -> None:
    """Mark a user reachable after a successful Telegram send.

    Only auto delivery blocks are cleared; explicit admin blocks remain blocked.
    """
    try:
        uid = int(user_id or 0)
    except (TypeError, ValueError):
        uid = 0
    if not uid:
        return
    now = utcnow()
    db.users.update_one(
        {
            "user_id": uid,
            "$or": [
                {"blocked_manually": {"$ne": True}},
                {"blocked_by_delivery": True},
                {"blocked_reason": "delivery_failed"},
            ],
        },
        {
            "$set": {
                "blocked": False,
                "blocked_by_delivery": False,
                "telegram_delivery_status": "active",
                "last_message_delivered_at": now,
                "last_delivery_source": str(source or "telegram"),
            },
            "$unset": {"blocked_reason": "", "last_delivery_error": ""},
        },
    )


def mark_user_delivery_failure(db, user_id: int, *, source: str = "telegram", error: str = "") -> None:
    """Auto-mark a user blocked when Telegram says the chat cannot receive DMs."""
    try:
        uid = int(user_id or 0)
    except (TypeError, ValueError):
        uid = 0
    if not uid:
        return
    now = utcnow()
    db.users.update_one(
        {"user_id": uid, "blocked_manually": {"$ne": True}},
        {
            "$set": {
                "blocked": True,
                "blocked_by_delivery": True,
                "blocked_reason": "delivery_failed",
                "telegram_delivery_status": "blocked",
                "last_message_failed_at": now,
                "last_delivery_source": str(source or "telegram"),
                "last_delivery_error": str(error or "Telegram delivery failed")[:500],
            }
        },
    )


def set_user_manual_block(db, user_id: int, blocked: bool) -> None:
    try:
        uid = int(user_id or 0)
    except (TypeError, ValueError):
        uid = 0
    if not uid:
        return
    now = utcnow()
    if blocked:
        update = {
            "$set": {
                "blocked": True,
                "blocked_manually": True,
                "blocked_by_delivery": False,
                "blocked_reason": "admin",
                "blocked_at": now,
                "telegram_delivery_status": "blocked_by_admin",
            }
        }
    else:
        update = {
            "$set": {
                "blocked": False,
                "blocked_manually": False,
                "blocked_by_delivery": False,
                "telegram_delivery_status": "active_by_admin",
                "unblocked_at": now,
            },
            "$unset": {"blocked_reason": "", "last_delivery_error": ""},
        }
    db.users.update_one({"user_id": uid}, update, upsert=False)


def _is_telegram_delivery_block_response(status_code: int | None, description: str) -> bool:
    desc = str(description or "").lower()
    if int(status_code or 0) not in {400, 403}:
        return False
    return any(
        marker in desc
        for marker in (
            "bot was blocked",
            "blocked by the user",
            "user is deactivated",
            "chat not found",
            "bot can't initiate conversation",
            "bot was kicked",
        )
    )


def product_notify_markup(product_name: str, lang: str = "en", product_id: Any | None = None) -> dict:
    callback_data = f"product_id:{product_id}" if product_id else f"product:{product_name}"
    return {"inline_keyboard": [[{"text": tr(lang, "btn_buy_now"), "callback_data": callback_data}]]}

def _product_notification_key(product_name: Any) -> str:
    return str(product_name or "").strip().lower()


def _last_product_notification(db, user_id: int, product_name: str, kind: str = "new_stock") -> dict | None:
    try:
        uid = int(user_id or 0)
    except (TypeError, ValueError):
        uid = 0
    key = _product_notification_key(product_name)
    if not uid or not key:
        return None
    return db.user_product_notifications.find_one(
        {"user_id": uid, "product_key": key, "kind": str(kind or "new_stock")},
        {"_id": 0, "message_id": 1, "product_name": 1, "updated_at": 1},
    )


def _save_product_notification(db, user_id: int, product_name: str, message_id: int, kind: str = "new_stock") -> None:
    try:
        uid = int(user_id or 0)
        mid = int(message_id or 0)
    except (TypeError, ValueError):
        uid = 0
        mid = 0
    key = _product_notification_key(product_name)
    if not uid or not mid or not key:
        return
    now = datetime.now(timezone.utc)
    db.user_product_notifications.update_one(
        {"user_id": uid, "product_key": key, "kind": str(kind or "new_stock")},
        {
            "$set": {
                "user_id": uid,
                "product_name": str(product_name or "").strip(),
                "product_key": key,
                "kind": str(kind or "new_stock"),
                "message_id": mid,
                "updated_at": now,
            },
            "$setOnInsert": {"created_at": now},
        },
        upsert=True,
    )


def notify_active_users(
    db,
    text,
    *,
    product_name: str,
    parse_mode: str = "HTML",
    admin_only: bool = False,
    include_admins: bool = True,
    cleanup_previous_stock_notification: bool = False,
) -> int:
    sent = 0
    admin_ids = set(get_admin_ids(db))
    product = db.products.find_one({"name": name_regex(product_name)}, {"_id": 1, "name": 1})
    product_id = product.get("_id") if product else None
    clean_product_name = str((product or {}).get("name") or product_name or "").strip()
    if admin_only:
        users = get_admin_recipient_users(db)
    else:
        users = db.users.find({"blocked": {"$ne": True}}, {"user_id": 1, "language": 1, "language_code": 1})
    seen: set[int] = set()
    for user in users:
        try:
            user_id = int(user.get("user_id", 0) or 0)
        except Exception:
            user_id = 0
        if not user_id or user_id in seen:
            continue
        seen.add(user_id)
        is_admin_target = user_id in admin_ids
        if admin_only and not is_admin_target:
            continue
        if not include_admins and is_admin_target:
            continue
        lang = lang_from_user(user)
        body = text(lang) if callable(text) else str(text)
        markup = product_notify_markup(clean_product_name or product_name, lang, product_id)
        previous_message_id = None
        if cleanup_previous_stock_notification:
            try:
                previous = _last_product_notification(db, user_id, clean_product_name or product_name, "new_stock")
                previous_message_id = int((previous or {}).get("message_id") or 0) or None
            except Exception:
                previous_message_id = None
        status = send_telegram_message_status(user_id, body, parse_mode=parse_mode, reply_markup=markup)
        result = status.get("result") if status.get("ok") else False
        new_message_id = _telegram_result_message_id(result)
        if result:
            sent += 1
            mark_user_delivery_success(db, user_id, source="stock_notification")
            if cleanup_previous_stock_notification:
                if previous_message_id and previous_message_id != new_message_id:
                    delete_telegram_message(user_id, previous_message_id)
                if new_message_id:
                    _save_product_notification(db, user_id, clean_product_name or product_name, new_message_id, "new_stock")
        elif status.get("blocked"):
            mark_user_delivery_failure(db, user_id, source="stock_notification", error=str(status.get("error") or ""))
    return sent


def notify_users_new_product(db, product: dict, *, include_admins: bool = True) -> int:
    if not product or product.get("enabled", True) is False:
        return 0
    product_name = str(product.get("name") or "Product")
    currencies = get_active_payment_currencies(db)
    lines: list[str] = []
    try:
        if "inr" in currencies and product.get("price_inr") is not None:
            lines.append(f"💰 {money_inr_price(product.get('price_inr'))}")
    except Exception:
        pass
    try:
        if "usdt" in currencies and product.get("price_usdt") is not None:
            lines.append(f"💰 {money_usdt_price(product.get('price_usdt'))}")
    except Exception:
        pass
    escaped_lines = "\n".join(html.escape(line, quote=False) for line in lines)

    def text_for(lang: str) -> str:
        return tr(lang, "new_product_added", product=html.escape(product_name, quote=False), lines=escaped_lines)

    if get_setting(db, "maintenance_mode", False):
        queue_maintenance_notification(
            db,
            "new_product",
            product_name,
            {"price_inr": product.get("price_inr"), "price_usdt": product.get("price_usdt")},
        )
        return notify_active_users(db, text_for, product_name=product_name, admin_only=True)
    return notify_active_users(db, text_for, product_name=product_name, include_admins=include_admins)


def notify_users_new_stock(db, product_name: str, available_stock: int | None = None, *, include_admins: bool = True) -> int:
    product = db.products.find_one({"name": name_regex(product_name)})
    if not product or product.get("enabled", True) is False:
        return 0
    try:
        available_stock = int(available_stock if available_stock is not None else get_available_stock_count(db, product.get("name", product_name)))
    except Exception:
        available_stock = 0
    if available_stock <= 0:
        return 0
    clean_name = str(product.get("name") or product_name)

    def text_for(lang: str) -> str:
        return tr(lang, "fresh_stock_added", product=html.escape(clean_name, quote=False), stock=available_stock)

    if get_setting(db, "maintenance_mode", False):
        queue_maintenance_notification(db, "new_stock", clean_name, {"available_stock": available_stock})
        return notify_active_users(db, text_for, product_name=clean_name, admin_only=True, cleanup_previous_stock_notification=True)
    return notify_active_users(db, text_for, product_name=clean_name, include_admins=include_admins, cleanup_previous_stock_notification=True)


def notify_users_price_drop(db, product: dict, *, old_inr=None, new_inr=None, old_usdt=None, new_usdt=None, include_admins: bool = True) -> int:
    if not product or product.get("enabled", True) is False:
        return 0
    settings = get_payment_settings(db)
    lines: list[str] = []
    try:
        if payment_method_enabled(settings, "upi") and old_inr is not None and new_inr is not None and float(new_inr) < float(old_inr):
            lines.append(f"{money_inr_price(old_inr)} → {money_inr_price(new_inr)}")
    except Exception:
        pass
    try:
        if payment_method_enabled(settings, "wallet_usdt") and old_usdt is not None and new_usdt is not None and float(new_usdt) < float(old_usdt):
            lines.append(f"{money_usdt_price(old_usdt)} → {money_usdt_price(new_usdt)}")
    except Exception:
        pass
    if not lines:
        return 0
    product_name = str(product.get("name") or "Product")
    escaped_lines = "\n".join(f"💰 {html.escape(line, quote=False)}" for line in lines)

    def text_for(lang: str) -> str:
        return tr(lang, "price_dropped", product=html.escape(product_name, quote=False), lines=escaped_lines)

    if get_setting(db, "maintenance_mode", False):
        queue_maintenance_notification(
            db,
            "price_drop",
            product_name,
            {"old_inr": old_inr, "new_inr": new_inr, "old_usdt": old_usdt, "new_usdt": new_usdt},
        )
        return notify_active_users(db, text_for, product_name=product_name, admin_only=True)
    return notify_active_users(db, text_for, product_name=product_name, include_admins=include_admins)

def send_telegram_message_status(chat_id: int, text: str, parse_mode: str | None = None, reply_markup: dict | None = None) -> dict:
    bot_token = get_bot_token()
    if not bot_token or not chat_id:
        return {"ok": False, "blocked": False, "error": "Missing bot token or chat ID"}
    payload: dict[str, Any] = {"chat_id": chat_id, "text": text}
    if parse_mode:
        payload["parse_mode"] = parse_mode
    if reply_markup:
        payload["reply_markup"] = reply_markup
    try:
        resp = requests.post(f"https://api.telegram.org/bot{bot_token}/sendMessage", json=payload, timeout=15)
        payload_resp = resp.json() if resp.content else {}
        if resp.ok and payload_resp.get("ok"):
            return {"ok": True, "result": payload_resp.get("result") or {"ok": True}, "blocked": False}
        description = str(payload_resp.get("description") or resp.text or "Telegram delivery failed")
        return {
            "ok": False,
            "blocked": _is_telegram_delivery_block_response(resp.status_code, description),
            "error": description,
            "status_code": resp.status_code,
        }
    except Exception as exc:
        return {"ok": False, "blocked": False, "error": str(exc)}


def send_telegram_message_result(chat_id: int, text: str, parse_mode: str | None = None, reply_markup: dict | None = None) -> dict | bool:
    status = send_telegram_message_status(chat_id, text, parse_mode=parse_mode, reply_markup=reply_markup)
    return status.get("result") if status.get("ok") else False


def send_telegram_message(chat_id: int, text: str, parse_mode: str | None = None, reply_markup: dict | None = None) -> bool:
    return bool(send_telegram_message_result(chat_id, text, parse_mode=parse_mode, reply_markup=reply_markup))


def delete_telegram_message(chat_id: int, message_id: int) -> bool:
    bot_token = get_bot_token()
    if not bot_token or not chat_id or not message_id:
        return False
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{bot_token}/deleteMessage",
            json={"chat_id": int(chat_id), "message_id": int(message_id)},
            timeout=10,
        )
        return bool(resp.ok and (resp.json() if resp.content else {}).get("ok"))
    except Exception:
        return False


def send_telegram_document(chat_id: int, filename: str, content: str, caption: str | None = None) -> dict | bool:
    bot_token = get_bot_token()
    if not bot_token or not chat_id:
        return False
    data: dict[str, Any] = {"chat_id": chat_id}
    if caption:
        data["caption"] = caption
    files = {"document": (filename, content.encode("utf-8"), "text/plain")}
    try:
        resp = requests.post(f"https://api.telegram.org/bot{bot_token}/sendDocument", data=data, files=files, timeout=30)
        payload = resp.json() if resp.content else {}
        if resp.ok and payload.get("ok"):
            return payload.get("result") or {"ok": True}
        current_app.logger.warning("Telegram sendDocument failed for chat=%s filename=%s: %s", chat_id, filename, payload or resp.text)
        return False
    except Exception:
        current_app.logger.exception("Telegram sendDocument exception for chat=%s filename=%s", chat_id, filename)
        return False


def _telegram_result_message_id(result: Any) -> int | None:
    if not isinstance(result, dict):
        return None
    try:
        return int(result.get("message_id"))
    except (TypeError, ValueError):
        return None


def record_order_delivery_message(
    db,
    order_id: str,
    chat_id: int | str,
    message_result: Any,
    *,
    filename: str = "",
    sent_by: str = "webadmin",
    resent: bool = False,
) -> None:
    """Save Telegram delivery message IDs so the panel can later revoke/delete them."""
    clean_order_id = str(order_id or "").strip().upper()
    message_id = _telegram_result_message_id(message_result)
    if not clean_order_id or not message_id:
        return
    try:
        clean_chat_id = int(chat_id)
    except (TypeError, ValueError):
        return
    now = utcnow()
    entry = {
        "chat_id": clean_chat_id,
        "message_id": message_id,
        "filename": str(filename or "").strip(),
        "sent_at": now,
        "sent_by": str(sent_by or "webadmin").strip()[:80],
        "resent": bool(resent),
    }
    db.orders.update_one(
        {"order_id": clean_order_id},
        {
            "$set": {
                "delivery_chat_id": clean_chat_id,
                "delivery_message_id": message_id,
                "delivery_message_sent_at": now,
                "delivery_filename": entry["filename"],
                "delivery_telegram_deleted": False,
            },
            "$push": {"delivery_telegram_messages": entry},
        },
    )


def order_delivery_messages(order: dict) -> list[dict]:
    """Return unique saved Telegram delivery message refs for an order."""
    refs: list[dict] = []
    seen: set[tuple[int, int]] = set()
    raw_messages = order.get("delivery_telegram_messages") or []
    if isinstance(raw_messages, list):
        candidates = raw_messages
    else:
        candidates = []
    latest_chat = order.get("delivery_chat_id") or order.get("user_id")
    latest_msg = order.get("delivery_message_id")
    if latest_chat and latest_msg:
        candidates = [*candidates, {"chat_id": latest_chat, "message_id": latest_msg, "sent_at": order.get("delivery_message_sent_at"), "filename": order.get("delivery_filename", "")}]
    for msg in candidates:
        if not isinstance(msg, dict):
            continue
        try:
            chat_id = int(msg.get("chat_id"))
            message_id = int(msg.get("message_id"))
        except (TypeError, ValueError):
            continue
        key = (chat_id, message_id)
        if key in seen:
            continue
        seen.add(key)
        refs.append({
            "chat_id": chat_id,
            "message_id": message_id,
            "sent_at": msg.get("sent_at"),
            "filename": str(msg.get("filename") or ""),
            "sent_by": str(msg.get("sent_by") or ""),
        })
    return refs


def delete_telegram_message(chat_id: int, message_id: int) -> tuple[bool, str]:
    bot_token = get_bot_token()
    if not bot_token:
        return False, "Bot token is not configured."
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{bot_token}/deleteMessage",
            json={"chat_id": int(chat_id), "message_id": int(message_id)},
            timeout=15,
        )
        payload = resp.json() if resp.content else {}
        if resp.ok and payload.get("ok"):
            return True, "Deleted"
        return False, str(payload.get("description") or resp.text or "Telegram refused to delete the message.")
    except Exception as exc:
        current_app.logger.exception("Telegram deleteMessage exception for chat=%s message=%s", chat_id, message_id)
        return False, str(exc)


def send_admin_message(text: str, parse_mode: str | None = None) -> None:
    # Telegram admin notifications were removed; WebAdmin is the admin surface.
    return None


def delete_payment_message(db, ref_id: str) -> None:
    pending = db.pending_payments.find_one({"ref_id": ref_id})
    bot_token = get_bot_token(db)
    if not pending or not bot_token:
        return
    chat_id = pending.get("payment_chat_id")
    msg_id = pending.get("payment_msg_id")
    if not chat_id or not msg_id:
        return
    try:
        requests.post(f"https://api.telegram.org/bot{bot_token}/deleteMessage", json={"chat_id": chat_id, "message_id": msg_id}, timeout=10)
    except Exception:
        pass
    db.pending_payments.update_one({"ref_id": ref_id}, {"$unset": {"payment_chat_id": "", "payment_msg_id": ""}})


def support_markup(lang: str = "en") -> dict | None:
    rows = []
    supports = get_support_usernames()
    for idx, support in enumerate(supports, 1):
        value = (support or "").strip()
        if not value:
            continue
        if value.startswith(("http://", "https://")):
            url = value
        else:
            username = value.lstrip("@").strip()
            url = f"https://t.me/{username}" if username else ""
        if url:
            label = tr(lang, "support_open") if len(supports) == 1 else tr(lang, "support_open_n", n=idx)
            rows.append([{"text": label, "url": url}])
    return {"inline_keyboard": rows} if rows else None

def notify_order_expired(order: dict, admin_expired: bool = False) -> bool:
    """Notify a user that their pending order was expired."""
    try:
        user_id = int(order.get("user_id", 0) or 0)
    except Exception:
        user_id = 0
    if not user_id:
        return False
    lang = get_user_language_sync(current_app.db, user_id)
    order_id = html.escape(str(order.get("order_id") or "N/A"))
    product_name = html.escape(str(order.get("product_name") or "Product"))
    quantity = int(order.get("quantity", 0) or 0)
    reason = tr(lang, "order_expired_admin_reason") if admin_expired else tr(lang, "order_expired_time_reason")
    return send_telegram_message(
        user_id,
        tr(lang, "order_expired_web", order_id=order_id, product=product_name, quantity=quantity, reason=html.escape(reason, quote=False)),
        parse_mode="HTML",
        reply_markup=support_markup(lang),
    )

def is_admin_created_order(order: dict | None) -> bool:
    """Return True for admin-created orders, including older rows saved before the flag existed."""
    if not order:
        return False
    if bool(order.get("admin_created_order")):
        return True
    method = str(order.get("payment_method") or "").strip().lower()
    return method in {"admin_created_order", "admin_created"} or "admin_created" in method


def order_refund_currency_amount(order: dict | None) -> tuple[str, float]:
    """Return the currency/amount that should be offered for a cancelled paid order."""
    order = order or {}
    currency = str(order.get("refund_currency") or "").strip().lower()
    try:
        amount = float(order.get("refund_amount") or 0)
    except (TypeError, ValueError):
        amount = 0.0
    if currency in {"inr", "usdt"} and amount > 0:
        return currency, round(amount, 2)

    method = str(order.get("payment_method") or "").strip().lower()
    try:
        amount_inr = float(order.get("amount_inr") or 0)
    except (TypeError, ValueError):
        amount_inr = 0.0
    try:
        amount_usdt = float(order.get("amount_usdt") or 0)
    except (TypeError, ValueError):
        amount_usdt = 0.0
    if method in {"upi", "wallet_inr", "inr"} or "upi" in method or method.endswith("_inr"):
        return "inr", round(max(0.0, amount_inr), 2)
    if amount_usdt > 0:
        return "usdt", round(max(0.0, amount_usdt), 2)
    return "inr", round(max(0.0, amount_inr), 2)


def order_refund_amount_text(order: dict | None) -> str:
    currency, amount = order_refund_currency_amount(order)
    if currency == "inr":
        return money_inr(amount)
    return money_usdt(amount)


def paid_user_order_refund_eligible(order: dict | None) -> bool:
    """Only real paid user orders can get the user refund/wallet choice.

    Admin-created/manual/replacement orders are deliberately excluded to avoid
    fake credits/refund requests for orders made by mistake from WebAdmin.
    """
    if not order or order.get("is_replacement"):
        return False
    if is_admin_created_order(order) or order.get("admin_stock_delivery"):
        return False
    method = str(order.get("payment_method") or "").strip().lower()
    if method in {"admin_created_order", "admin_stock", "replacement", "admin", "manual"}:
        return False
    currency, amount = order_refund_currency_amount(order)
    return amount > 0 and currency in {"inr", "usdt"}


def send_cancelled_order_refund_choice(user_id: int, order: dict, note: str = "") -> bool:
    order_id = str(order.get("order_id") or "").strip().upper()
    amount_text = order_refund_amount_text(order)
    product = str(order.get("product_name") or "Product")
    quantity = int(order.get("quantity", 0) or 0)
    buttons = []
    if order.get("refund_wallet_enabled"):
        buttons.append([{"text": "💰 Add to wallet", "callback_data": f"refund_wallet:{order_id}"}])
    if order.get("refund_external_enabled"):
        buttons.append([{"text": "🔁 Request refund", "callback_data": f"refund_request:{order_id}"}])
    if not buttons:
        return send_cancelled_order_notice(user_id, order, note)
    text = (
        "❌ Your paid order was cancelled by admin.\n\n"
        f"🧾 Order ID: {order_id}\n"
        f"📦 Product: {product} x{quantity}\n"
        f"💰 Amount: {amount_text}\n\n"
    )
    clean_note = str(note or "").strip()
    if clean_note:
        text += f"📝 Admin note: {clean_note[:500]}\n\n"
    text += "Choose one of the available options below:"
    reply_markup = {"inline_keyboard": buttons}
    return send_telegram_message(int(user_id), text, reply_markup=reply_markup)

def send_cancelled_order_notice(user_id: int, order: dict, note: str = "") -> bool:
    order_id = str(order.get("order_id") or "").strip().upper()
    product = str(order.get("product_name") or "Product")
    quantity = int(order.get("quantity", 0) or 0)
    text = (
        "❌ Your pending-stock order was cancelled by admin.\n\n"
        f"🧾 Order ID: {order_id}\n"
        f"📦 Product: {product} x{quantity}"
    )
    clean_note = str(note or "").strip()
    if clean_note:
        text += f"\n\n📝 Admin note: {clean_note[:500]}"
    text += "\n\nContact support if you need help."
    return send_telegram_message(int(user_id), text)


def send_refund_paid_notice(user_id: int, order: dict, note: str = "") -> bool:
    order_id = str(order.get("order_id") or "").strip().upper()
    text = (
        "✅ Your refund was marked as paid by admin.\n\n"
        f"🧾 Order ID: {order_id}\n"
        f"💰 Amount: {order_refund_amount_text(order)}"
    )
    clean_note = str(note or "").strip()
    if clean_note:
        text += f"\n\n📝 Note: {clean_note[:500]}"
    return send_telegram_message(int(user_id), text)


def send_pending_stock_notice(user_id: int, order: dict, queued: bool = False) -> None:
    order_id = str(order.get("order_id", "N/A") or "N/A")
    try:
        if not claim_pending_stock_notice(current_app.db, order_id):
            current_app.logger.info("Pending-stock notice already sent for order=%s", order_id)
            return
    except Exception:
        current_app.logger.exception("Could not claim pending-stock notice for order=%s", order_id)
        return
    lang = get_user_language_sync(current_app.db, user_id)
    product_name = order.get("product_name", "Product")
    if is_admin_created_order(order):
        queue_note = tr(lang, "admin_created_order_stock_queued") if queued else tr(lang, "admin_created_order_stock_waiting")
        message = tr(
            lang,
            "admin_created_order_pending_stock_notice",
            product=product_name,
            order_id=order_id,
            quantity=int(order.get("quantity", 0) or 0),
            queue_note=queue_note,
        )
    else:
        queue_note = tr(lang, "pending_stock_queued") if queued else tr(lang, "pending_stock_waiting")
        message = tr(lang, "pending_stock_notice", product=product_name, order_id=order_id, queue_note=queue_note)
    send_telegram_message(
        user_id,
        message,
        parse_mode="Markdown",
        reply_markup=support_markup(lang),
    )



def send_admin_created_order_created_notice(user_id: int, order: dict) -> None:
    if not is_admin_created_order(order):
        return
    lang = get_user_language_sync(current_app.db, user_id)
    send_telegram_message(
        user_id,
        tr(
            lang,
            "admin_created_order_created_notice",
            product=order.get("product_name", "Product"),
            order_id=order.get("order_id", "N/A"),
            quantity=int(order.get("quantity", 0) or 0),
        ),
        parse_mode="Markdown",
        reply_markup=support_markup(lang),
    )

def delivery_txt_filename(order: dict) -> str:
    order_id = str(order.get("order_id") or "order").strip() or "order"
    safe_order_id = "".join(ch for ch in order_id if ch.isalnum() or ch in ("-", "_")).strip() or "order"
    if order.get("is_replacement"):
        return f"replacement_{safe_order_id}_items.txt"
    return f"order_{safe_order_id}_items.txt"


def delivery_txt_content(order: dict, items: list[str], lang: str = "en") -> str:
    order_id = str(order.get("order_id", "N/A"))
    product_name = str(order.get("product_name", "Product"))
    quantity = int(order.get("quantity", len(items) or 0) or 0)
    is_replacement = bool(order.get("is_replacement"))
    lines = [
        tr(lang, "replacement_items_title") if is_replacement else tr(lang, "order_items_title"),
        f"{tr(lang, 'replacement_id') if is_replacement else tr(lang, 'order_id')}: {order_id}",
    ]
    if is_replacement and order.get("replacement_report_id"):
        lines.append(f"{tr(lang, 'report_id')}: {order.get('replacement_report_id')}")
    lines.extend([
        f"{tr(lang, 'product')}: {product_name}",
        f"{tr(lang, 'quantity')}: {quantity}",
        "",
        f"{tr(lang, 'items_label')}:",
    ])
    for item in items:
        lines.extend([str(item).strip(), ""])
    return "\n".join(lines).rstrip() + "\n"

def delivery_caption(order: dict, from_pending: bool = False, resent_by_admin: bool = False, lang: str = "en") -> str:
    order_id = str(order.get("order_id") or "N/A")
    product_name = str(order.get("product_name") or "Product")
    quantity = int(order.get("quantity", 0) or 0)
    if order.get("is_replacement"):
        title = tr(lang, "replacement_resent_admin") if resent_by_admin else tr(lang, "replacement_sent_admin")
        return (
            f"{title}\n\n"
            f"🧾 {tr(lang, 'replacement_id')}: {order_id}\n"
            f"🛠 {tr(lang, 'report_id')}: {order.get('replacement_report_id', 'N/A')}\n"
            f"📦 {tr(lang, 'product')}: {product_name}\n"
            f"🔢 {tr(lang, 'quantity')}: {quantity}"
        )
    if resent_by_admin:
        title = tr(lang, "order_resent_admin")
    elif order.get("admin_stock_delivery"):
        title = tr(lang, "admin_stock_sent")
    elif is_admin_created_order(order):
        title = tr(lang, "admin_created_order_delivery_title")
    elif from_pending:
        title = tr(lang, "pending_order_delivered")
    else:
        title = tr(lang, "order_placed_confirmed")
    caption = (
        f"{title}\n\n"
        f"🧾 {tr(lang, 'order_id')}: {order_id}\n"
        f"📦 {tr(lang, 'product')}: {product_name}\n"
        f"🔢 {tr(lang, 'quantity')}: {quantity}"
    )
    if (
        order.get("admin_stock_delivery")
        and not order.get("delivery_transfer")
        and str(order.get("admin_stock_source") or "").strip().lower() != "transfer"
    ):
        note = str(order.get("admin_stock_note") or "").strip()
        note_lower = note.lower()
        if note and not note_lower.startswith(("transferred from revoked order", "transferred from revoked replacement")):
            caption += f"\n\n📝 Admin note: {note[:700]}"
    return caption

def plain_from_html(value: str) -> str:
    return (
        value.replace("<b>", "")
        .replace("</b>", "")
        .replace("<pre>", "")
        .replace("</pre>", "")
        .replace("&amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&quot;", '"')
    )


def send_order_items(user_id: int, order: dict, items: list[str], from_pending: bool = False, resent_by_admin: bool = False) -> dict | bool:
    """Deliver purchased stock as one TXT file only.

    WebAdmin approval delivery should match the bot delivery format.
    The TXT file contains all items, separated by a blank line.
    """
    lang = get_user_language_sync(current_app.db, user_id)
    if not items:
        if resent_by_admin:
            title = tr(lang, "order_resent_admin")
        elif is_admin_created_order(order):
            title = tr(lang, "admin_created_order_delivery_title")
        elif from_pending:
            title = tr(lang, "pending_order_delivered")
        else:
            title = tr(lang, "order_placed_confirmed")
        ok = send_telegram_message(
            user_id,
            f"{html.escape(title)}\n\n📦 {tr(lang, 'product')}: <b>{html.escape(str(order.get('product_name', 'Product')))}</b>\n🔢 {tr(lang, 'quantity')}: {int(order.get('quantity', 0) or 0)}\n\n{html.escape(tr(lang, 'no_items_attached'))}",
            parse_mode="HTML",
        )
        return bool(ok)

    filename = delivery_txt_filename(order)
    sent = send_telegram_document(
        user_id,
        filename,
        delivery_txt_content(order, items, lang=lang),
        caption=delivery_caption(order, from_pending=from_pending, resent_by_admin=resent_by_admin, lang=lang),
    )
    if sent:
        record_order_delivery_message(
            current_app.db,
            str(order.get("order_id") or ""),
            user_id,
            sent,
            filename=filename,
            sent_by="webadmin_resend" if resent_by_admin else "webadmin_delivery",
            resent=bool(resent_by_admin),
        )
    return sent

def wallet_usdt_display(value: Any) -> str:
    """Format wallet USDT credits/balances with the normal 2-decimal display."""
    try:
        return f"{float(value or 0):.2f}"
    except (TypeError, ValueError):
        return "0.00"


def send_wallet_load_status(db, user_id: int, pending: dict, already: bool = False) -> None:
    lang = pending.get("language") or get_user_language_sync(db, user_id)
    currency = pending.get("currency", "inr")
    amount = float(pending.get("load_amount", 0.0) or 0.0)
    user = db.users.find_one({"user_id": user_id}) or {}
    title = tr(lang, "wallet_already_completed") if already else tr(lang, "wallet_completed")
    if currency == "inr":
        bal = float(user.get("wallet_inr", 0.0) or 0.0)
        text = f"{title}\n\n{tr(lang, 'wallet_added_inr', amount=f'{amount:.2f}')}\n{tr(lang, 'wallet_current_inr', balance=f'{bal:.2f}')}\n\n{tr(lang, 'wallet_use_wallet')}"
    else:
        bal = float(user.get("wallet_usdt", 0.0) or 0.0)
        text = f"{title}\n\n{tr(lang, 'wallet_added_usdt', amount=wallet_usdt_display(amount))}\n{tr(lang, 'wallet_current_usdt', balance=wallet_usdt_display(bal))}\n\n{tr(lang, 'wallet_use_wallet')}"
    send_telegram_message(user_id, text, parse_mode="Markdown")

def notify_user_balance_adjustment(user_id: int, action: str, amount_text: str, balance_text: str, label: str, note: str = "") -> None:
    if action == "add":
        lines = ["✅ Wallet balance added by admin.", f"Added: {amount_text}"]
    else:
        lines = ["⚠️ Wallet balance adjusted by admin.", f"Removed: {amount_text}"]
    if note:
        lines.append(f"Note: {note}")
    lines.extend([f"New {label} balance: {balance_text}", "Use /wallet to check your balance."])
    send_telegram_message(user_id, "\n".join(lines))


# ───────────────────────── Formatting ─────────────────────────


def fmt_dt(value: Any) -> str:
    if not value:
        return "N/A"
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, (int, float)):
        dt = datetime.fromtimestamp(float(value), tz=timezone.utc)
    else:
        try:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except Exception:
            return str(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def sort_dt(value: Any) -> float:
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, (int, float)):
        return float(value)
    elif value:
        try:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except Exception:
            return 0.0
    else:
        return 0.0
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def recent_created_rows(rows: Any, *, skip: int = 0, limit: int | None = None) -> list[dict]:
    """Return rows sorted newest-first using Python date parsing.

    Some older payment/order documents may have ``created_at`` stored as an ISO
    string while newer/admin-generated rows use Mongo dates. Mongo sorts mixed
    BSON types separately, which can put a newer wallet top-up below older admin
    wallet rows. Sorting through ``sort_dt`` keeps WebAdmin lists consistently
    date-wise regardless of the stored type.
    """
    sorted_rows = sorted(list(rows), key=lambda row: sort_dt((row or {}).get("created_at")), reverse=True)
    start = max(0, int(skip or 0))
    if limit is None:
        return sorted_rows[start:]
    end = start + max(0, int(limit or 0))
    return sorted_rows[start:end]


def money_inr(value: Any) -> str:
    try:
        return f"₹{float(value or 0):.2f}"
    except (TypeError, ValueError):
        return "₹0.00"


def _compact_amount(value: Any, max_decimals: int = 2) -> str:
    try:
        amount = float(value or 0)
    except (TypeError, ValueError):
        amount = 0.0
    return f"{amount:.{max_decimals}f}"


def money_inr_price(value: Any) -> str:
    return f"₹{_compact_amount(value, 2)}"


def money_usdt_price(value: Any) -> str:
    return f"${_compact_amount(value, 2)} USDT"


def money_usdt(value: Any) -> str:
    return money_usdt_price(value)


def money_usdt_exact(value: Any) -> str:
    try:
        return f"${float(value or 0):.3f} USDT"
    except (TypeError, ValueError):
        return "$0.000 USDT"


def order_paid_amounts(order: dict) -> tuple[float, float]:
    method = str(order.get("payment_method") or "").lower()
    inr = float(order.get("amount_inr") or 0)
    usdt = float(order.get("amount_usdt") or 0)
    if method in {"upi", "wallet_inr", "inr"} or "upi" in method or method.endswith("_inr"):
        return inr, 0.0
    if method in {"usdt", "polygon", "usdt_polygon", "wallet_usdt", "binance", "binance_pay", "binance_usdt", "bep20", "admin_created_order", "admin_stock"} or "usdt" in method or "polygon" in method or "binance" in method or "bep20" in method or "admin_created" in method:
        return 0.0, usdt
    if usdt and not inr:
        return 0.0, usdt
    return inr, 0.0


def order_amount_text(order: dict) -> str:
    """Display order value with USDT as the preferred WebAdmin currency.

    The original payment method still stays saved exactly as before, but WebAdmin
    amount columns should show the USDT value first whenever an order has one.
    INR is only shown as a secondary/fallback value.
    """
    try:
        usdt = float(order.get("amount_usdt") or 0)
    except (TypeError, ValueError):
        usdt = 0.0
    try:
        inr = float(order.get("amount_inr") or 0)
    except (TypeError, ValueError):
        inr = 0.0
    if usdt > 0:
        return money_usdt_price(usdt)
    if inr > 0:
        return money_inr(inr)
    return money_usdt_price(0)


def status_label(status: str | None) -> str:
    return {
        "pending_stock": "Paid — Waiting for Stock",
        "pending": "Pending",
        "needs_review": "Needs Review",
        "delivered": "Delivered",
        "failed": "Failed",
        "expired": "Expired",
        "confirmed": "Confirmed",
        "revoked": "Revoked",
        "cancelled": "Cancelled",
        "awaiting_refund_choice": "Awaiting Refund Choice",
        "waiting_user_choice": "Awaiting Refund Choice",
        "refund_requested": "Refund Requested",
        "wallet_credited": "Wallet Credited",
        "refund_paid": "Refund Paid",
    }.get((status or "unknown").lower(), str(status or "unknown").replace("_", " ").title())


def status_badge_class(status: str | None) -> str:
    status_key = (status or "unknown").lower()
    if status_key in {"delivered", "confirmed", "completed", "approved", "wallet_credited", "refund_paid"}:
        return "status-badge status-green"
    if status_key in {"pending", "pending_stock", "needs_review", "waiting", "upi_submitted", "binance_submitted", "usdt_manual_submitted", "awaiting_refund_choice", "waiting_user_choice", "refund_requested"}:
        return "status-badge status-yellow"
    if status_key in {"failed", "expired", "rejected", "blocked", "revoked", "cancelled"}:
        return "status-badge status-red"
    return "status-badge status-neutral"


def payment_status_badge_class(status: str | None) -> str:
    return status_badge_class(status)


def user_status_badge_class(blocked: bool) -> str:
    return "status-badge status-red" if blocked else "status-badge status-green"


def payment_status_label(status: str | None) -> str:
    return {
        "waiting": "Waiting for payment",
        "upi_submitted": "UPI submitted for review",
        "binance_submitted": "Binance Pay submitted for review",
        "usdt_manual_submitted": "Manual USDT submitted for review",
        "confirmed": "Confirmed",
        "approved": "Approved",
        "completed": "Completed",
        "expired": "Expired",
        "rejected": "Rejected",
    }.get((status or "unknown").lower(), str(status or "unknown").replace("_", " ").title())


def method_label(method: str | None) -> str:
    method = (method or "").lower()
    return {
        "upi": "UPI",
        "usdt": "USDT (BEP20)",
        "polygon": "USDT (POLYGON)",
        "usdt_polygon": "USDT (POLYGON)",
        "binance": "Binance Pay",
        "replacement": "Replacement",
        "admin": "Admin wallet adjustment",
        "admin_add": "Admin wallet add",
        "admin_remove": "Admin wallet remove",
        "admin_stock": "Admin stock send",
        "admin_created_order": "Admin-created order",
    }.get(method, method.upper() if method else "N/A")


if __name__ == "__main__":
    app = create_app()
    host = os.getenv("ADMIN_PANEL_HOST", "0.0.0.0")
    port = int(os.getenv("ADMIN_PANEL_PORT", "8080") or 8080)
    debug = os.getenv("ADMIN_PANEL_DEBUG", "0").lower() in {"1", "true", "yes"}
    app.run(host=host, port=port, debug=debug)
