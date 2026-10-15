"""Shared user/admin message text so command buttons and slash commands stay identical."""

import re
from utils.i18n import tr


def compact_blank_lines(text: str) -> str:
    """Collapse repeated empty lines to one blank line for clean Telegram pages."""
    return re.sub(r"\n{3,}", "\n\n", text.strip())


def md_code(value) -> str:
    """Return Markdown inline-code text for copyable values in Telegram."""
    if value is None:
        value = "N/A"
    text = str(value)
    text = text.replace("`", "ʼ")
    return f"`{text}`"


def telegram_id(value) -> str:
    """Format a Telegram numeric ID as copyable monospace text."""
    if value in (None, ""):
        return md_code("N/A")
    return md_code(value)


def _safe_float(value, default: float = 0.0) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return default


def _compact_amount(value, max_decimals: int = 2) -> str:
    amount = _safe_float(value)
    return f"{amount:.{max_decimals}f}"


def compact_money(value, currency: str) -> str:
    if str(currency).lower() == "usdt":
        return f"${_compact_amount(value, 2)} USDT"
    return f"₹{_compact_amount(value, 2)} INR"


def order_amount_text(order: dict) -> str:
    """Format an order amount using only the currency actually used to pay.

    Orders store both INR and USDT price equivalents for product pricing. For
    display, we should not show both. The payment_method decides the real
    paid currency: UPI/INR wallet => INR, BEP20/Binance/USDT wallet => USDT.
    """
    method = str((order or {}).get("payment_method") or "").lower()
    amount_inr = _safe_float((order or {}).get("amount_inr"))
    amount_usdt = _safe_float((order or {}).get("amount_usdt"))

    if method in {"upi", "wallet_inr", "inr"} or "upi" in method or method.endswith("_inr"):
        return compact_money(amount_inr, "inr")

    if (
        method in {"usdt", "wallet_usdt", "binance", "binance_pay", "binance_usdt", "bep20"}
        or "usdt" in method
        or "binance" in method
        or "bep20" in method
    ):
        return compact_money(amount_usdt, "usdt")

    # Fallback for older/incomplete orders. Show only the non-zero side when possible.
    if amount_usdt and not amount_inr:
        return compact_money(amount_usdt, "usdt")
    if amount_inr and not amount_usdt:
        return compact_money(amount_inr, "inr")
    if amount_usdt:
        return compact_money(amount_usdt, "usdt")
    return compact_money(amount_inr, "inr")


def aggregate_amount_text(amount_inr: float = 0.0, amount_usdt: float = 0.0) -> str:
    """Format aggregate totals without showing zero currencies."""
    amount_inr = _safe_float(amount_inr)
    amount_usdt = _safe_float(amount_usdt)
    parts = []
    if amount_inr:
        parts.append(compact_money(amount_inr, "inr"))
    if amount_usdt:
        parts.append(compact_money(amount_usdt, "usdt"))
    return " / ".join(parts) if parts else "₹0 INR"


def commands_text(lang: str = "en") -> str:
    return compact_blank_lines(tr(lang, "commands_text"))


def admin_commands_text() -> str:
    return compact_blank_lines(
        "🛠 *Admin Commands*\n\n"
        "`/addproduct <name> <price_inr> <price_usdt>` — Add product\n"
        "`/removeproduct <name>` — Remove product\n"
        "`/setprice <name> <price_inr> <price_usdt>` — Update prices\n"
        "`/addstock <name>` — Add stock, then paste items after\n"
        "`/removestock <name>` — Remove specific stock, then paste exact items after\n"
        "`/cancel` — Abort active stock add/remove session\n"
        "`/clearstock <name>` — Clear all stock\n"
        "`/disableproduct <name>` — Hide product from shop\n"
        "`/enableproduct <name>` — Show product in shop again\n"
        "`/listproducts` — List all products\n"
        "`/listusers` — List all users\n"
        "`/pendingorders` — View paid orders waiting for stock\n"
        "`/findorder <order_id>` — Find order details\n"
        "`/stats` — Bot sales/users/stock dashboard\n"
        "`/ranking` — Top buyers, 10 per page with Next/Previous\n"
        "`/userstats <user_id>` — Stats for one user\n"
        "`/userorders <user_id>` — View one user's orders, 10 per page, with get-order shortcuts\n"
        "`/userwallethistory <user_id>` — View one user's wallet top-up logs, 10 per page\n"
        "`/maintenance <on|off|status>` — Toggle maintenance mode\n"
        "`/addbalance <user_id> <inr|usdt> <amount> [note]` — Add wallet balance\n"
        "`/removebalance <user_id> <inr|usdt> <amount> [note]` — Remove wallet balance\n"
        "`/blockuser <user_id>` — Block a user\n"
        "`/unblockuser <user_id>` — Unblock a user\n"
        "`/broadcast <message>` — Broadcast message\n"
        "`/recentorders` — View recent bot orders, 10 per page\n"

        "📦 *Bulk Stock Format:*\n"
        "Use `/addstock Product Name`, then send stock separated by `---`. Send `/cancel` to abort.\n"
        "Paid pending-stock orders are auto-delivered first when new stock is added.\n\n"
        "Example:\n"
        "`user1\\npass1\\n---\\nuser2\\npass2\\n---\\nsinglekey123`\n\n"
        "🗑 *Removing Specific/Bulk Stock:*\n"
        "Use `/removestock Product Name`, then send the exact stock item(s) to remove. Separate multiple items with `---`."
    )
