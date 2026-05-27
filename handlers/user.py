"""
handlers/user.py — User-facing shopping flow.

Flow:
  /start    → Welcome + main menu
  /commands → Show user commands
  /orders   → Show user order history and tappable get-order shortcuts
  /shop     → Browse products (inline buttons)
  User picks product → asks quantity
  User enters quantity → shows total + payment options
  User picks payment → payment flow initiated
  After payment → delivery
"""

import uuid
import io
from datetime import datetime, timezone
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

import database as db
from config import SUPPORT_USERNAMES
from utils.crypto import UniqueUsdtAmountUnavailable, generate_unique_usdt_amount
from utils.messages import commands_text, compact_blank_lines, md_code, telegram_id, order_amount_text
from utils.i18n import tr, language_name, normalize_lang, SUPPORTED_LANGUAGES
from handlers.replacements import start_report_from_query

# Track users in shopping flow: { user_id: { step, product_name, quantity } }
_shop_flow: dict[int, dict] = {}


def clear_shop_flow(user_id: int | str | None) -> None:
    """Clear any active shopping quantity/payment session for this user."""
    try:
        uid = int(user_id or 0)
    except (TypeError, ValueError):
        return
    _shop_flow.pop(uid, None)

ORDERS_PAGE_SIZE = 10
REPLACEMENTS_PAGE_SIZE = 10


async def _user_lang(user_id: int) -> str:
    return await db.get_user_language(user_id)


def _language_keyboard(enabled_languages: list[str] | None = None) -> InlineKeyboardMarkup:
    enabled = enabled_languages or ["en", "es"]
    rows = []
    if "en" in enabled:
        rows.append([InlineKeyboardButton("🇬🇧 English", callback_data="lang:en")])
    if "es" in enabled:
        rows.append([InlineKeyboardButton("🇪🇸 Español", callback_data="lang:es")])
    if not rows:
        rows.append([InlineKeyboardButton("🇬🇧 English", callback_data="lang:en")])
    return InlineKeyboardMarkup(rows)


def _language_prompt(lang: str = "en") -> str:
    return f"{tr(lang, 'select_language_title')}\n\n{tr(lang, 'select_language_body')}"


def _payment_enabled(settings: dict, method: str) -> bool:
    return db.payment_method_enabled(settings, method)


def _compact_amount(value, max_decimals: int = 2) -> str:
    """Format normal display prices with a fixed number of decimals."""
    try:
        amount = float(value or 0)
    except (TypeError, ValueError):
        amount = 0.0
    return f"{amount:.{max_decimals}f}"


def _price_inr_label(value) -> str:
    return f"₹{_compact_amount(value, 2)}"


def _price_usdt_label(value) -> str:
    return f"${_compact_amount(value, 2)} USDT"


def _price_label(product: dict, settings: dict, *, quantity: int = 1, lang: str = "en") -> str:
    """Show enabled product prices with USDT first as the default display currency.

    Normal shop/order summaries stay at 2 decimals. Crypto payment instruction
    pages still use the separate unique 3-decimal send amount.
    """
    parts = []
    usdt_enabled = (
        _payment_enabled(settings, "usdt")
        or _payment_enabled(settings, "polygon")
        or _payment_enabled(settings, "binance")
        or _payment_enabled(settings, "wallet_usdt")
    )
    inr_enabled = _payment_enabled(settings, "upi") or _payment_enabled(settings, "wallet_inr")
    if usdt_enabled:
        parts.append(_price_usdt_label(float(product.get('price_usdt') or 0) * quantity))
    if inr_enabled:
        parts.append(_price_inr_label(float(product.get('price_inr') or 0) * quantity))
    return " / ".join(parts) if parts else tr(lang, "payment_not_configured")


def _product_description_block(product: dict, lang: str = "en") -> str:
    """Optional user-facing product description shown after product selection."""
    selected_lang = normalize_lang(lang)
    if selected_lang == "es":
        description = str(product.get("description_es") or "").strip()
    else:
        description = str(product.get("description_en") or product.get("description") or "").strip()
    if not description:
        return ""
    return tr(selected_lang, "product_description_block", description=description)


def _product_warranty_block(product: dict, lang: str = "en") -> str:
    """Optional per-product warranty shown under the description in the product details."""
    selected_lang = normalize_lang(lang)
    try:
        warranty_days = int(product.get("warranty_days") or 0)
    except (TypeError, ValueError):
        warranty_days = 0
    if warranty_days <= 0:
        return ""
    return tr(selected_lang, "product_warranty_block", days=warranty_days, day_label=tr(selected_lang, "day" if warranty_days == 1 else "days"))


def _product_details_block(product: dict, lang: str = "en") -> str:
    return compact_blank_lines("\n\n".join(
        block for block in (
            _product_description_block(product, lang),
            _product_warranty_block(product, lang),
        ) if block
    ))


def _ellipsize(text: str, max_len: int) -> str:
    text = str(text or "").strip()
    if len(text) <= max_len:
        return text
    return text[:max(1, max_len - 1)].rstrip() + "…"


def _fit_product_button(prefix: str, name: str, stock_part: str, price_part: str) -> str:
    """Telegram inline keyboard buttons do not reliably support multi-line text.

    Keep the old single-line layout so the button looks consistent on mobile.
    Long product names may still be clipped by Telegram, but the full name is
    shown after the user opens the product.
    """
    name = str(name or "Product").strip()
    return f"{prefix} {name} | {stock_part} | {price_part}"


def _product_callback(prefix: str, product: dict) -> str:
    """Use compact ObjectId callbacks so long product names do not break buttons."""
    product_id = product.get("_id")
    if product_id:
        return f"{prefix}_id:{product_id}"
    return f"{prefix}:{product.get('name', '')}"


def _callback_product_key(data: str) -> tuple[str, str]:
    action, value = (data or "").split(":", 1)
    return action, value


async def _product_from_callback(data: str) -> tuple[str, dict | None]:
    action, value = _callback_product_key(data)
    if action.endswith("_id"):
        return action, await db.get_product_by_id(value)
    return action, await db.get_product(value)


def _get_order_quantity_limits(product: dict, stock_count: int | None = None) -> tuple[int, int]:
    """Return per-product order limits.

    Limits live on each product so different products can have different
    minimum/maximum quantities. Missing legacy fields fall back to 1–100.
    """
    min_qty = db.parse_positive_int(product.get("min_order_quantity"), 1, minimum=1)
    max_qty = db.parse_positive_int(product.get("max_order_quantity"), 100, minimum=1)
    if max_qty < min_qty:
        max_qty = min_qty
    if stock_count is not None:
        max_qty = min(max_qty, max(0, int(stock_count or 0)))
    return min_qty, max_qty


def _preorder_limit_for_product(
    product: dict,
    capacity_remaining: int | None = None,
    user_remaining: int | None = None,
) -> int:
    max_preorder = db.get_product_preorder_max_quantity(product)
    product_min, product_max = _get_order_quantity_limits(product, None)
    limit = min(max_preorder, product_max)
    if capacity_remaining is not None:
        limit = min(limit, max(0, int(capacity_remaining or 0)))
    if user_remaining is not None:
        limit = min(limit, max(0, int(user_remaining or 0)))
    return max(0, limit)


async def _preorder_status_for_user(product: dict, user_id: int) -> dict:
    """Return live preorder limits for one user/product."""
    product_name = str((product or {}).get("name") or "")
    active_backorder = await db.get_active_preorder_backorder_quantity(product_name)
    active_user_qty = await db.get_active_user_preorder_quantity(user_id, product_name)
    total_remaining = db.get_preorder_capacity_remaining(product, active_backorder)
    user_remaining = db.get_user_preorder_capacity_remaining(product, active_user_qty)
    max_qty = _preorder_limit_for_product(product, total_remaining, user_remaining)
    _, product_max_qty = _get_order_quantity_limits(product, None)
    user_limit = min(db.get_product_preorder_max_quantity(product), product_max_qty)
    return {
        "active_backorder": active_backorder,
        "active_user_qty": active_user_qty,
        "total_remaining": total_remaining,
        "user_remaining": user_remaining,
        "user_limit": user_limit,
        "max_qty": max_qty,
    }


