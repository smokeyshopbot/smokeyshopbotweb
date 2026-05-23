"""
handlers/admin.py — All admin commands and callback handlers.

Admin Commands:
  /addproduct <name> <price_inr> <price_usdt>  — Add a new product
  /removeproduct <name>                         — Remove a product
  /setprice <name> <price_inr> <price_usdt>    — Update product prices
  /addstock <name>                              — Start bulk stock addition session
  /removestock <name>                           — Start specific/bulk stock removal session
  /cancel                                      — Abort active stock add/remove session
  /clearstock <name>                            — Clear all stock for a product
  /disableproduct <name>                        — Hide a product from shop
  /enableproduct <name>                         — Show a product in shop again
  /listproducts                                 — List all products with stock counts
  /listusers                                    — List all users
  /pendingorders                                — View paid orders waiting for stock
  /findorder <order_id>                         — Find one order by ID
  /stats                                        — View bot stats
  /ranking                                      — View top buyers, 10 per page
  /userstats <user_id>                          — View stats for one user
  /userorders <user_id>                         — View a user's orders with get-order shortcuts
  /userwallethistory <user_id>                   — View a user's wallet top-up logs
  /maintenance <on|off|status>                  — Toggle maintenance mode
  /addbalance <user_id> <inr|usdt> <amount> [note] — Add wallet balance for a user
  /removebalance <user_id> <inr|usdt> <amount> [note] — Remove wallet balance from a user
  /blockuser <user_id>                          — Block a user
  /unblockuser <user_id>                        — Unblock a user
  /broadcast <message>                          — Broadcast to all unblocked users
  /recentorders                                 — View recent orders
  /admincommands                                — Show admin commands
"""

from datetime import datetime, timezone
import html
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from utils.messages import admin_commands_text, compact_blank_lines, md_code, telegram_id, order_amount_text, aggregate_amount_text
from utils.admin_notify import send_admin_message, send_admin_photo
from utils.bscscan import get_usdt_network_label, normalize_usdt_network
from utils.wallet_history import (
    WALLET_HISTORY_PAGE_SIZE,
    format_wallet_history_text,
    wallet_history_keyboard,
)

import database as db
from config import (
    ADMIN_IDS,
    LOW_STOCK_ALERT_THRESHOLD,
    RESTOCK_HIGH_STOCK_THRESHOLD,
    RESTOCK_LONG_NOTIFICATION_COOLDOWN_MINUTES,
    RESTOCK_NOTIFICATION_COOLDOWN_MINUTES,
    is_admin_id,
)

# Track admins waiting to add stock: { user_id: product_name }
_awaiting_stock: dict[int, str] = {}

# Track admins waiting to remove stock: { user_id: product_name }
_awaiting_stock_removal: dict[int, str] = {}

RANKING_PAGE_SIZE = 10
ADMIN_PRODUCTS_PAGE_SIZE = 10
ADMIN_USERS_PAGE_SIZE = 10
ADMIN_USER_ORDERS_PAGE_SIZE = 10


def _html_escape(value) -> str:
    if value is None:
        return "N/A"
    return html.escape(str(value), quote=False)


def _html_code(value) -> str:
    if value is None or value == "":
        value = "N/A"
    return f"<code>{html.escape(str(value), quote=False)}</code>"


def _compact_amount(value, max_decimals: int = 2) -> str:
    try:
        amount = float(value or 0)
    except (TypeError, ValueError):
        amount = 0.0
    return f"{amount:.{max_decimals}f}"


def _price_inr(value) -> str:
    return f"₹{_compact_amount(value, 2)}"


def _price_usdt(value) -> str:
    return f"${_compact_amount(value, 2)} USDT"

def _product_notify_markup(product: dict | None, product_name: str) -> InlineKeyboardMarkup:
    callback_data = f"product_id:{product.get('_id')}" if product and product.get("_id") else f"product:{product_name}"
    return InlineKeyboardMarkup([[InlineKeyboardButton("🛒 Buy Now", callback_data=callback_data)]])


def _parse_admin_id_csv(raw: str) -> set[int]:
    ids: set[int] = set()
    for part in str(raw or "").split(","):
        try:
            uid = int(part.strip())
        except (TypeError, ValueError):
            continue
        if uid:
            ids.add(uid)
    return ids


async def _get_admin_id_set() -> set[int]:
    ids = {int(uid) for uid in ADMIN_IDS if uid}
    try:
        settings = await db.get_secret_settings()
        ids.update(_parse_admin_id_csv(str(settings.get("admin_ids") or "")))
    except Exception:
        pass
    return ids


async def _notify_active_users(
    bot,
    text: str,
    *,
    product_name: str,
    parse_mode: str = "HTML",
    admin_only: bool = False,
    include_admins: bool = True,
) -> int:
    users = await db.get_all_users()
    admin_ids = await _get_admin_id_set()
    sent = 0
    product = await db.get_product(product_name)
    markup = _product_notify_markup(product, product_name)
    for user in users:
        try:
            user_id = int(user.get("user_id", 0) or 0)
        except Exception:
            user_id = 0
        if not user_id:
            continue
        is_admin_target = user_id in admin_ids
        if admin_only and not is_admin_target:
            continue
        if not include_admins and is_admin_target:
            continue
        try:
            await bot.send_message(user_id, text, parse_mode=parse_mode, reply_markup=markup)
            sent += 1
        except Exception:
            continue
    return sent


async def flush_maintenance_notifications(bot) -> dict[str, int]:
    """Send queued product/stock/price notifications after maintenance is OFF."""
    rows = await db.get_maintenance_notifications(limit=500)
    sent = 0
    processed = 0
    for row in rows:
        payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
        kind = str(row.get("kind") or "")
        product_name = str(row.get("product_name") or "").strip()
        try:
            if kind == "new_product":
                product = await db.get_product(product_name)
                sent += await notify_users_new_product(bot, product or payload or {"name": product_name}, include_admins=False)
            elif kind == "new_stock":
                sent += await notify_users_new_stock(bot, product_name, payload.get("available_stock"), include_admins=False)
            elif kind == "price_drop":
                product = await db.get_product(product_name)
                if product:
                    sent += await notify_users_price_drop(
                        bot,
                        product,
                        old_inr=payload.get("old_inr"),
                        new_inr=payload.get("new_inr"),
                        old_usdt=payload.get("old_usdt"),
                        new_usdt=payload.get("new_usdt"),
                        include_admins=False,
                    )
            processed += 1
        finally:
            await db.delete_maintenance_notification(row.get("_id"))
    return {"processed": processed, "sent": sent}


async def _new_product_notification_text(product: dict, product_name: str) -> str:
    product_label = html.escape(product_name, quote=False)
    lines = []
    try:
        if product.get("price_inr") is not None:
            lines.append(f"💰 {_price_inr(product.get('price_inr'))}")
    except Exception:
        pass
    try:
        if product.get("price_usdt") is not None:
            lines.append(f"💰 {_price_usdt(product.get('price_usdt'))}")
    except Exception:
        pass
    price_lines = "\n".join(html.escape(line, quote=False) for line in lines)
    return (
        "🆕 <b>New Product Added!</b>\n\n"
        f"<b>{product_label}</b> is available now.\n"
        f"{price_lines}\n\n"
        "Tap below to buy."
    )


async def notify_users_new_product(bot, product: dict, *, include_admins: bool = True) -> int:
    if not product or product.get("enabled", True) is False:
        return 0
    product_name = str(product.get("name") or "Product")
    text = await _new_product_notification_text(product, product_name)
    if await db.is_maintenance_mode():
        await db.queue_maintenance_notification(
            "new_product",
            product_name,
            {
                "price_inr": product.get("price_inr"),
                "price_usdt": product.get("price_usdt"),
            },
        )
        # During maintenance, admin/tester IDs receive the notification now for testing.
        # Normal users receive the queued product notification only after maintenance is OFF.
        return await _notify_active_users(bot, text, product_name=product_name, admin_only=True)
    return await _notify_active_users(bot, text, product_name=product_name, include_admins=include_admins)


