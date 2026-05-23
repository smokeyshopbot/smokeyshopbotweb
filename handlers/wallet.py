"""
handlers/wallet.py — Wallet top-up and balance commands.

Commands:
  /wallet            — Show balance
  /loadwallet        — Start wallet top-up flow

Top-up flow:
  1. User picks method (INR via UPI / USDT via BEP20 / USDT via Binance Pay)
  2. User enters amount
  3. Payment flow is initiated (same as order payment)
  4. On confirmation, wallet is credited
"""

import uuid
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

import database as db
from utils.i18n import tr
from utils.crypto import UniqueUsdtAmountUnavailable, generate_unique_usdt_amount
from utils.wallet_history import (
    WALLET_HISTORY_PAGE_SIZE,
    format_wallet_history_text,
    wallet_history_keyboard,
)

# Track users in wallet top-up flow: { user_id: { step, currency, amount } }
_wallet_flow: dict[int, dict] = {}


def _payment_enabled(settings: dict, method: str) -> bool:
    return db.payment_method_enabled(settings, method)


def clear_wallet_flow(user_id: int | str | None) -> None:
    """Clears an active wallet top-up amount session for this user."""
    try:
        uid = int(user_id or 0)
    except (TypeError, ValueError):
        return
    _wallet_flow.pop(uid, None)


def _back_button(lang: str = "en") -> list[InlineKeyboardButton]:
    return [InlineKeyboardButton(tr(lang, "btn_back"), callback_data="nav:back")]


def _fmt_amount(value: float, currency: str) -> str:
    if currency == "inr":
        return f"₹{value:.2f}"
    return f"${value:.2f}"


def _wallet_min_amount(settings: dict, currency: str) -> float:
    limits = settings.get("wallet_limits") if isinstance(settings, dict) else {}
    if not isinstance(limits, dict):
        limits = {}
    if currency == "inr":
        return db.parse_positive_float(limits.get("min_inr"), 50, minimum=0.01)
    return db.parse_positive_float(limits.get("min_usdt"), 1, minimum=0.0001)


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
    if _payment_enabled(settings, "binance"):
        rows.append([InlineKeyboardButton(tr(lang, "wallet_topup_binance"), callback_data="wallet_currency:binance_usdt")])
    if _payment_enabled(settings, "upi"):
        rows.append([InlineKeyboardButton(tr(lang, "wallet_topup_inr"), callback_data="wallet_currency:inr")])
    has_methods = bool(rows)
    rows.append(_back_button(lang))
    return InlineKeyboardMarkup(rows), has_methods


async def cmd_wallet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user = await db.get_user(user_id)
    lang = await db.get_user_language(user_id)
    if not user:
        await update.message.reply_text(tr(lang, "start_first"))
        return

    settings = await db.get_payment_settings()
    inr_bal = float(user.get("wallet_inr", 0.0) or 0.0)
    usdt_bal = float(user.get("wallet_usdt", 0.0) or 0.0)

    lines = [tr(lang, "wallet_title"), ""]
    if _payment_enabled(settings, "wallet_inr"):
        lines.append(tr(lang, "wallet_inr_balance", balance=f"{inr_bal:.2f}"))
    if _payment_enabled(settings, "wallet_usdt"):
        lines.append(tr(lang, "wallet_usdt_balance", balance=f"{usdt_bal:.2f}"))
    if len(lines) == 2:
        lines.append(tr(lang, "wallet_no_currency"))

    keyboard = _wallet_keyboard(lang)
    await update.message.reply_text(
        "\n".join(lines),
        parse_mode="Markdown",
        reply_markup=keyboard
    )


async def _send_load_wallet_options(message, lang: str = "en"):
    keyboard, has_methods = await _load_wallet_keyboard(lang)
    text = tr(lang, "load_wallet_title") if has_methods else tr(lang, "load_wallet_none")
    await message.reply_text(
        text,
        parse_mode="Markdown",
        reply_markup=keyboard
    )


async def cmd_load_wallet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user = await db.get_user(user_id)
    lang = await db.get_user_language(user_id)
    if not user:
        await update.message.reply_text(tr(lang, "start_first"))
        return
    await _send_load_wallet_options(update.message, lang)




async def cmd_wallet_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show this user's wallet top-up history, 10 per page."""
    user = update.effective_user
    user_id = user.id
    await db.upsert_user(user_id, user.username or "")
    lang = await db.get_user_language(user_id)
    if await db.is_blocked(user_id):
        await update.message.reply_text(tr(lang, "blocked"))
        return

    total_count = await db.count_user_wallet_logs(user_id)
    logs = await db.get_user_wallet_logs(user_id, limit=WALLET_HISTORY_PAGE_SIZE, skip=0)
    await update.message.reply_text(
        format_wallet_history_text(logs, page=0, total_count=total_count, lang=lang),
        parse_mode="Markdown",
        reply_markup=wallet_history_keyboard(0, total_count, "wallethistory", back_callback="nav:wallet", load_again_callback="nav:loadwallet"),
    )