def _preorder_is_open(product: dict, stock_count: int | None = None) -> bool:
    if not product or product.get("enabled", True) is False:
        return False
    if stock_count is None:
        stock_count = int(product.get("available_stock", len(product.get("stock", []) or [])) or 0)
    capacity = int(product.get("preorder_capacity_remaining", 0) or 0)
    return stock_count <= 0 and db.product_preorder_enabled(product) and capacity > 0


def _product_button_label(product: dict, payment_settings: dict | None = None, lang: str = "en") -> str:
    """Button label showing product price and stock available to new buyers."""
    payment_settings = payment_settings or {}
    stock_count = int(product.get("available_stock", len(product.get("stock", []) or [])) or 0)
    enabled = product.get("enabled", True)
    if not enabled:
        prefix = "🚫"
        stock_part = tr(lang, "stock_disabled")
    elif stock_count <= 0:
        if db.product_preorder_enabled(product):
            capacity = int(product.get("preorder_capacity_remaining", 0) or 0)
            if capacity > 0:
                prefix = "📝"
                stock_part = tr(lang, "stock_preorder_open", remaining=capacity)
            else:
                prefix = "⛔"
                stock_part = tr(lang, "stock_preorder_full")
        else:
            prefix = "❌"
            stock_part = tr(lang, "stock_sold_out")
    else:
        prefix = "✅"
        stock_part = tr(lang, "stock_count", count=stock_count)
    return _fit_product_button(prefix, product['name'], stock_part, _price_label(product, payment_settings, lang=lang))


def _main_menu_keyboard(user_id: int, lang: str = "en") -> InlineKeyboardMarkup:
    """Builds the user-only main menu. Admin actions are handled in WebAdmin."""
    buttons = [
        [
            InlineKeyboardButton(tr(lang, "btn_shop"), callback_data="nav:shop"),
            InlineKeyboardButton(tr(lang, "btn_wallet"), callback_data="nav:wallet"),
        ],
        [InlineKeyboardButton(tr(lang, "btn_topup"), callback_data="nav:loadwallet")],
        [
            InlineKeyboardButton(tr(lang, "btn_orders"), callback_data="nav:orders"),
            InlineKeyboardButton(tr(lang, "btn_replacements"), callback_data="nav:replacements"),
        ],
        [InlineKeyboardButton(tr(lang, "btn_favorites"), callback_data="nav:favorites")],
        [InlineKeyboardButton(tr(lang, "btn_report"), callback_data="nav:report")],
        [InlineKeyboardButton(tr(lang, "btn_commands"), callback_data="nav:commands")],
        [InlineKeyboardButton(tr(lang, "btn_language"), callback_data="nav:language")],
        [InlineKeyboardButton(tr(lang, "btn_support"), callback_data="nav:support")],
    ]
    return InlineKeyboardMarkup(buttons)


def _main_menu_text(first_name: str, lang: str = "en") -> str:
    return tr(lang, "main_menu", first_name=first_name)


def _back_button(lang: str = "en") -> list[InlineKeyboardButton]:
    return [InlineKeyboardButton(tr(lang, "btn_back"), callback_data="nav:back")]


def _product_list_keyboard(products: list[dict], payment_settings: dict | None = None, include_back: bool = True, lang: str = "en") -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(_product_button_label(p, payment_settings, lang), callback_data=_product_callback("product", p))]
        for p in products
    ]
    if include_back:
        buttons.append(_back_button(lang))
    return InlineKeyboardMarkup(buttons)


def _product_buy_keyboard(back_to: str = "shop", lang: str = "en") -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if back_to == "favorites":
        rows.append([InlineKeyboardButton(tr(lang, "btn_back_favorites"), callback_data="nav:favorites")])
    else:
        rows.append([InlineKeyboardButton(tr(lang, "btn_back_shop"), callback_data="nav:shop")])
    rows.append(_back_button(lang))
    return InlineKeyboardMarkup(rows)


def _favorite_button_label(product: dict, payment_settings: dict | None = None, is_favorite: bool = False, lang: str = "en") -> str:
    star = "⭐" if is_favorite else "☆"
    stock_count = int(product.get("available_stock", len(product.get("stock", []) or [])) or 0)
    if stock_count <= 0 and db.product_preorder_enabled(product):
        capacity = int(product.get("preorder_capacity_remaining", 0) or 0)
        stock_part = tr(lang, "stock_preorder_open", remaining=capacity) if capacity > 0 else tr(lang, "stock_preorder_full")
    else:
        stock_part = tr(lang, "stock_sold_out") if stock_count <= 0 else tr(lang, "stock_count", count=stock_count)
    return _fit_product_button(star, product['name'], stock_part, _price_label(product, payment_settings or {}, lang=lang))


def _favorite_name_set(favorite_names: list[str] | None) -> set[str]:
    return {str(name).strip().lower() for name in (favorite_names or []) if str(name).strip()}


def _filter_favorite_products(products: list[dict], favorite_names: list[str] | None) -> list[dict]:
    favorite_set = _favorite_name_set(favorite_names)
    return [
        p for p in products
        if str(p.get("name", "")).strip().lower() in favorite_set
    ]


def _favorites_keyboard(favorite_products: list[dict], payment_settings: dict | None = None, lang: str = "en") -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(
            _favorite_button_label(p, payment_settings, True, lang),
            callback_data=_product_callback("favorite_buy", p)
        )]
        for p in favorite_products
    ]
    rows.append([InlineKeyboardButton(tr(lang, "btn_edit_favorites"), callback_data="nav:favorites_edit")])
    rows.append(_back_button(lang))
    return InlineKeyboardMarkup(rows)


def _favorites_edit_keyboard(products: list[dict], payment_settings: dict | None = None, favorite_names: list[str] | None = None, lang: str = "en") -> InlineKeyboardMarkup:
    favorite_set = _favorite_name_set(favorite_names)
    rows = [
        [InlineKeyboardButton(
            _favorite_button_label(p, payment_settings, str(p.get("name", "")).strip().lower() in favorite_set, lang),
            callback_data=_product_callback("fav", p)
        )]
        for p in products
    ]
    rows.append([InlineKeyboardButton(tr(lang, "btn_back_favorites"), callback_data="nav:favorites")])
    rows.append(_back_button(lang))
    return InlineKeyboardMarkup(rows)


def _wallet_keyboard(lang: str = "en") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(tr(lang, "btn_topup"), callback_data="nav:loadwallet")],
        [InlineKeyboardButton(tr(lang, "wallet_history_title"), callback_data="wallethistory:0")],
        _back_button(lang),
    ])


async def _load_wallet_keyboard(lang: str = "en") -> tuple[InlineKeyboardMarkup, bool]:
    settings = await db.get_payment_settings()
    rows: list[list[InlineKeyboardButton]] = []
    if _payment_enabled(settings, "usdt"):
        rows.append([InlineKeyboardButton(tr(lang, "wallet_topup_usdt"), callback_data="wallet_currency:usdt")])
    if _payment_enabled(settings, "polygon"):
        rows.append([InlineKeyboardButton(tr(lang, "wallet_topup_polygon"), callback_data="wallet_currency:polygon_usdt")])
    if _payment_enabled(settings, "binance"):
        rows.append([InlineKeyboardButton(tr(lang, "wallet_topup_binance"), callback_data="wallet_currency:binance_usdt")])
    if _payment_enabled(settings, "upi"):
        rows.append([InlineKeyboardButton(tr(lang, "wallet_topup_inr"), callback_data="wallet_currency:inr")])
    has_methods = bool(rows)
    rows.append(_back_button(lang))
    return InlineKeyboardMarkup(rows), has_methods


def _clear_wallet_flow_if_available(user_id: int) -> None:
    """Clear wallet amount input state when user navigates away from wallet pages."""
    try:
        from handlers.wallet import clear_wallet_flow
        clear_wallet_flow(user_id)
    except Exception:
        pass