async def notify_users_new_stock(bot, product_name: str, available_stock: int | None = None, *, include_admins: bool = True) -> int:
    product = await db.get_product(product_name)
    if not product or product.get("enabled", True) is False:
        return 0
    if available_stock is None:
        available_stock = await db.get_available_stock_count(product.get("name", product_name))
    try:
        available_stock = int(available_stock or 0)
    except Exception:
        available_stock = 0
    if available_stock <= 0:
        return 0
    clean_name = str(product.get("name") or product_name)
    product_label = html.escape(clean_name, quote=False)
    text = (
        "📦 <b>Fresh Stock Added!</b>\n\n"
        f"<b>{product_label}</b> is available now.\n"
        f"🛒 Available stock: <b>{available_stock}</b>\n\n"
        "Tap below to buy."
    )
    if await db.is_maintenance_mode():
        await db.queue_maintenance_notification("new_stock", clean_name, {"available_stock": available_stock})
        return await _notify_active_users(bot, text, product_name=clean_name, admin_only=True)
    return await _notify_active_users(bot, text, product_name=clean_name, include_admins=include_admins)


async def notify_users_price_drop(bot, product: dict, *, old_inr=None, new_inr=None, old_usdt=None, new_usdt=None, include_admins: bool = True) -> int:
    if not product or product.get("enabled", True) is False:
        return 0
    product_name = str(product.get("name") or "Product")
    settings = await db.get_payment_settings()
    lines = []
    try:
        if db.payment_method_enabled(settings, "upi") and old_inr is not None and new_inr is not None and float(new_inr) < float(old_inr):
            lines.append(f"{_price_inr(old_inr)} → {_price_inr(new_inr)}")
    except Exception:
        pass
    try:
        if db.payment_method_enabled(settings, "wallet_usdt") and old_usdt is not None and new_usdt is not None and float(new_usdt) < float(old_usdt):
            lines.append(f"{_price_usdt(old_usdt)} → {_price_usdt(new_usdt)}")
    except Exception:
        pass
    if not lines:
        return 0
    product_label = html.escape(product_name, quote=False)
    text = (
        "🔻 <b>Price Dropped!</b>\n\n"
        f"<b>{product_label}</b> price is lower now.\n"
        + "\n".join(f"💰 {html.escape(line, quote=False)}" for line in lines)
        + "\n\nTap below to buy."
    )
    if await db.is_maintenance_mode():
        await db.queue_maintenance_notification(
            "price_drop",
            product_name,
            {"old_inr": old_inr, "new_inr": new_inr, "old_usdt": old_usdt, "new_usdt": new_usdt},
        )
        return await _notify_active_users(bot, text, product_name=product_name, admin_only=True)
    return await _notify_active_users(bot, text, product_name=product_name, include_admins=include_admins)

def is_admin(user_id: int) -> bool:
    return is_admin_id(user_id)


def admin_only(func):
    """Decorator to restrict handlers to admin only."""
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not is_admin(update.effective_user.id):
            await update.message.reply_text("⛔ Unauthorized.")
            return
        return await func(update, context)
    return wrapper


# ─────────────────────────── COMMANDS ────────────────────────

