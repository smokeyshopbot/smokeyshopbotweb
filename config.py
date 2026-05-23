"""Configuration loader for the Telegram shop bot.

Only MongoDB connection values are loaded from .env. Bot secrets such as the
Telegram bot token, explorer API keys, support usernames, and timing/settings
are managed from WebAdmin → Secret Settings and stored in MongoDB.
"""

from __future__ import annotations

import os
import hashlib
from dotenv import load_dotenv
from pymongo import MongoClient

load_dotenv()


def _required(name: str) -> str:
    value = os.getenv(name)
    if value is None or not value.strip():
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value.strip()


def _split_csv(value: str) -> list[str]:
    return [part.strip() for part in str(value or "").split(",") if part.strip()]


# MongoDB is the only required bot-side .env configuration. All other bot
# secrets/settings are pulled from the database so changing them in WebAdmin is
# the single source of truth.
MONGO_URI: str = _required("MONGO_URI")
DB_NAME: str = os.getenv("DB_NAME", "shopbot").strip() or "shopbot"
SECRET_SETTINGS_KEY = "secret_settings"
RUNTIME_BOT_TOKEN_KEY = "telegram_bot_token"


def mask_secret_value(value: str | None, *, keep_left: int = 4, keep_right: int = 4) -> str:
    """Return a safe preview for logs/UI without exposing the secret."""
    raw = str(value or "").strip()
    if not raw:
        return "missing"
    if len(raw) <= keep_left + keep_right:
        return "saved"
    return f"{raw[:keep_left]}…{raw[-keep_right:]}"


def secret_fingerprint(value: str | None) -> str:
    raw = str(value or "").strip()
    if not raw:
        return "missing"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]


def mask_bot_token(value: str | None) -> str:
    """Mask Telegram bot tokens while keeping enough info to compare configs."""
    raw = str(value or "").strip()
    if not raw:
        return "missing"
    if ":" not in raw:
        return f"saved / sha256:{secret_fingerprint(raw)}"
    bot_id, token_secret = raw.split(":", 1)
    safe_id = mask_secret_value(bot_id, keep_left=4, keep_right=2)
    safe_secret = mask_secret_value(token_secret, keep_left=0, keep_right=4)
    return f"bot_id {safe_id}:{safe_secret} / sha256:{secret_fingerprint(raw)}"


def _load_secret_settings() -> dict:
    """Load WebAdmin-managed secrets from MongoDB.

    The Telegram bot token is read from runtime_config.telegram_bot_token first.
    This makes the latest token saved from WebAdmin authoritative even if an old
    value is still present in the legacy settings document.
    """
    try:
        client = MongoClient(
            MONGO_URI,
            tls=True,
            tlsAllowInvalidCertificates=True,
            serverSelectionTimeoutMS=30000,
        )
        db = client[DB_NAME]
        doc = db.settings.find_one({"key": SECRET_SETTINGS_KEY}) or {}
        value = doc.get("value") if isinstance(doc, dict) else {}
        settings = dict(value) if isinstance(value, dict) else {}

        token_doc = db.runtime_config.find_one({"key": RUNTIME_BOT_TOKEN_KEY}) or {}
        runtime_token = str(token_doc.get("value") or "").strip() if isinstance(token_doc, dict) else ""
        if runtime_token:
            settings["bot_token"] = runtime_token
            settings["bot_token_runtime_updated_at"] = token_doc.get("updated_at")
        settings["secret_settings_updated_at"] = doc.get("updated_at") if isinstance(doc, dict) else None
        client.close()
        return settings
    except Exception:
        # Keep config import readable; the required-token error below gives the
        # admin a clear action after WebAdmin is configured.
        return {}


_SECRET_SETTINGS = _load_secret_settings()


def _secret_str(key: str, default: str = "") -> str:
    value = _SECRET_SETTINGS.get(key, default)
    if value is None:
        return default
    return str(value).strip()


def _secret_required(key: str, label: str) -> str:
    value = _secret_str(key)
    if not value:
        raise RuntimeError(
            f"Missing {label}. Open WebAdmin → Secret Settings, save {label}, then restart the bot."
        )
    return value


def _secret_int(key: str, default: int) -> int:
    value = _secret_str(key)
    if not value:
        return default
    try:
        return int(value)
    except ValueError as exc:
        raise RuntimeError(f"Secret setting {key} must be an integer, got: {value!r}") from exc


# Telegram
BOT_TOKEN: str = _secret_required("bot_token", "Telegram bot token")
BOT_TOKEN_SOURCE: str = f"MongoDB WebAdmin runtime_config/key={RUNTIME_BOT_TOKEN_KEY}, fallback {DB_NAME}.settings/key={SECRET_SETTINGS_KEY}"
BOT_TOKEN_RUNTIME_UPDATED_AT = _SECRET_SETTINGS.get("bot_token_runtime_updated_at")
SECRET_SETTINGS_UPDATED_AT = _SECRET_SETTINGS.get("secret_settings_updated_at")
BOT_TOKEN_PREVIEW: str = mask_bot_token(BOT_TOKEN)
BOT_TOKEN_FINGERPRINT: str = secret_fingerprint(BOT_TOKEN)
LEGACY_ENV_BOT_TOKEN_NAMES: tuple[str, ...] = ("BOT_TOKEN", "TELEGRAM_BOT_TOKEN", "TG_BOT_TOKEN")