def _normalize_support_username(value: str) -> str:
    value = (value or "").strip()
    if value and not value.startswith(("@", "http://", "https://")):
        return f"@{value}"
    return value


def _support_entries() -> list[str]:
    """Returns configured support usernames/links as display text."""
    return [_normalize_support_username(v) for v in SUPPORT_USERNAMES if _normalize_support_username(v)]


def _support_url(value: str) -> str | None:
    """Builds a Telegram support URL from one SUPPORT_USERNAMES entry when possible."""
    value = _normalize_support_username(value)
    if not value:
        return None
    if value.startswith(("http://", "https://")):
        return value
    username = value.lstrip("@").strip()
    return f"https://t.me/{username}" if username else None


def _support_keyboard(include_back: bool = False, lang: str = "en") -> InlineKeyboardMarkup | None:
    buttons = []
    supports = _support_entries()
    for idx, support in enumerate(supports, 1):
        url = _support_url(support)
        if url:
            label = tr(lang, "support_open") if len(supports) == 1 else tr(lang, "support_open_n", n=idx)
            buttons.append([InlineKeyboardButton(label, url=url)])
    if include_back:
        buttons.append([InlineKeyboardButton(tr(lang, "btn_back"), callback_data="nav:back")])
    return InlineKeyboardMarkup(buttons) if buttons else None


def _support_text(lang: str = "en") -> str:
    supports = _support_entries()
    if not supports:
        return tr(lang, "support_not_configured")
    if len(supports) == 1:
        return tr(lang, "support_one", contact=supports[0])
    contacts = "\n".join(f"• {support}" for support in supports)
    return tr(lang, "support_many", contacts=contacts)