@admin_only
async def cmd_admin_commands(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(admin_commands_text(), parse_mode="Markdown")




def _format_dt(value) -> str:
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


def _safe_int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _newest_sort_value(value) -> float:
    """Sortable timestamp value for newest-first admin lists."""
    if isinstance(value, datetime):
        dt = value
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


def _status_label(status: str) -> str:
    return {
        "pending_stock": "Paid — Waiting for Stock",
        "pending": "Pending",
        "delivered": "Delivered",
        "failed": "Failed",
        "expired": "Expired",
        "confirmed": "Confirmed",
    }.get((status or "unknown").lower(), str(status or "unknown").replace("_", " ").title())


def _status_emoji(status: str) -> str:
    return {
        "pending": "⏳",
        "pending_stock": "⏳",
        "delivered": "✅",
        "failed": "❌",
        "expired": "❌",
        "confirmed": "✅",
    }.get((status or "").lower(), "❓")


def _format_order_detail(order: dict, *, include_items: bool = True) -> str:
    if not order:
        return "❌ Order not found."
    lines = [
        "🔎 *Order Details*",
        "",
        f"Order ID: `{order.get('order_id', 'N/A')}`",
        f"User ID: {telegram_id(order.get('user_id', 'N/A'))}",
        f"Product: *{order.get('product_name', 'N/A')}* x{order.get('quantity', 0)}",
        f"Payment Method: `{str(order.get('payment_method', 'N/A')).upper()}`",
        f"Amount: {order_amount_text(order)}",
        f"Status: {_status_emoji(order.get('status'))} {_status_label(order.get('status'))}",
        f"Created: {_format_dt(order.get('created_at'))}",
        f"Delivered: {_format_dt(order.get('delivered_at'))}",
    ]
    if include_items:
        items = order.get("items", []) or []
        if items:
            lines.append("\n🎁 *Items:*")
            for idx, item in enumerate(items[:10], 1):
                lines.append(f"{idx}. `{item}`")
            if len(items) > 10:
                lines.append(f"...and {len(items) - 10} more item(s)")
    return compact_blank_lines("\n".join(lines))


@admin_only
async def cmd_add_product(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if len(args) < 3:
        await update.message.reply_text(
            "Usage: `/addproduct <name> <price_inr> <price_usdt>`\n"
            "Example: `/addproduct Netflix 299 3.59`",
            parse_mode="Markdown"
        )
        return
    name = " ".join(args[:-2]).strip()
    try:
        price_inr = float(args[-2])
        price_usdt = float(args[-1])
    except ValueError:
        await update.message.reply_text("❌ Prices must be numbers.")
        return

    success = await db.add_product(name, price_inr, price_usdt)
    if success:
        product = await db.get_product(name) or {"name": name, "price_inr": price_inr, "price_usdt": price_usdt, "enabled": True}
        notified = await notify_users_new_product(context.bot, product)
        await update.message.reply_text(
            f"✅ Product *{name}* added!\n💰 ₹{price_inr} / ${price_usdt} USDT\n📦 Stock: 0\n🔔 Notified {notified} user(s).",
            parse_mode="Markdown"
        )
    else:
        await update.message.reply_text(f"❌ Product *{name}* already exists.", parse_mode="Markdown")


@admin_only
async def cmd_remove_product(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: `/removeproduct <name>`", parse_mode="Markdown")
        return
    name = " ".join(context.args)
    success = await db.remove_product(name)
    if success:
        await update.message.reply_text(f"✅ Product *{name}* removed.", parse_mode="Markdown")
    else:
        await update.message.reply_text(f"❌ Product *{name}* not found.", parse_mode="Markdown")


@admin_only
async def cmd_set_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if len(args) < 3:
        await update.message.reply_text(
            "Usage: `/setprice <name> <price_inr> <price_usdt>`",
            parse_mode="Markdown"
        )
        return
    name = " ".join(args[:-2]).strip()
    try:
        price_inr = float(args[-2])
        price_usdt = float(args[-1])
    except ValueError:
        await update.message.reply_text("❌ Prices must be numbers.")
        return

    product = await db.get_product(name)
    if not product:
        await update.message.reply_text(f"❌ Product *{name}* not found.", parse_mode="Markdown")
        return

    old_inr = product.get("price_inr")
    old_usdt = product.get("price_usdt")
    success = await db.update_product_price(product["name"], price_inr, price_usdt)
    if success:
        await notify_users_price_drop(
            context.bot,
            product,
            old_inr=old_inr,
            new_inr=price_inr,
            old_usdt=old_usdt,
            new_usdt=price_usdt,
        )
        await update.message.reply_text(
            f"✅ *{product['name']}* prices updated!\n💰 ₹{price_inr} / ${price_usdt} USDT",
            parse_mode="Markdown"
        )
    else:
        await update.message.reply_text(f"❌ Product *{name}* not found.", parse_mode="Markdown")


@admin_only
async def cmd_add_stock(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: `/addstock <product_name>`", parse_mode="Markdown")
        return
    name = " ".join(context.args)
    product = await db.get_product(name)
    if not product:
        await update.message.reply_text(f"❌ Product *{name}* not found.", parse_mode="Markdown")
        return

    _awaiting_stock_removal.pop(update.effective_user.id, None)
    _awaiting_stock[update.effective_user.id] = product["name"]
    await update.message.reply_text(
        f"📦 Ready to add stock for *{product['name']}*\n\n"
        f"Send your stock now. Each item separated by `---`\n\n"
        f"*Examples:*\n"
        f"Single lines:\n`key1\\n---\\nkey2\\n---\\nkey3`\n\n"
        f"Multi-line (ID+Pass):\n`user1\\npass1\\n---\\nuser2\\npass2`\n\n"
        f"Send `/cancel` to abort.",
        parse_mode="Markdown"
    )


@admin_only
async def cmd_remove_stock(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start a session to remove exact stock item(s) from a product."""
    if not context.args:
        await update.message.reply_text(
            "Usage: `/removestock <product_name>`\n"
            "Example: `/removestock Netflix`",
            parse_mode="Markdown"
        )
        return

    name = " ".join(context.args)
    product = await db.get_product(name)
    if not product:
        await update.message.reply_text(f"❌ Product *{name}* not found.", parse_mode="Markdown")
        return

    current_stock = len(product.get("stock", []))
    if current_stock <= 0:
        await update.message.reply_text(f"⚠️ *{product['name']}* has no stock to remove.", parse_mode="Markdown")
        return

    _awaiting_stock.pop(update.effective_user.id, None)
    _awaiting_stock_removal[update.effective_user.id] = product["name"]
    await update.message.reply_text(
        f"🗑 Ready to remove stock from *{product['name']}*\n"
        f"📦 Current stock: *{current_stock}*\n\n"
        f"Send the exact stock item you want to remove.\n"
        f"For bulk removal, separate each item with `---`\n\n"
        f"*Examples:*\n"
        f"Single item:\n`key1`\n\n"
        f"Bulk items:\n`key1\n---\nkey2\n---\nuser3\\npass3`\n\n"
        f"Send `/cancel` to abort.",
        parse_mode="Markdown"
    )


@admin_only
async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    adding = _awaiting_stock.pop(update.effective_user.id, None)
    removing = _awaiting_stock_removal.pop(update.effective_user.id, None)
    if adding or removing:
        await update.message.reply_text("❌ Stock session aborted.")
    else:
        await update.message.reply_text("No active stock session to abort.")


# Backward-compatible internal alias. Not registered as a public command.
cmd_cancel_stock = cmd_cancel


@admin_only
async def cmd_clear_stock(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: `/clearstock <name>`", parse_mode="Markdown")
        return
    name = " ".join(context.args)
    product = await db.get_product(name)
    if not product:
        await update.message.reply_text(f"❌ Product *{name}* not found.", parse_mode="Markdown")
        return
    await db.clear_stock(name)
    from handlers.payment import notify_low_stock_if_needed
    await notify_low_stock_if_needed(context.bot, name)
    await update.message.reply_text(f"🗑 Stock cleared for *{name}*.", parse_mode="Markdown")


@admin_only
async def cmd_disable_product(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: `/disableproduct <name>`", parse_mode="Markdown")
        return
    name = " ".join(context.args)
    product = await db.get_product(name)
    if not product:
        await update.message.reply_text(f"❌ Product *{name}* not found.", parse_mode="Markdown")
        return
    await db.set_product_enabled(name, False)
    await update.message.reply_text(f"🚫 Product *{product['name']}* disabled and hidden from /shop.", parse_mode="Markdown")


@admin_only
async def cmd_enable_product(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: `/enableproduct <name>`", parse_mode="Markdown")
        return
    name = " ".join(context.args)
    product = await db.get_product(name)
    if not product:
        await update.message.reply_text(f"❌ Product *{name}* not found.", parse_mode="Markdown")
        return
    await db.set_product_enabled(name, True)
    await update.message.reply_text(f"✅ Product *{product['name']}* enabled and visible in /shop.", parse_mode="Markdown")


def _paginate_list(items: list, page: int, page_size: int) -> tuple[list, int, int]:
    total_count = len(items)
    total_pages = max(1, (total_count + page_size - 1) // page_size) if total_count else 1
    page = max(0, min(int(page or 0), total_pages - 1))
    start = page * page_size
    return items[start:start + page_size], page, total_pages


def _admin_products_keyboard(page: int, total_pages: int) -> InlineKeyboardMarkup:
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️ Previous", callback_data=f"adminproducts:{page - 1}"))
    if page + 1 < total_pages:
        nav.append(InlineKeyboardButton("➡️ Next", callback_data=f"adminproducts:{page + 1}"))
    return InlineKeyboardMarkup([nav] if nav else [])


def _admin_users_keyboard(page: int, total_pages: int) -> InlineKeyboardMarkup:
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️ Previous", callback_data=f"adminusers:{page - 1}"))
    if page + 1 < total_pages:
        nav.append(InlineKeyboardButton("➡️ Next", callback_data=f"adminusers:{page + 1}"))
    return InlineKeyboardMarkup([nav] if nav else [])


def _format_products_page(products: list[dict], page: int, total_pages: int, total_count: int) -> str:
    if not products:
        return "📦 All Products\n\nNo products found."
    lines = [
        f"📦 All Products — Page {page + 1}/{total_pages}",
        f"Showing 10 products per page, newest first. Total products: {total_count}",
        "",
    ]
    for p in products:
        enabled = p.get("enabled", True)
        status = "✅ Enabled" if enabled else "🚫 Disabled"
        actual = int(p.get("actual_stock", len(p.get("stock", []) or [])) or 0)
        pending = int(p.get("pending_stock_quantity", 0) or 0)
        available = int(p.get("available_stock", max(0, actual - pending)) or 0)
        low = " ⚠️ LOW" if actual < 10 else ""
        lines.extend([
            "",
            f"Product: {p.get('name', 'N/A')}",
            f"Status: {status}{low}",
            f"Price: {_price_inr(p.get('price_inr'))} / {_price_usdt(p.get('price_usdt'))}",
            f"Stock: {actual}",
            f"Pending reserved: {pending}",
            f"Available: {available}",
            "",
        ])
    return compact_blank_lines("\n".join(lines))


def _format_users_page(users: list[dict], page: int, total_pages: int, total_count: int) -> str:
    if not users:
        return "👥 Users\n\nNo users yet."
    lines = [
        f"👥 Users — Page {page + 1}/{total_pages}",
        f"Showing 10 users per page, newest first. Total users: {total_count}",
        "",
    ]
    for u in users:
        status = "🚫 Blocked" if u.get("blocked") else "✅ Active"
        username = md_code(f"@{u.get('username')}") if u.get("username") else "No username"
        lines.extend([
            "",
            f"User ID: {telegram_id(u.get('user_id', 'N/A'))}",
            f"Username: {username}",
            f"Status: {status}",
            f"INR Wallet: ₹{float(u.get('wallet_inr') or 0):.2f}",
            f"USDT Wallet: ${float(u.get('wallet_usdt') or 0):.2f}",
            "",
        ])
    return compact_blank_lines("\n".join(lines))


@admin_only
async def cmd_list_products(update: Update, context: ContextTypes.DEFAULT_TYPE):
    products = await db.get_all_products_with_availability(include_disabled=True)
    products.sort(key=lambda p: _newest_sort_value(p.get("created_at")), reverse=True)
    page_items, page, total_pages = _paginate_list(products, 0, ADMIN_PRODUCTS_PAGE_SIZE)
    await update.message.reply_text(
        _format_products_page(page_items, page, total_pages, len(products)),
        reply_markup=_admin_products_keyboard(page, total_pages),
    )


async def handle_admin_products_page(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not is_admin(query.from_user.id):
        await query.answer("⛔ Unauthorized.", show_alert=True)
        return
    await query.answer()
    try:
        page = int(query.data.split(":", 1)[1])
    except Exception:
        page = 0
    products = await db.get_all_products_with_availability(include_disabled=True)
    products.sort(key=lambda p: _newest_sort_value(p.get("created_at")), reverse=True)
    page_items, page, total_pages = _paginate_list(products, page, ADMIN_PRODUCTS_PAGE_SIZE)
    await query.edit_message_text(
        _format_products_page(page_items, page, total_pages, len(products)),
        reply_markup=_admin_products_keyboard(page, total_pages),
    )


@admin_only
async def cmd_list_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    users = await db.get_all_users_including_blocked()
    users.sort(key=lambda u: _newest_sort_value(u.get("joined_at")), reverse=True)
    page_items, page, total_pages = _paginate_list(users, 0, ADMIN_USERS_PAGE_SIZE)
    await update.message.reply_text(
        _format_users_page(page_items, page, total_pages, len(users)),
        parse_mode="Markdown",
        reply_markup=_admin_users_keyboard(page, total_pages),
    )


async def handle_admin_users_page(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not is_admin(query.from_user.id):
        await query.answer("⛔ Unauthorized.", show_alert=True)
        return
    await query.answer()
    try:
        page = int(query.data.split(":", 1)[1])
    except Exception:
        page = 0
    users = await db.get_all_users_including_blocked()
    users.sort(key=lambda u: _newest_sort_value(u.get("joined_at")), reverse=True)
    page_items, page, total_pages = _paginate_list(users, page, ADMIN_USERS_PAGE_SIZE)
    await query.edit_message_text(
        _format_users_page(page_items, page, total_pages, len(users)),
        parse_mode="Markdown",
        reply_markup=_admin_users_keyboard(page, total_pages),
    )

@admin_only
async def cmd_add_balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin command to credit a user's wallet manually.

    Usage:
      /addbalance <user_id> <inr|usdt> <amount> [note]

    Examples:
      /addbalance 123456789 usdt 10
      /addbalance 123456789 inr 500 bonus refund
    """
    if len(context.args) < 3:
        await update.message.reply_text(
            "Usage: `/addbalance <user_id> <inr|usdt> <amount> [note]`\n"
            "Examples:\n"
            "`/addbalance 123456789 usdt 10`\n"
            "`/addbalance 123456789 inr 500 refund`",
            parse_mode="Markdown",
        )
        return

    try:
        target_user_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ Invalid user ID. It must be a numeric Telegram user ID.")
        return

    currency = context.args[1].lower().strip()
    currency_aliases = {
        "inr": "inr",
        "rs": "inr",
        "rupee": "inr",
        "rupees": "inr",
        "₹": "inr",
        "usdt": "usdt",
        "usd": "usdt",
        "$": "usdt",
    }
    currency = currency_aliases.get(currency, currency)
    if currency not in {"inr", "usdt"}:
        await update.message.reply_text("❌ Currency must be `inr` or `usdt`.", parse_mode="Markdown")
        return

    try:
        amount = float(context.args[2])
    except ValueError:
        await update.message.reply_text("❌ Amount must be a number.")
        return

    if amount <= 0:
        await update.message.reply_text("❌ Amount must be greater than 0.")
        return

    user = await db.get_user(target_user_id)
    if not user:
        await update.message.reply_text(
            "❌ User not found in database. Ask the user to press /start first, then try again."
        )
        return

    note = " ".join(context.args[3:]).strip()

    if currency == "inr":
        amount = round(amount, 2)
        await db.add_wallet_inr(target_user_id, amount)
        updated = await db.get_user(target_user_id)
        new_balance = float(updated.get("wallet_inr", 0)) if updated else amount
        currency_label = "INR"
        amount_text = f"₹{amount:.2f}"
        balance_text = f"₹{new_balance:.2f}"
    else:
        amount = round(amount, 2)
        await db.add_wallet_usdt(target_user_id, amount)
        updated = await db.get_user(target_user_id)
        new_balance = float(updated.get("wallet_usdt", 0)) if updated else amount
        currency_label = "USDT"
        amount_text = f"${amount:.2f} USDT"
        balance_text = f"${new_balance:.2f} USDT"

    admin_lines = [
        "✅ Wallet balance added successfully.",
        f"User ID: {telegram_id(target_user_id)}",
        f"Added: {amount_text}",
        f"New {currency_label} balance: {balance_text}",
    ]
    if note:
        admin_lines.append(f"Note: {md_code(note)}")
    await update.message.reply_text("\n".join(admin_lines), parse_mode="Markdown")

    try:
        user_lines = [
            "✅ Wallet balance added by admin.",
            f"Added: {amount_text}",
            f"New {currency_label} balance: {balance_text}",
            "Use /wallet to check your balance.",
        ]
        if note:
            user_lines.insert(2, f"Note: {note}")
        await context.bot.send_message(target_user_id, "\n".join(user_lines))
    except Exception:
        await update.message.reply_text(
            "⚠️ Balance was added, but I could not notify the user. They may have blocked the bot."
        )


@admin_only
async def cmd_remove_balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin command to debit a user's wallet manually.

    Usage:
      /removebalance <user_id> <inr|usdt> <amount> [note]

    Examples:
      /removebalance 123456789 usdt 10
      /removebalance 123456789 inr 500 chargeback
    """
    if len(context.args) < 3:
        await update.message.reply_text(
            "Usage: `/removebalance <user_id> <inr|usdt> <amount> [note]`\n"
            "Examples:\n"
            "`/removebalance 123456789 usdt 10`\n"
            "`/removebalance 123456789 inr 500 chargeback`",
            parse_mode="Markdown",
        )
        return

    try:
        target_user_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ Invalid user ID. It must be a numeric Telegram user ID.")
        return

    currency = context.args[1].lower().strip()
    currency_aliases = {
        "inr": "inr",
        "rs": "inr",
        "rupee": "inr",
        "rupees": "inr",
        "₹": "inr",
        "usdt": "usdt",
        "usd": "usdt",
        "$": "usdt",
    }
    currency = currency_aliases.get(currency, currency)
    if currency not in {"inr", "usdt"}:
        await update.message.reply_text("❌ Currency must be `inr` or `usdt`.", parse_mode="Markdown")
        return

    try:
        amount = float(context.args[2])
    except ValueError:
        await update.message.reply_text("❌ Amount must be a number.")
        return

    if amount <= 0:
        await update.message.reply_text("❌ Amount must be greater than 0.")
        return

    user = await db.get_user(target_user_id)
    if not user:
        await update.message.reply_text(
            "❌ User not found in database. Ask the user to press /start first, then try again."
        )
        return

    note = " ".join(context.args[3:]).strip()

    if currency == "inr":
        amount = round(amount, 2)
        current_balance = float(user.get("wallet_inr", 0) or 0)
        if current_balance < amount:
            await update.message.reply_text(
                f"❌ Cannot remove ₹{amount:.2f}. User only has ₹{current_balance:.2f}."
            )
            return
        success = await db.deduct_wallet_inr(target_user_id, amount)
        updated = await db.get_user(target_user_id)
        new_balance = float(updated.get("wallet_inr", 0)) if updated else max(0, current_balance - amount)
        currency_label = "INR"
        amount_text = f"₹{amount:.2f}"
        balance_text = f"₹{new_balance:.2f}"
    else:
        amount = round(amount, 2)
        current_balance = float(user.get("wallet_usdt", 0) or 0)
        if current_balance < amount:
            await update.message.reply_text(
                f"❌ Cannot remove ${amount:.2f} USDT. User only has ${current_balance:.2f} USDT."
            )
            return
        success = await db.deduct_wallet_usdt(target_user_id, amount)
        updated = await db.get_user(target_user_id)
        new_balance = float(updated.get("wallet_usdt", 0)) if updated else max(0, current_balance - amount)
        currency_label = "USDT"
        amount_text = f"${amount:.2f} USDT"
        balance_text = f"${new_balance:.2f} USDT"

    if not success:
        await update.message.reply_text(
            "❌ Could not remove balance. The user's balance may have changed. Please check /listusers and try again."
        )
        return

    admin_lines = [
        "✅ Wallet balance removed successfully.",
        f"User ID: {telegram_id(target_user_id)}",
        f"Removed: {amount_text}",
        f"New {currency_label} balance: {balance_text}",
    ]
    if note:
        admin_lines.append(f"Note: {md_code(note)}")
    await update.message.reply_text("\n".join(admin_lines), parse_mode="Markdown")

    try:
        user_lines = [
            "⚠️ Wallet balance adjusted by admin.",
            f"Removed: {amount_text}",
            f"New {currency_label} balance: {balance_text}",
            "Use /wallet to check your balance.",
        ]
        if note:
            user_lines.insert(2, f"Note: {note}")
        await context.bot.send_message(target_user_id, "\n".join(user_lines))
    except Exception:
        await update.message.reply_text(
            "⚠️ Balance was removed, but I could not notify the user. They may have blocked the bot."
        )


@admin_only
async def cmd_pending_orders(update: Update, context: ContextTypes.DEFAULT_TYPE):
    orders = await db.get_all_pending_stock_orders(limit=100)
    if not orders:
        await update.message.reply_text("✅ No paid orders are waiting for stock.")
        return
    lines = [f"⏳ *Pending Stock Orders ({len(orders)}):*\n"]
    for o in orders:
        lines.append(
            f"Order ID: `{o.get('order_id', 'N/A')}`\n"
            f"User: `{o.get('user_id', 'N/A')}`\n"
            f"Product: *{o.get('product_name', 'N/A')}* x{o.get('quantity', 0)}\n"
            f"Paid: {order_amount_text(o)}\n"
            f"Date: {_format_dt(o.get('created_at'))}\n"
        )
    text = compact_blank_lines("\n".join(lines))
    if len(text) > 4000:
        for i in range(0, len(lines), 20):
            await update.message.reply_text(compact_blank_lines("\n".join(lines[i:i+20])), parse_mode="Markdown")
    else:
        await update.message.reply_text(text, parse_mode="Markdown")


@admin_only
async def cmd_find_order(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: `/findorder <order_id>`", parse_mode="Markdown")
        return
    order_id = context.args[0].strip().upper()
    order = await db.get_order(order_id)
    if not order:
        await update.message.reply_text(f"❌ Order `{order_id}` not found.", parse_mode="Markdown")
        return
    await update.message.reply_text(_format_order_detail(order), parse_mode="Markdown")


@admin_only
async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    stats = await db.get_bot_stats()
    text = (
        "📊 *Bot Stats*\n\n"
        f"👥 Users: *{stats['users_total']}* total | 🚫 {stats['users_blocked']} blocked\n"
        f"📦 Products: *{stats['products_enabled']}* enabled / {stats['products_total']} total\n"
        f"📚 Stock units: *{stats['total_stock']}*\n\n"
        f"🧾 Orders: *{stats['orders_total']}* total\n"
        f"✅ Delivered: *{stats['orders_delivered']}*\n"
        f"⏳ Waiting stock: *{stats['orders_pending_stock']}*\n"
        f"🕒 Pending/unpaid: *{stats['orders_pending']}*\n"
        f"❌ Failed/expired: *{stats['orders_failed']}*\n\n"
        f"💰 Revenue/paid value:\n"
        f"₹{stats['revenue_inr']:.2f} INR\n"
        f"${stats['revenue_usdt']:.2f} USDT"
    )
    await update.message.reply_text(text, parse_mode="Markdown")


@admin_only
async def cmd_user_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: `/userstats <user_id>`", parse_mode="Markdown")
        return
    try:
        target_user_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ Invalid user ID. It must be numeric.")
        return
    user = await db.get_user(target_user_id)
    if not user:
        await update.message.reply_text("❌ User not found. Ask them to press /start first.")
        return
    stats = await db.get_user_order_stats(target_user_id)
    username = md_code(f"@{user.get('username')}") if user.get('username') else "no username"
    text = (
        "👤 *User Stats*\n\n"
        f"User ID: {telegram_id(target_user_id)}\n"
        f"Username: {username}\n"
        f"Status: {'🚫 Blocked' if user.get('blocked') else '✅ Active'}\n"
        f"Joined: {_format_dt(user.get('joined_at'))}\n"
        f"Wallet INR: ₹{float(user.get('wallet_inr') or 0):.2f}\n"
        f"Wallet USDT: ${float(user.get('wallet_usdt') or 0):.2f}\n\n"
        f"Orders: *{stats['total_orders']}*\n"
        f"✅ Delivered: *{stats['delivered']}*\n"
        f"⏳ Waiting stock: *{stats['pending_stock']}*\n"
        f"🕒 Pending/unpaid: *{stats['pending']}*\n"
        f"❌ Failed/expired: *{stats['failed']}*\n"
        f"Paid value: {aggregate_amount_text(stats['total_inr'], stats['total_usdt'])}"
    )
    await update.message.reply_text(text, parse_mode="Markdown")



def _format_user_admin_orders_text(user: dict | None, orders: list[dict], page: int, total_count: int, target_user_id: int) -> str:
    """Clean admin view of one user's orders.

    It intentionally does not dump delivered stock/items here. Admin can tap/send
    the /getorder... shortcut under any order to fetch the saved items when needed.
    """
    total_pages = max(1, (total_count + ADMIN_USER_ORDERS_PAGE_SIZE - 1) // ADMIN_USER_ORDERS_PAGE_SIZE) if total_count else 1
    username = md_code(f"@{user.get('username')}") if user and user.get("username") else "No username"

    lines = [
        f"👤 User Orders — Page {page + 1}/{total_pages}",
        "",
        f"User ID: {telegram_id(target_user_id)}",
        f"Username: {username}",
        f"Total orders: {total_count}",
        "Showing 10 orders per page, newest first.",
        "Use the /getorder... line under any order to fetch delivered stock/items.",
        "",
    ]

    if not orders:
        lines.append("No orders found for this user.")
        return compact_blank_lines("\n".join(lines))

    for order in orders:
        status = order.get("status", "unknown")
        order_id = order.get("order_id", "N/A")
        lines.extend([
            "",
            f"Order ID: {order_id}",
            f"Date/Time: {_format_dt(order.get('created_at'))}",
            f"Payment Method: {str(order.get('payment_method', 'N/A')).upper()}",
            f"Product: {order.get('product_name', 'N/A')} x{order.get('quantity', 0)}",
            f"Amount: {order_amount_text(order)}",
            f"Status: {_status_emoji(status)} {_status_label(status)}",
            f"Fetch items: /getorder{order_id}",
            "",
        ])

    return compact_blank_lines("\n".join(lines))

def _admin_user_orders_keyboard(target_user_id: int, page: int, total_count: int) -> InlineKeyboardMarkup:
    total_pages = max(1, (total_count + ADMIN_USER_ORDERS_PAGE_SIZE - 1) // ADMIN_USER_ORDERS_PAGE_SIZE) if total_count else 1
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️ Previous", callback_data=f"userorders:{target_user_id}:{page - 1}"))
    if page + 1 < total_pages:
        nav.append(InlineKeyboardButton("➡️ Next", callback_data=f"userorders:{target_user_id}:{page + 1}"))
    return InlineKeyboardMarkup([nav] if nav else [])


@admin_only
async def cmd_user_orders(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /userorders <user_id>")
        return
    try:
        target_user_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ Invalid user ID. It must be numeric.")
        return

    user = await db.get_user(target_user_id)
    total_count = await db.count_user_orders(target_user_id)
    orders = await db.get_user_orders(target_user_id, limit=ADMIN_USER_ORDERS_PAGE_SIZE, skip=0)
    await update.message.reply_text(
        _format_user_admin_orders_text(user, orders, page=0, total_count=total_count, target_user_id=target_user_id),
        parse_mode="Markdown",
        reply_markup=_admin_user_orders_keyboard(target_user_id, page=0, total_count=total_count),
    )


async def handle_admin_user_orders_page(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not is_admin(query.from_user.id):
        await query.answer("⛔ Unauthorized.", show_alert=True)
        return
    await query.answer()
    try:
        _, user_id_text, page_text = query.data.split(":", 2)
        target_user_id = int(user_id_text)
        page = int(page_text)
    except Exception:
        await query.edit_message_text("❌ Invalid user orders page request.")
        return

    total_count = await db.count_user_orders(target_user_id)
    total_pages = max(1, (total_count + ADMIN_USER_ORDERS_PAGE_SIZE - 1) // ADMIN_USER_ORDERS_PAGE_SIZE) if total_count else 1
    page = max(0, min(page, total_pages - 1))
    user = await db.get_user(target_user_id)
    orders = await db.get_user_orders(target_user_id, limit=ADMIN_USER_ORDERS_PAGE_SIZE, skip=page * ADMIN_USER_ORDERS_PAGE_SIZE)
    await query.edit_message_text(
        _format_user_admin_orders_text(user, orders, page=page, total_count=total_count, target_user_id=target_user_id),
        parse_mode="Markdown",
        reply_markup=_admin_user_orders_keyboard(target_user_id, page=page, total_count=total_count),
    )



@admin_only
async def cmd_user_wallet_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin: show one user's wallet top-up logs, 10 per page."""
    if not context.args:
        await update.message.reply_text("Usage: /userwallethistory <user_id>")
        return
    try:
        target_user_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ Invalid user ID. It must be numeric.")
        return

    user = await db.get_user(target_user_id)
    total_count = await db.count_user_wallet_logs(target_user_id)
    logs = await db.get_user_wallet_logs(target_user_id, limit=WALLET_HISTORY_PAGE_SIZE, skip=0)
    username = md_code(f"@{user.get('username')}") if user and user.get("username") else "No username"
    extra_lines = [
        f"User ID: {telegram_id(target_user_id)}",
        f"Username: {username}",
        f"Total wallet logs: {total_count}",
    ]
    await update.message.reply_text(
        format_wallet_history_text(
            logs,
            page=0,
            total_count=total_count,
            title="👛 User Wallet Logs",
            extra_lines=extra_lines,
        ),
        parse_mode="Markdown",
        reply_markup=wallet_history_keyboard(
            0, total_count, f"userwallethistory:{target_user_id}"
        ),
    )


async def handle_admin_user_wallet_history_page(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin pagination for /userwallethistory."""
    query = update.callback_query
    if not is_admin(query.from_user.id):
        await query.answer("⛔ Unauthorized.", show_alert=True)
        return
    await query.answer()
    try:
        _, user_id_text, page_text = query.data.split(":", 2)
        target_user_id = int(user_id_text)
        page = int(page_text)
    except Exception:
        await query.edit_message_text("❌ Invalid wallet history page request.")
        return

    total_count = await db.count_user_wallet_logs(target_user_id)
    total_pages = max(1, (total_count + WALLET_HISTORY_PAGE_SIZE - 1) // WALLET_HISTORY_PAGE_SIZE) if total_count else 1
    page = max(0, min(page, total_pages - 1))
    user = await db.get_user(target_user_id)
    logs = await db.get_user_wallet_logs(
        target_user_id,
        limit=WALLET_HISTORY_PAGE_SIZE,
        skip=page * WALLET_HISTORY_PAGE_SIZE,
    )
    username = md_code(f"@{user.get('username')}") if user and user.get("username") else "No username"
    extra_lines = [
        f"User ID: {telegram_id(target_user_id)}",
        f"Username: {username}",
        f"Total wallet logs: {total_count}",
    ]
    await query.edit_message_text(
        format_wallet_history_text(
            logs,
            page=page,
            total_count=total_count,
            title="👛 User Wallet Logs",
            extra_lines=extra_lines,
        ),
        parse_mode="Markdown",
        reply_markup=wallet_history_keyboard(
            page, total_count, f"userwallethistory:{target_user_id}"
        ),
    )


def _format_ranking_text(rows: list[dict], page: int, total_count: int) -> str:
    total_pages = max(1, (total_count + RANKING_PAGE_SIZE - 1) // RANKING_PAGE_SIZE)
    if not rows:
        return "🏆 *Top Buyers*\n\nNo paid buyer data yet."

    lines = [f"🏆 *Top Buyers — Page {page + 1}/{total_pages}*", ""]
    start_rank = page * RANKING_PAGE_SIZE
    for idx, row in enumerate(rows, start=start_rank + 1):
        username = row.get("username")
        username_text = md_code(f"@{username}") if username else "no username"
        lines.extend([
            f"*#{idx}*",
            f"User ID: {telegram_id(row.get('user_id', 'N/A'))}",
            f"Username: {username_text}",
            f"Total Orders: *{int(row.get('total_orders') or 0)}*",
            f"Total Order Value: {aggregate_amount_text(row.get('total_inr'), row.get('total_usdt'))}",
            "",
        ])
    return compact_blank_lines("\n".join(lines))


def _ranking_keyboard(page: int, total_count: int) -> InlineKeyboardMarkup:
    total_pages = max(1, (total_count + RANKING_PAGE_SIZE - 1) // RANKING_PAGE_SIZE)
    buttons = []
    if page > 0:
        buttons.append(InlineKeyboardButton("⬅️ Previous", callback_data=f"ranking:{page - 1}"))
    if page < total_pages - 1:
        buttons.append(InlineKeyboardButton("➡️ Next", callback_data=f"ranking:{page + 1}"))
    return InlineKeyboardMarkup([buttons] if buttons else [])


@admin_only
async def cmd_ranking(update: Update, context: ContextTypes.DEFAULT_TYPE):
    total_count = await db.count_ranked_buyers()
    rows = await db.get_buyer_ranking(limit=RANKING_PAGE_SIZE, skip=0)
    await update.message.reply_text(
        _format_ranking_text(rows, page=0, total_count=total_count),
        parse_mode="Markdown",
        reply_markup=_ranking_keyboard(page=0, total_count=total_count),
    )


async def handle_ranking_page(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not is_admin(query.from_user.id):
        await query.answer("⛔ Unauthorized.", show_alert=True)
        return
    await query.answer()
    try:
        page = int(query.data.split(":", 1)[1])
    except Exception:
        page = 0
    total_count = await db.count_ranked_buyers()
    total_pages = max(1, (total_count + RANKING_PAGE_SIZE - 1) // RANKING_PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))
    rows = await db.get_buyer_ranking(limit=RANKING_PAGE_SIZE, skip=page * RANKING_PAGE_SIZE)
    await query.edit_message_text(
        _format_ranking_text(rows, page=page, total_count=total_count),
        parse_mode="Markdown",
        reply_markup=_ranking_keyboard(page=page, total_count=total_count),
    )


@admin_only
async def cmd_maintenance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    arg = context.args[0].lower().strip() if context.args else "status"
    if arg in {"on", "enable", "enabled", "true", "1"}:
        await db.set_maintenance_mode(True)
        await update.message.reply_text("🛠 Maintenance mode is now ON. Users cannot use the shop until you turn it off.")
        return
    if arg in {"off", "disable", "disabled", "false", "0"}:
        await db.set_maintenance_mode(False)
        flushed = await flush_maintenance_notifications(context.bot)
        await update.message.reply_text(
            "✅ Maintenance mode is now OFF. Users can use the bot normally.\n"
            f"📨 Delivered queued product/stock/price notification events: {flushed['processed']} "
            f"(messages sent: {flushed['sent']})."
        )
        return
    if arg == "status":
        enabled = await db.is_maintenance_mode()
        await update.message.reply_text(f"Maintenance mode: {'ON 🛠' if enabled else 'OFF ✅'}")
        return
    await update.message.reply_text("Usage: `/maintenance <on|off|status>`", parse_mode="Markdown")


@admin_only
async def cmd_block_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: `/blockuser <user_id>`", parse_mode="Markdown")
        return
    try:
        uid = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ Invalid user ID.")
        return
    await db.set_blocked(uid, True)
    await update.message.reply_text(f"🚫 User `{uid}` has been blocked.", parse_mode="Markdown")
    try:
        await context.bot.send_message(uid, "🚫 You have been blocked from this bot.")
    except Exception:
        pass


@admin_only
async def cmd_unblock_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: `/unblockuser <user_id>`", parse_mode="Markdown")
        return
    try:
        uid = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ Invalid user ID.")
        return
    await db.set_blocked(uid, False)
    await update.message.reply_text(f"✅ User `{uid}` has been unblocked.", parse_mode="Markdown")
    try:
        await context.bot.send_message(uid, "✅ You have been unblocked. You can use the bot again.")
    except Exception:
        pass


@admin_only
async def cmd_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: `/broadcast <your message>`", parse_mode="Markdown")
        return
    message = " ".join(context.args)
    users = await db.get_all_users()
    admin_ids = await _get_admin_id_set()
    maintenance_on = await db.is_maintenance_mode()
    sent = 0
    failed = 0
    seen: set[int] = set()

    for user in users:
        try:
            user_id = int(user.get("user_id", 0) or 0)
        except Exception:
            user_id = 0
        if not user_id or user_id in seen:
            continue
        if maintenance_on and user_id not in admin_ids:
            continue
        seen.add(user_id)
        try:
            await context.bot.send_message(
                user_id,
                f"📢 *Broadcast Message:*\n\n{message}",
                parse_mode="Markdown"
            )
            sent += 1
        except Exception:
            failed += 1

    if maintenance_on:
        # Admin/tester IDs should receive test broadcasts during maintenance even
        # if their user row is not in the users collection. Normal users are not queued.
        for user_id in sorted(admin_ids - seen):
            try:
                await context.bot.send_message(
                    user_id,
                    f"📢 *Broadcast Message:*\n\n{message}",
                    parse_mode="Markdown"
                )
                sent += 1
            except Exception:
                failed += 1
        await update.message.reply_text(
            f"🛠 Maintenance mode is ON. Broadcast sent only to admin/tester IDs and not queued for normal users.\n✅ Sent: {sent}\n❌ Failed: {failed}"
        )
        return

    await update.message.reply_text(
        f"📢 Broadcast complete!\n✅ Sent: {sent}\n❌ Failed: {failed}"
    )


@admin_only
async def cmd_recent_orders(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin recent orders, paginated 10 per page."""
    total_count = await db.count_all_orders()
    orders = await db.get_recent_orders(limit=10, skip=0)
    await update.message.reply_text(
        _format_recent_orders_page(orders, page=0, total_count=total_count),
        parse_mode="HTML",
        reply_markup=_recent_orders_keyboard(page=0, total_count=total_count),
    )


def _format_recent_orders_page(orders: list[dict], page: int, total_count: int) -> str:
    page_size = 10
    total_pages = max(1, (total_count + page_size - 1) // page_size) if total_count else 1
    if not orders:
        return "📋 Recent Orders\n\nNo orders yet."

    lines = [
        f"📋 <b>Recent Orders</b> — Page {page + 1}/{total_pages}",
        "",
        "Showing 10 orders per page, newest first.",
        "Use /findorder &lt;order_id&gt; for full details or /getorder&lt;order_id&gt; to fetch delivered items.",
        "",
    ]

    for order in orders:
        status = order.get("status", "unknown")
        username = f"@{order.get('username')}" if order.get("username") else "No username"
        order_id = str(order.get("order_id", "N/A"))
        lines.extend([
            f"Order ID: {_html_code(order_id)}",
            f"Date/Time: {_html_escape(_format_dt(order.get('created_at')))}",
            f"Payment Method: {_html_escape(str(order.get('payment_method', 'N/A')).upper())}",
            f"User ID: {_html_code(order.get('user_id', 'N/A'))}",
            f"Username: {_html_escape(username)}",
            f"Product: {_html_escape(order.get('product_name', 'N/A'))} x{_html_escape(order.get('quantity', 0))}",
            f"Amount: {order_amount_text(order)}",
            f"Status: {_html_escape(_status_emoji(status))} {_html_escape(_status_label(status))}",
            f"Fetch items: /getorder{_html_escape(order_id)}",
            "",
        ])
    return compact_blank_lines("\n".join(lines))



def _recent_orders_keyboard(page: int, total_count: int) -> InlineKeyboardMarkup:
    page_size = 10
    total_pages = max(1, (total_count + page_size - 1) // page_size) if total_count else 1
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️ Previous", callback_data=f"adminrecentorders:{page - 1}"))
    if page + 1 < total_pages:
        nav.append(InlineKeyboardButton("➡️ Next", callback_data=f"adminrecentorders:{page + 1}"))
    return InlineKeyboardMarkup([nav] if nav else [])


async def handle_admin_recent_orders_page(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not is_admin(query.from_user.id):
        await query.answer("⛔ Unauthorized.", show_alert=True)
        return
    await query.answer()
    try:
        page = int(query.data.split(":", 1)[1])
    except Exception:
        page = 0
    total_count = await db.count_all_orders()
    total_pages = max(1, (total_count + 10 - 1) // 10) if total_count else 1
    page = max(0, min(page, total_pages - 1))
    orders = await db.get_recent_orders(limit=10, skip=page * 10)
    await query.edit_message_text(
        _format_recent_orders_page(orders, page=page, total_count=total_count),
        parse_mode="HTML",
        reply_markup=_recent_orders_keyboard(page=page, total_count=total_count),
    )



# ─────────────────────────── STOCK INPUT ─────────────────────


async def handle_stock_remove_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """
    Called from the main message handler.
    Returns True if this message was consumed as stock removal input.
    """
    user_id = update.effective_user.id
    if user_id not in _awaiting_stock_removal:
        return False

    product_name = _awaiting_stock_removal[user_id]
    raw = update.message.text.strip()

    # Split by --- separator; each block is one exact stock item to remove.
    blocks = [b.strip() for b in raw.split("---") if b.strip()]
    if not blocks:
        await update.message.reply_text(
            "❌ No valid stock items found. Try again or send `/cancel`.",
            parse_mode="Markdown"
        )
        return True

    result = await db.remove_stock_items(product_name, blocks)
    _awaiting_stock_removal.pop(user_id, None)

    removed_count = len(result["removed"])
    not_found_count = len(result["not_found"])
    total = result["remaining"]

    lines = [
        f"🗑 Removed *{removed_count}* item(s) from *{product_name}*.",
        f"📦 Remaining stock: *{total}*",
    ]

    if not_found_count:
        preview = "\n---\n".join(result["not_found"][:5])
        more = not_found_count - min(not_found_count, 5)
        lines.append(
            f"\n⚠️ *{not_found_count}* item(s) were not found and were not removed."
        )
        lines.append(f"First not found item(s):\n`{preview}`")
        if more > 0:
            lines.append(f"...and {more} more.")

    await update.message.reply_text(compact_blank_lines("\n".join(lines)), parse_mode="Markdown")
    from handlers.payment import notify_low_stock_if_needed
    await notify_low_stock_if_needed(context.bot, product_name)
    return True


async def handle_stock_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """
    Called from the main message handler.
    Returns True if this message was consumed as stock input.
    """
    user_id = update.effective_user.id
    if user_id not in _awaiting_stock:
        return False

    product_name = _awaiting_stock[user_id]
    raw = update.message.text.strip()

    # Split by --- separator; each block is one stock item
    blocks = [b.strip() for b in raw.split("---") if b.strip()]
    if not blocks:
        await update.message.reply_text("❌ No valid items found. Try again or send `/cancel`.", parse_mode="Markdown")
        return True

    previous_available = await db.get_available_stock_count(product_name)
    count = await db.add_stock(product_name, blocks)
    skipped_count = max(0, len(blocks) - count)

    # Paid orders that were waiting for this product must be fulfilled before
    # newly available stock is shown/used for new buyers.
    from handlers.payment import process_pending_stock_orders
    delivery_summary = await process_pending_stock_orders(context.bot, product_name)

    total = await db.get_stock_count(product_name)
    available = await db.get_available_stock_count(product_name)
    pending_qty = await db.get_pending_stock_quantity(product_name)
    _awaiting_stock.pop(user_id)

    delivered_orders = delivery_summary.get("orders_delivered", 0)
    delivered_items = delivery_summary.get("items_delivered", 0)
    should_notify_restock = (
        count > 0
        and await db.claim_restock_notification_slot(
            product_name,
            previous_available,
            available,
            cooldown_minutes=RESTOCK_NOTIFICATION_COOLDOWN_MINUTES,
            long_cooldown_minutes=RESTOCK_LONG_NOTIFICATION_COOLDOWN_MINUTES,
            high_stock_threshold=RESTOCK_HIGH_STOCK_THRESHOLD,
            default_threshold=LOW_STOCK_ALERT_THRESHOLD,
        )
    )
    if should_notify_restock:
        await notify_users_new_stock(context.bot, product_name, available)

    skipped_line = f"↩️ Skipped duplicate item(s): *{skipped_count}*\n" if skipped_count else ""
    await update.message.reply_text(
        f"✅ Added *{count}* fresh item(s) to *{product_name}*\n"
        f"{skipped_line}"
        f"🚚 Auto-delivered pending orders: *{delivered_orders}* order(s), *{delivered_items}* item(s)\n"
        f"⏳ Still pending stock quantity: *{pending_qty}*\n"
        f"📦 Total stock now: *{total}*\n"
        f"🛒 Available for new buyers: *{available}*",
        parse_mode="Markdown"
    )
    return True


# ─────────────────────── UPI APPROVAL ────────────────────────

async def send_upi_approval_request(
    bot,
    pending: dict,
    order_or_wallet_info: str,
    screenshot_file_id: str | None = None,
):
    """Sends the admin an approval request for a UPI payment with screenshot proof."""
    ref_id = pending["ref_id"]
    screenshot_file_id = screenshot_file_id or pending.get("upi_screenshot_file_id")
    caption = (
        f"💳 *UPI Payment Verification Request*\n\n"
        f"👤 User ID: `{pending['user_id']}`\n"
        f"📝 Ref: `{ref_id}`\n"
        f"💰 Amount: ₹{pending['expected_inr']:.2f}\n"
        f"📋 {order_or_wallet_info}\n\n"
        f"👤 Payer Name: `{pending.get('upi_payee_name', 'N/A')}`\n"
        f"🔖 Transaction ID: `{pending.get('upi_txn_id', 'N/A')}`"
    )
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Approve", callback_data=f"upi_approve:{ref_id}"),
            InlineKeyboardButton("❌ Reject", callback_data=f"upi_reject:{ref_id}"),
        ]
    ])
    if screenshot_file_id:
        await send_admin_photo(
            bot,
            photo=screenshot_file_id,
            caption=caption,
            parse_mode="Markdown",
            reply_markup=keyboard,
        )
    else:
        await send_admin_message(bot, caption, parse_mode="Markdown", reply_markup=keyboard)


async def handle_upi_decision(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles admin approve/reject button presses for UPI payments."""
    query = update.callback_query
    await query.answer()

    if not is_admin(query.from_user.id):
        return

    action, ref_id = query.data.split(":", 1)
    pending = await db.get_pending_by_ref(ref_id)

    if not pending or pending["status"] not in ("upi_submitted",):
        if query.message and query.message.photo:
            await query.edit_message_caption("⚠️ This payment request is no longer active.")
        else:
            await query.edit_message_text("⚠️ This payment request is no longer active.")
        return

    user_id = pending["user_id"]

    if action == "upi_approve":
        await db.update_pending_status(ref_id, "approved", reviewed=True, reviewed_by=query.from_user.id)
        if query.message and query.message.photo:
            await query.edit_message_caption(
                f"✅ UPI payment approved for `{ref_id}`", parse_mode="Markdown"
            )
        else:
            await query.edit_message_text(
                f"✅ UPI payment approved for `{ref_id}`", parse_mode="Markdown"
            )
        # Trigger delivery via context
        context.application.create_task(
            deliver_after_approval(context, user_id, ref_id, pending)
        )
    else:
        await db.update_pending_status(ref_id, "rejected", reviewed=True, reviewed_by=query.from_user.id)
        if pending.get("pay_type") == "order":
            await db.update_order_status(ref_id, "failed")
        if query.message and query.message.photo:
            await query.edit_message_caption(
                f"❌ UPI payment rejected for `{ref_id}`", parse_mode="Markdown"
            )
        else:
            await query.edit_message_text(
                f"❌ UPI payment rejected for `{ref_id}`", parse_mode="Markdown"
            )
        try:
            await context.bot.send_message(
                user_id,
                "❌ Your payment could not be verified. Please contact support if you believe this is a mistake."
            )
        except Exception:
            pass


async def deliver_after_approval(context, user_id: int, ref_id: str, pending: dict):
    """Completes order or wallet top-up after admin UPI/Binance approval."""
    from handlers.payment import complete_order, complete_wallet_load
    if pending["pay_type"] == "order":
        await complete_order(context.bot, user_id, pending["ref_id"])
    else:
        await complete_wallet_load(context.bot, user_id, pending)


# ─────────────────────── BINANCE PAY APPROVAL ────────────────

async def send_usdt_manual_approval_request(
    bot,
    pending: dict,
    order_or_wallet_info: str,
    txn_hash: str,
    screenshot_file_id: str | None = None,
):
    """Sends admin an approval request for manual USDT verification with screenshot proof."""
    ref_id = pending["ref_id"]
    screenshot_file_id = screenshot_file_id or pending.get("usdt_screenshot_file_id")
    network = normalize_usdt_network(pending.get("usdt_network") or pending.get("method"))
    network_label = get_usdt_network_label(network)
    explorer_name = "PolygonScan" if network == "polygon" else "BSCScan"
    explorer_url = "https://polygonscan.com/tx" if network == "polygon" else "https://bscscan.com/tx"
    caption = (
        f"🔍 *Manual USDT Verification Request*\n\n"
        f"👤 User ID: `{pending['user_id']}`\n"
        f"📝 Ref: `{ref_id}`\n"
        f"🌐 Network: *{network_label}*\n"
        f"💰 Expected Amount: ${pending['expected_usdt']:.2f} USDT\n"
        f"💰 Unique Amount: ${pending.get('unique_usdt', pending['expected_usdt']):.3f} USDT\n"
        f"📋 {order_or_wallet_info}\n\n"
        f"🔗 TxHash: `{txn_hash}`\n\n"
        f"Verify on {explorer_name}:\n"
        f"{explorer_url}/{txn_hash}"
    )
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Approve", callback_data=f"usdtm_approve:{ref_id}"),
            InlineKeyboardButton("❌ Reject", callback_data=f"usdtm_reject:{ref_id}"),
        ]
    ])
    if screenshot_file_id:
        await send_admin_photo(
            bot,
            photo=screenshot_file_id,
            caption=caption,
            parse_mode="Markdown",
            reply_markup=keyboard,
        )
    else:
        await send_admin_message(bot, caption, parse_mode="Markdown", reply_markup=keyboard)


async def handle_usdt_manual_decision(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles admin approve/reject for manual USDT verification."""
    query = update.callback_query
    await query.answer()

    if not is_admin(query.from_user.id):
        return

    action, ref_id = query.data.split(":", 1)
    pending = await db.get_pending_by_ref(ref_id)

    if not pending or pending["status"] != "usdt_manual_submitted":
        if query.message and query.message.photo:
            await query.edit_message_caption("⚠️ This request is no longer active.")
        else:
            await query.edit_message_text("⚠️ This request is no longer active.")
        return

    user_id = pending["user_id"]

    if action == "usdtm_approve":
        await db.update_pending_status(ref_id, "confirmed", reviewed=True, reviewed_by=query.from_user.id)
        if query.message and query.message.photo:
            await query.edit_message_caption(
                f"✅ Manual USDT approved for `{ref_id}`", parse_mode="Markdown"
            )
        else:
            await query.edit_message_text(
                f"✅ Manual USDT approved for `{ref_id}`", parse_mode="Markdown"
            )
        context.application.create_task(
            deliver_after_approval(context, user_id, ref_id, pending)
        )
    else:
        await db.update_pending_status(ref_id, "rejected", reviewed=True, reviewed_by=query.from_user.id)
        if pending.get("pay_type") == "order":
            await db.update_order_status(ref_id, "failed")
        if query.message and query.message.photo:
            await query.edit_message_caption(
                f"❌ Manual USDT rejected for `{ref_id}`", parse_mode="Markdown"
            )
        else:
            await query.edit_message_text(
                f"❌ Manual USDT rejected for `{ref_id}`", parse_mode="Markdown"
            )
        try:
            await context.bot.send_message(
                user_id,
                "❌ Your payment could not be verified. Please contact support if you believe this is a mistake."
            )
        except Exception:
            pass


async def send_binance_approval_request(
    bot,
    pending: dict,
    order_or_wallet_info: str,
    screenshot_file_id: str,
):
    """Sends the admin an approval request for a Binance Pay payment with screenshot."""
    ref_id = pending["ref_id"]
    caption = (
        f"🟡 *Binance Pay Verification Request*\n\n"
        f"👤 User ID: `{pending['user_id']}`\n"
        f"📝 Ref: `{ref_id}`\n"
        f"💰 Amount: ${pending['expected_usdt']:.2f} USDT\n"
        f"📋 {order_or_wallet_info}\n\n"
        f"👤 Binance Name: `{pending.get('binance_name', 'N/A')}`"
    )
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Approve", callback_data=f"binance_approve:{ref_id}"),
            InlineKeyboardButton("❌ Reject", callback_data=f"binance_reject:{ref_id}"),
        ]
    ])
    await send_admin_photo(
        bot,
        photo=screenshot_file_id,
        caption=caption,
        parse_mode="Markdown",
        reply_markup=keyboard,
    )


async def handle_binance_decision(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles admin approve/reject button presses for Binance Pay payments."""
    query = update.callback_query
    await query.answer()

    if not is_admin(query.from_user.id):
        return

    action, ref_id = query.data.split(":", 1)
    pending = await db.get_pending_by_ref(ref_id)

    if not pending or pending["status"] != "binance_submitted":
        await query.edit_message_caption("⚠️ This payment request is no longer active.")
        return

    user_id = pending["user_id"]

    if action == "binance_approve":
        await db.update_pending_status(ref_id, "approved", reviewed=True, reviewed_by=query.from_user.id)
        await query.edit_message_caption(
            f"✅ Binance Pay approved for `{ref_id}`", parse_mode="Markdown"
        )
        context.application.create_task(
            deliver_after_approval(context, user_id, ref_id, pending)
        )
    else:
        await db.update_pending_status(ref_id, "rejected", reviewed=True, reviewed_by=query.from_user.id)
        if pending.get("pay_type") == "order":
            await db.update_order_status(ref_id, "failed")
        await query.edit_message_caption(
            f"❌ Binance Pay rejected for `{ref_id}`", parse_mode="Markdown"
        )
        try:
            await context.bot.send_message(
                user_id,
                "❌ Your payment could not be verified. Please contact support if you believe this is a mistake."
            )
        except Exception:
            pass
