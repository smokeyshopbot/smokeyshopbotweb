"""
database.py — MongoDB models and all DB helper functions.
Collections:
  - products   : { name, price_inr, price_usdt, stock: [str], created_at }
  - users      : { user_id, username, blocked, wallet_inr, wallet_usdt, joined_at }
  - orders     : { order_id, user_id, product_name, quantity, items, payment_method,
                   amount_inr, amount_usdt, status, created_at, delivered_at }
  - pending_payments : { user_id, order_id/wallet_load, type, expected_usdt,
                         expected_inr, unique_amount, created_at }
"""

from __future__ import annotations
import re
import hashlib
import time
import uuid
from datetime import datetime, timezone, timedelta
from typing import Optional, Any
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from motor.motor_asyncio import AsyncIOMotorClient
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError
from bson import ObjectId
from config import MONGO_URI, DB_NAME

_client: Optional[AsyncIOMotorClient] = None
_db = None


def _name_regex(name: str) -> dict:
    """Case-insensitive exact match for product names, escaping regex metacharacters."""
    return {"$regex": f"^{re.escape(name.strip())}$", "$options": "i"}


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


def get_db():
    global _client, _db
    if _db is None:
        _client = AsyncIOMotorClient(
            MONGO_URI,
            tls=True,
            tlsAllowInvalidCertificates=True,  # Fixes SSL handshake on Python 3.13
            serverSelectionTimeoutMS=30000,
        )
        _db = _client[DB_NAME]
    return _db


async def record_admin_activity(
    action: str,
    details: str = "",
    *,
    username: str = "",
    role: str = "telegram_admin",
    user_id: int | None = None,
    source: str = "telegram_bot",
) -> None:
    """Mirror Telegram-side admin actions into WebAdmin Activity Log."""
    doc = {
        "action": str(action or "").strip(),
        "details": str(details or "").strip(),
        "created_at": datetime.now(timezone.utc),
        "admin_username": str(username or "").strip().lstrip("@"),
        "admin_role": str(role or "telegram_admin").strip(),
        "admin_source": str(source or "telegram_bot").strip(),
    }
    if user_id is not None:
        try:
            doc["admin_user_id"] = int(user_id)
        except Exception:
            pass
    try:
        await get_db().admin_activity.insert_one(doc)
    except Exception:
        pass

def _stock_ledger_product_key(product_name: Any) -> str:
    return str(product_name or "").strip().lower()


def _stock_ledger_item_hash(item: Any) -> str:
    clean = normalize_approved_stock_item(item)
    return hashlib.sha256(clean.encode("utf-8")).hexdigest() if clean else ""


def _stock_ledger_search_text(item: Any) -> str:
    return re.sub(r"\s+", " ", normalize_approved_stock_item(item).lower()).strip()


def _stock_ledger_movement(
    movement_type: str,
    *,
    username: str = "",
    role: str = "",
    user_id: int | None = None,
    source: str = "system",
    order_id: str = "",
    note: str = "",
) -> dict:
    movement = {
        "type": str(movement_type or "").strip().lower(),
        "at": datetime.now(timezone.utc),
        "username": str(username or "").strip().lstrip("@"),
        "role": str(role or "").strip(),
        "source": str(source or "system").strip(),
        "order_id": str(order_id or "").strip().upper(),
        "note": str(note or "").strip()[:1000],
    }
    if user_id is not None:
        try:
            movement["user_id"] = int(user_id)
        except Exception:
            pass
    return movement


async def record_stock_ledger_add(
    product_name: str,
    items: list[str],
    *,
    username: str = "",
    role: str = "telegram_admin",
    user_id: int | None = None,
    source: str = "telegram_bot",
    stock_upload_kind: str = "normal",
    note: str = "",
) -> None:
    """Keep every stock item searchable even after it is sold/removed later."""
    clean_product = str(product_name or "").strip()
    product_key = _stock_ledger_product_key(clean_product)
    if not product_key:
        return
    now = datetime.now(timezone.utc)
    clean_username = str(username or "").strip().lstrip("@")
    clean_role = str(role or "telegram_admin").strip()
    upload_kind = str(stock_upload_kind or "normal").strip().lower() or "normal"
    movement = _stock_ledger_movement(
        "added", username=clean_username, role=clean_role, user_id=user_id, source=source, note=note
    )
    database = get_db()
    for item in items or []:
        clean_item = normalize_approved_stock_item(item)
        item_hash = _stock_ledger_item_hash(clean_item)
        if not clean_item or not item_hash:
            continue
        try:
            await database.stock_item_ledger.update_one(
                {"product_key": product_key, "item_hash": item_hash},
                {
                    "$setOnInsert": {
                        "product_key": product_key,
                        "item_hash": item_hash,
                        "first_added_at": now,
                        "first_added_by_username": clean_username,
                        "first_added_by_role": clean_role,
                        "first_added_by_user_id": int(user_id) if user_id is not None else None,
                        "first_added_source": str(source or "telegram_bot").strip(),
                    },
                    "$set": {
                        "product_name": clean_product,
                        "item_text": clean_item,
                        "item_search_text": _stock_ledger_search_text(clean_item),
                        "current_status": "available",
                        "current_status_at": now,
                        "last_movement_at": now,
                        "last_added_at": now,
                        "last_added_by_username": clean_username,
                        "last_added_by_role": clean_role,
                        "last_added_by_user_id": int(user_id) if user_id is not None else None,
                        "last_added_source": str(source or "telegram_bot").strip(),
                        "stock_upload_kind": upload_kind,
                        "current_order_id": "",
                        "current_user_id": None,
                        "current_username": "",
                        "updated_at": now,
                    },
                    "$push": {"movements": {"$each": [movement], "$slice": -80}},
                },
                upsert=True,
            )
        except Exception:
            pass