def _format_dt(value) -> str:
    """Formats Mongo datetime values in a compact UTC format."""
    if not value:
        return "N/A"
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except Exception:
            return str(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _status_emoji(status: str) -> str:
    return {
        "pending": "⏳",
        "pending_stock": "⏳",
        "delivered": "✅",
        "failed": "❌",
        "expired": "❌",
        "confirmed": "✅",
        "revoked": "🚫",
    }.get((status or "").lower(), "❓")


def _status_label(status: str, lang: str = "en") -> str:
    if (status or "").lower() == "revoked":
        return "Revoked"
    key = {
        "pending_stock": "status_pending_stock",
        "pending": "status_pending",
        "delivered": "status_delivered",
        "failed": "status_failed",
        "expired": "status_expired",
        "confirmed": "status_confirmed",
        "revoked": "delivery_revoked",
    }.get((status or "unknown").lower())
    return tr(lang, key) if key else str(status or "unknown").replace("_", " ").title()


def _getorder_shortcut(order_id: str) -> str:
    """Returns a tappable Telegram-style shortcut without registering it as a BotFather command."""
    return f"/getorder{order_id}"


def _format_user_orders_text(orders: list[dict], page: int = 0, total_count: int | None = None, lang: str = "en") -> str:
    if not orders:
        return tr(lang, "orders_empty")

    total_pages = 1
    if total_count is not None and total_count > 0:
        total_pages = max(1, ((total_count - 1) // ORDERS_PAGE_SIZE) + 1)

    lines = [
        tr(lang, "orders_header", page=page + 1, total_pages=total_pages),
        tr(lang, "orders_hint"),
    ]
    for order in orders:
        order_id = order.get("order_id", "N/A")
        display_status = "revoked" if order.get("delivery_revoked") else order.get("status")
        lines.extend([
            f"{tr(lang, 'order_id')}: `{order_id}`",
            f"{tr(lang, 'date_time')}: {_format_dt(order.get('created_at'))}",
            f"{tr(lang, 'payment_method')}: `{str(order.get('payment_method', 'N/A')).upper()}`",
            f"{tr(lang, 'product')}: *{order.get('product_name', 'N/A')}* x{order.get('quantity', 0)}",
            f"{tr(lang, 'status')}: {_status_emoji(display_status)} {_status_label(display_status, lang)}",
            f"{tr(lang, 'fetch_again')}: {'Disabled by admin' if order.get('delivery_revoked') else _getorder_shortcut(order_id)}",
            "",
        ])
    return compact_blank_lines("\n".join(lines))


def _order_items_txt_filename(order: dict) -> str:
    order_id = str(order.get("order_id") or "order").strip() or "order"
    safe_order_id = "".join(ch for ch in order_id if ch.isalnum() or ch in ("-", "_")).strip() or "order"
    if order.get("is_replacement"):
        return f"replacement_{safe_order_id}_items.txt"
    return f"order_{safe_order_id}_items.txt"


def _order_items_txt_content(order: dict, lang: str = "en") -> str:
    order_id = str(order.get("order_id", "N/A"))
    product_name = str(order.get("product_name", "Product"))
    items = order.get("items", []) or []
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


def _order_items_caption(order: dict, *, is_refetch: bool = False, lang: str = "en") -> str:
    order_id = str(order.get("order_id") or "N/A")
    product_name = str(order.get("product_name") or "Product")
    quantity = int(order.get("quantity", 0) or 0)
    is_replacement = bool(order.get("is_replacement"))
    if is_replacement:
        title = tr(lang, "previous_replacement_items") if is_refetch else tr(lang, "replacement_sent_admin")
        return (
            f"{title}\n\n"
            f"🧾 {tr(lang, 'replacement_id')}: {order_id}\n"
            f"🛠 {tr(lang, 'report_id')}: {order.get('replacement_report_id', 'N/A')}\n"
            f"📦 {tr(lang, 'product')}: {product_name}\n"
            f"🔢 {tr(lang, 'quantity')}: {quantity}"
        )
    if is_refetch:
        title = tr(lang, "previous_order_items")
    elif order.get("admin_stock_delivery"):
        title = tr(lang, "admin_stock_sent")
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
        and not is_refetch
        and not order.get("delivery_transfer")
        and str(order.get("admin_stock_source") or "").strip().lower() != "transfer"
    ):
        note = str(order.get("admin_stock_note") or "").strip()
        note_lower = note.lower()
        if note and not note_lower.startswith(("transferred from revoked order", "transferred from revoked replacement")):
            caption += f"\n\n📝 Admin note: {note[:700]}"
    return caption


async def _reply_order_items_file(update: Update, order: dict, *, is_refetch: bool = False) -> None:
    lang = await _user_lang(update.effective_user.id)
    data = _order_items_txt_content(order, lang).encode("utf-8")
    document = io.BytesIO(data)
    document.name = _order_items_txt_filename(order)
    caption = _order_items_caption(order, is_refetch=is_refetch, lang=lang)
    sent_message = await update.message.reply_document(
        document=document,
        filename=document.name,
        caption=caption,
    )
    await db.record_order_delivery_message(
        str(order.get("order_id") or ""),
        update.effective_chat.id if update.effective_chat else update.effective_user.id,
        getattr(sent_message, "message_id", None),
        filename=document.name,
        sent_by="user_refetch",
        resent=True,
    )


def _format_order_delivery_text(order: dict, *, is_refetch: bool = False, lang: str = "en") -> str:
    order_id = order.get("order_id", "N/A")
    product_name = order.get("product_name", "N/A")
    quantity = order.get("quantity", 0)
    status = "revoked" if order.get("delivery_revoked") else order.get("status", "unknown")
    items = order.get("items", []) or []
    is_replacement = bool(order.get("is_replacement"))
    id_label = tr(lang, "replacement_id") if is_replacement else tr(lang, "order_id")

    if status != "delivered" or not items:
        return (
            f"📦 *{id_label} `{order_id}`*\n\n"
            f"{tr(lang, 'product')}: *{product_name}* x{quantity}\n"
            f"{tr(lang, 'date_time')}: {_format_dt(order.get('created_at'))}\n"
            f"{tr(lang, 'payment_method')}: {str(order.get('payment_method', 'N/A')).upper()}\n"
            f"{tr(lang, 'status')}: {_status_emoji(status)} {_status_label(status, lang)}\n\n"
            f"{tr(lang, 'no_delivered_items')}"
        )

    if is_replacement:
        title = f"🎁 *{tr(lang, 'previous_replacement_items').replace('📄 ', '')}*" if is_refetch else f"✅ *{tr(lang, 'replacement_items_title')}*"
    else:
        title = f"📦 *{tr(lang, 'previous_order_items').replace('📄 ', '')}*" if is_refetch else f"✅ *{tr(lang, 'order_items_title')}*"
    text = (
        f"{title}\n\n"
        f"{id_label}: `{order_id}`\n"
    )
    if is_replacement and order.get("replacement_report_id"):
        text += f"{tr(lang, 'report_id')}: `{order.get('replacement_report_id')}`\n"
    text += (
        f"{tr(lang, 'date_time')}: {_format_dt(order.get('created_at'))}\n"
        f"{tr(lang, 'payment_method')}: {str(order.get('payment_method', 'N/A')).upper()}\n"
        f"{tr(lang, 'delivered')}: {_format_dt(order.get('delivered_at'))}\n"
        f"{tr(lang, 'product')}: *{product_name}*\n"
        f"{tr(lang, 'quantity')}: {quantity}\n\n"
        f"🎁 *{tr(lang, 'items_label')}:*\n\n"
    )
    for i, item in enumerate(items, 1):
        text += f"*{tr(lang, 'item_label', n=i)}:*\n`{item}`\n\n"
    text = text.strip()
    return text


def _orders_keyboard(page: int = 0, total_count: int = 0, lang: str = "en") -> InlineKeyboardMarkup:
    total_pages = max(1, ((total_count - 1) // ORDERS_PAGE_SIZE) + 1) if total_count else 1
    buttons: list[list[InlineKeyboardButton]] = []

    nav_buttons: list[InlineKeyboardButton] = []
    if page > 0:
        nav_buttons.append(InlineKeyboardButton(tr(lang, "btn_prev"), callback_data=f"nav:orders:{page - 1}"))
    if page + 1 < total_pages:
        nav_buttons.append(InlineKeyboardButton(tr(lang, "btn_next"), callback_data=f"nav:orders:{page + 1}"))
    if nav_buttons:
        buttons.append(nav_buttons)

    buttons.append([InlineKeyboardButton(tr(lang, "btn_shop_again"), callback_data="nav:shop")])
    buttons.append(_back_button(lang))
    return InlineKeyboardMarkup(buttons)


def _format_user_replacements_text(replacements: list[dict], page: int = 0, total_count: int | None = None, lang: str = "en") -> str:
    if not replacements:
        return tr(lang, "replacements_empty")

    total_pages = 1
    if total_count is not None and total_count > 0:
        total_pages = max(1, ((total_count - 1) // REPLACEMENTS_PAGE_SIZE) + 1)

    lines = [
        tr(lang, "replacements_header", page=page + 1, total_pages=total_pages),
        tr(lang, "replacements_hint"),
    ]
    for order in replacements:
        order_id = order.get("order_id", "N/A")
        display_status = "revoked" if order.get("delivery_revoked") else order.get("status")
        report_id = order.get("replacement_report_id", "N/A")
        original_orders = ", ".join(str(x) for x in (order.get("original_order_ids") or []) if str(x).strip()) or str(order.get("original_order_id") or "N/A")
        lines.extend([
            f"{tr(lang, 'replacement_id')}: `{order_id}`",
            f"{tr(lang, 'report_id')}: `{report_id}`",
            f"{tr(lang, 'date_time')}: {_format_dt(order.get('created_at'))}",
            f"{tr(lang, 'product')}: *{order.get('product_name', 'N/A')}* x{order.get('quantity', 0)}",
            f"{tr(lang, 'original_order')}: `{original_orders}`",
            f"{tr(lang, 'status')}: {_status_emoji(display_status)} {_status_label(display_status, lang)}",
            f"{tr(lang, 'fetch_again')}: {'Disabled by admin' if order.get('delivery_revoked') else _getorder_shortcut(order_id)}",
            "",
        ])
    return compact_blank_lines("\n".join(lines))


def _replacements_keyboard(page: int = 0, total_count: int = 0, lang: str = "en") -> InlineKeyboardMarkup:
    total_pages = max(1, ((total_count - 1) // REPLACEMENTS_PAGE_SIZE) + 1) if total_count else 1
    buttons: list[list[InlineKeyboardButton]] = []
    nav_buttons: list[InlineKeyboardButton] = []
    if page > 0:
        nav_buttons.append(InlineKeyboardButton(tr(lang, "btn_prev"), callback_data=f"nav:replacements:{page - 1}"))
    if page + 1 < total_pages:
        nav_buttons.append(InlineKeyboardButton(tr(lang, "btn_next"), callback_data=f"nav:replacements:{page + 1}"))
    if nav_buttons:
        buttons.append(nav_buttons)
    buttons.append([InlineKeyboardButton(tr(lang, "btn_orders"), callback_data="nav:orders")])
    buttons.append(_back_button(lang))
    return InlineKeyboardMarkup(buttons)




async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    saved_user = await db.upsert_user(user.id, user.username or "")

    if await db.is_blocked(user.id):
        await update.message.reply_text(tr(await _user_lang(user.id), "blocked"))
        return

    if not saved_user.get("language_selected"):
        await update.message.reply_text(
            _language_prompt("en"),
            parse_mode="Markdown",
            reply_markup=_language_keyboard(await db.get_enabled_languages()),
        )
        return

    lang = await _user_lang(user.id)
    await update.message.reply_text(
        _main_menu_text(user.first_name, lang),
        parse_mode="Markdown",
        reply_markup=_main_menu_keyboard(user.id, lang)
    )


async def cmd_language(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await db.upsert_user(user.id, user.username or "")
    if await db.is_blocked(user.id):
        await update.message.reply_text(tr(await _user_lang(user.id), "blocked"))
        return
    lang = await _user_lang(user.id)
    await update.message.reply_text(
        tr(lang, "language_current", language=language_name(lang)),
        parse_mode="Markdown",
        reply_markup=_language_keyboard(await db.get_enabled_languages()),
    )


async def handle_language_select(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data or ""
    lang = normalize_lang(data.split(":", 1)[1] if ":" in data else "en")
    enabled = await db.get_enabled_languages()
    if lang not in enabled:
        lang = "en"
    user_id = query.from_user.id
    await db.upsert_user(user_id, query.from_user.username or "")
    await db.set_user_language(user_id, lang)
    await query.answer(tr(lang, "language_changed"), show_alert=False)
    await query.edit_message_text(
        _main_menu_text(query.from_user.first_name, lang),
        parse_mode="Markdown",
        reply_markup=_main_menu_keyboard(user_id, lang),
    )


async def cmd_commands(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await db.upsert_user(user.id, user.username or "")
    lang = await _user_lang(user.id)
    await update.message.reply_text(
        commands_text(lang),
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([_back_button(lang)])
    )


async def cmd_support(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Shows the configured support username/link."""
    user_id = update.effective_user.id
    await db.upsert_user(user_id, update.effective_user.username or "")
    lang = await _user_lang(user_id)
    if await db.is_blocked(user_id):
        await update.message.reply_text(tr(lang, "blocked"))
        return
    await update.message.reply_text(_support_text(lang), reply_markup=_support_keyboard(include_back=True, lang=lang))


async def cmd_shop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Shows the user shop product list from the /shop command."""
    user = update.effective_user
    user_id = user.id
    await db.upsert_user(user_id, user.username or "")
    lang = await _user_lang(user_id)
    if await db.is_blocked(user_id):
        await update.message.reply_text(tr(lang, "blocked"))
        return

    _shop_flow.pop(user_id, None)
    _clear_wallet_flow_if_available(user_id)
    products = await db.get_all_products_with_availability()
    payment_settings = await db.get_payment_settings()
    if not products:
        await update.message.reply_text(
            tr(lang, "shop_empty"),
            reply_markup=InlineKeyboardMarkup([_back_button(lang)])
        )
        return

    await update.message.reply_text(
        tr(lang, "shop_title"),
        parse_mode="Markdown",
        reply_markup=_product_list_keyboard(products, payment_settings, lang=lang)
    )


async def cmd_favorites(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Shows the caller's favorite manager and quick-buy products."""
    user_id = update.effective_user.id
    await db.upsert_user(user_id, update.effective_user.username or "")
    lang = await _user_lang(user_id)
    if await db.is_blocked(user_id):
        await update.message.reply_text(tr(lang, "blocked"))
        return
    favorite_names = await db.get_user_favorite_products(user_id)
    payment_settings = await db.get_payment_settings()
    products = await db.get_all_products_with_availability()
    if not products:
        await update.message.reply_text(
            tr(lang, "favorites_empty_products"),
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([_back_button(lang)])
        )
        return

    favorite_products = _filter_favorite_products(products, favorite_names)
    text = tr(lang, "favorites_has") if favorite_products else tr(lang, "favorites_none")
    await update.message.reply_text(
        text,
        parse_mode="Markdown",
        reply_markup=_favorites_keyboard(favorite_products, payment_settings, lang)
    )


async def cmd_orders(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Shows the caller's own order history, even if the caller is an admin."""
    user_id = update.effective_user.id
    await db.upsert_user(user_id, update.effective_user.username or "")
    lang = await _user_lang(user_id)
    if await db.is_blocked(user_id):
        await update.message.reply_text(tr(lang, "blocked"))
        return

    total_count = await db.count_user_orders(user_id)
    orders = await db.get_user_orders(user_id, limit=ORDERS_PAGE_SIZE, skip=0)
    await update.message.reply_text(
        _format_user_orders_text(orders, page=0, total_count=total_count, lang=lang),
        parse_mode="Markdown",
        reply_markup=_orders_keyboard(page=0, total_count=total_count, lang=lang)
    )


async def cmd_replacements(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Shows the caller's replacement delivery history separately from purchases."""
    user_id = update.effective_user.id
    await db.upsert_user(user_id, update.effective_user.username or "")
    lang = await _user_lang(user_id)
    if await db.is_blocked(user_id):
        await update.message.reply_text(tr(lang, "blocked"))
        return

    total_count = await db.count_user_replacement_orders(user_id)
    replacements = await db.get_user_replacement_orders(user_id, limit=REPLACEMENTS_PAGE_SIZE, skip=0)
    await update.message.reply_text(
        _format_user_replacements_text(replacements, page=0, total_count=total_count, lang=lang),
        parse_mode="Markdown",
        reply_markup=_replacements_keyboard(page=0, total_count=total_count, lang=lang)
    )


async def handle_get_order_lookup(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Handles tappable /getorderORDERID shortcuts without listing it as a public command."""
    user_id = update.effective_user.id
    lang = await _user_lang(user_id)
    if await db.is_blocked(user_id):
        await update.message.reply_text(tr(lang, "blocked"))
        return True

    text = (update.message.text or "").strip()
    lower = text.lower()
    if lower.startswith("/getorder"):
        order_id = text[len("/getorder"):].strip()
    elif lower.startswith("/getid"):
        order_id = text[len("/getid"):].strip()
    else:
        return False
    if order_id.startswith("_") or order_id.startswith("-") or order_id.startswith(":"):
        order_id = order_id[1:].strip()

    if not order_id:
        await update.message.reply_text(
            tr(lang, "usage_getorder"),
            parse_mode="Markdown"
        )
        return True

    order = await db.get_order(order_id.upper())
    if not order:
        await update.message.reply_text(tr(lang, "order_not_found"))
        return True

    if int(order.get("user_id", 0)) != int(user_id):
        await update.message.reply_text(tr(lang, "order_not_yours"))
        return True

    if order.get("delivery_revoked"):
        await update.message.reply_text("🚫 This delivery was revoked by admin, so it can no longer be fetched from the bot.")
        return True

    if order.get("status") == "delivered" and (order.get("items") or []):
        await _reply_order_items_file(update, order, is_refetch=True)
    else:
        await update.message.reply_text(
            _format_order_delivery_text(order, is_refetch=True, lang=lang),
            parse_mode="Markdown"
        )
    return True


async def handle_nav_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles main menu navigation buttons from /start."""
    query = update.callback_query
    await query.answer()
    action = query.data.split(":", 1)[1]
    action_parts = action.split(":")
    base_action = action_parts[0]
    user_id = query.from_user.id
    await db.upsert_user(user_id, query.from_user.username or "")
    lang = await _user_lang(user_id)

    if await db.is_blocked(user_id):
        await query.edit_message_text(tr(lang, "blocked"))
        return

    if base_action == "shop":
        _shop_flow.pop(user_id, None)
        _clear_wallet_flow_if_available(user_id)
        products = await db.get_all_products_with_availability()
        payment_settings = await db.get_payment_settings()
        if not products:
            await query.edit_message_text(
                tr(lang, "shop_empty"),
                reply_markup=InlineKeyboardMarkup([_back_button(lang)])
            )
            return
        await query.edit_message_text(
            tr(lang, "shop_title"),
            parse_mode="Markdown",
            reply_markup=_product_list_keyboard(products, payment_settings, lang=lang)
        )
        return

    if base_action == "wallet":
        _shop_flow.pop(user_id, None)
        _clear_wallet_flow_if_available(user_id)
        user = await db.get_user(user_id)
        inr_bal = float(user.get("wallet_inr", 0.0) if user else 0.0)
        usdt_bal = float(user.get("wallet_usdt", 0.0) if user else 0.0)
        payment_settings = await db.get_payment_settings()
        lines = [tr(lang, "wallet_title"), ""]
        if _payment_enabled(payment_settings, "wallet_usdt"):
            lines.append(tr(lang, "wallet_usdt_balance", balance=_compact_amount(usdt_bal, 2)))
        if _payment_enabled(payment_settings, "wallet_inr"):
            lines.append(tr(lang, "wallet_inr_balance", balance=f"{inr_bal:.2f}"))
        if len(lines) == 2:
            lines.append(tr(lang, "wallet_no_currency"))
        await query.edit_message_text(
            "\n".join(lines),
            parse_mode="Markdown",
            reply_markup=_wallet_keyboard(lang)
        )
        return

    if base_action == "loadwallet":
        _shop_flow.pop(user_id, None)
        _clear_wallet_flow_if_available(user_id)
        keyboard, has_methods = await _load_wallet_keyboard(lang)
        text = (
            tr(lang, "load_wallet_title")
            if has_methods else
            tr(lang, "load_wallet_none")
        )
        await query.edit_message_text(text, parse_mode="Markdown", reply_markup=keyboard)
        return

    if base_action == "orders":
        _shop_flow.pop(user_id, None)
        _clear_wallet_flow_if_available(user_id)
        try:
            page = int(action_parts[1]) if len(action_parts) > 1 else 0
        except (TypeError, ValueError):
            page = 0
        page = max(0, page)
        total_count = await db.count_user_orders(user_id)
        total_pages = max(1, ((total_count - 1) // ORDERS_PAGE_SIZE) + 1) if total_count else 1
        if page >= total_pages:
            page = total_pages - 1
        orders = await db.get_user_orders(user_id, limit=ORDERS_PAGE_SIZE, skip=page * ORDERS_PAGE_SIZE)
        await query.edit_message_text(
            _format_user_orders_text(orders, page=page, total_count=total_count, lang=lang),
            parse_mode="Markdown",
            reply_markup=_orders_keyboard(page=page, total_count=total_count, lang=lang)
        )
        return

    if base_action == "replacements":
        _shop_flow.pop(user_id, None)
        _clear_wallet_flow_if_available(user_id)
        try:
            page = int(action_parts[1]) if len(action_parts) > 1 else 0
        except (TypeError, ValueError):
            page = 0
        page = max(0, page)
        total_count = await db.count_user_replacement_orders(user_id)
        total_pages = max(1, ((total_count - 1) // REPLACEMENTS_PAGE_SIZE) + 1) if total_count else 1
        if page >= total_pages:
            page = total_pages - 1
        replacements = await db.get_user_replacement_orders(user_id, limit=REPLACEMENTS_PAGE_SIZE, skip=page * REPLACEMENTS_PAGE_SIZE)
        await query.edit_message_text(
            _format_user_replacements_text(replacements, page=page, total_count=total_count, lang=lang),
            parse_mode="Markdown",
            reply_markup=_replacements_keyboard(page=page, total_count=total_count, lang=lang)
        )
        return

    if base_action == "favorites":
        _shop_flow.pop(user_id, None)
        _clear_wallet_flow_if_available(user_id)
        favorite_names = await db.get_user_favorite_products(user_id)
        payment_settings = await db.get_payment_settings()
        products = await db.get_all_products_with_availability()
        if not products:
            await query.edit_message_text(
                tr(lang, "favorites_empty_products"),
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([_back_button(lang)])
            )
            return

        favorite_products = _filter_favorite_products(products, favorite_names)
        if favorite_products:
            text = tr(lang, "favorites_has")
        else:
            text = tr(lang, "favorites_none")

        await query.edit_message_text(
            text,
            parse_mode="Markdown",
            reply_markup=_favorites_keyboard(favorite_products, payment_settings, lang)
        )
        return

    if base_action == "favorites_edit":
        _shop_flow.pop(user_id, None)
        _clear_wallet_flow_if_available(user_id)
        favorite_names = await db.get_user_favorite_products(user_id)
        payment_settings = await db.get_payment_settings()
        products = await db.get_all_products_with_availability()
        if not products:
            await query.edit_message_text(
                tr(lang, "favorites_edit_empty"),
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([_back_button(lang)])
            )
            return
        await query.edit_message_text(
            tr(lang, "favorites_edit_title"),
            parse_mode="Markdown",
            reply_markup=_favorites_edit_keyboard(products, payment_settings, favorite_names, lang)
        )
        return

    if base_action == "report":
        _shop_flow.pop(user_id, None)
        _clear_wallet_flow_if_available(user_id)
        await start_report_from_query(update, context)
        return

    if base_action == "commands":
        await query.edit_message_text(
            commands_text(lang),
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([_back_button(lang)])
        )
        return

    if base_action == "language":
        await query.edit_message_text(
            tr(lang, "language_current", language=language_name(lang)),
            parse_mode="Markdown",
            reply_markup=_language_keyboard(await db.get_enabled_languages()),
        )
        return

    if base_action == "support":
        await query.edit_message_text(
            _support_text(lang),
            reply_markup=_support_keyboard(include_back=True, lang=lang)
        )
        return

    if base_action == "back":
        _shop_flow.pop(user_id, None)
        _clear_wallet_flow_if_available(user_id)
        await query.edit_message_text(
            _main_menu_text(query.from_user.first_name, lang),
            parse_mode="Markdown",
            reply_markup=_main_menu_keyboard(user_id, lang)
        )
        return


async def _show_quantity_prompt(query, user_id: int, product_name: str, *, back_to: str = "shop"):
    """Validate a product and show the quantity prompt without favorite controls."""
    lang = await _user_lang(user_id)
    _shop_flow.pop(user_id, None)
    _clear_wallet_flow_if_available(user_id)

    product = await db.get_product(product_name)
    if not product:
        await query.edit_message_text(
            tr(lang, "product_not_found"),
            reply_markup=InlineKeyboardMarkup([_back_button(lang)])
        )
        return

    back_keyboard = _product_buy_keyboard(back_to, lang)

    if product.get("enabled", True) is False:
        await query.edit_message_text(
            tr(lang, "product_unavailable", product=product_name),
            parse_mode="Markdown",
            reply_markup=back_keyboard
        )
        return

    payment_settings = await db.get_payment_settings()
    if not (
        _payment_enabled(payment_settings, "usdt")
        or _payment_enabled(payment_settings, "upi")
        or _payment_enabled(payment_settings, "polygon")
        or _payment_enabled(payment_settings, "binance")
        or _payment_enabled(payment_settings, "wallet_usdt")
        or _payment_enabled(payment_settings, "wallet_inr")
    ):
        await query.edit_message_text(
            tr(lang, "no_payment_methods"),
            reply_markup=InlineKeyboardMarkup([_back_button(lang)])
        )
        return

    stock_count = await db.get_available_stock_count(product_name)
    is_preorder = False
    preorder_capacity = 0
    preorder_max_qty = 0

    if stock_count <= 0:
        status = await _preorder_status_for_user(product, user_id)
        preorder_capacity = int(status["total_remaining"] or 0)
        preorder_max_qty = int(status["max_qty"] or 0)
        user_remaining = int(status["user_remaining"] or 0)
        user_limit = int(status["user_limit"] or 0)
        user_active_qty = int(status["active_user_qty"] or 0)
        min_qty_for_product, _ = _get_order_quantity_limits(product, None)

        if not db.product_preorder_enabled(product):
            await query.edit_message_text(
                tr(lang, "product_out_stock", product=product_name),
                parse_mode="Markdown",
                reply_markup=back_keyboard
            )
            return

        if user_remaining < min_qty_for_product and preorder_capacity >= min_qty_for_product:
            await query.edit_message_text(
                tr(lang, "preorder_user_limit_reached", product=product_name, active=user_active_qty, limit=user_limit),
                parse_mode="Markdown",
                reply_markup=back_keyboard
            )
            return

        if preorder_capacity < min_qty_for_product or preorder_max_qty < min_qty_for_product:
            await query.edit_message_text(
                tr(lang, "preorder_full", product=product_name),
                parse_mode="Markdown",
                reply_markup=back_keyboard
            )
            return

        is_preorder = True
    else:
        min_qty, max_qty = _get_order_quantity_limits(product, stock_count)
        if stock_count < min_qty:
            await query.edit_message_text(
                tr(lang, "product_not_enough_stock", product=product_name),
                parse_mode="Markdown",
                reply_markup=back_keyboard
            )
            return

    if is_preorder:
        _shop_flow[user_id] = {"step": "quantity", "product": product, "is_preorder": True}
        await query.edit_message_text(
            compact_blank_lines(tr(
                lang,
                "preorder_quantity_prompt",
                product=product['name'],
                description=_product_details_block(product, lang),
                price=_price_label(product, payment_settings, lang=lang),
                remaining=preorder_capacity,
                user_remaining=user_remaining,
                user_limit=user_limit,
                active=user_active_qty,
                max_qty=preorder_max_qty,
            )),
            parse_mode="Markdown",
            reply_markup=back_keyboard
        )
        return

    _shop_flow[user_id] = {"step": "quantity", "product": product}
    await query.edit_message_text(
        compact_blank_lines(tr(lang, "quantity_prompt", product=product['name'], description=_product_details_block(product, lang), price=_price_label(product, payment_settings, lang=lang), stock=stock_count)),
        parse_mode="Markdown",
        reply_markup=back_keyboard
    )


async def handle_product_select(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles selecting a product from the normal shop list."""
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    await db.upsert_user(user_id, query.from_user.username or "")
    lang = await _user_lang(user_id)

    if await db.is_blocked(user_id):
        await query.edit_message_text(tr(lang, "blocked"))
        return

    _, product = await _product_from_callback(query.data or "")
    if not product:
        await query.edit_message_text(
            tr(lang, "product_not_found"),
            reply_markup=InlineKeyboardMarkup([_back_button(lang)])
        )
        return
    await _show_quantity_prompt(query, user_id, product["name"], back_to="shop")

async def handle_quantity_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Returns True if the message was consumed as quantity input."""
    user_id = update.effective_user.id
    lang = await _user_lang(user_id)
    state = _shop_flow.get(user_id)
    if not state or state.get("step") != "quantity":
        return False

    product = state["product"]
    fresh_product = await db.get_product(product["name"])
    if not fresh_product or fresh_product.get("enabled", True) is False:
        _shop_flow.pop(user_id, None)
        await update.message.reply_text(tr(lang, "product_now_unavailable"))
        return True
    product = fresh_product

    try:
        qty = int((update.message.text or "").strip())
    except ValueError:
        await update.message.reply_text(tr(lang, "enter_valid_number"))
        return True

    stock_count = await db.get_available_stock_count(product["name"])
    is_preorder = bool(state.get("is_preorder"))
    if is_preorder:
        min_qty, product_max_qty = _get_order_quantity_limits(product, None)
        status = await _preorder_status_for_user(product, user_id)
        preorder_capacity = int(status["total_remaining"] or 0)
        user_remaining = int(status["user_remaining"] or 0)
        user_limit = int(status["user_limit"] or 0)
        user_active_qty = int(status["active_user_qty"] or 0)
        max_qty = min(product_max_qty, int(status["max_qty"] or 0))
        if not db.product_preorder_enabled(product) or preorder_capacity < min_qty or max_qty < min_qty:
            _shop_flow.pop(user_id, None)
            if user_remaining < min_qty and preorder_capacity >= min_qty:
                await update.message.reply_text(tr(lang, "preorder_user_limit_reached", product=product["name"], active=user_active_qty, limit=user_limit), parse_mode="Markdown")
            else:
                await update.message.reply_text(tr(lang, "preorder_full", product=product["name"]), parse_mode="Markdown")
            return True
        if qty < min_qty:
            await update.message.reply_text(tr(lang, "min_purchase", min_qty=min_qty))
            return True
        if qty > max_qty:
            await update.message.reply_text(tr(lang, "preorder_max_purchase", max_qty=max_qty), parse_mode="Markdown")
            return True
    else:
        min_qty, max_qty = _get_order_quantity_limits(product, stock_count)
        if qty < min_qty:
            await update.message.reply_text(
                tr(lang, "min_purchase", min_qty=min_qty),
            )
            return True

        if stock_count < min_qty:
            _shop_flow.pop(user_id, None)
            await update.message.reply_text(
                tr(lang, "not_enough_stock_now")
            )
            return True

        if qty > max_qty:
            await update.message.reply_text(
                tr(lang, "max_purchase", max_qty=max_qty),
                parse_mode="Markdown"
            )
            return True

        if qty > stock_count:
            await update.message.reply_text(
                tr(lang, "only_stock_available", stock=stock_count),
                parse_mode="Markdown"
            )
            return True

    payment_settings = await db.get_payment_settings()
    total_inr = round(float(product.get("price_inr") or 0) * qty, 2)
    total_usdt = round(float(product.get("price_usdt") or 0) * qty, 2)

    user = await db.get_user(user_id)
    wallet_inr = float(user.get("wallet_inr", 0.0) if user else 0.0)
    wallet_usdt = float(user.get("wallet_usdt", 0.0) if user else 0.0)

    wallet_inr_ok = wallet_inr >= total_inr and _payment_enabled(payment_settings, "wallet_inr")
    wallet_usdt_ok = wallet_usdt >= total_usdt and _payment_enabled(payment_settings, "wallet_usdt")

    _shop_flow[user_id] = {
        "step": "payment",
        "product": product,
        "quantity": qty,
        "total_inr": total_inr,
        "total_usdt": total_usdt,
        "is_preorder": is_preorder,
    }

    buttons = []
    if wallet_usdt_ok:
        buttons.append([InlineKeyboardButton(
            tr(lang, "wallet_usdt_available", balance=_compact_amount(wallet_usdt, 2)), callback_data="pay:wallet_usdt"
        )])
    if _payment_enabled(payment_settings, "usdt"):
        buttons.append([InlineKeyboardButton(tr(lang, "pay_usdt"), callback_data="pay:usdt")])
    if _payment_enabled(payment_settings, "polygon"):
        buttons.append([InlineKeyboardButton(tr(lang, "pay_polygon"), callback_data="pay:polygon")])
    if _payment_enabled(payment_settings, "binance"):
        buttons.append([InlineKeyboardButton(tr(lang, "pay_binance"), callback_data="pay:binance")])
    if wallet_inr_ok:
        buttons.append([InlineKeyboardButton(
            tr(lang, "wallet_inr_available", balance=f"{wallet_inr:.2f}"), callback_data="pay:wallet_inr"
        )])
    if _payment_enabled(payment_settings, "upi"):
        buttons.append([InlineKeyboardButton(tr(lang, "pay_upi"), callback_data="pay:upi")])

    if not buttons:
        _shop_flow.pop(user_id, None)
        await update.message.reply_text(
            tr(lang, "no_payment_methods"),
            reply_markup=InlineKeyboardMarkup([_back_button(lang)])
        )
        return True

    buttons.append([InlineKeyboardButton(tr(lang, "btn_cancel"), callback_data="pay:cancel")])

    summary_key = "preorder_order_summary" if is_preorder else "order_summary"
    await update.message.reply_text(
        tr(lang, summary_key, product=product['name'], quantity=qty, total=_price_label(product, payment_settings, quantity=qty, lang=lang)),
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(buttons)
    )
    return True


async def _finalize_wallet_order_after_charge(context: ContextTypes.DEFAULT_TYPE, user_id: int, order_id: str):
    """Finalize a wallet order after the user's balance has been deducted.

    The wallet balance is already charged at this point, so the order must
    become Delivered or Paid — Waiting for Stock. It should never remain the
    unpaid Pending state.
    """
    from handlers.payment import complete_order

    try:
        await complete_order(context.bot, user_id, order_id)
    except Exception as exc:
        import logging
        logging.getLogger(__name__).exception("Wallet order finalization failed for %s: %s", order_id, exc)
        try:
            await db.mark_order_pending_stock(order_id)
        except Exception:
            pass
        try:
            await context.bot.send_message(
                user_id,
                tr(await _user_lang(user_id), "wallet_paid_waiting_stock"),
            )
        except Exception:
            pass
        return

    # Final safety check: complete_order should have changed the status.
    order = await db.get_order(order_id)
    if order and order.get("status") == "pending":
        await db.mark_order_pending_stock(order_id)
        try:
            await context.bot.send_message(
                user_id,
                tr(await _user_lang(user_id), "wallet_paid_waiting_stock"),
            )
        except Exception:
            pass


def _preorder_create_error_text(lang: str, result: dict, product_name: str) -> str:
    key = str((result or {}).get("message_key") or "preorder_changed")
    if key == "preorder_full":
        return tr(lang, "preorder_full", product=product_name)
    if key == "preorder_user_limit_reached":
        return tr(
            lang,
            "preorder_user_limit_reached",
            product=product_name,
            active=int((result or {}).get("user_active") or 0),
            limit=int((result or {}).get("user_limit") or 0),
        )
    if key == "preorder_max_purchase":
        return tr(lang, "preorder_max_purchase", max_qty=int((result or {}).get("max_qty") or 0))
    if key == "min_purchase":
        return tr(lang, "min_purchase", min_qty=int((result or {}).get("min_qty") or 1))
    if key == "product_now_unavailable":
        return tr(lang, "product_now_unavailable")
    if key == "preorder_busy":
        return tr(lang, "preorder_busy")
    return tr(lang, "preorder_changed")


async def _create_checkout_order(
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
    username: str,
    product: dict,
    qty: int,
    method: str,
    total_inr: float,
    total_usdt: float,
    is_preorder: bool,
    lang: str,
) -> str | None:
    product_name = str(product.get("name") or "")
    if not is_preorder:
        return await db.create_order(user_id, product_name, qty, method, total_inr, total_usdt, username, is_preorder=False)

    result = await db.create_preorder_order_with_limits(
        user_id,
        product_name,
        qty,
        method,
        total_inr,
        total_usdt,
        username,
    )
    if result.get("ok"):
        return str(result.get("order_id"))
    try:
        await context.bot.send_message(
            user_id,
            _preorder_create_error_text(lang, result, product_name),
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([_back_button(lang)]),
        )
    except Exception:
        pass
    return None


async def handle_payment_method_select(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles payment method button selection."""
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    lang = await _user_lang(user_id)

    method = query.data.split(":", 1)[1]
    state = _shop_flow.get(user_id)

    if not state or state.get("step") != "payment":
        await query.edit_message_text(tr(lang, "session_expired_shop"))
        return

    if method == "cancel":
        _shop_flow.pop(user_id, None)
        await query.edit_message_text(tr(lang, "order_cancelled"))
        return

    product = state["product"]
    latest_product = await db.get_product(product["name"])
    if not latest_product or latest_product.get("enabled", True) is False:
        _shop_flow.pop(user_id, None)
        await query.edit_message_text(tr(lang, "product_now_unavailable"))
        return
    product = latest_product
    qty = state["quantity"]
    is_preorder = bool(state.get("is_preorder"))
    stock_count = await db.get_available_stock_count(product["name"])
    if is_preorder:
        min_qty, product_max_qty = _get_order_quantity_limits(product, None)
        status = await _preorder_status_for_user(product, user_id)
        preorder_capacity = int(status["total_remaining"] or 0)
        user_remaining = int(status["user_remaining"] or 0)
        max_qty = min(product_max_qty, int(status["max_qty"] or 0))
        if (
            not db.product_preorder_enabled(product)
            or preorder_capacity < min_qty
            or user_remaining < min_qty
            or qty < min_qty
            or qty > max_qty
        ):
            _shop_flow.pop(user_id, None)
            await query.edit_message_text(
                tr(lang, "preorder_changed"),
                reply_markup=InlineKeyboardMarkup([_back_button(lang)])
            )
            return
    else:
        min_qty, max_qty = _get_order_quantity_limits(product, stock_count)
        if stock_count < min_qty or qty < min_qty or qty > max_qty or qty > stock_count:
            _shop_flow.pop(user_id, None)
            await query.edit_message_text(
                tr(lang, "qty_or_stock_changed"),
                reply_markup=InlineKeyboardMarkup([_back_button(lang)])
            )
            return
    total_inr = state["total_inr"]
    total_usdt = state["total_usdt"]

    payment_settings = await db.get_payment_settings()
    if not db.payment_method_enabled(payment_settings, method):
        await query.edit_message_text(tr(lang, "method_unavailable"))
        _shop_flow.pop(user_id, None)
        return

    # Delete the order summary message
    try:
        await query.delete_message()
    except Exception:
        pass

    _shop_flow.pop(user_id, None)

    # ── Wallet payment (instant, no verification) ──
    if method == "wallet_inr":
        order_id = await _create_checkout_order(context, user_id, query.from_user.username or "", product, qty, "wallet_inr", total_inr, 0, is_preorder, lang)
        if not order_id:
            return
        success = await db.deduct_wallet_inr(user_id, total_inr)
        if not success:
            await db.update_order_status(order_id, "cancelled")
            await context.bot.send_message(user_id, tr(lang, "wallet_insufficient_inr"))
            return
        await _finalize_wallet_order_after_charge(context, user_id, order_id)
        return

    if method == "wallet_usdt":
        order_id = await _create_checkout_order(context, user_id, query.from_user.username or "", product, qty, "wallet_usdt", 0, total_usdt, is_preorder, lang)
        if not order_id:
            return
        success = await db.deduct_wallet_usdt(user_id, total_usdt)
        if not success:
            await db.update_order_status(order_id, "cancelled")
            await context.bot.send_message(user_id, tr(lang, "wallet_insufficient_usdt"))
            return
        await _finalize_wallet_order_after_charge(context, user_id, order_id)
        return

    # ── External payment ──
    if method in {"usdt", "polygon"}:
        try:
            unique_usdt = await generate_unique_usdt_amount(total_usdt)
        except UniqueUsdtAmountUnavailable:
            await context.bot.send_message(
                user_id,
                "⚠️ Too many active USDT payments are using this exact amount right now. Please try again in a few minutes."
            )
            return
        order_id = await _create_checkout_order(context, user_id, query.from_user.username or "", product, qty, method, total_inr, total_usdt, is_preorder, lang)
        if not order_id:
            return
        description = f"{tr(lang, 'order_id')} `{order_id}` | {product['name']} x{qty}"
        await db.create_pending_payment(
            user_id=user_id, ref_id=order_id, pay_type="order",
            method=method, expected_inr=total_inr, expected_usdt=total_usdt,
            unique_usdt=unique_usdt
        )
        from handlers.payment import initiate_usdt_payment
        await initiate_usdt_payment(
            None, context, user_id, order_id,
            amount_inr=total_inr, unique_usdt=unique_usdt,
            description=description, method=method
        )

    elif method == "upi":
        order_id = await _create_checkout_order(context, user_id, query.from_user.username or "", product, qty, method, total_inr, total_usdt, is_preorder, lang)
        if not order_id:
            return
        description = f"{tr(lang, 'order_id')} `{order_id}` | {product['name']} x{qty}"
        await db.create_pending_payment(
            user_id=user_id, ref_id=order_id, pay_type="order",
            method="upi", expected_inr=total_inr, expected_usdt=total_usdt,
            unique_usdt=0.0
        )
        from handlers.payment import initiate_upi_payment
        await initiate_upi_payment(
            None, context, user_id, order_id,
            amount_inr=total_inr, description=description
        )

    elif method == "binance":
        try:
            unique_usdt = await generate_unique_usdt_amount(total_usdt)
        except UniqueUsdtAmountUnavailable:
            await context.bot.send_message(
                user_id,
                "⚠️ Too many active Binance Pay payments are using this exact amount right now. Please try again in a few minutes."
            )
            return
        order_id = await _create_checkout_order(context, user_id, query.from_user.username or "", product, qty, method, total_inr, total_usdt, is_preorder, lang)
        if not order_id:
            return
        description = f"{tr(lang, 'order_id')} `{order_id}` | {product['name']} x{qty}"
        await db.create_pending_payment(
            user_id=user_id, ref_id=order_id, pay_type="order",
            method="binance", expected_inr=0.0, expected_usdt=total_usdt,
            unique_usdt=unique_usdt
        )
        from handlers.payment import initiate_binance_payment
        await initiate_binance_payment(
            None, context, user_id, order_id,
            amount_usdt=unique_usdt, description=description
        )


async def handle_favorite_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Favorite-tab actions: quick-buy favorites or edit the favorite list."""
    query = update.callback_query
    user_id = query.from_user.id
    lang = await _user_lang(user_id)
    data = query.data or ""
    action, product = await _product_from_callback(data)
    product_name = str((product or {}).get("name") or "")

    if await db.is_blocked(user_id):
        await query.answer(tr(lang, "blocked"), show_alert=True)
        await query.edit_message_text(tr(lang, "blocked"))
        return

    if action in {"favorite_buy", "favorite_product", "favorite_buy_id", "favorite_product_id"}:
        await query.answer()
        if not product:
            await query.edit_message_text(
                tr(lang, "product_not_found"),
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(tr(lang, "btn_back_favorites"), callback_data="nav:favorites")], _back_button(lang)])
            )
            return
        await _show_quantity_prompt(query, user_id, product_name, back_to="favorites")
        return

    if action in {"fav", "fav_id"}:
        if not product or product.get("enabled", True) is False:
            await query.answer(tr(lang, "favorite_unavailable"), show_alert=True)
            await query.edit_message_text(
                tr(lang, "product_now_unavailable"),
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(tr(lang, "btn_back_favorites"), callback_data="nav:favorites")], _back_button(lang)])
            )
            return

        is_favorite = await db.toggle_product_favorite(user_id, product["name"])
        await query.answer(tr(lang, "favorite_added") if is_favorite else tr(lang, "favorite_removed"))

        favorite_names = await db.get_user_favorite_products(user_id)
        payment_settings = await db.get_payment_settings()
        products = await db.get_all_products_with_availability()
        await query.edit_message_text(
            tr(lang, "favorites_edit_title"),
            parse_mode="Markdown",
            reply_markup=_favorites_edit_keyboard(products, payment_settings, favorite_names, lang)
        )
        return

    await query.answer(tr(lang, "unknown_favorite"), show_alert=True)