# Telegram admin panel commands are removed, but these IDs are allowed to use
# the bot while maintenance mode is ON for testing. Configure in WebAdmin →
# Secret Settings → Telegram admin/tester IDs, or fallback to ADMIN_IDS in .env.
def _parse_admin_ids(value: str) -> list[int]:
    ids: list[int] = []
    for part in _split_csv(value):
        try:
            ids.append(int(part))
        except (TypeError, ValueError):
            continue
    return ids


ADMIN_IDS: list[int] = _parse_admin_ids(_secret_str("admin_ids", os.getenv("ADMIN_IDS", "")))
ADMIN_ID: int = ADMIN_IDS[0] if ADMIN_IDS else 0


def is_admin_id(user_id: int | str | None) -> bool:
    try:
        return int(user_id or 0) in ADMIN_IDS
    except (TypeError, ValueError):
        return False


# USDT BEP20 verification providers. Managed in WebAdmin → Secret Settings.
BSCSAN_API_KEY_COMPAT = _secret_str("bscscan_api_key")
BSCSCAN_API_KEY: str = BSCSAN_API_KEY_COMPAT
ETHERSCAN_API_KEY: str = _secret_str("etherscan_api_key", BSCSCAN_API_KEY)
BSC_RPC_URL: str = _secret_str("bsc_rpc_url", "https://bsc-rpc.publicnode.com") or "https://bsc-rpc.publicnode.com"
BSC_RPC_URLS: str = _secret_str(
    "bsc_rpc_urls",
    "https://bsc-rpc.publicnode.com,https://bsc.drpc.org,https://rpc.ankr.com/bsc",
)
USDT_LOOKBACK_SECONDS: int = _secret_int("usdt_lookback_seconds", 3600)
BSC_RPC_BLOCK_CHUNK_SIZE: int = _secret_int("bsc_rpc_block_chunk_size", 450)
BEP20_REQUIRED_CONFIRMATIONS: int = _secret_int("bep20_required_confirmations", 3)

# Binance Pay auto-verification. Managed in WebAdmin → Secret Settings.
# These are optional at import time so Binance Pay can still be used manually
# until the admin saves API credentials and restarts the bot.
BINANCE_API_KEY: str = _secret_str("binance_api_key")
BINANCE_API_SECRET: str = _secret_str("binance_api_secret")
BINANCE_API_BASE_URL: str = _secret_str("binance_api_base_url", "https://api.binance.com") or "https://api.binance.com"
BINANCE_RECV_WINDOW_MS: int = _secret_int("binance_recv_window_ms", 5000)
BINANCE_PAY_HISTORY_LOOKBACK_SECONDS: int = _secret_int("binance_pay_history_lookback_seconds", 3600)

# Support
SUPPORT_USERNAMES: list[str] = _split_csv(_secret_str("support_usernames"))
SUPPORT_USERNAME: str = SUPPORT_USERNAMES[0] if SUPPORT_USERNAMES else ""

# Rates & Settings
PAYMENT_TIMEOUT_MINUTES: int = _secret_int("payment_timeout_minutes", 30)
PAYMENT_REMINDER_MINUTES: int = _secret_int("payment_reminder_minutes", 20)
if PAYMENT_REMINDER_MINUTES >= PAYMENT_TIMEOUT_MINUTES:
    PAYMENT_REMINDER_MINUTES = max(1, PAYMENT_TIMEOUT_MINUTES - 10)
USDT_VERIFY_INTERVAL: int = _secret_int("usdt_verify_interval_seconds", 30)
USDT_MANUAL_VERIFY_DELAY_MINUTES: int = _secret_int("usdt_manual_verify_delay_minutes", 5)
LOW_STOCK_ALERT_THRESHOLD: int = _secret_int("low_stock_alert_threshold", 10)
RESTOCK_NOTIFICATION_COOLDOWN_MINUTES: int = _secret_int("restock_notification_cooldown_minutes", 60)
RESTOCK_LONG_NOTIFICATION_COOLDOWN_MINUTES: int = _secret_int("restock_long_notification_cooldown_minutes", 360)
RESTOCK_HIGH_STOCK_THRESHOLD: int = _secret_int("restock_high_stock_threshold", 20)
MIN_ORDER_QUANTITY: int = max(1, _secret_int("min_order_quantity", 1))
MAX_ORDER_QUANTITY: int = max(MIN_ORDER_QUANTITY, _secret_int("max_order_quantity", 100))