async def handle_wallet_history_page(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle Wallet History pagination for normal users."""
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    await db.upsert_user(user_id, query.from_user.username or "")
    lang = await db.get_user_language(user_id)
    if await db.is_blocked(user_id):
        await query.edit_message_text(tr(lang, "blocked"))
        return

    try:
        page = int(query.data.split(":", 1)[1])
    except Exception:
        page = 0

    total_count = await db.count_user_wallet_logs(user_id)
    total_pages = max(1, (total_count + WALLET_HISTORY_PAGE_SIZE - 1) // WALLET_HISTORY_PAGE_SIZE) if total_count else 1
    page = max(0, min(page, total_pages - 1))
    logs = await db.get_user_wallet_logs(user_id, limit=WALLET_HISTORY_PAGE_SIZE, skip=page * WALLET_HISTORY_PAGE_SIZE)
    await query.edit_message_text(
        format_wallet_history_text(logs, page=page, total_count=total_count, lang=lang),
        parse_mode="Markdown",
        reply_markup=wallet_history_keyboard(page, total_count, "wallethistory", back_callback="nav:wallet", load_again_callback="nav:loadwallet"),
    )


async def handle_wallet_currency_select(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    lang = await db.get_user_language(user_id)
    currency = query.data.split(":", 1)[1]

    if currency not in {"inr", "usdt", "binance_usdt"}:
        await query.edit_message_text(tr(lang, "invalid_topup"))
        return

    settings = await db.get_payment_settings()
    method_key = {"inr": "upi", "usdt": "usdt", "binance_usdt": "binance"}[currency]
    if not _payment_enabled(settings, method_key):
        await query.edit_message_text(tr(lang, "topup_unavailable"))
        return

    _wallet_flow[user_id] = {"step": "amount", "currency": currency}
    min_amount = _wallet_min_amount(settings, "inr" if currency == "inr" else "usdt")
    if currency == "inr":
        unit = "₹ (INR)"
        method_note = tr(lang, "method_upi")
        minimum = _fmt_amount(min_amount, "inr")
    elif currency == "binance_usdt":
        unit = "$ (USDT)"
        method_note = tr(lang, "method_binance_pay")
        minimum = _fmt_amount(min_amount, "usdt")
    else:
        unit = "$ (USDT)"
        method_note = tr(lang, "method_bep20_auto")
        minimum = _fmt_amount(min_amount, "usdt")

    await query.edit_message_text(
        tr(lang, "topup_prompt", unit=unit, method_note=method_note, minimum=minimum),
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([_back_button(lang)])
    )

async def handle_wallet_amount_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Returns True if message was consumed as wallet amount input."""
    user_id = update.effective_user.id
    lang = await db.get_user_language(user_id)
    if user_id not in _wallet_flow or _wallet_flow[user_id].get("step") != "amount":
        return False

    state = _wallet_flow[user_id]
    try:
        amount = float(update.message.text.strip())
    except ValueError:
        await update.message.reply_text(tr(lang, "enter_valid_number"))
        return True

    currency = state["currency"]
    settings = await db.get_payment_settings()
    method_key = {"inr": "upi", "usdt": "usdt", "binance_usdt": "binance"}.get(currency, "")
    if not _payment_enabled(settings, method_key):
        _wallet_flow.pop(user_id, None)
        await update.message.reply_text(tr(lang, "topup_no_longer"))
        return True

    min_amount = _wallet_min_amount(settings, "inr" if currency == "inr" else "usdt")
    if amount < min_amount:
        await update.message.reply_text(
            tr(lang, "minimum_topup", amount=_fmt_amount(min_amount, 'inr' if currency == 'inr' else 'usdt'))
        )
        return True

    _wallet_flow.pop(user_id, None)

    ref_id = f"wallet_{uuid.uuid4().hex[:8].upper()}"

    if currency == "inr":
        amount_inr = round(amount, 2)
        await db.create_pending_payment(
            user_id=user_id, ref_id=ref_id, pay_type="wallet",
            method="upi", expected_inr=amount_inr, expected_usdt=0.0,
            unique_usdt=0.0, currency="inr", load_amount=amount_inr
        )
        from handlers.payment import initiate_upi_payment
        await initiate_upi_payment(
            update, context, user_id, ref_id, amount_inr,
            description=f"{tr(lang, 'wallet_topup_id')} `{ref_id}` | ₹{amount_inr:.2f}"
        )

    elif currency == "binance_usdt":
        amount_usdt = round(amount, 2)
        try:
            unique_usdt = await generate_unique_usdt_amount(amount_usdt)
        except UniqueUsdtAmountUnavailable:
            await update.message.reply_text(
                "⚠️ Too many active Binance Pay payments are using this exact amount right now. Please try again in a few minutes."
            )
            return True
        await db.create_pending_payment(
            user_id=user_id, ref_id=ref_id, pay_type="wallet",
            method="binance", expected_inr=0.0, expected_usdt=amount_usdt,
            unique_usdt=unique_usdt, currency="usdt", load_amount=amount_usdt
        )
        from handlers.payment import initiate_binance_payment
        await initiate_binance_payment(
            update, context, user_id, ref_id,
            amount_usdt=unique_usdt,
            description=f"{tr(lang, 'wallet_topup_id')} `{ref_id}` | ${amount_usdt:.2f} USDT"
        )

    else:  # usdt via BEP20
        amount_usdt = round(amount, 2)
        try:
            unique_usdt = await generate_unique_usdt_amount(amount_usdt)
        except UniqueUsdtAmountUnavailable:
            await update.message.reply_text(
                "⚠️ Too many active USDT payments are using this exact amount right now. Please try again in a few minutes."
            )
            return True
        await db.create_pending_payment(
            user_id=user_id, ref_id=ref_id, pay_type="wallet",
            method="usdt", expected_inr=0.0, expected_usdt=amount_usdt,
            unique_usdt=unique_usdt, currency="usdt", load_amount=amount_usdt
        )
        from handlers.payment import initiate_usdt_payment
        await initiate_usdt_payment(
            update, context, user_id, ref_id,
            amount_inr=0, unique_usdt=unique_usdt,
            description=f"{tr(lang, 'wallet_topup_id')} `{ref_id}` | ${amount_usdt:.2f} USDT"
        )

    return True