async def record_stock_ledger_status(
    product_name: str,
    items: list[str],
    status: str,
    *,
    order: dict | None = None,
    username: str = "",
    role: str = "",
    user_id: int | None = None,
    source: str = "system",
    note: str = "",
) -> None:
    clean_product = str(product_name or "").strip()
    product_key = _stock_ledger_product_key(clean_product)
    clean_status = str(status or "").strip().lower() or "unknown"
    if not product_key or not items:
        return
    now = datetime.now(timezone.utc)
    order = order or {}
    order_id = str(order.get("order_id") or "").strip().upper()
    current_user_id = user_id
    if current_user_id is None and order.get("user_id") is not None:
        try:
            current_user_id = int(order.get("user_id"))
        except Exception:
            current_user_id = None
    current_username = str(order.get("username") or "").strip().lstrip("@")
    movement = _stock_ledger_movement(
        clean_status,
        username=username,
        role=role,
        user_id=current_user_id,
        source=source,
        order_id=order_id,
        note=note,
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
    database = get_db()
    for item in items or []:
        clean_item = normalize_approved_stock_item(item)
        item_hash = _stock_ledger_item_hash(clean_item)
        if not clean_item or not item_hash:
            continue
        try:
            await database.stock_item_ledger.update_one(
                {"product_key": product_key, "item_hash": item_hash},
                {
                    "$setOnInsert": {
                        "product_key": product_key,
                        "item_hash": item_hash,
                        "first_added_at": now,
                        "first_added_by_username": str(username or "").strip().lstrip("@"),
                        "first_added_by_role": str(role or "").strip(),
                        "first_added_source": str(source or "system").strip(),
                    },
                    "$set": {**set_fields, "item_text": clean_item, "item_search_text": _stock_ledger_search_text(clean_item)},
                    "$push": {"movements": {"$each": [movement], "$slice": -80}},
                },
                upsert=True,
            )
        except Exception:
            pass


async def record_order_items_delivered_in_ledger(order: dict, items: list[str], *, source: str = "telegram_bot") -> None:
    if not order or not items:
        return
    if order.get("is_replacement"):
        status = "replacement_delivered"
    elif order.get("admin_stock_delivery") or order.get("payment_method") == "admin_stock":
        status = "admin_sent"
    else:
        status = "delivered"
    await record_stock_ledger_status(
        str(order.get("product_name") or ""),
        items,
        status,
        order=order,
        source=source,
        note="Attached to delivered order history",
    )



# ─────────────────────────── USERS ───────────────────────────

async def get_user(user_id: int) -> Optional[dict]:
    return await get_db().users.find_one({"user_id": user_id})


async def upsert_user(user_id: int, username: str = "") -> dict:
    db = get_db()
    username = (username or "").strip().lstrip("@")
    existing = await db.users.find_one({"user_id": user_id})
    if existing:
        if username and existing.get("username") != username:
            await db.users.update_one(
                {"user_id": user_id},
                {"$set": {"username": username, "username_updated_at": datetime.now(timezone.utc)}},
            )
            existing["username"] = username
        return existing
    doc = {
        "user_id": user_id,
        "username": username,
        "blocked": False,
        "wallet_inr": 0.0,
        "wallet_usdt": 0.0,
        "joined_at": datetime.now(timezone.utc),
        "language": None,
        "language_selected": False,
    }
    await db.users.insert_one(doc)
    return doc


async def is_blocked(user_id: int) -> bool:
    user = await get_user(user_id)
    return bool(user and user.get("blocked"))


def normalize_language(value: str | None) -> str:
    value = (value or "").strip().lower()
    if value in {"spanish", "espanol", "español"}:
        return "es"
    if value in {"english", "eng"}:
        return "en"
    return value if value in {"en", "es"} else "en"


async def get_user_language(user_id: int, default: str = "en") -> str:
    user = await get_user(user_id)
    if not user:
        return normalize_language(default)
    return normalize_language(user.get("language") or user.get("language_code") or default)


async def has_selected_language(user_id: int) -> bool:
    user = await get_user(user_id)
    return bool(user and user.get("language_selected"))


async def set_user_language(user_id: int, language: str) -> str:
    language = normalize_language(language)
    await upsert_user(user_id, "")
    await get_db().users.update_one(
        {"user_id": user_id},
        {"$set": {"language": language, "language_selected": True, "language_updated_at": datetime.now(timezone.utc)}},
    )
    return language


async def get_enabled_languages() -> list[str]:
    doc = await get_db().settings.find_one({"key": "language_settings"}) or {}
    value = doc.get("value") if isinstance(doc, dict) else {}
    enabled = value.get("enabled_languages") if isinstance(value, dict) else None
    if not isinstance(enabled, list):
        enabled = ["en", "es"]
    cleaned = []
    for lang in enabled:
        lang = normalize_language(str(lang))
        if lang not in cleaned:
            cleaned.append(lang)
    if "en" not in cleaned:
        cleaned.insert(0, "en")
    return [lang for lang in cleaned if lang in {"en", "es"}] or ["en"]


async def get_language_settings() -> dict:
    enabled = await get_enabled_languages()
    return {"default_language": "en", "enabled_languages": enabled}


async def set_blocked(user_id: int, blocked: bool):
    await get_db().users.update_one(
        {"user_id": user_id}, {"$set": {"blocked": blocked}}
    )


async def get_all_users() -> list[dict]:
    """Return active users newest first."""
    return await get_db().users.find({"blocked": False}).sort("joined_at", -1).to_list(length=None)


async def get_all_users_including_blocked() -> list[dict]:
    """Return all users newest first."""
    return await get_db().users.find().sort("joined_at", -1).to_list(length=None)


async def get_user_favorite_products(user_id: int) -> list[str]:
    user = await get_user(user_id)
    favorites = user.get("favorite_products", []) if user else []
    return [str(name) for name in favorites if str(name).strip()]


async def is_favorite_product(user_id: int, product_name: str) -> bool:
    favorites = await get_user_favorite_products(user_id)
    target = str(product_name or "").strip().lower()
    return any(str(name).strip().lower() == target for name in favorites)


async def set_product_favorite(user_id: int, product_name: str, favorite: bool) -> bool:
    product_name = str(product_name or "").strip()
    if not product_name:
        return False
    await upsert_user(user_id, "")
    if favorite:
        await get_db().users.update_one({"user_id": user_id}, {"$addToSet": {"favorite_products": product_name}})
    else:
        current = await get_user_favorite_products(user_id)
        kept = [name for name in current if str(name).strip().lower() != product_name.lower()]
        await get_db().users.update_one({"user_id": user_id}, {"$set": {"favorite_products": kept}})
    return True


async def toggle_product_favorite(user_id: int, product_name: str) -> bool:
    currently = await is_favorite_product(user_id, product_name)
    await set_product_favorite(user_id, product_name, not currently)
    return not currently


async def add_wallet_inr(user_id: int, amount: float):
    await get_db().users.update_one(
        {"user_id": user_id}, {"$inc": {"wallet_inr": round(amount, 2)}}
    )


async def add_wallet_usdt(user_id: int, amount: float):
    await get_db().users.update_one(
        {"user_id": user_id}, {"$inc": {"wallet_usdt": round(amount, 2)}}
    )


async def deduct_wallet_inr(user_id: int, amount: float) -> bool:
    """Atomically deduct INR wallet balance only when enough balance exists.

    The balance check and deduction happen in one MongoDB update, preventing two
    fast wallet-payment clicks from both spending the same balance.
    """
    try:
        amount = round(float(amount or 0), 2)
    except (TypeError, ValueError):
        return False
    if amount <= 0:
        return False
    result = await get_db().users.update_one(
        {"user_id": user_id, "wallet_inr": {"$gte": amount}},
        {"$inc": {"wallet_inr": -amount}},
    )
    return bool(result.modified_count)


async def deduct_wallet_usdt(user_id: int, amount: float) -> bool:
    """Atomically deduct USDT wallet balance only when enough balance exists."""
    try:
        amount = round(float(amount or 0), 6)
    except (TypeError, ValueError):
        return False
    if amount <= 0:
        return False
    result = await get_db().users.update_one(
        {"user_id": user_id, "wallet_usdt": {"$gte": amount}},
        {"$inc": {"wallet_usdt": -amount}},
    )
    return bool(result.modified_count)


# ─────────────────────────── PRODUCTS ────────────────────────


def parse_product_shop_order(value, default=None):
    """Return a safe product display order value or default.

    Lower numbers appear first in the Telegram shop. Missing/blank legacy
    products stay in automatic/newest-first order after explicitly ordered
    products. This helper never writes to the database, so it is safe for
    existing data.
    """
    try:
        order = int(str(value).strip())
    except (TypeError, ValueError):
        return default
    return order if order >= 1 else default


def _sort_dt_value(value) -> float:
    if isinstance(value, datetime):
        dt = value
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    return 0.0


def product_shop_sort_key(product: dict) -> tuple:
    """Sort key used everywhere products are shown to buyers/admins.

    Explicit shop_order values come first from low to high. Products without a
    saved shop_order keep the previous automatic newest-first behavior.
    """
    order = parse_product_shop_order((product or {}).get("shop_order"))
    if order is not None:
        return (0, order, -_sort_dt_value((product or {}).get("created_at")), str((product or {}).get("name") or "").lower())
    return (1, 0, -_sort_dt_value((product or {}).get("created_at")), str((product or {}).get("name") or "").lower())


def sort_products_for_shop(products: list[dict]) -> list[dict]:
    return sorted(products, key=product_shop_sort_key)


async def get_all_products() -> list[dict]:
    """Return products in Telegram shop display order."""
    products = await get_db().products.find().to_list(length=None)
    return sort_products_for_shop(products)


async def get_product(name: str) -> Optional[dict]:
    return await get_db().products.find_one({"name": _name_regex(name)})


async def get_product_by_id(product_id: str) -> Optional[dict]:
    try:
        oid = ObjectId(str(product_id))
    except Exception:
        return None
    return await get_db().products.find_one({"_id": oid})


async def add_product(name: str, price_inr: float, price_usdt: float, shop_order: int | None = None, warranty_days: int = 0) -> bool:
    existing = await get_product(name)
    if existing:
        return False
    doc = {
        "name": name,
        "price_inr": price_inr,
        "price_usdt": price_usdt,
        "stock": [],
        "enabled": True,
        "min_order_quantity": 1,
        "max_order_quantity": 100,
        "low_stock_threshold": 10,
        "low_stock_alert_sent": False,
        "warranty_days": max(0, int(warranty_days or 0)),
        "created_at": datetime.now(timezone.utc),
    }
    parsed_order = parse_product_shop_order(shop_order)
    if parsed_order is not None:
        doc["shop_order"] = parsed_order
    await get_db().products.insert_one(doc)
    return True


async def set_product_shop_order(name: str, shop_order: int | None) -> bool:
    """Set or clear a product's Telegram shop display order.

    Passing None clears the custom order, returning the product to automatic
    newest-first placement after explicitly ordered products.
    """
    parsed_order = parse_product_shop_order(shop_order)
    if parsed_order is None:
        update = {
            "$unset": {"shop_order": ""},
            "$set": {"updated_at": datetime.now(timezone.utc)},
        }
    else:
        update = {
            "$set": {
                "shop_order": parsed_order,
                "updated_at": datetime.now(timezone.utc),
            }
        }
    res = await get_db().products.update_one({"name": _name_regex(name)}, update)
    return res.matched_count > 0


async def remove_product(name: str) -> bool:
    res = await get_db().products.delete_one(
        {"name": _name_regex(name)}
    )
    return res.deleted_count > 0


async def update_product_price(name: str, price_inr: float, price_usdt: float) -> bool:
    res = await get_db().products.update_one(
        {"name": _name_regex(name)},
        {"$set": {"price_inr": price_inr, "price_usdt": price_usdt}},
    )
    return res.matched_count > 0


async def set_product_enabled(name: str, enabled: bool) -> bool:
    """Enable/disable a product without deleting it."""
    res = await get_db().products.update_one(
        {"name": _name_regex(name)},
        {"$set": {"enabled": bool(enabled)}},
    )
    return res.matched_count > 0


async def set_low_stock_alert_sent(product_name: str, sent: bool) -> bool:
    res = await get_db().products.update_one(
        {"name": _name_regex(product_name)},
        {"$set": {"low_stock_alert_sent": bool(sent)}},
    )
    return res.matched_count > 0


async def record_stock_upload_rejection(
    product_name: str,
    rejected_items: list[str],
    *,
    accepted_count: int = 0,
    duplicate_count: int = 0,
    upload_kind: str = "normal",
    source: str = "telegram",
    username: str = "",
    role: str = "telegram_admin",
    user_id: int | None = None,
) -> dict | None:
    rejected = [normalize_approved_stock_item(item) for item in rejected_items if normalize_approved_stock_item(item)]
    if not rejected:
        return None
    doc = {
        "product_name": str(product_name or "").strip(),
        "username": str(username or "").strip(),
        "username_key": str(username or "").strip().lower(),
        "role": str(role or "telegram_admin").strip(),
        "source": str(source or "telegram").strip(),
        "upload_kind": "replacement" if str(upload_kind or "").strip().lower() == "replacement" else "normal",
        "accepted_count": int(accepted_count or 0),
        "duplicate_count": int(duplicate_count or 0),
        "rejected_count": len(rejected),
        "rejected_items": rejected[:200],
        "rejected_preview": rejected[:5],
        "reason": "Not in owner-approved stock pool",
        "created_at": datetime.now(timezone.utc),
    }
    if user_id is not None:
        doc["user_id"] = int(user_id)
    try:
        await get_db().stock_upload_rejections.insert_one(doc)
    except Exception:
        pass
    return doc


async def add_stock(
    product_name: str,
    items: list[str],
    *,
    username: str = "",
    role: str = "telegram_admin",
    user_id: int | None = None,
    source: str = "telegram_bot",
    stock_upload_kind: str = "normal",
) -> int:
    """Add only fresh stock items to the end of this product's queue.

    Exact duplicate stock is rejected within the same product, including
    stock already available, stock already delivered, and duplicates repeated
    in the same upload. The same stock text may still be added to a different
    product.

    Stock is delivered FIFO: older items already in the array stay at the
    front, and newly added fresh items go to the back.
    """
    product = await get_product(product_name)
    if not product:
        return 0

    existing = {
        normalize_approved_stock_item(item)
        for item in (product.get("stock", []) or [])
        if normalize_approved_stock_item(item)
    }
    cursor = get_db().orders.find(
        {"product_name": _name_regex(product_name), "items.0": {"$exists": True}},
        {"items": 1},
    )
    async for order in cursor:
        existing.update(normalize_approved_stock_item(item) for item in (order.get("items", []) or []) if normalize_approved_stock_item(item))

    seen_in_upload: set[str] = set()
    fresh_items: list[str] = []
    for item in items:
        clean = normalize_approved_stock_item(item)
        if not clean:
            continue
        if clean in existing or clean in seen_in_upload:
            continue
        fresh_items.append(clean)
        seen_in_upload.add(clean)

    if not fresh_items:
        return 0

    res = await get_db().products.update_one(
        {"_id": product["_id"]},
        {"$push": {"stock": {"$each": fresh_items}}},
    )
    if res.matched_count:
        await record_stock_ledger_add(
            product.get("name") or product_name,
            fresh_items,
            username=username,
            role=role,
            user_id=user_id,
            source=source,
            stock_upload_kind=stock_upload_kind,
        )
    return len(fresh_items) if res.matched_count else 0


async def remove_stock_items(product_name: str, items: list[str]) -> dict:
    """
    Remove specific stock item(s) from a product.

    Matching is exact after the admin input is stripped. If the same stock value
    exists multiple times, one occurrence is removed for each matching item sent
    by the admin. Returns removed items, not-found items, and remaining count.
    """
    product = await get_product(product_name)
    if not product:
        return {"removed": [], "not_found": items, "remaining": 0}

    stock = list(product.get("stock", []))
    removed: list[str] = []
    not_found: list[str] = []

    for item in items:
        try:
            index = stock.index(item)
        except ValueError:
            not_found.append(item)
            continue
        removed.append(stock.pop(index))

    if removed:
        await get_db().products.update_one(
            {"_id": product["_id"]},
            {"$set": {"stock": stock}},
        )
        await record_stock_ledger_status(product.get("name") or product_name, removed, "removed", source="telegram_bot", note="Removed from current stock")

    return {"removed": removed, "not_found": not_found, "remaining": len(stock)}


async def pop_stock(product_name: str, quantity: int) -> list[str]:
    """Atomically remove and return the oldest `quantity` stock items.

    This is FIFO delivery: stock added first is sold/delivered first.
    """
    if quantity < 1:
        return []

    stock_expr = {"$ifNull": ["$stock", []]}
    db = get_db()
    updated = await db.products.find_one_and_update(
        {
            "name": _name_regex(product_name),
            "$expr": {"$gte": [{"$size": stock_expr}, quantity]},
        },
        [
            {
                "$set": {
                    "_last_popped": {"$slice": [stock_expr, quantity]},
                    "stock": {
                        "$cond": [
                            {"$gt": [{"$size": stock_expr}, quantity]},
                            {
                                "$slice": [
                                    stock_expr,
                                    quantity,
                                    {"$max": [1, {"$subtract": [{"$size": stock_expr}, quantity]}]},
                                ]
                            },
                            [],
                        ]
                    },
                }
            }
        ],
        return_document=ReturnDocument.AFTER,
    )
    if not updated:
        return []

    items = updated.get("_last_popped", [])
    await db.products.update_one(
        {"_id": updated["_id"]},
        {"$unset": {"_last_popped": ""}},
    )
    if items:
        await record_stock_ledger_status(product_name, items, "popped", source="telegram_bot", note="Removed from live stock before delivery finalization")
    return items


async def get_stock_count(product_name: str) -> int:
    product = await get_product(product_name)
    return len(product.get("stock", [])) if product else 0


async def get_pending_stock_quantity(product_name: str) -> int:
    """Total quantity already paid but waiting for stock for this product."""
    cursor = get_db().orders.aggregate([
        {
            "$match": {
                "product_name": _name_regex(product_name),
                "status": "pending_stock",
            }
        },
        {"$group": {"_id": None, "total": {"$sum": "$quantity"}}},
    ])
    rows = await cursor.to_list(length=1)
    return int(rows[0].get("total", 0)) if rows else 0


async def get_available_stock_count(product_name: str) -> int:
    """Stock available for new buyers after paid pending-stock orders are considered."""
    actual = await get_stock_count(product_name)
    pending_qty = await get_pending_stock_quantity(product_name)
    return max(0, actual - pending_qty)

def get_product_restock_threshold(product: dict | None, default: int = 10) -> int:
    try:
        return max(1, int((product or {}).get("low_stock_threshold") or default or 10))
    except Exception:
        return max(1, int(default or 10))


async def claim_restock_notification_slot(
    product_name: str,
    previous_available_stock: int,
    current_available_stock: int,
    *,
    cooldown_minutes: int = 60,
    back_in_stock_cooldown_minutes: int = 30,
    long_cooldown_minutes: int = 360,
    big_restock_quantity: int = 20,
    high_stock_threshold: int | None = None,
    default_threshold: int = 10,
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
    product = await get_db().products.find_one(
        {"name": _name_regex(product_name)},
        {"_id": 1, "name": 1, "enabled": 1, "low_stock_threshold": 1},
    )
    if not product or product.get("enabled", True) is False:
        return False
    try:
        previous_available_stock = max(0, int(previous_available_stock or 0))
        current_available_stock = max(0, int(current_available_stock or 0))
    except Exception:
        return False
    if current_available_stock <= 0:
        return False

    threshold = get_product_restock_threshold(product, default_threshold)
    available_increase = max(0, current_available_stock - previous_available_stock)
    try:
        uploaded_count = max(0, int(added_stock_count if added_stock_count is not None else available_increase))
    except Exception:
        uploaded_count = available_increase
    try:
        big_quantity = max(1, int(big_restock_quantity if big_restock_quantity is not None else high_stock_threshold or 20))
    except Exception:
        big_quantity = 20

    back_in_stock = previous_available_stock <= 0 and current_available_stock >= threshold
    low_stock_recovered = 0 < previous_available_stock < threshold <= current_available_stock
    stock_doubled = previous_available_stock > 0 and current_available_stock >= previous_available_stock * 2 and available_increase > 0
    big_restock = previous_available_stock >= threshold and current_available_stock >= threshold and available_increase > 0 and (
        uploaded_count >= big_quantity or stock_doubled
    )

    if not back_in_stock and not low_stock_recovered and not big_restock:
        return False

    try:
        back_in_stock_cooldown = max(1, int(back_in_stock_cooldown_minutes or 30))
    except Exception:
        back_in_stock_cooldown = 30
    try:
        normal_cooldown = max(1, int(cooldown_minutes or 60))
    except Exception:
        normal_cooldown = 60
    try:
        long_cooldown = max(normal_cooldown, int(long_cooldown_minutes or 360))
    except Exception:
        long_cooldown = max(normal_cooldown, 360)

    if back_in_stock:
        required_cooldown = back_in_stock_cooldown
    elif low_stock_recovered:
        required_cooldown = normal_cooldown
    else:
        required_cooldown = long_cooldown

    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(minutes=required_cooldown)
    result = await get_db().products.update_one(
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


async def get_all_products_with_availability(include_disabled: bool = False) -> list[dict]:
    """Return products with available_stock and pending_stock_quantity fields.

    By default, disabled products are hidden from the shop. Admin views can pass
    include_disabled=True to see everything. Missing "enabled" means enabled for
    backwards compatibility with existing products.
    """
    if include_disabled:
        products = await get_all_products()
    else:
        products = await get_db().products.find({
            "$or": [{"enabled": True}, {"enabled": {"$exists": False}}]
        }).to_list(length=None)
        products = sort_products_for_shop(products)
    for product in products:
        actual_stock = len(product.get("stock", []) or [])
        pending_qty = await get_pending_stock_quantity(product.get("name", ""))
        product["enabled"] = product.get("enabled", True)
        product["shop_order"] = parse_product_shop_order(product.get("shop_order"), default=None)
        product["actual_stock"] = actual_stock
        product["pending_stock_quantity"] = pending_qty
        product["available_stock"] = max(0, actual_stock - pending_qty)
    return products


async def clear_stock(product_name: str) -> bool:
    product = await get_product(product_name)
    current_items = list((product or {}).get("stock", []) or [])
    res = await get_db().products.update_one(
        {"name": _name_regex(product_name)},
        {"$set": {"stock": []}},
    )
    if res.matched_count and current_items:
        await record_stock_ledger_status(product_name, current_items, "removed", source="telegram_bot", note="Product stock cleared")
    return res.matched_count > 0


# ─────────────────────────── ORDERS ──────────────────────────

async def create_order(
    user_id: int,
    product_name: str,
    quantity: int,
    payment_method: str,
    amount_inr: float,
    amount_usdt: float,
    username: str = "",
) -> str:
    """Create an order with duplicate-safe order-id generation.

    Existing public order IDs stay short. If a rare collision happens, the insert
    is retried with a new ID instead of overwriting or failing the checkout.
    """
    username = (username or "").strip().lstrip("@")
    if username:
        await upsert_user(user_id, username)

    order_doc_base = {
        "user_id": user_id,
        "username": username,
        "product_name": product_name,
        "quantity": quantity,
        "items": [],
        "payment_method": payment_method,
        "amount_inr": amount_inr,
        "amount_usdt": amount_usdt,
        "status": "pending",
        "created_at": datetime.now(timezone.utc),
        "delivered_at": None,
    }

    database = get_db()
    for attempt in range(25):
        # Keep the familiar 8-character IDs first; fall back to a longer ID only
        # after repeated collisions.
        id_len = 8 if attempt < 20 else 12
        order_id = uuid.uuid4().hex[:id_len].upper()
        if await database.orders.find_one({"order_id": order_id}, {"_id": 1}):
            continue
        try:
            await database.orders.insert_one({**order_doc_base, "order_id": order_id})
            return order_id
        except DuplicateKeyError:
            continue

    # Extremely unlikely fallback. Let the caller see a real failure instead of
    # silently creating a duplicate or charging without an order.
    raise RuntimeError("Could not generate a unique order ID after multiple attempts")


async def get_order(order_id: str) -> Optional[dict]:
    return await get_db().orders.find_one({"order_id": order_id})


async def claim_order_delivery(order_id: str, *, lock_seconds: int = 300) -> Optional[dict]:
    """Atomically claim an order before removing stock.

    Payment confirmation can be reached by more than one worker/button at the
    same time (background scanner, Check Payment, startup recovery). Without a
    per-order delivery lock, two workers can both try to finalize the same paid
    order; one may remove stock while the other marks the same order pending.
    This claim makes order delivery idempotent and prevents double stock pops.
    """
    clean_order_id = str(order_id or "").strip().upper()
    if not clean_order_id:
        return None
    now = datetime.now(timezone.utc)
    try:
        seconds = max(30, int(lock_seconds or 300))
    except Exception:
        seconds = 300
    cutoff = now - timedelta(seconds=seconds)
    token = uuid.uuid4().hex
    order = await get_db().orders.find_one_and_update(
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
    return order


async def clear_order_delivery_lock(order_id: str, delivery_token: str | None = None) -> bool:
    query = {"order_id": str(order_id or "").strip().upper()}
    if delivery_token:
        query["delivery_lock_token"] = delivery_token
    res = await get_db().orders.update_one(
        query,
        {"$unset": {"delivery_lock_token": "", "delivery_lock_at": ""}},
    )
    return bool(res.modified_count)


async def update_order_status(
    order_id: str,
    status: str,
    items: list[str] = None,
    *,
    delivery_token: str | None = None,
) -> bool:
    now = datetime.now(timezone.utc)
    clean_order_id = str(order_id or "").strip().upper()
    query: dict[str, Any] = {"order_id": clean_order_id}
    if delivery_token:
        query["delivery_lock_token"] = delivery_token
    if status == "pending_stock":
        # Never let a late duplicate worker overwrite an already-delivered order
        # or an order that another worker is actively finalizing.
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
        update["$set"]["delivered_at"] = now
    if status == "pending_stock":
        update["$set"].setdefault("pending_stock_at", now)
    if status in {"delivered", "pending_stock", "expired", "cancelled", "rejected"}:
        update["$unset"] = {"delivery_lock_token": "", "delivery_lock_at": ""}
    res = await get_db().orders.update_one(query, update)
    if res.matched_count and status == "delivered" and items:
        try:
            order = await get_db().orders.find_one({"order_id": clean_order_id}) or {}
            await record_order_items_delivered_in_ledger(order, items, source="telegram_bot")
        except Exception:
            pass
    return bool(res.matched_count)


async def record_order_delivery_message(
    order_id: str,
    chat_id: int | str,
    message_id: int | str,
    *,
    filename: str = "",
    sent_by: str = "bot",
    resent: bool = False,
):
    """Save Telegram message IDs for delivered stock files so WebAdmin can revoke/delete them later."""
    clean_order_id = str(order_id or "").strip().upper()
    if not clean_order_id:
        return
    try:
        clean_chat_id = int(chat_id)
        clean_message_id = int(message_id)
    except (TypeError, ValueError):
        return
    now = datetime.now(timezone.utc)
    entry = {
        "chat_id": clean_chat_id,
        "message_id": clean_message_id,
        "filename": str(filename or "").strip(),
        "sent_at": now,
        "sent_by": str(sent_by or "bot").strip()[:80],
        "resent": bool(resent),
    }
    await get_db().orders.update_one(
        {"order_id": clean_order_id},
        {
            "$set": {
                "delivery_chat_id": clean_chat_id,
                "delivery_message_id": clean_message_id,
                "delivery_message_sent_at": now,
                "delivery_filename": entry["filename"],
                "delivery_telegram_deleted": False,
            },
            "$push": {"delivery_telegram_messages": entry},
        },
    )


async def mark_order_pending_stock(order_id: str, *, delivery_token: str | None = None) -> bool:
    return await update_order_status(order_id, "pending_stock", delivery_token=delivery_token)


async def has_pending_stock_ahead(product_name: str, created_at, order_id: str | None = None) -> bool:
    """True when an older paid order for the same product is already waiting for stock."""
    query = {
        "product_name": _name_regex(product_name),
        "status": "pending_stock",
    }
    if created_at:
        query["created_at"] = {"$lt": created_at}
    if order_id:
        query["order_id"] = {"$ne": order_id}
    return await get_db().orders.count_documents(query, limit=1) > 0


async def get_pending_stock_orders(product_name: str, limit: int = 100) -> list[dict]:
    """Oldest paid orders waiting for this product's stock."""
    return await get_db().orders.find({
        "product_name": _name_regex(product_name),
        "status": "pending_stock",
    }).sort("created_at", 1).limit(limit).to_list(length=limit)


async def get_stuck_wallet_pending_orders(limit: int = 100) -> list[dict]:
    """Wallet-paid orders should never remain plain pending.

    This helper finds old wallet orders that were charged but not finalized,
    so startup recovery can deliver them or move them to pending_stock.
    """
    limit = max(1, min(int(limit or 100), 500))
    return await get_db().orders.find({
        "status": "pending",
        "payment_method": {"$in": ["wallet_inr", "wallet_usdt"]},
    }).sort("created_at", 1).limit(limit).to_list(length=limit)


async def get_user_orders(user_id: int, limit: int = 20, skip: int = 0) -> list[dict]:
    """Return the newest purchase orders belonging to one user.

    Replacement deliveries are shown separately in /replacements so normal
    order history does not get mixed with replacement history.
    """
    return await get_db().orders.find(
        {"user_id": user_id, "is_replacement": {"$ne": True}}
    ).sort("created_at", -1).skip(skip).limit(limit).to_list(length=limit)


async def count_user_orders(user_id: int) -> int:
    """Count purchase orders belonging to one user for pagination."""
    return await get_db().orders.count_documents({"user_id": user_id, "is_replacement": {"$ne": True}})


async def get_user_replacement_orders(user_id: int, limit: int = 20, skip: int = 0) -> list[dict]:
    """Return newest replacement deliveries belonging to one user."""
    return await get_db().orders.find(
        {"user_id": user_id, "is_replacement": True}
    ).sort("created_at", -1).skip(skip).limit(limit).to_list(length=limit)


async def count_user_replacement_orders(user_id: int) -> int:
    """Count replacement deliveries belonging to one user for pagination."""
    return await get_db().orders.count_documents({"user_id": user_id, "is_replacement": True})


async def get_recent_orders(limit: int = 20, skip: int = 0) -> list[dict]:
    """Return newest orders across all users for admin views, with user info joined in."""
    limit = max(1, min(int(limit or 20), 100))
    skip = max(0, int(skip or 0))
    pipeline = [
        {"$sort": {"created_at": -1}},
        {"$skip": skip},
        {"$limit": limit},
        {"$lookup": {
            "from": "users",
            "localField": "user_id",
            "foreignField": "user_id",
            "as": "user_doc",
        }},
        {"$unwind": {"path": "$user_doc", "preserveNullAndEmptyArrays": True}},
        {"$addFields": {
            "username": {
                "$ifNull": [
                    "$username",
                    {"$ifNull": ["$user_doc.username", ""]}
                ]
            },
            "user_blocked": {"$ifNull": ["$user_doc.blocked", False]},
        }},
    ]
    return await get_db().orders.aggregate(pipeline).to_list(length=limit)


async def count_all_orders() -> int:
    """Count all orders across all users for admin pagination."""
    return await get_db().orders.count_documents({})


async def get_all_pending_stock_orders(limit: int = 100) -> list[dict]:
    """All paid orders waiting for stock, oldest first."""
    return await get_db().orders.find({"status": "pending_stock"}).sort(
        "created_at", 1
    ).limit(limit).to_list(length=limit)


def _order_paid_amounts(order: dict) -> tuple[float, float]:
    """Return paid amount as (INR, USDT) using the actual payment currency only.

    Orders keep both INR and USDT product-price equivalents, but revenue must
    count only the currency used for payment.
    """
    method = str((order or {}).get("payment_method") or "").lower()
    amount_inr = float((order or {}).get("amount_inr") or 0)
    amount_usdt = float((order or {}).get("amount_usdt") or 0)

    if method in {"upi", "wallet_inr", "inr"} or "upi" in method or method.endswith("_inr"):
        return amount_inr, 0.0
    if (
        method in {"usdt", "polygon", "usdt_polygon", "wallet_usdt", "binance", "binance_pay", "binance_usdt", "bep20"}
        or "usdt" in method
        or "binance" in method
        or "bep20" in method
    ):
        return 0.0, amount_usdt

    if amount_usdt and not amount_inr:
        return 0.0, amount_usdt
    return amount_inr, 0.0


def _paid_inr_mongo_expr() -> dict:
    """Mongo expression: amount_inr only for INR-paid order methods."""
    method = {"$toLower": {"$ifNull": ["$payment_method", ""]}}
    return {"$cond": [
        {"$or": [
            {"$in": [method, ["upi", "wallet_inr", "inr"]]},
            {"$regexMatch": {"input": method, "regex": "upi"}},
            {"$regexMatch": {"input": method, "regex": "_inr$"}},
        ]},
        {"$ifNull": ["$amount_inr", 0]},
        0,
    ]}


def _paid_usdt_mongo_expr() -> dict:
    """Mongo expression: amount_usdt only for USDT/Binance/BEP20-paid order methods."""
    method = {"$toLower": {"$ifNull": ["$payment_method", ""]}}
    return {"$cond": [
        {"$or": [
            {"$in": [method, ["usdt", "polygon", "usdt_polygon", "wallet_usdt", "binance", "binance_pay", "binance_usdt", "bep20"]]},
            {"$regexMatch": {"input": method, "regex": "usdt"}},
            {"$regexMatch": {"input": method, "regex": "binance"}},
            {"$regexMatch": {"input": method, "regex": "bep20"}},
        ]},
        {"$ifNull": ["$amount_usdt", 0]},
        0,
    ]}


async def get_user_order_stats(user_id: int) -> dict:
    """Aggregated order stats for one user."""
    db = get_db()
    orders = await db.orders.find({"user_id": user_id}).to_list(length=None)
    total_orders = len(orders)
    delivered = sum(1 for o in orders if o.get("status") == "delivered")
    pending_stock = sum(1 for o in orders if o.get("status") == "pending_stock")
    pending = sum(1 for o in orders if o.get("status") == "pending")
    failed = sum(1 for o in orders if o.get("status") in {"failed", "expired"})
    paid_orders = [o for o in orders if o.get("status") in {"delivered", "pending_stock"}]
    paid_amounts = [_order_paid_amounts(o) for o in paid_orders]
    total_inr = sum(v[0] for v in paid_amounts)
    total_usdt = sum(v[1] for v in paid_amounts)
    return {
        "total_orders": total_orders,
        "delivered": delivered,
        "pending_stock": pending_stock,
        "pending": pending,
        "failed": failed,
        "total_inr": total_inr,
        "total_usdt": total_usdt,
    }


async def get_bot_stats() -> dict:
    """Aggregated stats for admin dashboard."""
    db = get_db()
    users_total = await db.users.count_documents({})
    users_blocked = await db.users.count_documents({"blocked": True})
    orders_total = await db.orders.count_documents({})
    orders_delivered = await db.orders.count_documents({"status": "delivered"})
    orders_pending_stock = await db.orders.count_documents({"status": "pending_stock"})
    orders_pending = await db.orders.count_documents({"status": "pending"})
    orders_failed = await db.orders.count_documents({"status": {"$in": ["failed", "expired"]}})
    products_total = await db.products.count_documents({})
    products_enabled = await db.products.count_documents({"$or": [{"enabled": True}, {"enabled": {"$exists": False}}]})

    revenue_rows = await db.orders.aggregate([
        {"$match": {"status": {"$in": ["delivered", "pending_stock"]}}},
        {"$group": {
            "_id": None,
            "inr": {"$sum": _paid_inr_mongo_expr()},
            "usdt": {"$sum": _paid_usdt_mongo_expr()},
        }},
    ]).to_list(length=1)
    revenue = revenue_rows[0] if revenue_rows else {"inr": 0, "usdt": 0}

    products = await db.products.find().to_list(length=None)
    total_stock = sum(len(p.get("stock", []) or []) for p in products)

    return {
        "users_total": users_total,
        "users_blocked": users_blocked,
        "orders_total": orders_total,
        "orders_delivered": orders_delivered,
        "orders_pending_stock": orders_pending_stock,
        "orders_pending": orders_pending,
        "orders_failed": orders_failed,
        "products_total": products_total,
        "products_enabled": products_enabled,
        "total_stock": total_stock,
        "revenue_inr": float(revenue.get("inr", 0) or 0),
        "revenue_usdt": float(revenue.get("usdt", 0) or 0),
    }


def _buyer_ranking_match() -> dict:
    """Only real paid buyer orders should affect Buyer Ranking.

    Free admin stock sends and replacement deliveries are delivered order records too,
    but they are not paid purchases, so they must not move buyer ranks.
    """
    return {
        "status": {"$in": ["delivered", "pending_stock"]},
        "is_replacement": {"$ne": True},
        "admin_stock_delivery": {"$ne": True},
        "payment_method": {"$nin": ["admin_stock", "replacement"]},
        "$or": [{"amount_inr": {"$gt": 0}}, {"amount_usdt": {"$gt": 0}}],
    }


async def get_buyer_ranking(limit: int = 10, skip: int = 0) -> list[dict]:
    """Return top buyers by paid order value with user info.

    Counts delivered paid orders and paid orders waiting for stock. Pending/unpaid,
    failed, expired, replacements, and free admin stock sends are excluded.
    """
    limit = max(1, min(int(limit or 10), 50))
    skip = max(0, int(skip or 0))
    pipeline = [
        {"$match": _buyer_ranking_match()},
        {"$group": {
            "_id": "$user_id",
            "total_orders": {"$sum": 1},
            "delivered_orders": {"$sum": {"$cond": [{"$eq": ["$status", "delivered"]}, 1, 0]}},
            "pending_stock_orders": {"$sum": {"$cond": [{"$eq": ["$status", "pending_stock"]}, 1, 0]}},
            "total_inr": {"$sum": _paid_inr_mongo_expr()},
            "total_usdt": {"$sum": _paid_usdt_mongo_expr()},
            "last_order_at": {"$max": "$created_at"},
        }},
        {"$lookup": {
            "from": "users",
            "localField": "_id",
            "foreignField": "user_id",
            "as": "user_doc",
        }},
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
        {"$sort": {"total_usdt": -1, "total_inr": -1, "total_orders": -1, "user_id": 1}},
        {"$skip": skip},
        {"$limit": limit},
    ]
    return await get_db().orders.aggregate(pipeline).to_list(length=limit)


async def count_ranked_buyers() -> int:
    """Count users with at least one real paid buyer order."""
    rows = await get_db().orders.aggregate([
        {"$match": _buyer_ranking_match()},
        {"$group": {"_id": "$user_id"}},
        {"$count": "count"},
    ]).to_list(length=1)
    return int(rows[0].get("count", 0)) if rows else 0




# ─────────────────────── WALLET HISTORY ──────────────────────

async def get_user_wallet_logs(user_id: int, limit: int = 10, skip: int = 0) -> list[dict]:
    """Return wallet top-up/payment logs for one user, newest first.

    These are stored in pending_payments with pay_type='wallet'. They include
    BEP20, UPI and Binance Pay wallet top-ups across waiting, submitted,
    completed, expired and rejected states.
    """
    limit = max(1, min(int(limit or 10), 50))
    skip = max(0, int(skip or 0))
    return await get_db().pending_payments.find({
        "user_id": user_id,
        "pay_type": "wallet",
    }).sort("created_at", -1).skip(skip).limit(limit).to_list(length=limit)


async def count_user_wallet_logs(user_id: int) -> int:
    """Count wallet top-up logs for one user."""
    return await get_db().pending_payments.count_documents({
        "user_id": user_id,
        "pay_type": "wallet",
    })



# ───────────────────── PAYMENT METHOD SETTINGS ─────────────────

PAYMENT_SETTINGS_KEY = "payment_settings"

DEFAULT_PAYMENT_SETTINGS = {
    "usdt_bep20": {
        "enabled": False,
        "wallet_address": "",
    },
    "usdt_polygon": {
        "enabled": False,
        "wallet_address": "",
    },
    "upi": {
        "enabled": False,
        "upi_id": "",
        "upi_name": "",
    },
    "binance": {
        "enabled": False,
        "binance_pay_id": "",
        "binance_pay_name": "",
    },
    "wallet_limits": {
        "min_inr": "50",
        "min_usdt": "1",
    },
}


def _clean_payment_settings(settings: dict | None) -> dict:
    """Return a normalized payment-settings document.

    Payment details must come from MongoDB/WebAdmin only. Missing values are
    treated as disabled, never filled from .env.
    """
    settings = settings or {}
    cleaned = {
        method: dict(values)
        for method, values in DEFAULT_PAYMENT_SETTINGS.items()
    }

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

    # Do not allow a method to be enabled without the details required for users.
    if not cleaned["usdt_bep20"].get("wallet_address"):
        cleaned["usdt_bep20"]["enabled"] = False
    if not cleaned["usdt_polygon"].get("wallet_address"):
        cleaned["usdt_polygon"]["enabled"] = False
    if not cleaned["upi"].get("upi_id"):
        cleaned["upi"]["enabled"] = False
    if not cleaned["binance"].get("binance_pay_id"):
        cleaned["binance"]["enabled"] = False

    return cleaned


async def get_payment_settings() -> dict:
    doc = await get_db().settings.find_one({"key": PAYMENT_SETTINGS_KEY})
    value = doc.get("value") if doc else None
    return _clean_payment_settings(value if isinstance(value, dict) else None)


async def set_payment_settings(settings: dict) -> dict:
    cleaned = _clean_payment_settings(settings)
    await get_db().settings.update_one(
        {"key": PAYMENT_SETTINGS_KEY},
        {"$set": {"key": PAYMENT_SETTINGS_KEY, "value": cleaned, "updated_at": datetime.now(timezone.utc)}},
        upsert=True,
    )
    return cleaned


def payment_method_enabled(settings: dict, method: str) -> bool:
    method = (method or "").lower()
    settings = _clean_payment_settings(settings)
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


async def get_enabled_payment_methods() -> dict:
    settings = await get_payment_settings()
    return {
        "usdt": payment_method_enabled(settings, "usdt"),
        "polygon": payment_method_enabled(settings, "polygon"),
        "upi": payment_method_enabled(settings, "upi"),
        "binance": payment_method_enabled(settings, "binance"),
        "wallet_inr": payment_method_enabled(settings, "wallet_inr"),
        "wallet_usdt": payment_method_enabled(settings, "wallet_usdt"),
    }

# ─────────────────────────── SETTINGS ───────────────────────

async def set_setting(key: str, value):
    await get_db().settings.update_one(
        {"key": key},
        {"$set": {"key": key, "value": value, "updated_at": datetime.now(timezone.utc)}},
        upsert=True,
    )


async def get_setting(key: str, default=None):
    doc = await get_db().settings.find_one({"key": key})
    return doc.get("value", default) if doc else default


async def get_secret_settings() -> dict:
    value = await get_setting("secret_settings", {})
    settings = dict(value) if isinstance(value, dict) else {}
    try:
        token_doc = await get_db().runtime_config.find_one({"key": "telegram_bot_token"})
        runtime_token = str((token_doc or {}).get("value") or "").strip()
        if runtime_token:
            settings["bot_token"] = runtime_token
            settings["bot_token_runtime_updated_at"] = (token_doc or {}).get("updated_at")
    except Exception:
        pass
    return settings


def parse_positive_int(value, default: int, *, minimum: int = 1) -> int:
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError):
        parsed = int(default)
    return max(int(minimum), parsed)


def parse_positive_float(value, default: float, *, minimum: float = 0.000001) -> float:
    try:
        parsed = float(str(value).strip())
    except (TypeError, ValueError):
        parsed = float(default)
    return max(float(minimum), parsed)


async def set_maintenance_mode(enabled: bool):
    await set_setting("maintenance_mode", bool(enabled))


async def is_maintenance_mode() -> bool:
    return bool(await get_setting("maintenance_mode", False))

# ─────────────────────── MAINTENANCE NOTIFICATION QUEUE ────────────────────

async def queue_maintenance_notification(kind: str, product_name: str, payload: dict | None = None):
    """Queue product/stock/price notifications created while maintenance is ON.

    Broadcasts are intentionally not queued. Queued product notifications are
    flushed when maintenance mode is turned OFF. One queued row is kept per
    notification kind + product, so repeated edits during maintenance collapse
    into the latest notification instead of spamming users later.
    """
    kind = str(kind or "").strip()
    product_name = str(product_name or "").strip()
    if not kind or not product_name:
        return
    now = datetime.now(timezone.utc)
    await get_db().maintenance_notifications.update_one(
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


async def get_maintenance_notifications(limit: int = 100) -> list[dict]:
    cursor = get_db().maintenance_notifications.find({}).sort("created_at", 1).limit(max(1, int(limit or 100)))
    return await cursor.to_list(length=max(1, int(limit or 100)))


async def delete_maintenance_notification(notification_id):
    await get_db().maintenance_notifications.delete_one({"_id": notification_id})


async def count_maintenance_notifications() -> int:
    return await get_db().maintenance_notifications.count_documents({})


# ─────────────────────── PENDING PAYMENTS ────────────────────


def _normalize_new_unique_usdt(value: float) -> float:
    """Store new generated payment amounts at 3 decimals while keeping display simple."""
    try:
        amount = Decimal(str(value or 0))
    except (InvalidOperation, TypeError, ValueError):
        return 0.0
    if amount <= 0:
        return 0.0
    return float(amount.quantize(Decimal("0.001"), rounding=ROUND_HALF_UP))

async def create_pending_payment(
    user_id: int,
    ref_id: str,          # order_id or "wallet_<uuid>"
    pay_type: str,        # "order" or "wallet"
    method: str,          # "usdt", "upi", or "binance"
    expected_inr: float,
    expected_usdt: float,
    unique_usdt: float,   # The unique decimal amount for USDT matching
    currency: str = "",   # For wallet top-ups: "inr" or "usdt"
    load_amount: float = 0.0,
) -> dict:
    unique_usdt = _normalize_new_unique_usdt(unique_usdt)
    doc = {
        "user_id": user_id,
        "ref_id": ref_id,
        "pay_type": pay_type,
        "method": method,
        "expected_inr": expected_inr,
        "expected_usdt": expected_usdt,
        "unique_usdt": unique_usdt,
        "currency": currency,
        "load_amount": load_amount,
        "status": "waiting",
        "upi_payee_name": None,
        "upi_txn_id": None,
        "upi_screenshot_file_id": None,
        "binance_name": None,
        "binance_screenshot_file_id": None,
        "usdt_txn_hash": None,
        "usdt_screenshot_file_id": None,
        # Stores the payment instruction message so it can be deleted even
        # after a bot restart or when delivery is retried later.
        "payment_chat_id": None,
        "payment_msg_id": None,
        # For wallet top-ups: prevents double credit if user presses Check Payment
        # multiple times or if auto-polling and manual checking overlap.
        "wallet_credited_at": None,
        "reminder_sent_at": None,
        "expired_at": None,
        "created_at": time.time(),
        "language": await get_user_language(user_id),
    }
    await get_db().pending_payments.insert_one(doc)
    return doc


async def get_pending_payment(user_id: int) -> Optional[dict]:
    return await get_db().pending_payments.find_one(
        {"user_id": user_id, "status": "waiting"}
    )


async def get_pending_by_ref(ref_id: str) -> Optional[dict]:
    return await get_db().pending_payments.find_one({"ref_id": ref_id})


async def update_pending_status(ref_id: str, status: str, reviewed: bool = False, reviewed_by: int | None = None):
    update = {"status": status}
    if reviewed:
        update["reviewed_at"] = datetime.now(timezone.utc)
        if reviewed_by is not None:
            update["reviewed_by"] = reviewed_by
    await get_db().pending_payments.update_one(
        {"ref_id": ref_id}, {"$set": update}
    )


async def confirm_pending_payment_if_waiting(ref_id: str) -> Optional[dict]:
    """Atomically mark a waiting payment as confirmed.

    This prevents duplicate processing when the background BEP20 auto-check and
    the user's Check Payment button detect the same transfer at the same time.
    """
    return await get_db().pending_payments.find_one_and_update(
        {"ref_id": ref_id, "status": "waiting"},
        {"$set": {"status": "confirmed", "confirmed_at": datetime.now(timezone.utc)}},
        return_document=ReturnDocument.AFTER,
    )


def normalize_usdt_tx_hash(txn_hash: str | None) -> str:
    """Normalize EVM USDT tx hashes for duplicate checks.

    Users often paste a full explorer URL. Extract the first 0x-prefixed
    32-byte transaction hash when present and store it lowercase.
    """
    raw = str(txn_hash or "").strip()
    match = re.search(r"0x[a-fA-F0-9]{64}", raw)
    return match.group(0).lower() if match else raw.lower()


def is_valid_usdt_tx_hash(txn_hash: str | None) -> bool:
    return bool(re.fullmatch(r"0x[a-f0-9]{64}", normalize_usdt_tx_hash(txn_hash)))


def normalize_usdt_network_key(network: str | None = None) -> str:
    value = str(network or "").strip().lower()
    return "polygon" if value in {"polygon", "matic", "polygon_pos", "usdt_polygon", "polygon_usdt"} else "bep20"


def make_usdt_tx_hash_key(network: str | None, txn_hash: str | None) -> str:
    normalized_hash = normalize_usdt_tx_hash(txn_hash)
    if not normalized_hash:
        return ""
    return f"{normalize_usdt_network_key(network)}:{normalized_hash}"


def _usdt_tx_hash_from_transaction(transaction: dict | None) -> str:
    tx = transaction or {}
    return normalize_usdt_tx_hash(tx.get("hash") or tx.get("txhash") or tx.get("transactionHash") or "")


def _usdt_tx_hash_exact_query(txn_hash: str) -> dict:
    normalized = normalize_usdt_tx_hash(txn_hash)
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


async def find_used_usdt_tx_hash(txn_hash: str | None, exclude_ref_id: str | None = None) -> Optional[dict]:
    """Return a payment row already using this BEP20 tx hash/ID.

    Checks both auto-verification hashes (usdt_transaction_hash) and manual
    proof hashes (usdt_txn_hash), so one transaction cannot be reused between
    auto and manual flows.
    """
    normalized = normalize_usdt_tx_hash(txn_hash)
    if not normalized:
        return None
    query = _usdt_tx_hash_exact_query(normalized)
    if exclude_ref_id:
        query = {"$and": [query, {"ref_id": {"$ne": exclude_ref_id}}]}
    return await get_db().pending_payments.find_one(query)


async def confirm_pending_usdt_payment_if_waiting(ref_id: str, transaction: dict | None = None) -> Optional[dict]:
    """Atomically mark a waiting BEP20 payment as confirmed.

    When a transaction hash is available, store it and refuse to reuse the same
    hash for another order/top-up. This is important now that tiny USDT rounding
    differences are tolerated.
    """
    tx_hash = _usdt_tx_hash_from_transaction(transaction)
    database = get_db()
    update = {
        "status": "confirmed",
        "confirmed_at": datetime.now(timezone.utc),
    }

    if tx_hash:
        duplicate = await find_used_usdt_tx_hash(tx_hash, exclude_ref_id=ref_id)
        if duplicate:
            return None
        update.update({
            "usdt_auto_verified": True,
            "usdt_transaction_hash": tx_hash,
            "usdt_txn_hash_key": make_usdt_tx_hash_key((transaction or {}).get("network"), tx_hash),
            "usdt_network": normalize_usdt_network_key((transaction or {}).get("network")),
            "usdt_transaction_amount": str((transaction or {}).get("match_actual_usdt") or (transaction or {}).get("value_usdt") or ""),
            "usdt_expected_amount": str((transaction or {}).get("match_expected_usdt") or ""),
            "usdt_amount_difference": str((transaction or {}).get("match_difference_usdt") or ""),
            "usdt_match_type": str((transaction or {}).get("match_type") or ""),
            "usdt_transaction_source": str((transaction or {}).get("source") or ""),
        })

    try:
        return await database.pending_payments.find_one_and_update(
            {"ref_id": ref_id, "status": "waiting"},
            {"$set": update},
            return_document=ReturnDocument.AFTER,
        )
    except DuplicateKeyError:
        # A concurrent verifier already used the same BEP20 transaction hash.
        return None


async def confirm_expired_usdt_payment(ref_id: str, transaction: dict | None = None) -> Optional[dict]:
    """Mark an expired BEP20 row as confirmed when a late transaction is found."""
    tx_hash = _usdt_tx_hash_from_transaction(transaction)
    database = get_db()
    update = {
        "status": "confirmed",
        "confirmed_at": datetime.now(timezone.utc),
    }
    if tx_hash:
        duplicate = await find_used_usdt_tx_hash(tx_hash, exclude_ref_id=ref_id)
        if duplicate:
            return None
        update.update({
            "usdt_auto_verified": True,
            "usdt_transaction_hash": tx_hash,
            "usdt_txn_hash_key": make_usdt_tx_hash_key((transaction or {}).get("network"), tx_hash),
            "usdt_network": normalize_usdt_network_key((transaction or {}).get("network")),
            "usdt_transaction_amount": str((transaction or {}).get("match_actual_usdt") or (transaction or {}).get("value_usdt") or ""),
            "usdt_expected_amount": str((transaction or {}).get("match_expected_usdt") or ""),
            "usdt_amount_difference": str((transaction or {}).get("match_difference_usdt") or ""),
            "usdt_match_type": str((transaction or {}).get("match_type") or ""),
            "usdt_transaction_source": str((transaction or {}).get("source") or ""),
        })
    try:
        return await database.pending_payments.find_one_and_update(
            {"ref_id": ref_id, "status": "expired"},
            {"$set": update},
            return_document=ReturnDocument.AFTER,
        )
    except DuplicateKeyError:
        return None


async def confirm_manual_usdt_payment_if_waiting(ref_id: str, transaction: dict | None = None) -> Optional[dict]:
    """Atomically confirm a waiting payment from a manually submitted tx hash.

    Used only after the submitted transaction hash has been verified on-chain
    against the correct network, USDT contract, receiver wallet, confirmations,
    payment creation time, and manual amount tolerance.
    """
    tx_hash = _usdt_tx_hash_from_transaction(transaction)
    if not tx_hash:
        return None

    duplicate = await find_used_usdt_tx_hash(tx_hash, exclude_ref_id=ref_id)
    if duplicate:
        return None

    network = normalize_usdt_network_key((transaction or {}).get("network"))
    update = {
        "status": "confirmed",
        "confirmed_at": datetime.now(timezone.utc),
        "usdt_auto_verified": True,
        "usdt_manual_hash_auto_verified": True,
        "usdt_txn_hash": tx_hash,
        "usdt_transaction_hash": tx_hash,
        "usdt_txn_hash_key": make_usdt_tx_hash_key(network, tx_hash),
        "usdt_network": network,
        "usdt_transaction_amount": str((transaction or {}).get("match_actual_usdt") or (transaction or {}).get("value_usdt") or ""),
        "usdt_expected_amount": str((transaction or {}).get("match_expected_usdt") or ""),
        "usdt_amount_difference": str((transaction or {}).get("match_difference_usdt") or ""),
        "usdt_match_type": str((transaction or {}).get("match_type") or ""),
        "usdt_transaction_source": str((transaction or {}).get("source") or ""),
        "usdt_manual_auto_check_result": "passed",
        "usdt_manual_auto_check_reason": "",
        "usdt_manual_auto_checked_at": datetime.now(timezone.utc),
    }

    try:
        return await get_db().pending_payments.find_one_and_update(
            {"ref_id": ref_id, "status": "waiting"},
            {"$set": update},
            return_document=ReturnDocument.AFTER,
        )
    except DuplicateKeyError:
        return None




async def record_usdt_manual_auto_check_result(
    ref_id: str,
    *,
    result: str,
    reason: str = "",
    txn_hash: str | None = None,
    network: str | None = None,
    extra: dict | None = None,
) -> bool:
    """Store why a submitted USDT TxHash was or was not auto-verified.

    The row can still continue to manual screenshot/admin review afterwards.
    Keeping this reason in the payment record makes WebAdmin reviews safer and
    keeps the WebAdmin Tx Hash Logs and Payment Reviews clear.
    """
    clean_result = str(result or "unknown").strip().lower()[:40] or "unknown"
    clean_reason = str(reason or "").strip()[:1200]
    normalized_hash = normalize_usdt_tx_hash(txn_hash)
    update = {
        "usdt_manual_auto_check_result": clean_result,
        "usdt_manual_auto_check_reason": clean_reason,
        "usdt_manual_auto_checked_at": datetime.now(timezone.utc),
    }
    if normalized_hash:
        update["usdt_txn_hash"] = normalized_hash
        update["usdt_txn_hash_key"] = make_usdt_tx_hash_key(network, normalized_hash)
    if network:
        update["usdt_network"] = normalize_usdt_network_key(network)
    if isinstance(extra, dict):
        for key, value in extra.items():
            if not key:
                continue
            clean_key = str(key)[:80]
            clean_value = str(value or "")[:500]
            if clean_key == "received_usdt" and clean_value:
                update["usdt_transaction_amount"] = clean_value
            elif clean_key == "expected_usdt" and clean_value:
                update["usdt_expected_amount"] = clean_value
            update[f"usdt_manual_auto_check_{clean_key}"] = clean_value
    result_obj = await get_db().pending_payments.update_one(
        {"ref_id": ref_id},
        {"$set": update},
    )
    return bool(result_obj.matched_count)


async def confirm_pending_binance_payment_if_waiting(ref_id: str, transaction: dict) -> Optional[dict]:
    """Atomically mark a waiting Binance Pay payment as confirmed.

    The Binance transaction id is stored so the same Pay history row cannot be
    reused for another order/top-up. A unique sparse index is created at bot
    startup; this helper also performs a defensive duplicate check for older DBs.
    """
    tx_id = str((transaction or {}).get("transactionId") or (transaction or {}).get("tranId") or "").strip()
    if not tx_id:
        return None

    database = get_db()
    duplicate = await database.pending_payments.find_one({
        "binance_transaction_id": tx_id,
        "ref_id": {"$ne": ref_id},
    })
    if duplicate:
        return None

    update = {
        "status": "confirmed",
        "confirmed_at": datetime.now(timezone.utc),
        "binance_auto_verified": True,
        "binance_transaction_id": tx_id,
        "binance_transaction_time": transaction.get("transactionTime"),
        "binance_transaction_amount": str(transaction.get("amount") or ""),
        "binance_transaction_currency": str(transaction.get("currency") or ""),
    }
    payer = transaction.get("payerInfo")
    if isinstance(payer, dict):
        update["binance_payer_name"] = str(payer.get("name") or "").strip()
        update["binance_payer_id"] = str(payer.get("binanceId") or "").strip()

    try:
        return await database.pending_payments.find_one_and_update(
            {"ref_id": ref_id, "method": "binance", "status": "waiting"},
            {"$set": update},
            return_document=ReturnDocument.AFTER,
        )
    except Exception:
        # Most commonly a duplicate-key error if two workers race on the same tx.
        return None


async def get_confirmed_usdt_payments_needing_completion() -> list[dict]:
    """Return BEP20 payments that were marked confirmed but not completed.

    Older versions could mark/detect a BEP20 payment and then fail before
    delivery/credit. This lets the bot recover automatically after restart.
    """
    return await get_db().pending_payments.find({
        "method": {"$in": ["usdt", "polygon"]},
        "status": {"$in": ["confirmed", "approved"]},
    }).to_list(length=None)


async def get_confirmed_binance_payments_needing_completion() -> list[dict]:
    """Return auto-confirmed Binance Pay rows that may still need delivery/credit."""
    return await get_db().pending_payments.find({
        "method": "binance",
        "status": "confirmed",
        "binance_auto_verified": True,
    }).to_list(length=None)


async def mark_wallet_load_credited(ref_id: str) -> Optional[dict]:
    """Atomically mark a wallet top-up as credited.

    Returns the payment row only the first time it is credited. Subsequent calls
    return None, which prevents duplicate wallet balance updates.
    """
    return await get_db().pending_payments.find_one_and_update(
        {
            "ref_id": ref_id,
            "pay_type": "wallet",
            "$or": [
                {"wallet_credited_at": None},
                {"wallet_credited_at": {"$exists": False}},
            ],
        },
        {
            "$set": {
                "wallet_credited_at": datetime.now(timezone.utc),
                "status": "completed",
            }
        },
        return_document=ReturnDocument.AFTER,
    )


async def set_pending_payment_config(ref_id: str, payment_details: dict):
    """Store the exact payment details shown to the user for this session.

    This prevents a later admin settings change from making an active payment
    verify against a different wallet/payee than the user was shown.
    """
    clean = {}
    for key, value in (payment_details or {}).items():
        clean[str(key)] = str(value or "").strip()
    if clean:
        await get_db().pending_payments.update_one(
            {"ref_id": ref_id},
            {"$set": {"payment_details": clean}},
        )


async def set_pending_payment_message(ref_id: str, chat_id: int, msg_id: int):
    """Persist the Telegram payment instruction message for later cleanup."""
    await get_db().pending_payments.update_one(
        {"ref_id": ref_id},
        {"$set": {"payment_chat_id": chat_id, "payment_msg_id": msg_id}},
    )


async def set_pending_payment_message_meta(ref_id: str, *, kind: str, template: str):
    """Store a payment message template so the countdown can be edited in-place."""
    await get_db().pending_payments.update_one(
        {"ref_id": ref_id},
        {"$set": {"payment_message_kind": kind, "payment_message_template": template}},
    )


async def clear_pending_payment_message(ref_id: str):
    await get_db().pending_payments.update_one(
        {"ref_id": ref_id},
        {"$unset": {"payment_chat_id": "", "payment_msg_id": ""}},
    )


async def set_usdt_manual_details(ref_id: str, txn_hash: str, screenshot_file_id: str | None = None, network: str | None = None) -> bool:
    normalized_hash = normalize_usdt_tx_hash(txn_hash)
    if not normalized_hash:
        return False
    duplicate = await find_used_usdt_tx_hash(normalized_hash, exclude_ref_id=ref_id)
    if duplicate:
        return False
    update = {
        "usdt_txn_hash": normalized_hash,
        "usdt_txn_hash_key": make_usdt_tx_hash_key(network, normalized_hash),
        "usdt_network": normalize_usdt_network_key(network),
        "status": "usdt_manual_submitted",
    }
    if screenshot_file_id:
        update["usdt_screenshot_file_id"] = screenshot_file_id
    try:
        result = await get_db().pending_payments.update_one(
            {"ref_id": ref_id, "status": "waiting"},
            {"$set": update},
        )
    except DuplicateKeyError:
        return False
    return bool(result.modified_count or result.matched_count)


def created_at_to_timestamp(value) -> float | None:
    return _created_at_ts(value)


async def set_upi_details(ref_id: str, payee_name: str, txn_id: str, screenshot_file_id: str | None = None):
    update = {
        "upi_payee_name": payee_name,
        "upi_txn_id": txn_id,
        "status": "upi_submitted",
    }
    if screenshot_file_id:
        update["upi_screenshot_file_id"] = screenshot_file_id
    await get_db().pending_payments.update_one(
        {"ref_id": ref_id},
        {"$set": update},
    )


async def set_binance_details(ref_id: str, binance_name: str, screenshot_file_id: str):
    await get_db().pending_payments.update_one(
        {"ref_id": ref_id},
        {"$set": {
            "binance_name": binance_name,
            "binance_screenshot_file_id": screenshot_file_id,
            "status": "binance_submitted",
        }},
    )


async def mark_payment_reminder_sent(ref_id: str) -> Optional[dict]:
    """Mark reminder sent only once for an active waiting payment."""
    return await get_db().pending_payments.find_one_and_update(
        {
            "ref_id": ref_id,
            "status": "waiting",
            "$or": [
                {"reminder_sent_at": None},
                {"reminder_sent_at": {"$exists": False}},
            ],
        },
        {"$set": {"reminder_sent_at": datetime.now(timezone.utc)}},
        return_document=ReturnDocument.AFTER,
    )


async def expire_pending_payment_if_waiting(ref_id: str) -> Optional[dict]:
    """Expire a payment only if it is still waiting.

    Returns the expired row only for the process that actually changed it. This
    prevents duplicate expiry notifications when multiple workers/checks race.
    """
    return await get_db().pending_payments.find_one_and_update(
        {"ref_id": ref_id, "status": "waiting"},
        {"$set": {"status": "expired", "expired_at": datetime.now(timezone.utc)}},
        return_document=ReturnDocument.AFTER,
    )


def _created_at_ts(value) -> float | None:
    """Return a UTC timestamp for legacy/new created_at values.

    Orders use timezone-aware datetimes, pending_payments may use time.time(),
    and older local test rows can contain strings like ``2026-05-17 08:24 UTC``.
    Cleanup must support all of them, otherwise unpaid orders can stay Pending.
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


async def expire_stale_unpaid_payments_and_orders(timeout_minutes: int = 30) -> dict:
    """Expire old unpaid payment sessions and their plain-pending orders.

    Manual review submissions are intentionally not expired here: once a user has
    submitted proof, the order remains pending until an admin approves/rejects it.
    This cleanup supports numeric, datetime, and ISO-string created_at fields.
    """
    timeout_seconds = max(60, int(timeout_minutes or 30) * 60)
    cutoff_ts = time.time() - timeout_seconds
    now_dt = datetime.now(timezone.utc)
    database = get_db()
    result = {"payments_expired": 0, "orders_expired": 0, "orphan_orders_expired": 0, "failed_orders_marked": 0}

    # Expire waiting payments by parsing created_at in Python so legacy datetime
    # or string values are handled too, not only numeric time.time() values.
    waiting_payments = await database.pending_payments.find(
        {"status": "waiting"},
        {"ref_id": 1, "pay_type": 1, "created_at": 1},
    ).to_list(length=None)
    stale_payments = [p for p in waiting_payments if (_created_at_ts(p.get("created_at")) or time.time()) < cutoff_ts]
    stale_refs = [str(p.get("ref_id") or "") for p in stale_payments if p.get("ref_id")]
    if stale_refs:
        payment_update = await database.pending_payments.update_many(
            {"ref_id": {"$in": stale_refs}, "status": "waiting"},
            {"$set": {"status": "expired", "expired_at": now_dt}},
        )
        result["payments_expired"] += int(payment_update.modified_count or 0)

        order_refs = [str(p.get("ref_id") or "") for p in stale_payments if p.get("pay_type") == "order" and p.get("ref_id")]
        if order_refs:
            order_update = await database.orders.update_many(
                {"order_id": {"$in": order_refs}, "status": "pending"},
                {"$set": {"status": "expired", "expired_at": now_dt}},
            )
            result["orders_expired"] += int(order_update.modified_count or 0)

    review_or_paid_statuses = {
        "upi_submitted", "binance_submitted", "usdt_manual_submitted",
        "approved", "confirmed", "completed",
    }
    pending_orders = await database.orders.find(
        {"status": "pending"},
        {"order_id": 1, "created_at": 1},
    ).to_list(length=None)
    for order in pending_orders:
        ref_id = str(order.get("order_id") or "")
        if not ref_id:
            continue
        order_ts = _created_at_ts(order.get("created_at"))
        if order_ts is None or order_ts >= cutoff_ts:
            continue
        pending = await database.pending_payments.find_one({"ref_id": ref_id})
        if pending and pending.get("status") in review_or_paid_statuses:
            continue
        if pending and pending.get("status") == "rejected":
            update = await database.orders.update_one(
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
            await database.pending_payments.update_one(
                {"ref_id": ref_id, "status": "waiting"},
                {"$set": {"status": "expired", "expired_at": now_dt}},
            )
        update = await database.orders.update_one(
            {"order_id": ref_id, "status": "pending"},
            {"$set": {"status": "expired", "expired_at": now_dt}},
        )
        changed = int(update.modified_count or 0)
        result["orphan_orders_expired"] += changed
        result["orders_expired"] += changed

    return result


async def get_all_waiting_payments() -> list[dict]:
    return await get_db().pending_payments.find({"status": "waiting"}).to_list(length=None)


async def get_all_pending_usdt() -> list[dict]:
    return await get_db().pending_payments.find(
        {"method": {"$in": ["usdt", "polygon"]}, "status": "waiting"}
    ).to_list(length=None)


async def get_all_pending_binance() -> list[dict]:
    return await get_db().pending_payments.find(
        {"method": "binance", "status": "waiting"}
    ).to_list(length=None)


async def get_all_pending_unique_usdt_payments() -> list[dict]:
    """Rows whose unique_usdt amount should not collide across auto-verifiers."""
    return await get_db().pending_payments.find({
        "method": {"$in": ["usdt", "polygon", "binance"]},
        "status": "waiting",
        "unique_usdt": {"$gt": 0},
    }).to_list(length=None)


async def get_used_binance_transaction_ids() -> set[str]:
    rows = await get_db().pending_payments.find(
        {"binance_transaction_id": {"$exists": True, "$ne": ""}},
        {"binance_transaction_id": 1},
    ).to_list(length=None)
    return {str(row.get("binance_transaction_id") or "").strip() for row in rows if row.get("binance_transaction_id")}


async def get_used_usdt_transaction_hashes() -> set[str]:
    rows = await get_db().pending_payments.find(
        {
            "$or": [
                {"usdt_transaction_hash": {"$exists": True, "$ne": ""}},
                {"usdt_txn_hash": {"$exists": True, "$ne": ""}},
            ]
        },
        {"usdt_transaction_hash": 1, "usdt_txn_hash": 1},
    ).to_list(length=None)
    hashes: set[str] = set()
    for row in rows:
        for key in ("usdt_transaction_hash", "usdt_txn_hash"):
            value = normalize_usdt_tx_hash(row.get(key))
            if value:
                hashes.add(value)
    return hashes


async def clear_pending(user_id: int):
    await get_db().pending_payments.delete_many({"user_id": user_id, "status": "waiting"})


# ───────────────────── REPLACEMENT REPORTS ─────────────────────

_REPORT_EMAIL_RE = re.compile(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", re.IGNORECASE)
_REPORT_ORDER_ID_RE = re.compile(r"\border\s*id\s*[:#\-]?\s*([A-Z0-9]{6,16})\b", re.IGNORECASE)


def stock_item_hash(item: str) -> str:
    return hashlib.sha256(str(item or "").strip().encode("utf-8")).hexdigest()


def _normalize_report_match_text(value: str) -> str:
    text = str(value or "").strip().lower()
    text = re.sub(r"\s+", " ", text)
    return text


def _emails_in_text(value: str) -> set[str]:
    return {m.group(0).lower() for m in _REPORT_EMAIL_RE.finditer(str(value or ""))}


def _order_ids_in_report(value: str) -> set[str]:
    return {m.group(1).upper() for m in _REPORT_ORDER_ID_RE.finditer(str(value or ""))}


def _report_has_items_section(value: str) -> bool:
    return any(re.match(r"^items\s*:?\s*$", str(line or "").strip(), flags=re.IGNORECASE) for line in str(value or "").splitlines())


def _clean_report_candidate_line(line: str) -> str:
    raw = str(line or "").strip()
    raw = re.sub(r"^\s*[•*\-]+\s*", "", raw)
    raw = re.sub(r"^\s*\d+[.)]\s*", "", raw)
    raw = raw.strip(" `\t\r\n")
    if not raw:
        return ""

    lowered = raw.lower().strip()
    if lowered in {"order items", "items", "item", "stock items", "accounts", "account", "codes", "code"}:
        return ""
    if re.match(r"^(order\s*id|product|quantity|qty)\s*[:#\-]?", lowered, flags=re.IGNORECASE):
        return ""
    if re.match(r"^(mail|email|pass|password|mail\s*pass|username|login)\s*[:#\-]?", lowered, flags=re.IGNORECASE):
        # Mail-format reports are handled by exact email extraction. Password-only
        # lines must never be treated as simple-code stock identifiers.
        return ""

    labeled = re.match(r"^(?:code|item|account|account\s*id|stock)\s*[:#\-]\s*(.+)$", raw, flags=re.IGNORECASE)
    if labeled:
        raw = labeled.group(1).strip()

    return raw.strip(" `\t\r\n")


def _report_candidate_values(value: str) -> list[str]:
    """Return exact report identifiers submitted by the user.

    For mail-format stock this returns exact emails. For simple-code stock it
    returns exact item/code lines. When the user forwards an order file, only
    lines under the ``Items:`` section are used so headers like Order ID,
    Product and Quantity cannot create extra false matches.
    """
    raw = str(value or "")
    emails = sorted(_emails_in_text(raw), key=lambda x: raw.lower().find(x))
    if emails:
        seen: set[str] = set()
        result: list[str] = []
        for email in emails:
            if email not in seen:
                seen.add(email)
                result.append(email)
        return result

    lines = raw.splitlines()
    item_section_lines: list[str] = []
    in_items = False
    for line in lines:
        stripped = str(line or "").strip()
        if re.match(r"^items\s*:?\s*$", stripped, flags=re.IGNORECASE):
            in_items = True
            continue
        if in_items:
            item_section_lines.append(stripped)

    candidate_lines = item_section_lines if item_section_lines else lines
    seen_norms: set[str] = set()
    result: list[str] = []
    for line in candidate_lines:
        cleaned = _clean_report_candidate_line(line)
        if not cleaned:
            continue
        # Ignore very short/generic fragments; exact simple-code stock should be
        # at least 5 chars to avoid matching words from notes or headers.
        if len(_normalize_report_match_text(cleaned)) < 5:
            continue
        norm = _normalize_report_match_text(cleaned)
        if norm in seen_norms:
            continue
        seen_norms.add(norm)
        result.append(cleaned)
    return result


def _report_exact_candidate_match(submitted_candidates: list[str], delivered_item: str) -> bool:
    """Strictly match one delivered item against exact submitted items/emails.

    Used for forwarded order text that contains an Order ID or an Items section.
    In that case, labels like Product/Quantity and fuzzy token matching must not
    create extra replacement matches from another product/order.
    """
    if not submitted_candidates:
        return False

    delivered_values = _report_candidate_values(delivered_item)
    delivered_norms = {_normalize_report_match_text(v) for v in delivered_values}
    delivered_norm = _normalize_report_match_text(delivered_item)

    for candidate in submitted_candidates:
        candidate_norm = _normalize_report_match_text(candidate)
        if not candidate_norm:
            continue
        if candidate_norm == delivered_norm or candidate_norm in delivered_norms:
            return True
    return False


def _significant_report_tokens(value: str) -> set[str]:
    text = _normalize_report_match_text(value)
    email_domains = {email.split("@", 1)[1] for email in _emails_in_text(text) if "@" in email}
    tokens = set(_emails_in_text(text))
    # Account IDs / usernames / short IDs are often pasted without separators.
    # Skip generic labels and email-domain fragments so a multi-account report
    # does not match every account from the same domain (example: example.com).
    ignored = {
        "mail", "email", "pass", "password", "login", "username", "user",
        "order", "items", "item", "product", "quantity", "qty", "stock",
        "format", "account", "accounts", "code", "codes",
    }
    for token in re.findall(r"[a-z0-9._%+\-]{5,}", text, flags=re.IGNORECASE):
        cleaned = token.strip("._-+% ").lower()
        if len(cleaned) < 5 or cleaned in ignored:
            continue
        if cleaned in email_domains:
            continue
        tokens.add(cleaned)
    return tokens


def _report_submitted_item_matches(submitted: str, delivered_item: str) -> bool:
    submitted_norm = _normalize_report_match_text(submitted)
    delivered_norm = _normalize_report_match_text(delivered_item)
    if not submitted_norm or not delivered_norm:
        return False

    submitted_emails = _emails_in_text(submitted_norm)
    delivered_emails = _emails_in_text(delivered_norm)
    if submitted_emails and delivered_emails:
        # Mail-format stock should match by the exact email only. Without this,
        # shared domains like example.com can make a 10-account report count as
        # 20+ accounts when the user bought many accounts from the same domain.
        return bool(submitted_emails.intersection(delivered_emails))

    submitted_values = _report_candidate_values(submitted)
    delivered_values = _report_candidate_values(delivered_item)
    delivered_value_norms = {_normalize_report_match_text(v) for v in delivered_values}

    if submitted_values:
        for value in submitted_values:
            value_norm = _normalize_report_match_text(value)
            if not value_norm:
                continue
            if value_norm == delivered_norm or value_norm in delivered_value_norms:
                return True
            # Allow a pasted code to match a labeled delivered item such as
            # "Code: ABCD-1234", but do not use the full report header text for
            # broad substring matching.
            if len(value_norm) >= 6 and value_norm in delivered_norm:
                return True
        submitted_token_source = "\n".join(submitted_values)
    else:
        submitted_token_source = submitted_norm

    # One pasted message can contain many account IDs/codes. Match useful tokens
    # from the delivered stock item instead of requiring the whole message to match.
    submitted_tokens = _significant_report_tokens(submitted_token_source)
    delivered_tokens = _significant_report_tokens(delivered_norm)
    if submitted_tokens and delivered_tokens and submitted_tokens.intersection(delivered_tokens):
        return True

    # Let users send only the account ID/code or a copied part of the stock text.
    if not submitted_values and len(submitted_norm) >= 6 and submitted_norm in delivered_norm:
        return True
    return submitted_norm == delivered_norm


def _report_submitted_match_snippet(submitted: str, delivered_item: str) -> str:
    """Return the exact submitted line/token that matched one delivered item.

    This keeps multi-item reports readable in WebAdmin: each matched item shows
    the user's relevant submitted account/email instead of repeating the whole
    pasted message.
    """
    submitted_raw = str(submitted or "").strip()
    delivered_raw = str(delivered_item or "").strip()
    submitted_norm = _normalize_report_match_text(submitted_raw)
    delivered_norm = _normalize_report_match_text(delivered_raw)
    submitted_emails = _emails_in_text(submitted_norm)
    delivered_emails = _emails_in_text(delivered_norm)
    common_emails = submitted_emails.intersection(delivered_emails)
    if common_emails:
        email = sorted(common_emails, key=len, reverse=True)[0]
        for line in submitted_raw.splitlines():
            if email.lower() in line.lower():
                return line.strip() or email
        return email
    if submitted_emails and delivered_emails:
        return submitted_raw[:1000]

    for candidate in _report_candidate_values(submitted_raw):
        if _report_submitted_item_matches(candidate, delivered_raw):
            return candidate

    submitted_tokens = _significant_report_tokens("\n".join(_report_candidate_values(submitted_raw)) or submitted_norm)
    delivered_tokens = _significant_report_tokens(delivered_norm)
    common_tokens = submitted_tokens.intersection(delivered_tokens)
    if common_tokens:
        token = sorted(common_tokens, key=len, reverse=True)[0]
        for line in submitted_raw.splitlines():
            if token.lower() in line.lower():
                return line.strip() or token
        return token

    for line in submitted_raw.splitlines():
        clean_line = line.strip()
        if clean_line and _report_submitted_item_matches(clean_line, delivered_raw):
            return clean_line

    return submitted_raw[:1000]


async def _find_stock_owner_record_for_item(product_name: str, item: str) -> dict:
    item_hash = stock_item_hash(item)
    db = get_db()
    product = await db.products.find_one({"name": _name_regex(product_name)}, {"name": 1, "stock_added_by": 1})
    if product:
        for record in product.get("stock_added_by", []) or []:
            if isinstance(record, dict) and str(record.get("item_hash") or "") == item_hash:
                result = dict(record)
                result.setdefault("product_name", product.get("name") or product_name)
                return result

    cursor = db.products.find({"stock_added_by.item_hash": item_hash}, {"name": 1, "stock_added_by": 1})
    async for product in cursor:
        for record in product.get("stock_added_by", []) or []:
            if isinstance(record, dict) and str(record.get("item_hash") or "") == item_hash:
                result = dict(record)
                result.setdefault("product_name", product.get("name") or product_name)
                return result
    return {"item_hash": item_hash}


async def find_user_delivered_stock_items_for_report(user_id: int, submitted_text: str, limit: int = 200) -> list[dict]:
    """Find delivered items belonging to this user from one pasted report message.

    The user can paste one item, only an email/account ID, or multiple account
    details in one message. Every delivered stock item that can be found inside
    that message is returned. Active/replaced reports are marked so the bot can
    skip duplicates while still allowing cancelled/rejected items to be reported
    again.
    """
    matches: list[dict] = []
    seen_hashes: set[str] = set()
    seen_submitted_keys: set[str] = set()
    try:
        max_matches = max(1, min(int(limit or 200), 500))
    except Exception:
        max_matches = 200

    order_query: dict = {"user_id": int(user_id), "status": "delivered", "items.0": {"$exists": True}}
    explicit_order_ids = _order_ids_in_report(submitted_text)
    submitted_candidates = _report_candidate_values(submitted_text)
    strict_order_report = bool(explicit_order_ids or _report_has_items_section(submitted_text))
    if explicit_order_ids:
        # When the user forwards an order TXT/caption, only search that order.
        # This prevents the same simple code or a fuzzy token from counting an
        # extra item from some older delivered order.
        order_query["order_id"] = {"$in": sorted(explicit_order_ids)}

    cursor = get_db().orders.find(
        order_query,
        {"order_id": 1, "product_name": 1, "items": 1, "created_at": 1, "delivered_at": 1, "payment_method": 1},
    ).sort("delivered_at", -1)
    async for order in cursor:
        for item in order.get("items", []) or []:
            item_text = str(item or "").strip()
            if not item_text:
                continue
            if strict_order_report:
                if not _report_exact_candidate_match(submitted_candidates, item_text):
                    continue
            elif not _report_submitted_item_matches(submitted_text, item_text):
                continue
            item_hash = stock_item_hash(item_text)
            if item_hash in seen_hashes:
                continue
            submitted_snippet = _report_submitted_match_snippet(submitted_text, item_text)
            submitted_key = _normalize_report_match_text(submitted_snippet)
            if submitted_key and submitted_key in seen_submitted_keys:
                continue
            seen_hashes.add(item_hash)
            if submitted_key:
                seen_submitted_keys.add(submitted_key)
            owner_record = await _find_stock_owner_record_for_item(str(order.get("product_name") or ""), item_text)
            existing_report = await get_db().replacement_reports.find_one(
                {
                    "user_id": int(user_id),
                    "$or": [{"item_hash": item_hash}, {"items.item_hash": item_hash}],
                    "status": {"$nin": ["cancelled", "rejected", "closed"]},
                },
                {"report_id": 1, "status": 1, "created_at": 1},
                sort=[("created_at", -1)],
            )
            matches.append({
                "order_id": str(order.get("order_id") or ""),
                "product_name": str(order.get("product_name") or ""),
                "payment_method": str(order.get("payment_method") or ""),
                "delivered_item": item_text,
                "submitted_item": submitted_snippet,
                "item_hash": item_hash,
                "already_reported": bool(existing_report),
                "existing_report_id": str((existing_report or {}).get("report_id") or ""),
                "existing_report_status": str((existing_report or {}).get("status") or ""),
                "order_created_at": order.get("created_at"),
                "sold_at": order.get("delivered_at") or order.get("created_at"),
                "stock_added_by_username": str(owner_record.get("added_by_username") or ""),
                "stock_added_by_role": str(owner_record.get("added_by_role") or ""),
                "stock_added_at": owner_record.get("added_at"),
                "stock_metadata_product_name": str(owner_record.get("product_name") or ""),
            })
            if len(matches) >= max_matches:
                return matches
    return matches


async def find_user_delivered_stock_item_for_report(user_id: int, submitted_text: str) -> Optional[dict]:
    """Find the newest matching delivered item belonging to this user."""
    matches = await find_user_delivered_stock_items_for_report(user_id, submitted_text, limit=1)
    return matches[0] if matches else None


async def create_replacement_report(
    *,
    user_id: int,
    username: str = "",
    matched: dict | None = None,
    matched_items: list[dict] | None = None,
    issue_text: str,
    screenshot_file_id: str = "",
) -> str:
    db = get_db()
    now = datetime.now(timezone.utc)
    clean_username = str(username or "").strip().lstrip("@")
    matches = list(matched_items or [])
    if not matches and matched:
        matches = [matched]
    matches = [m for m in matches if isinstance(m, dict)]
    if not matches:
        raise ValueError("create_replacement_report requires at least one matched item")

    report_items: list[dict] = []
    for m in matches:
        report_items.append({
            "order_id": str(m.get("order_id") or ""),
            "product_name": str(m.get("product_name") or ""),
            "payment_method": str(m.get("payment_method") or ""),
            "submitted_item": str(m.get("submitted_item") or ""),
            "delivered_item": str(m.get("delivered_item") or ""),
            "item_hash": str(m.get("item_hash") or ""),
            "sold_at": m.get("sold_at"),
            "order_created_at": m.get("order_created_at"),
            "stock_added_by_username": str(m.get("stock_added_by_username") or ""),
            "stock_added_by_role": str(m.get("stock_added_by_role") or ""),
            "stock_added_at": m.get("stock_added_at"),
            "stock_metadata_product_name": str(m.get("stock_metadata_product_name") or ""),
        })

    first = report_items[0]
    product_names: list[str] = []
    order_ids: list[str] = []
    for item in report_items:
        product = str(item.get("product_name") or "")
        order_id = str(item.get("order_id") or "")
        if product and product not in product_names:
            product_names.append(product)
        if order_id and order_id not in order_ids:
            order_ids.append(order_id)

    for _ in range(20):
        report_id = "REP" + uuid.uuid4().hex[:8].upper()
        if not await db.replacement_reports.find_one({"report_id": report_id}, {"_id": 1}):
            break
    else:
        report_id = "REP" + uuid.uuid4().hex[:12].upper()

    doc = {
        "report_id": report_id,
        "user_id": int(user_id),
        "username": clean_username,
        "order_id": str(first.get("order_id") or ""),
        "order_ids": order_ids,
        "product_name": str(first.get("product_name") or ""),
        "product_names": product_names,
        "payment_method": str(first.get("payment_method") or ""),
        "submitted_item": str(first.get("submitted_item") or ""),
        "delivered_item": str(first.get("delivered_item") or ""),
        "item_hash": str(first.get("item_hash") or ""),
        "items": report_items,
        "item_count": len(report_items),
        "issue_text": str(issue_text or "").strip()[:2000],
        "screenshot_file_id": str(screenshot_file_id or ""),
        "status": "pending",
        "created_at": now,
        "sold_at": first.get("sold_at"),
        "order_created_at": first.get("order_created_at"),
        "stock_added_by_username": str(first.get("stock_added_by_username") or ""),
        "stock_added_by_role": str(first.get("stock_added_by_role") or ""),
        "stock_added_at": first.get("stock_added_at"),
        "stock_metadata_product_name": str(first.get("stock_metadata_product_name") or ""),
    }
    await db.replacement_reports.insert_one(doc)
    return report_id
