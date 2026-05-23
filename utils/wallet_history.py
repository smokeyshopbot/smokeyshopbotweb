"""Helpers for formatting wallet top-up history pages."""

from __future__ import annotations
from datetime import datetime, timezone
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from utils.messages import compact_blank_lines, md_code
from utils.i18n import tr

WALLET_HISTORY_PAGE_SIZE = 10


def format_dt(value) -> str:
    if isinstance(value, datetime):
        dt = value
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    return "N/A"


def method_label(method: str) -> str:
    method = (method or "").lower()
    if method == "usdt":
        return "USDT BEP20"
    if method == "upi":
        return "UPI"
    if method == "binance":
        return "Binance Pay"
    if method in {"admin", "admin_add", "admin_remove"}:
        if method == "admin_add":
            return "Admin wallet add"
        if method == "admin_remove":
            return "Admin wallet remove"
        return "Admin wallet adjustment"
    return method.upper() if method else "N/A"


def status_label(status: str, lang: str = "en") -> str:
    status = (status or "unknown").lower()
    mapping = {
        "waiting": "wallet_status_waiting",
        "upi_submitted": "wallet_status_review",
        "binance_submitted": "wallet_status_review",
        "usdt_manual_submitted": "wallet_status_review",
        "confirmed": "wallet_status_confirmed",
        "approved": "wallet_status_approved",
        "completed": "wallet_status_completed",
        "expired": "wallet_status_expired",
        "rejected": "wallet_status_rejected",
    }
    key = mapping.get(status)
    return tr(lang, key) if key else status.replace("_", " ").title()


def amount_text(log: dict) -> str:
    currency = (log.get("currency") or "").lower()
    method = (log.get("method") or "").lower()
    load_amount = float(log.get("load_amount") or 0)
    expected_inr = float(log.get("expected_inr") or 0)
    expected_usdt = float(log.get("expected_usdt") or 0)
    admin_action = (log.get("admin_wallet_action") or "").lower()
    sign = "-" if admin_action == "remove" else "+" if admin_action == "add" else ""

    if currency == "inr" or method == "upi" or expected_inr > 0:
        amount = load_amount if load_amount > 0 else expected_inr
        return f"{sign}₹{amount:.2f} INR"

    amount = load_amount if load_amount > 0 else expected_usdt
    return f"{sign}${amount:.2f} USDT"


def format_wallet_history_text(
    logs: list[dict],
    page: int,
    total_count: int,
    title: str | None = None,
    extra_lines: list[str] | None = None,
    lang: str = "en",
) -> str:
    if title is None:
        title = tr(lang, "wallet_history_title")
    total_pages = max(1, (total_count + WALLET_HISTORY_PAGE_SIZE - 1) // WALLET_HISTORY_PAGE_SIZE) if total_count else 1
    lines = [tr(lang, "wallet_history_page", title=title, page=page + 1, total_pages=total_pages), ""]
    if extra_lines:
        lines.extend(extra_lines)
        lines.append("")
    lines.append(tr(lang, "wallet_history_hint"))
    lines.append("")

    if not logs:
        lines.append(tr(lang, "wallet_history_empty"))
        return compact_blank_lines("\n".join(lines))

    for log in logs:
        ref_id = log.get("ref_id", "N/A")
        method = (log.get("method") or "").lower()
        lines.extend([
            "",
            f"{tr(lang, 'wallet_topup_id')}: {md_code(ref_id)}",
            f"{tr(lang, 'date_time')}: {format_dt(log.get('created_at'))}",
            f"{tr(lang, 'payment_method')}: {method_label(method)}",
            f"{tr(lang, 'amount')}: {amount_text(log)}",
            f"{tr(lang, 'status')}: {status_label(log.get('status'), lang)}",
        ])
        if log.get("admin_wallet_adjustment") and log.get("notes"):
            lines.append(f"Note: {log.get('notes')}")
        lines.append("")

    return compact_blank_lines("\n".join(lines))


def wallet_history_keyboard(
    page: int,
    total_count: int,
    callback_prefix: str,
    back_callback: str | None = None,
    load_again_callback: str | None = None,
) -> InlineKeyboardMarkup:
    total_pages = max(1, (total_count + WALLET_HISTORY_PAGE_SIZE - 1) // WALLET_HISTORY_PAGE_SIZE) if total_count else 1
    rows: list[list[InlineKeyboardButton]] = []
    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(InlineKeyboardButton(tr("en", "btn_prev"), callback_data=f"{callback_prefix}:{page - 1}"))
    if page + 1 < total_pages:
        nav.append(InlineKeyboardButton("➡️ Next", callback_data=f"{callback_prefix}:{page + 1}"))
    if nav:
        rows.append(nav)
    if load_again_callback:
        rows.append([InlineKeyboardButton("➕ Top-up Again", callback_data=load_again_callback)])
    if back_callback:
        rows.append([InlineKeyboardButton("🔙 Back", callback_data=back_callback)])
    return InlineKeyboardMarkup(rows)
