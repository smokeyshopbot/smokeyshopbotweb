"""
handlers/payment.py — Payment flows: USDT BEP20, UPI, Binance Pay.
"""

import asyncio
import logging
import io
from decimal import Decimal, InvalidOperation
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from telegram import Update

import database as db
from utils.i18n import tr
from config import (
    SUPPORT_USERNAMES, PAYMENT_TIMEOUT_MINUTES, PAYMENT_REMINDER_MINUTES, USDT_VERIFY_INTERVAL, LOW_STOCK_ALERT_THRESHOLD,
    BEP20_REQUIRED_CONFIRMATIONS, POLYGON_REQUIRED_CONFIRMATIONS, USDT_MANUAL_VERIFY_DELAY_MINUTES, is_admin_id,
)
from utils.bscscan import (
    check_usdt_received_detailed,
    get_usdt_network_label,
    get_usdt_required_confirmations,
    normalize_usdt_network,
    public_usdt_error_text,
    extract_usdt_received_amount_from_error,
    verify_usdt_tx_hash_detailed,
)
from utils.qr import generate_upi_qr
from utils.crypto import generate_unique_usdt_amount
from utils.admin_notify import send_admin_message
from utils.binance_pay import (
    check_binance_pay_received_detailed,
    fetch_binance_pay_history,
    find_matching_binance_pay_transaction,
)

# NOTE: admin imports done inside functions to avoid circular imports

logger = logging.getLogger(__name__)

USDT_PAYMENT_QUANT = Decimal("0.001")
USDT_LEGACY_PAYMENT_QUANT = Decimal("0.000001")
MANUAL_USDT_BEP20_HASH_TOLERANCE = Decimal("0.01")
MANUAL_USDT_POLYGON_HASH_TOLERANCE = Decimal("0.07")


def _manual_usdt_hash_tolerance_for_network(network: str | None) -> Decimal:
    return MANUAL_USDT_POLYGON_HASH_TOLERANCE if normalize_usdt_network(network) == "polygon" else MANUAL_USDT_BEP20_HASH_TOLERANCE


def _decimal_usdt(value) -> Decimal | None:
    """Normalize expected USDT amounts while keeping old 6-decimal pending rows valid."""
    try:
        amount = Decimal(str(value))
        if amount == amount.quantize(USDT_PAYMENT_QUANT):
            return amount.quantize(USDT_PAYMENT_QUANT)
        return amount.quantize(USDT_LEGACY_PAYMENT_QUANT)
    except (InvalidOperation, TypeError, ValueError):
        return None


def _format_payment_usdt(value) -> str:
    """Format the user-facing crypto payment amount with exactly 3 decimals."""
    try:
        amount = Decimal(str(value or 0))
    except (InvalidOperation, TypeError, ValueError):
        amount = Decimal("0")
    return f"{amount.quantize(USDT_PAYMENT_QUANT):.3f}"


def _format_wallet_usdt_display(value) -> str:
    """Format wallet balances/top-up credits with normal 2-decimal money display."""
    try:
        return f"{float(value or 0):.2f}"
    except (TypeError, ValueError):
        return "0.00"


def _raw_decimal_usdt(value) -> Decimal | None:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


def _tx_actual_usdt(transaction: dict | None) -> Decimal | None:
    tx = transaction or {}
    for key in ("match_actual_usdt", "value_usdt", "amount"):
        amount = _raw_decimal_usdt(tx.get(key))
        if amount is not None:
            return amount
    try:
        raw_value = tx.get("value")
        token_decimal = int(tx.get("tokenDecimal", "18") or 18)
        if raw_value is not None:
            return Decimal(str(raw_value)) / (Decimal(10) ** token_decimal)
    except Exception:
        return None
    return None


def _tx_match_type(transaction: dict | None, expected) -> str:
    tx = transaction or {}
    match_type = str(tx.get("match_type") or "").lower().strip()
    if match_type:
        return match_type
    actual = _tx_actual_usdt(tx)
    expected_dec = _decimal_usdt(expected)
    if actual is not None and expected_dec is not None and actual == expected_dec:
        return "exact"
    return "non_exact"


async def _is_exact_usdt_match(ref_id: str, method: str, transaction: dict | None) -> bool:
    """Return True only when the detected USDT amount exactly matches the unique amount.

    Tolerance-based auto-verification is disabled. If a buyer underpays,
    rounds the amount, or forgets the unique decimals, the payment must go
    through Manual Verify instead of being auto-delivered.
    """
    pending = await db.get_pending_by_ref(ref_id)
    if not pending:
        return False

    expected = _decimal_usdt(pending.get("unique_usdt") or pending.get("expected_usdt"))
    actual = _tx_actual_usdt(transaction)
    if expected is None or actual is None:
        return False

    if actual == expected:
        return True

    logger.info(
        "USDT exact match required; detected amount ignored. ref=%s method=%s actual=%s expected=%s",
        ref_id, method, actual, expected,
    )
    return False

# { user_id: { "step": "payer_name"|"txn_id"|"screenshot", "ref_id", "payer_name", "txn_id" } }
_upi_collection: dict[int, dict] = {}

# { user_id: { "step": "binance_name"|"screenshot", "ref_id", "binance_name" } }
_binance_collection: dict[int, dict] = {}

# { ref_id: { user_id, msg_id, chat_id } }
_payment_msg_map: dict[str, dict] = {}

# { ref_id: asyncio.Task }
_usdt_tasks: dict[str, asyncio.Task] = {}

# { ref_id: asyncio.Task }
_payment_notice_tasks: dict[str, asyncio.Task] = {}

# { ref_id: asyncio.Task }
_payment_timer_tasks: dict[str, asyncio.Task] = {}

# Single process-wide watcher that scans all waiting BEP20 payments.
# This is more reliable on hosts where per-payment background tasks can be lost.
_usdt_global_watcher_task: asyncio.Task | None = None

# Single process-wide watcher that scans all waiting Binance Pay payments.
_binance_global_watcher_task: asyncio.Task | None = None


# { user_id: { "step": "txn_hash"|"screenshot", "ref_id", "txn_hash" } }
_usdt_manual: dict[int, dict] = {}


def is_payment_manual_input_active(user_id: int | str | None) -> bool:
    """True when a user is in the middle of a manual payment proof flow."""
    try:
        uid = int(user_id or 0)
    except (TypeError, ValueError):
        return False
    return uid in _upi_collection or uid in _binance_collection or uid in _usdt_manual


def clear_payment_manual_flows(user_id: int | str | None) -> None:
    """Cancel any active manual payment proof collection for this user.

    This only clears in-memory input steps. It does not cancel or expire the
    actual pending payment record, so the user can still press manual verify
    again from the payment page if needed.
    """
    try:
        uid = int(user_id or 0)
    except (TypeError, ValueError):
        return
    _upi_collection.pop(uid, None)
    _binance_collection.pop(uid, None)
    _usdt_manual.pop(uid, None)


def _details_from_pending(pending: dict | None) -> dict:
    details = (pending or {}).get("payment_details") or {}
    return details if isinstance(details, dict) else {}


async def _current_payment_settings() -> dict:
    return await db.get_payment_settings()


async def _lang_for_user(user_id: int | str | None) -> str:
    try:
        return await db.get_user_language(int(user_id or 0))
    except Exception:
        return "en"


def _usdt_network_for_method(method: str | None) -> str:
    return "polygon" if str(method or "").lower() in {"polygon", "usdt_polygon", "polygon_usdt"} else "bep20"


def _usdt_network_for_pending(pending: dict | None = None) -> str:
    details = _details_from_pending(pending)
    return normalize_usdt_network(details.get("usdt_network") or _usdt_network_for_method((pending or {}).get("method")))


def _usdt_method_from_network(network: str | None = None) -> str:
    return "polygon" if normalize_usdt_network(network) == "polygon" else "usdt"


def _usdt_payment_label(method_or_network: str | None = None) -> str:
    return get_usdt_network_label(method_or_network)


async def _get_usdt_wallet_for_pending(pending: dict | None = None) -> str:
    details = _details_from_pending(pending)
    network = _usdt_network_for_pending(pending)
    if network == "polygon":
        wallet = str(details.get("usdt_polygon_wallet_address") or details.get("usdt_wallet_address") or "").strip()
        if wallet:
            return wallet
        settings = await _current_payment_settings()
        if db.payment_method_enabled(settings, "polygon"):
            return str(settings["usdt_polygon"].get("wallet_address") or "").strip()
        return ""

    wallet = str(details.get("usdt_wallet_address") or "").strip()
    if wallet:
        return wallet
    settings = await _current_payment_settings()
    if db.payment_method_enabled(settings, "usdt"):
        return str(settings["usdt_bep20"].get("wallet_address") or "").strip()
    return ""


async def _payment_method_unavailable(context: ContextTypes.DEFAULT_TYPE, user_id: int, ref_id: str, label: str):
    try:
        await db.update_pending_status(ref_id, "expired")
    except Exception:
        pass
    try:
        order = await db.get_order(ref_id)
        if order and order.get("status") == "pending":
            await db.update_order_status(ref_id, "failed")
    except Exception:
        pass
    lang = await _lang_for_user(user_id)
    await context.bot.send_message(
        user_id,
        tr(lang, "payment_method_unavailable", label=label),
    )


async def _wallet_load_id_line(ref_id: str, lang: str = "en") -> str:
    """Wallet top-up IDs are shown in the main payment description line."""
    return ""


def _manual_verify_unlock_message(pending: dict, lang: str = "en") -> str:
    """Return dynamic Manual Verify unlock text for a pending USDT payment."""
    import time as _time

    delay_minutes = int(USDT_MANUAL_VERIFY_DELAY_MINUTES or 5)
    delay_seconds = max(0, delay_minutes * 60)
    created_at = float(pending.get("created_at") or _time.time())
    age_seconds = _time.time() - created_at

    if age_seconds >= delay_seconds:
        return tr(lang, "manual_unlocked")

    remaining_seconds = max(1, int(delay_seconds - age_seconds))
    remaining_minutes = max(1, (remaining_seconds + 59) // 60)
    return tr(lang, "manual_unlocks", minutes=remaining_minutes)


def _format_payment_time_left(seconds_left: int | float) -> str:
    seconds = max(0, int(seconds_left))
    minutes, seconds = divmod(seconds, 60)
    if minutes >= 60:
        hours, minutes = divmod(minutes, 60)
        return f"{hours}h {minutes:02d}m"
    return f"{minutes}m {seconds:02d}s"


def _render_payment_template(template: str, pending: dict | None = None) -> str:
    import time as _time
    pending = pending or {}
    created_at = float(pending.get("created_at") or _time.time())
    expire_at = created_at + max(1, PAYMENT_TIMEOUT_MINUTES) * 60
    remaining = max(0, int(expire_at - _time.time()))
    return (str(template or "")
            .replace("{{TIME_LEFT}}", _format_payment_time_left(remaining))
            .replace("{TIME_LEFT}", _format_payment_time_left(remaining)))


async def _try_auto_confirm_before_manual(update: Update, context: ContextTypes.DEFAULT_TYPE, pending: dict) -> bool:
    """Silently run one auto-check before opening manual verification.

    This is intentionally quiet: when a buyer taps Manual Verify, we first give
    the automatic verifier one last chance. If it detects payment, the normal
    delivery/top-up flow runs. If it does not detect payment, the user simply
    continues into the usual manual verification steps.
    """
    ref_id = str((pending or {}).get("ref_id") or "")
    if not ref_id or pending.get("status") != "waiting":
        return False
    query = update.callback_query
    method = str(pending.get("method") or "").lower()
    try:
        if method in {"usdt", "polygon"}:
            network = _usdt_network_for_pending(pending)
            wallet = await _get_usdt_wallet_for_pending(pending)
            result = await check_usdt_received_detailed(pending.get("unique_usdt"), wallet_address=wallet, network=network)
            if result.found:
                if await _is_exact_usdt_match(ref_id, method, result.tx or {}):
                    try:
                        await query.answer()
                    except Exception:
                        pass
                    await _on_usdt_confirmed(context, int(pending.get("user_id") or query.from_user.id), ref_id, result.tx or {})
                    return True
                logger.info("Manual Verify pre-check found %s but amount was not exact ref=%s", _usdt_payment_label(network), ref_id)
            logger.info(
                "Manual Verify pre-check did not find %s ref=%s amount=%s reason=%s",
                _usdt_payment_label(network), ref_id, pending.get("unique_usdt"), result.short_error_text(),
            )
        elif method == "binance":
            result = await _check_binance_pending_payment(pending)
            if result.found:
                if await _is_exact_usdt_match(ref_id, "binance", result.transaction or {}):
                    try:
                        await query.answer()
                    except Exception:
                        pass
                    await _on_binance_confirmed(context, int(pending.get("user_id") or query.from_user.id), ref_id, result.transaction or {})
                    return True
                logger.info("Manual Verify pre-check found Binance Pay but amount was not exact ref=%s", ref_id)
            logger.info(
                "Manual Verify pre-check did not find Binance Pay ref=%s amount=%s reason=%s",
                ref_id, pending.get("unique_usdt"), result.short_error_text(),
            )
    except Exception as exc:
        logger.warning("Manual Verify silent auto-check failed for ref=%s method=%s: %s", ref_id, method, exc)
    return False


def _payment_keyboard_for_pending(pending: dict) -> InlineKeyboardMarkup | None:
    ref_id = str((pending or {}).get("ref_id") or "")
    method = str((pending or {}).get("method") or "").lower()
    if not ref_id:
        return None
    if method in {"usdt", "polygon"}:
        return InlineKeyboardMarkup([[
            InlineKeyboardButton(tr((pending or {}).get("language"), "btn_check_payment"), callback_data=f"check_usdt:{ref_id}"),
            InlineKeyboardButton(tr((pending or {}).get("language"), "btn_manual_verify"), callback_data=f"usdt_manual:{ref_id}"),
        ]])
    if method == "binance":
        return InlineKeyboardMarkup([[
            InlineKeyboardButton(tr((pending or {}).get("language"), "btn_check_payment"), callback_data=f"check_binance:{ref_id}"),
            InlineKeyboardButton(tr((pending or {}).get("language"), "btn_manual_verify"), callback_data=f"binance_paid:{ref_id}"),
        ]])
    if method == "upi":
        return InlineKeyboardMarkup([[InlineKeyboardButton(tr((pending or {}).get("language"), "btn_ive_paid"), callback_data=f"upi_paid:{ref_id}")]])
    return None


async def _send_payment_detected_notice(bot, pending: dict, *, method_label: str):
    """Mark auto-detected payment internally without sending an extra user message.

    Manual Check Payment still uses Telegram's short callback toast. For background
    auto-verification, the user should only receive the normal delivered order or
    wallet top-up completion message.
    """
    ref_id = str((pending or {}).get("ref_id") or "")
    if not ref_id:
        return
    try:
        await db.get_db().pending_payments.update_one(
            {"ref_id": ref_id, "auto_detected_notice_sent_at": {"$exists": False}},
            {"$set": {"auto_detected_notice_sent_at": __import__('datetime').datetime.now(__import__('datetime').timezone.utc)}},
        )
    except Exception:
        pass

# ───────────────────────── USDT PAYMENT ──────────────────────

async def initiate_usdt_payment(
    update_or_query,
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
    ref_id: str,
    amount_inr: float,
    unique_usdt: float,
    description: str,
    method: str = "usdt",
):
    lang = await _lang_for_user(user_id)
    settings = await _current_payment_settings()
    method = _usdt_method_from_network(method)
    network = _usdt_network_for_method(method)
    method_label = _usdt_payment_label(network)
    if not db.payment_method_enabled(settings, method):
        await _payment_method_unavailable(context, user_id, ref_id, f"{method_label} payment")
        return

    if network == "polygon":
        usdt_wallet = str(settings["usdt_polygon"].get("wallet_address") or "").strip()
        payment_details = {
            "usdt_network": "polygon",
            "usdt_polygon_wallet_address": usdt_wallet,
            "usdt_wallet_address": usdt_wallet,
            "language": lang,
        }
        title = tr(lang, "usdt_polygon_payment_title")
        network_line = tr(lang, "network_polygon")
    else:
        usdt_wallet = str(settings["usdt_bep20"].get("wallet_address") or "").strip()
        payment_details = {
            "usdt_network": "bep20",
            "usdt_wallet_address": usdt_wallet,
            "language": lang,
        }
        title = tr(lang, "usdt_payment_title")
        network_line = tr(lang, "network_bep20")

    await db.set_pending_payment_config(ref_id, payment_details)
    display_unique_usdt = _format_payment_usdt(unique_usdt)
    required_confirmations = get_usdt_required_confirmations(network)

    wallet_id_line = await _wallet_load_id_line(ref_id, lang)
    text_template = (
        f"{title}\n\n"
        f"📋 {description}\n"
        f"{wallet_id_line}\n"
        f"{tr(lang, 'send_this_amount')}\n"
        f"```\n{display_unique_usdt} USDT\n```\n"
        f"{tr(lang, 'to_this_wallet')}\n"
        f"`{usdt_wallet}`\n\n"
        f"{tr(lang, 'usdt_exact_warning')}\n"
        f"{network_line}\n\n"
        f"{tr(lang, 'time_left')}\n"
        f"{tr(lang, 'bot_checks_every', seconds=USDT_VERIFY_INTERVAL)}\n"
        f"{tr(lang, 'confirmations_required', confirmations=required_confirmations)}\n"
        f"{tr(lang, 'manual_if_paid')}"
    )
    text = _render_payment_template(text_template, {"created_at": __import__('time').time()})
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton(tr(lang, "btn_check_payment"), callback_data=f"check_usdt:{ref_id}"),
        InlineKeyboardButton(tr(lang, "btn_manual_verify"), callback_data=f"usdt_manual:{ref_id}"),
    ]])

    if hasattr(update_or_query, "message") and update_or_query.message:
        msg = await update_or_query.message.reply_text(text, parse_mode="Markdown", reply_markup=keyboard)
    else:
        msg = await context.bot.send_message(user_id, text, parse_mode="Markdown", reply_markup=keyboard)

    _payment_msg_map[ref_id] = {"user_id": user_id, "msg_id": msg.message_id, "chat_id": msg.chat_id}
    await db.set_pending_payment_message(ref_id, msg.chat_id, msg.message_id)
    await db.set_pending_payment_message_meta(ref_id, kind="text", template=text_template)
    schedule_payment_notice_task(context, ref_id)
    schedule_payment_timer_task(context, ref_id)

    create_task = getattr(getattr(context, "application", None), "create_task", None)
    if callable(create_task):
        task = create_task(_poll_usdt(context, user_id, ref_id, unique_usdt, usdt_wallet, network=network))
    else:
        task = asyncio.create_task(_poll_usdt(context, user_id, ref_id, unique_usdt, usdt_wallet, network=network))
    _usdt_tasks[ref_id] = task

async def _poll_usdt(context, user_id: int, ref_id: str, unique_usdt: float, usdt_wallet: str | None = None, network: str | None = None):
    """Background polling — checks BSCScan until the configured payment window expires."""
    interval = max(5, USDT_VERIFY_INTERVAL)
    attempts = max(1, (PAYMENT_TIMEOUT_MINUTES * 60) // interval)
    for _ in range(attempts):
        await asyncio.sleep(interval)
        pending = await db.get_pending_by_ref(ref_id)
        if not pending or pending["status"] != "waiting":
            return
        try:
            network_key = normalize_usdt_network(network or _usdt_network_for_pending(pending))
            wallet = usdt_wallet or await _get_usdt_wallet_for_pending(pending)
            result = await check_usdt_received_detailed(unique_usdt, wallet_address=wallet, network=network_key)
            found = result.found
            if not found:
                logger.info(
                    "USDT auto-check pending ref=%s amount=%s reason=%s",
                    ref_id, unique_usdt, result.short_error_text()
                )
        except Exception as exc:
            logger.exception("USDT auto-check crashed for ref=%s: %s", ref_id, exc)
            found = False
        if found:
            logger.info("USDT payment detected ref=%s amount=%s", ref_id, unique_usdt)
            await _on_usdt_confirmed(context, user_id, ref_id, result.tx or {})
            return
    # Timeout
    await _expire_payment_session(context.bot, ref_id)


async def handle_check_usdt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """'Check Payment' button — verifies pending/expired USDT payments and explains processed states."""
    query = update.callback_query
    ref_id = query.data.split(":", 1)[1]
    pending = await db.get_pending_by_ref(ref_id)

    if not pending:
        lang = await _lang_for_user(query.from_user.id)
        await query.answer(tr(lang, "payment_session_not_found"), show_alert=True)
        return

    lang = pending.get("language") or await _lang_for_user(pending.get("user_id") or query.from_user.id)
    user_id = query.from_user.id
    if int(pending.get("user_id", 0)) != int(user_id) and not is_admin_id(user_id):
        await query.answer(tr(lang, "payment_not_yours"), show_alert=True)
        return

    status = pending.get("status")

    if status != "waiting":
        if pending.get("pay_type") == "order":
            order = await db.get_order(ref_id)
            if order:
                order_status = order.get("status")
                if order_status == "delivered":
                    await _delete_payment_msg(context, ref_id)
                    await query.answer(tr(lang, "payment_already_processed_delivered"), show_alert=True)
                    return
                if order_status == "pending_stock":
                    await _delete_payment_msg(context, ref_id)
                    await query.answer(tr(lang, "payment_received_waiting_stock"), show_alert=True)
                    return
                if status in {"confirmed", "approved"}:
                    await query.answer(tr(lang, "payment_already_found_retry"), show_alert=False)
                    await complete_order(context.bot, int(pending["user_id"]), ref_id)
                    return

        if pending.get("pay_type") == "wallet":
            if status in {"confirmed", "approved", "completed"} or pending.get("wallet_credited_at"):
                await _delete_payment_msg(context, ref_id)
                await _send_wallet_load_status(context.bot, int(pending["user_id"]), pending, already=True)
                await query.answer(tr(lang, "wallet_topup_already_completed"), show_alert=True)
                return

        if status == "expired" and pending.get("method") in {"usdt", "polygon"}:
            try:
                network = _usdt_network_for_pending(pending)
                wallet = await _get_usdt_wallet_for_pending(pending)
                result = await check_usdt_received_detailed(pending["unique_usdt"], wallet_address=wallet, network=network)
                found = result.found
                if not found:
                    logger.info("Expired Check Payment did not find USDT ref=%s amount=%s reason=%s", ref_id, pending.get("unique_usdt"), result.short_error_text())
            except Exception as exc:
                logger.exception("Expired Check Payment crashed for ref=%s: %s", ref_id, exc)
                found = False

            if found:
                if not await _is_exact_usdt_match(ref_id, str(pending.get("method") or "usdt"), result.tx or {}):
                    logger.info("Late USDT payment found but amount was not exact ref=%s", ref_id)
                    await query.answer(tr(lang, "payment_not_found", unlock_text=""), show_alert=True)
                    return
                confirmed_pending = await db.confirm_expired_usdt_payment(ref_id, result.tx or {})
                if not confirmed_pending:
                    logger.info("Late USDT payment could not be claimed ref=%s", ref_id)
                    await query.answer(tr(lang, "payment_not_found", unlock_text=""), show_alert=True)
                    return
                await query.answer(tr(lang, "late_payment_detected"), show_alert=False)
                if confirmed_pending.get("pay_type") == "order":
                    await complete_order(context.bot, int(confirmed_pending["user_id"]), ref_id)
                else:
                    await complete_wallet_load(context.bot, int(confirmed_pending["user_id"]), confirmed_pending)
                return

        await query.answer(tr(lang, "payment_status", status=status), show_alert=True)
        return

    try:
        network = _usdt_network_for_pending(pending)
        wallet = await _get_usdt_wallet_for_pending(pending)
        result = await check_usdt_received_detailed(pending["unique_usdt"], wallet_address=wallet, network=network)
        found = result.found
        if not found:
            logger.info("Manual Check Payment did not find USDT ref=%s amount=%s reason=%s", ref_id, pending.get("unique_usdt"), result.short_error_text())
    except Exception as exc:
        logger.exception("Manual Check Payment crashed for ref=%s: %s", ref_id, exc)
        found = False

    if found:
        if not await _is_exact_usdt_match(ref_id, str(pending.get("method") or "usdt"), result.tx or {}):
            logger.info("Manual Check Payment found USDT but amount was not exact ref=%s", ref_id)
            unlock_text = _manual_verify_unlock_message(pending, lang)
            await query.answer(tr(lang, "payment_not_found", unlock_text=unlock_text), show_alert=True)
            return
        await query.answer(tr(lang, "payment_detected_processing"), show_alert=False)
        await _on_usdt_confirmed(context, int(pending["user_id"]), ref_id, result.tx or {})
    else:
        unlock_text = _manual_verify_unlock_message(pending, lang)
        await query.answer(tr(lang, "payment_not_found", unlock_text=unlock_text), show_alert=True)

async def handle_usdt_manual_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """User pressed 'Manual Verify' — ask for txn hash when manual verification is unlocked."""
    query = update.callback_query
    ref_id = query.data.split(":", 1)[1]
    user_id = query.from_user.id

    pending = await db.get_pending_by_ref(ref_id)
    lang = (pending or {}).get("language") or await _lang_for_user(user_id)
    if not pending or pending.get("status") != "waiting":
        await query.answer(tr(lang, "session_processed_or_expired"), show_alert=True)
        return

    if int(pending.get("user_id", 0)) != int(user_id) and not is_admin_id(user_id):
        await query.answer(tr(lang, "payment_not_yours"), show_alert=True)
        return

    unlock_text = _manual_verify_unlock_message(pending, lang)
    if tr(lang, "manual_unlocked") not in unlock_text:
        await query.answer(tr(lang, "auto_running_unlock", unlock_text=unlock_text), show_alert=True)
        return

    await query.answer()
    _usdt_manual[user_id] = {"step": "txn_hash", "ref_id": ref_id, "language": lang}
    await context.bot.send_message(
        user_id,
        f"{tr(lang, 'manual_usdt_title')}\n\n{tr(lang, 'manual_usdt_body')}",
        parse_mode="Markdown"
    )

async def _try_auto_confirm_submitted_usdt_hash(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    pending: dict,
    txn_hash: str,
    lang: str,
) -> str:
    """Try to auto-confirm a manually submitted USDT tx hash.

    Returns: "confirmed", "duplicate", or "review".
    """
    ref_id = str(pending.get("ref_id") or "")
    user_id = int(pending.get("user_id") or update.effective_user.id)
    network = _usdt_network_for_pending(pending)
    tolerance = _manual_usdt_hash_tolerance_for_network(network)
    try:
        wallet = await _get_usdt_wallet_for_pending(pending)
        min_ts = db.created_at_to_timestamp(pending.get("created_at"))
        result = await verify_usdt_tx_hash_detailed(
            txn_hash,
            pending.get("unique_usdt") or pending.get("expected_usdt"),
            wallet_address=wallet,
            network=network,
            min_timestamp=min_ts,
            amount_tolerance=tolerance,
        )
    except Exception as exc:
        logger.exception("Manual USDT tx-hash auto-check crashed ref=%s hash=%s: %s", ref_id, txn_hash, exc)
        await db.record_usdt_manual_auto_check_result(
            ref_id,
            result="error",
            reason="Automatic TxHash check could not finish. Admin review required.",
            txn_hash=txn_hash,
            network=network,
        )
        return "review"

    if not result.found:
        reason = public_usdt_error_text(result.errors)
        extra = {"expected_usdt": pending.get("unique_usdt") or pending.get("expected_usdt"), "tolerance_usdt": tolerance}
        received_usdt = extract_usdt_received_amount_from_error(result.errors)
        if received_usdt:
            extra["received_usdt"] = received_usdt
        await db.record_usdt_manual_auto_check_result(
            ref_id,
            result="failed",
            reason=reason,
            txn_hash=txn_hash,
            network=network,
            extra=extra,
        )
        logger.info(
            "Manual USDT tx-hash auto-check left for review ref=%s hash=%s reason=%s",
            ref_id, txn_hash, reason,
        )
        return "review"

    confirmed_pending = await db.confirm_manual_usdt_payment_if_waiting(ref_id, result.tx or {})
    if not confirmed_pending:
        if await db.find_used_usdt_tx_hash(txn_hash, exclude_ref_id=ref_id):
            await db.record_usdt_manual_auto_check_result(
                ref_id,
                result="duplicate",
                reason="This TxHash is already linked to another payment.",
                txn_hash=txn_hash,
                network=network,
            )
            return "duplicate"
        await db.record_usdt_manual_auto_check_result(
            ref_id,
            result="failed",
            reason="TxHash passed the chain check, but the payment was no longer waiting when the bot tried to claim it.",
            txn_hash=txn_hash,
            network=network,
        )
        logger.info("Manual USDT tx-hash verified but payment could not be claimed ref=%s", ref_id)
        return "review"

    logger.info("Manual USDT tx-hash auto-verified ref=%s hash=%s", ref_id, txn_hash)
    if confirmed_pending.get("pay_type") == "order":
        await complete_order(context.bot, user_id, ref_id)
    else:
        await complete_wallet_load(context.bot, user_id, confirmed_pending)
    return "confirmed"


async def handle_usdt_manual_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Collects txn hash for manual USDT verification. Returns True if consumed."""
    user_id = update.effective_user.id
    state = _usdt_manual.get(user_id)
    if not state:
        return False
    lang = state.get("language") or await _lang_for_user(user_id)

    if state.get("step") == "screenshot":
        await update.message.reply_text(tr(lang, "send_usdt_screenshot_photo"), parse_mode="Markdown")
        return True

    txn_hash = db.normalize_usdt_tx_hash(update.message.text)
    ref_id = state["ref_id"]

    if not db.is_valid_usdt_tx_hash(txn_hash):
        _usdt_manual[user_id]["step"] = "txn_hash"
        await update.message.reply_text(tr(lang, "usdt_tx_hash_invalid"), parse_mode="Markdown")
        return True

    pending = await db.get_pending_by_ref(ref_id)
    if not pending or pending["status"] != "waiting":
        _usdt_manual.pop(user_id, None)
        await update.message.reply_text(tr(lang, "session_no_longer_active"))
        return True

    if await db.find_used_usdt_tx_hash(txn_hash, exclude_ref_id=ref_id):
        _usdt_manual[user_id]["step"] = "txn_hash"
        await update.message.reply_text(tr(lang, "usdt_tx_hash_already_used"), parse_mode="Markdown")
        return True

    auto_result = await _try_auto_confirm_submitted_usdt_hash(update, context, pending, txn_hash, lang)
    if auto_result == "confirmed":
        _usdt_manual.pop(user_id, None)
        return True
    if auto_result == "duplicate":
        _usdt_manual[user_id]["step"] = "txn_hash"
        await update.message.reply_text(tr(lang, "usdt_tx_hash_already_used"), parse_mode="Markdown")
        return True

    _usdt_manual[user_id]["txn_hash"] = txn_hash
    _usdt_manual[user_id]["step"] = "screenshot"
    await update.message.reply_text(tr(lang, "now_send_usdt_screenshot"), parse_mode="Markdown")
    return True

async def handle_usdt_manual_screenshot(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Collects USDT manual screenshot after TxHash."""
    user_id = update.effective_user.id
    state = _usdt_manual.get(user_id)
    if not state or state.get("step") != "screenshot":
        return False
    lang = state.get("language") or await _lang_for_user(user_id)

    if not update.message.photo:
        await update.message.reply_text(tr(lang, "send_screenshot_not_file"))
        return True

    ref_id = state["ref_id"]
    txn_hash = state.get("txn_hash", "N/A")
    photo = update.message.photo[-1]
    _usdt_manual.pop(user_id, None)

    pending = await db.get_pending_by_ref(ref_id)
    if not pending or pending["status"] != "waiting":
        await update.message.reply_text(tr(lang, "session_no_longer_active"))
        return True

    network = _usdt_network_for_pending(pending)
    if not await db.set_usdt_manual_details(ref_id, txn_hash, photo.file_id, network=network):
        _usdt_manual[user_id] = {"step": "txn_hash", "ref_id": ref_id, "language": lang}
        await update.message.reply_text(tr(lang, "usdt_tx_hash_already_used"), parse_mode="Markdown")
        return True

    await update.message.reply_text(tr(lang, "manual_usdt_submitted"), parse_mode="Markdown")
    logger.info("Manual USDT proof %s saved for WebAdmin review.", ref_id)
    return True

async def _on_usdt_confirmed(context, user_id: int, ref_id: str, transaction: dict | None = None):
    """Process a detected BEP20 payment exactly once.

    The background auto-check task calls this function too. Do NOT cancel the
    current task from inside itself; doing that was the reason auto-check could
    detect a transfer but stop before delivering items / crediting wallet.
    """
    current_task = asyncio.current_task()
    task = _usdt_tasks.pop(ref_id, None)
    if task and task is not current_task and not task.done():
        task.cancel()

    # Claim the payment atomically. If another worker/button already claimed it,
    # use the latest row and only retry safe idempotent completion paths.
    if transaction and not await _is_exact_usdt_match(ref_id, "usdt", transaction):
        logger.info("USDT confirmation skipped because amount was not exact ref=%s", ref_id)
        return
    pending = await db.confirm_pending_usdt_payment_if_waiting(ref_id, transaction) if transaction else await db.confirm_pending_payment_if_waiting(ref_id)
    if not pending:
        pending = await db.get_pending_by_ref(ref_id)
        if not pending:
            logger.info("USDT confirmation ignored; payment row missing ref=%s", ref_id)
            return

        status = pending.get("status")
        if pending.get("pay_type") == "order" and status in {"confirmed", "approved"}:
            order = await db.get_order(ref_id)
            if order and order.get("status") in {"delivered", "pending_stock", "expired", "cancelled", "rejected"}:
                await _delete_payment_msg_by_bot(context.bot, ref_id)
                logger.info("USDT payment already confirmed; order already terminal/queued ref=%s order_status=%s", ref_id, order.get("status"))
                return
            logger.info("USDT payment already confirmed; retrying order completion ref=%s", ref_id)
            await _delete_payment_msg_by_bot(context.bot, ref_id)
            await complete_order(context.bot, int(pending.get("user_id", user_id)), ref_id)
            return

        if pending.get("pay_type") == "wallet" and status in {"confirmed", "approved"}:
            logger.info("USDT wallet payment already confirmed; retrying wallet credit ref=%s", ref_id)
            await complete_wallet_load(context.bot, int(pending.get("user_id", user_id)), pending)
            return

        logger.info("USDT confirmation ignored; ref=%s status=%s", ref_id, status)
        return

    await _send_payment_detected_notice(context.bot, pending, method_label=_usdt_payment_label(_usdt_network_for_pending(pending)))
    await _delete_payment_msg_by_bot(context.bot, ref_id)

    if pending["pay_type"] == "order":
        await complete_order(context.bot, user_id, ref_id)
    else:
        # Wallet top-up completion is idempotent and marks the row completed.
        await complete_wallet_load(context.bot, user_id, pending)


# ───────────────────────── UPI PAYMENT ───────────────────────

async def initiate_upi_payment(
    update_or_query,
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
    ref_id: str,
    amount_inr: float,
    description: str,
):
    lang = await _lang_for_user(user_id)
    settings = await _current_payment_settings()
    if not db.payment_method_enabled(settings, "upi"):
        await _payment_method_unavailable(context, user_id, ref_id, "UPI payment")
        return

    upi_id = str(settings["upi"].get("upi_id") or "").strip()
    upi_name = str(settings["upi"].get("upi_name") or "Merchant").strip() or "Merchant"
    await db.set_pending_payment_config(ref_id, {"upi_id": upi_id, "upi_name": upi_name, "language": lang})

    try:
        qr_bytes = await generate_upi_qr(amount_inr, upi_id, upi_name, note=ref_id)
    except Exception:
        qr_bytes = None

    wallet_id_line = await _wallet_load_id_line(ref_id, lang)
    caption_template = (
        f"{tr(lang, 'upi_payment_title')}\n\n"
        f"📋 {description}\n"
        f"{wallet_id_line}\n"
        f"{tr(lang, 'upi_amount', amount=f'{amount_inr:.2f}')}\n"
        f"{tr(lang, 'upi_id', upi_id=upi_id)}\n"
        f"{tr(lang, 'upi_name', name=upi_name)}\n\n"
        f"{tr(lang, 'upi_scan')}\n"
        f"{tr(lang, 'upi_after_pay')}\n\n"
        f"{tr(lang, 'time_left')}\n"
        f"{tr(lang, 'screenshot_required')}"
    )
    caption = _render_payment_template(caption_template, {"created_at": __import__('time').time()})
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton(tr(lang, "btn_ive_paid"), callback_data=f"upi_paid:{ref_id}")
    ]])

    if hasattr(update_or_query, "message") and update_or_query.message:
        chat_id = update_or_query.message.chat_id
    else:
        chat_id = user_id

    if qr_bytes:
        msg = await context.bot.send_photo(chat_id, photo=qr_bytes, caption=caption, parse_mode="Markdown", reply_markup=keyboard)
        kind = "caption"
    else:
        msg = await context.bot.send_message(chat_id, caption, parse_mode="Markdown", reply_markup=keyboard)
        kind = "text"

    _payment_msg_map[ref_id] = {"user_id": user_id, "msg_id": msg.message_id, "chat_id": chat_id}
    await db.set_pending_payment_message(ref_id, chat_id, msg.message_id)
    await db.set_pending_payment_message_meta(ref_id, kind=kind, template=caption_template)
    schedule_payment_notice_task(context, ref_id)
    schedule_payment_timer_task(context, ref_id)

async def handle_upi_paid_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    ref_id = query.data.split(":", 1)[1]
    user_id = query.from_user.id

    pending = await db.get_pending_by_ref(ref_id)
    lang = (pending or {}).get("language") or await _lang_for_user(user_id)
    if not pending or pending["status"] != "waiting":
        try:
            await query.edit_message_text(tr(lang, "upi_expired"))
        except Exception:
            await context.bot.send_message(user_id, tr(lang, "upi_expired"))
        return

    await _delete_payment_msg(context, ref_id)

    _upi_collection[user_id] = {"step": "payer_name", "ref_id": ref_id, "language": lang}
    await context.bot.send_message(user_id, tr(lang, "upi_payer_prompt"), parse_mode="Markdown")

async def handle_upi_text_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    user_id = update.effective_user.id
    if user_id not in _upi_collection:
        return False

    state = _upi_collection[user_id]
    lang = state.get("language") or await _lang_for_user(user_id)
    text = update.message.text.strip()

    if state["step"] == "payer_name":
        _upi_collection[user_id]["payer_name"] = text
        _upi_collection[user_id]["step"] = "txn_id"
        await update.message.reply_text(tr(lang, "upi_utr_prompt"), parse_mode="Markdown")
        return True

    if state["step"] == "txn_id":
        _upi_collection[user_id]["txn_id"] = text
        _upi_collection[user_id]["step"] = "screenshot"
        await update.message.reply_text(tr(lang, "upi_screenshot_prompt"), parse_mode="Markdown")
        return True

    if state["step"] == "screenshot":
        await update.message.reply_text(tr(lang, "payment_screenshot_photo"), parse_mode="Markdown")
        return True

    return False

async def handle_upi_screenshot(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Collects UPI payment screenshot after payer name + UTR."""
    user_id = update.effective_user.id
    state = _upi_collection.get(user_id)
    if not state or state.get("step") != "screenshot":
        return False
    lang = state.get("language") or await _lang_for_user(user_id)

    if not update.message.photo:
        await update.message.reply_text(tr(lang, "send_screenshot_not_file"))
        return True

    ref_id = state["ref_id"]
    payer_name = state.get("payer_name", "")
    txn_id = state.get("txn_id", "")
    photo = update.message.photo[-1]
    _upi_collection.pop(user_id)

    pending = await db.get_pending_by_ref(ref_id)
    if not pending or pending["status"] != "waiting":
        await update.message.reply_text(tr(lang, "session_no_longer_active"))
        return True

    await db.set_upi_details(ref_id, payer_name, txn_id, photo.file_id)
    await update.message.reply_text(tr(lang, "manual_submitted_admin"), parse_mode="Markdown")
    logger.info("UPI proof %s saved for WebAdmin review.", ref_id)
    return True

async def initiate_binance_payment(
    update_or_query,
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
    ref_id: str,
    amount_usdt: float,
    description: str,
):
    lang = await _lang_for_user(user_id)
    settings = await _current_payment_settings()
    if not db.payment_method_enabled(settings, "binance"):
        await _payment_method_unavailable(context, user_id, ref_id, "Binance Pay")
        return

    binance_pay_id = str(settings["binance"].get("binance_pay_id") or "").strip()
    binance_pay_name = str(settings["binance"].get("binance_pay_name") or "Merchant").strip() or "Merchant"
    display_amount_usdt = _format_payment_usdt(amount_usdt)
    await db.set_pending_payment_config(ref_id, {
        "binance_pay_id": binance_pay_id,
        "binance_pay_name": binance_pay_name,
        "binance_unique_usdt": display_amount_usdt,
        "language": lang,
    })

    wallet_id_line = await _wallet_load_id_line(ref_id, lang)
    text_template = (
        f"{tr(lang, 'binance_payment_title')}\n\n"
        f"📋 {description}\n"
        f"{wallet_id_line}\n"
        f"{tr(lang, 'send_exact_amount')}\n"
        f"```\n{display_amount_usdt} USDT\n```\n"
        f"{tr(lang, 'binance_pay_id', pay_id=binance_pay_id)}\n"
        f"{tr(lang, 'binance_name', name=binance_pay_name)}\n\n"
        f"{tr(lang, 'binance_steps', pay_id=binance_pay_id)}\n\n"
        f"{tr(lang, 'binance_no_round')}\n"
        f"{tr(lang, 'time_left')}\n"
        f"{tr(lang, 'bot_checks_every', seconds=USDT_VERIFY_INTERVAL)}\n"
        f"{tr(lang, 'manual_if_paid')}"
    )
    text = _render_payment_template(text_template, {"created_at": __import__('time').time()})
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton(tr(lang, "btn_check_payment"), callback_data=f"check_binance:{ref_id}"),
        InlineKeyboardButton(tr(lang, "btn_manual_verify"), callback_data=f"binance_paid:{ref_id}"),
    ]])

    if hasattr(update_or_query, "message") and update_or_query.message:
        chat_id = update_or_query.message.chat_id
    else:
        chat_id = user_id

    msg = await context.bot.send_message(chat_id, text, parse_mode="Markdown", reply_markup=keyboard)
    _payment_msg_map[ref_id] = {"user_id": user_id, "msg_id": msg.message_id, "chat_id": chat_id}
    await db.set_pending_payment_message(ref_id, chat_id, msg.message_id)
    await db.set_pending_payment_message_meta(ref_id, kind="text", template=text_template)
    schedule_payment_notice_task(context, ref_id)
    schedule_payment_timer_task(context, ref_id)

async def _check_binance_pending_payment(pending: dict):
    used_ids = await db.get_used_binance_transaction_ids()
    return await check_binance_pay_received_detailed(
        pending.get("unique_usdt") or pending.get("expected_usdt"),
        pending.get("created_at"),
        used_transaction_ids=used_ids,
    )


async def handle_check_binance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """'Check Payment' button for Binance Pay history auto-verification."""
    query = update.callback_query
    ref_id = query.data.split(":", 1)[1]
    pending = await db.get_pending_by_ref(ref_id)

    if not pending:
        lang = await _lang_for_user(query.from_user.id)
        await query.answer(tr(lang, "payment_session_not_found"), show_alert=True)
        return

    lang = pending.get("language") or await _lang_for_user(pending.get("user_id") or query.from_user.id)
    user_id = query.from_user.id
    if int(pending.get("user_id", 0)) != int(user_id) and not is_admin_id(user_id):
        await query.answer(tr(lang, "payment_not_yours"), show_alert=True)
        return

    status = pending.get("status")
    if status != "waiting":
        if pending.get("pay_type") == "order":
            order = await db.get_order(ref_id)
            if order:
                order_status = order.get("status")
                if order_status == "delivered":
                    await _delete_payment_msg(context, ref_id)
                    await query.answer(tr(lang, "payment_already_processed_delivered"), show_alert=True)
                    return
                if order_status == "pending_stock":
                    await _delete_payment_msg(context, ref_id)
                    await query.answer(tr(lang, "payment_received_waiting_stock"), show_alert=True)
                    return
                if status in {"confirmed", "approved"}:
                    await query.answer(tr(lang, "payment_already_found_retry"), show_alert=False)
                    await complete_order(context.bot, int(pending["user_id"]), ref_id)
                    return

        if pending.get("pay_type") == "wallet":
            if status in {"confirmed", "approved", "completed"} or pending.get("wallet_credited_at"):
                await _delete_payment_msg(context, ref_id)
                await _send_wallet_load_status(context.bot, int(pending["user_id"]), pending, already=True)
                await query.answer(tr(lang, "wallet_topup_already_completed"), show_alert=True)
                return

        if status == "expired" and pending.get("method") == "binance":
            result = await _check_binance_pending_payment(pending)
            if result.found:
                await query.answer(tr(lang, "late_binance_detected"), show_alert=False)
                await _on_binance_confirmed(context, int(pending["user_id"]), ref_id, result.transaction or {})
                return
            logger.info("Expired Check Payment did not find Binance Pay ref=%s amount=%s reason=%s", ref_id, pending.get("unique_usdt"), result.short_error_text())

        await query.answer(tr(lang, "payment_status", status=status), show_alert=True)
        return

    result = await _check_binance_pending_payment(pending)
    if result.found:
        if not await _is_exact_usdt_match(ref_id, "binance", result.transaction or {}):
            logger.info("Manual Binance Check Payment found transaction but amount was not exact ref=%s", ref_id)
            unlock_text = _manual_verify_unlock_message(pending, lang)
            await query.answer(tr(lang, "binance_not_found", unlock_text=unlock_text), show_alert=True)
            return
        await query.answer(tr(lang, "binance_detected_processing"), show_alert=False)
        await _on_binance_confirmed(context, int(pending["user_id"]), ref_id, result.transaction or {})
    else:
        logger.info("Manual Binance Check Payment did not find ref=%s amount=%s reason=%s", ref_id, pending.get("unique_usdt"), result.short_error_text())
        unlock_text = _manual_verify_unlock_message(pending, lang)
        await query.answer(tr(lang, "binance_not_found", unlock_text=unlock_text), show_alert=True)

async def _on_binance_confirmed(context, user_id: int, ref_id: str, transaction: dict):
    """Process a detected Binance Pay history transaction exactly once."""
    if transaction and not await _is_exact_usdt_match(ref_id, "binance", transaction):
        logger.info("Binance confirmation skipped because amount was not exact ref=%s", ref_id)
        return
    pending = await db.confirm_pending_binance_payment_if_waiting(ref_id, transaction)
    if not pending:
        pending = await db.get_pending_by_ref(ref_id)
        if not pending:
            logger.info("Binance confirmation ignored; payment row missing ref=%s", ref_id)
            return

        status = pending.get("status")
        if pending.get("pay_type") == "order" and status in {"confirmed", "approved"}:
            order = await db.get_order(ref_id)
            if order and order.get("status") in {"delivered", "pending_stock", "expired", "cancelled", "rejected"}:
                await _delete_payment_msg_by_bot(context.bot, ref_id)
                logger.info("Binance payment already confirmed; order already terminal/queued ref=%s order_status=%s", ref_id, order.get("status"))
                return
            logger.info("Binance payment already confirmed; retrying order completion ref=%s", ref_id)
            await _delete_payment_msg_by_bot(context.bot, ref_id)
            await complete_order(context.bot, int(pending.get("user_id", user_id)), ref_id)
            return

        if pending.get("pay_type") == "wallet" and status in {"confirmed", "approved", "completed"}:
            logger.info("Binance wallet payment already confirmed; retrying wallet credit ref=%s", ref_id)
            await complete_wallet_load(context.bot, int(pending.get("user_id", user_id)), pending)
            return

        logger.info("Binance confirmation ignored; ref=%s status=%s", ref_id, status)
        return

    await _send_payment_detected_notice(context.bot, pending, method_label="Binance Pay")
    await _delete_payment_msg_by_bot(context.bot, ref_id)

    if pending["pay_type"] == "order":
        await complete_order(context.bot, int(pending.get("user_id", user_id)), ref_id)
    else:
        await complete_wallet_load(context.bot, int(pending.get("user_id", user_id)), pending)


async def handle_binance_paid_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    ref_id = query.data.split(":", 1)[1]
    user_id = query.from_user.id

    pending = await db.get_pending_by_ref(ref_id)
    lang = (pending or {}).get("language") or await _lang_for_user(user_id)
    if not pending or pending.get("status") != "waiting":
        await query.answer(tr(lang, "session_processed_or_expired"), show_alert=True)
        return

    if int(pending.get("user_id", 0)) != int(user_id) and not is_admin_id(user_id):
        await query.answer(tr(lang, "payment_not_yours"), show_alert=True)
        return

    unlock_text = _manual_verify_unlock_message(pending, lang)
    if tr(lang, "manual_unlocked") not in unlock_text:
        await query.answer(tr(lang, "auto_running_unlock", unlock_text=unlock_text), show_alert=True)
        return

    await query.answer()
    _binance_collection[user_id] = {"step": "binance_name", "ref_id": ref_id, "language": lang}
    await context.bot.send_message(user_id, f"{tr(lang, 'binance_manual_title')}\n\n{tr(lang, 'binance_manual_body')}", parse_mode="Markdown")

async def handle_binance_text_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    user_id = update.effective_user.id
    if user_id not in _binance_collection:
        return False

    state = _binance_collection[user_id]
    lang = state.get("language") or await _lang_for_user(user_id)

    if state["step"] == "binance_name":
        pending = await db.get_pending_by_ref(state.get("ref_id", ""))
        if not pending or pending.get("status") != "waiting":
            _binance_collection.pop(user_id, None)
            await update.message.reply_text(tr(lang, "session_no_longer_active"))
            return True

        name = update.message.text.strip() if update.message.text else None
        if not name:
            await update.message.reply_text(tr(lang, "binance_name_text"))
            return True
        _binance_collection[user_id]["binance_name"] = name
        _binance_collection[user_id]["step"] = "screenshot"
        await update.message.reply_text(tr(lang, "binance_screenshot_prompt"), parse_mode="Markdown")
        return True

    return False

async def handle_binance_screenshot(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    user_id = update.effective_user.id
    state = _binance_collection.get(user_id)

    if not state or state.get("step") != "screenshot":
        return False
    lang = state.get("language") or await _lang_for_user(user_id)

    if not update.message.photo:
        await update.message.reply_text(tr(lang, "send_screenshot_not_file"))
        return True

    ref_id = state["ref_id"]
    binance_name = state.get("binance_name", "N/A")
    photo = update.message.photo[-1]
    _binance_collection.pop(user_id)

    pending = await db.get_pending_by_ref(ref_id)
    if not pending or pending.get("status") != "waiting":
        await update.message.reply_text(tr(lang, "session_no_longer_active"))
        return True

    await db.set_binance_details(ref_id, binance_name, photo.file_id)
    await update.message.reply_text(tr(lang, "binance_manual_submitted"), parse_mode="Markdown")
    logger.info("Binance proof %s saved for WebAdmin review.", ref_id)
    return True

def _support_url(value: str) -> str | None:
    value = (value or "").strip()
    if not value:
        return None
    if value.startswith(("http://", "https://")):
        return value
    username = value.lstrip("@").strip()
    return f"https://t.me/{username}" if username else None


def _support_markup(lang: str = "en") -> InlineKeyboardMarkup | None:
    buttons = []
    for idx, support in enumerate(SUPPORT_USERNAMES, 1):
        url = _support_url(support)
        if url:
            label = tr(lang, "support_open") if len(SUPPORT_USERNAMES) == 1 else tr(lang, "support_open_n", n=idx)
            buttons.append([InlineKeyboardButton(label, url=url)])
    return InlineKeyboardMarkup(buttons) if buttons else None

def _is_admin_created_order(order: dict | None) -> bool:
    """Return True for admin-created orders, including older rows saved before the flag existed."""
    if not order:
        return False
    if bool(order.get("admin_created_order")):
        return True
    method = str(order.get("payment_method") or "").strip().lower()
    return method in {"admin_created_order", "admin_created"} or "admin_created" in method


async def _send_pending_stock_notice(bot, user_id: int, order: dict, *, queued: bool = False):
    order_id = str(order.get("order_id", "N/A") or "N/A")
    try:
        if not await db.claim_pending_stock_notice(order_id):
            logger.info("Pending-stock notice already sent for order=%s", order_id)
            return
    except Exception:
        logger.exception("Could not claim pending-stock notice for order=%s", order_id)
        return
    lang = await _lang_for_user(user_id)
    product_name = order.get("product_name", "Product")
    quantity = int(order.get("quantity", 0) or 0)
    if _is_admin_created_order(order):
        queue_note = tr(lang, "admin_created_order_stock_queued") if queued else tr(lang, "admin_created_order_stock_waiting")
        message = tr(
            lang,
            "admin_created_order_pending_stock_notice",
            product=product_name,
            order_id=order_id,
            quantity=quantity,
            queue_note=queue_note,
        )
    else:
        queue_note = tr(lang, "pending_stock_queued") if queued else tr(lang, "pending_stock_waiting")
        message = tr(lang, "pending_stock_notice", product=product_name, order_id=order_id, queue_note=queue_note)
    await bot.send_message(
        user_id,
        message,
        parse_mode="Markdown",
        reply_markup=_support_markup(lang),
    )


async def _send_admin_created_order_created_notice(bot, user_id: int, order: dict) -> None:
    if not _is_admin_created_order(order):
        return
    lang = await _lang_for_user(user_id)
    try:
        await bot.send_message(
            user_id,
            tr(
                lang,
                "admin_created_order_created_notice",
                product=order.get("product_name", "Product"),
                order_id=order.get("order_id", "N/A"),
                quantity=int(order.get("quantity", 0) or 0),
            ),
            parse_mode="Markdown",
            reply_markup=_support_markup(lang),
        )
    except Exception:
        logger.exception("Could not send admin-created order notice for order=%s", order.get("order_id"))


def _localized_order_txt_instructions(order: dict, lang: str = "en") -> str:
    """Pick the order/product TXT instructions for the user's language."""
    if not order:
        return ""
    normalized = (lang or "en").strip().lower()
    keys = []
    if normalized.startswith("es"):
        keys.extend(["order_txt_instructions_es", "order_txt_instructions_en"])
    else:
        keys.extend(["order_txt_instructions_en", "order_txt_instructions_es"])
    keys.extend(["order_txt_instructions", "delivery_instructions"])
    for key in keys:
        value = str(order.get(key) or "").strip()
        if value:
            return value
    return ""


async def _with_product_txt_instructions(order: dict) -> dict:
    """Use saved order instructions; fall back to current product settings for old orders."""
    if not order:
        return order
    if str(order.get("order_txt_instructions_en") or "").strip() or str(order.get("order_txt_instructions_es") or "").strip():
        return order
    product_name = str(order.get("product_name") or "").strip()
    if not product_name:
        return order
    try:
        product = await db.get_product(product_name)
    except Exception:
        logger.exception("Could not load product TXT instructions for order=%s", order.get("order_id"))
        return order
    if not product:
        return order
    enriched = dict(order)
    enriched["order_txt_instructions_en"] = str(product.get("order_txt_instructions_en") or "").strip()
    enriched["order_txt_instructions_es"] = str(product.get("order_txt_instructions_es") or "").strip()
    return enriched

def _delivery_txt_filename(order: dict) -> str:
    order_id = str(order.get("order_id") or "order").strip() or "order"
    safe_order_id = "".join(ch for ch in order_id if ch.isalnum() or ch in ("-", "_")).strip() or "order"
    if order.get("is_replacement"):
        return f"replacement_{safe_order_id}_items.txt"
    return f"order_{safe_order_id}_items.txt"


def _delivery_txt_content(order: dict, items: list[str], lang: str = "en") -> str:
    order_id = str(order.get("order_id", "N/A"))
    product_name = str(order.get("product_name", "Product"))
    quantity = int(order.get("quantity", len(items) or 0) or 0)
    lines = [
        tr(lang, "order_items_title"),
        f"{tr(lang, 'order_id')}: {order_id}",
        f"{tr(lang, 'product')}: {product_name}",
        f"{tr(lang, 'quantity')}: {quantity}",
    ]
    instructions = _localized_order_txt_instructions(order, lang)
    if instructions:
        lines.extend(["", f"{tr(lang, 'delivery_instructions_label')}:", instructions])
    lines.extend(["", f"{tr(lang, 'items_label')}:"])
    for item in items:
        # Stock/content must be delivered exactly as saved; only labels above are translated.
        lines.extend([str(item).strip(), ""])
    return "\n".join(lines).rstrip() + "\n"

def _delivery_caption(order: dict, *, from_pending: bool = False, lang: str = "en") -> str:
    order_id = str(order.get("order_id") or "N/A")
    product_name = str(order.get("product_name") or "Product")
    quantity = int(order.get("quantity", 0) or 0)
    if order.get("is_replacement"):
        title = tr(lang, "replacement_resent_admin") if order.get("resent_by_admin") else tr(lang, "replacement_sent_admin")
        return (
            f"{title}\n\n"
            f"🧾 {tr(lang, 'replacement_id')}: {order_id}\n"
            f"🛠 {tr(lang, 'report_id')}: {order.get('replacement_report_id', 'N/A')}\n"
            f"📦 {tr(lang, 'product')}: {product_name}\n"
            f"🔢 {tr(lang, 'quantity')}: {quantity}"
        )
    if order.get("resent_by_admin"):
        title = tr(lang, "order_resent_admin")
    elif _is_admin_created_order(order):
        title = tr(lang, "admin_created_order_delivery_title")
    else:
        title = tr(lang, "pending_order_delivered") if from_pending else tr(lang, "order_placed_confirmed")
    return (
        f"{title}\n\n"
        f"🧾 {tr(lang, 'order_id')}: {order_id}\n"
        f"📦 {tr(lang, 'product')}: {product_name}\n"
        f"🔢 {tr(lang, 'quantity')}: {quantity}"
    )

async def _send_delivery_txt_file(bot, user_id: int, order: dict, items: list[str], *, from_pending: bool = False) -> None:
    lang = await _lang_for_user(user_id)
    order = await _with_product_txt_instructions(order)
    data = _delivery_txt_content(order, items, lang=lang).encode("utf-8")
    document = io.BytesIO(data)
    document.name = _delivery_txt_filename(order)
    try:
        sent_message = await bot.send_document(
            chat_id=user_id,
            document=document,
            filename=document.name,
            caption=_delivery_caption(order, from_pending=from_pending, lang=lang),
        )
        await db.record_order_delivery_message(
            str(order.get("order_id") or ""),
            user_id,
            getattr(sent_message, "message_id", None),
            filename=document.name,
            sent_by="bot_delivery",
            resent=bool(order.get("resent_by_admin")),
        )
    except Exception:
        logger.exception("Could not send delivery txt file for order=%s", order.get("order_id"))

async def _send_order_items(bot, user_id: int, order: dict, items: list[str], *, from_pending: bool = False):
    """Deliver purchased stock as one TXT file only."""
    lang = await _lang_for_user(user_id)
    if not items:
        if order.get("resent_by_admin"):
            title = tr(lang, "order_resent_admin")
        elif _is_admin_created_order(order):
            title = tr(lang, "admin_created_order_delivery_title")
        else:
            title = tr(lang, "pending_order_delivered") if from_pending else tr(lang, "order_placed_confirmed")
        await bot.send_message(
            user_id,
            f"{title}\n\n📦 {tr(lang, 'product')}: {order.get('product_name', 'Product')}\n🔢 {tr(lang, 'quantity')}: {int(order.get('quantity', 0) or 0)}\n\n{tr(lang, 'no_items_attached')}",
        )
        return

    await _send_delivery_txt_file(bot, user_id, order, items, from_pending=from_pending)

async def notify_low_stock_if_needed(bot, product_name: str):
    """Notify admin once when actual stock drops below the configured threshold."""
    product = await db.get_product(product_name)
    if not product:
        return

    stock_count = len(product.get("stock", []) or [])
    threshold = max(1, int(product.get("low_stock_threshold") or LOW_STOCK_ALERT_THRESHOLD or 10))

    # Reset the alert once stock is healthy again, so future low-stock drops alert again.
    if stock_count >= threshold:
        if product.get("low_stock_alert_sent"):
            await db.set_low_stock_alert_sent(product_name, False)
        return

    if product.get("low_stock_alert_sent"):
        return

    await db.set_low_stock_alert_sent(product_name, True)
    try:
        await send_admin_message(
            bot,
            f"⚠️ *Low Stock Alert*\n\n"
            f"Product: *{product.get('name', product_name)}*\n"
            f"Stock left: *{stock_count}*\n"
            f"Alert threshold: below *{threshold}* units",
            parse_mode="Markdown",
        )
    except Exception:
        pass


async def complete_order(bot, user_id: int, ref_id: str):
    # Claim the order before touching stock. The same paid order can be reached
    # by the background auto-checker, Check Payment button, startup recovery, or
    # admin retry at nearly the same time. This lock prevents a duplicate worker
    # from popping stock and then overwriting the order back to pending_stock.
    order = await db.claim_order_delivery(ref_id)
    if not order:
        latest = await db.get_order(ref_id)
        if not latest:
            return
        if latest.get("status") in {"delivered", "expired", "cancelled", "rejected"}:
            return
        logger.info("Order delivery already in progress or not deliverable ref=%s status=%s", ref_id, latest.get("status"))
        return

    delivery_token = order.get("delivery_lock_token")
    product_name = order["product_name"]
    quantity = int(order.get("quantity", 1) or 1)

    # If older paid orders are already waiting for this product, do not let
    # a newer payment consume the next stock. Put this order in the same queue.
    if order.get("status") != "pending_stock":
        if await db.has_pending_stock_ahead(product_name, order.get("created_at"), order.get("order_id")):
            await db.mark_order_pending_stock(ref_id, delivery_token=delivery_token)
            order["status"] = "pending_stock"
            await _delete_payment_msg_by_bot(bot, ref_id)

            # A newly paid order may have been placed while older paid orders
            # were already holding available stock. Drain the pending-stock queue
            # immediately instead of waiting for the next stock upload, otherwise
            # one order can look "stuck" even though enough stock exists.
            try:
                await process_pending_stock_orders(bot, product_name)
            except Exception as exc:
                logger.exception("Could not drain pending-stock queue after queuing %s: %s", ref_id, exc)

            latest = await db.get_order(ref_id)
            if latest and latest.get("status") == "delivered":
                return

            await _send_pending_stock_notice(bot, user_id, order, queued=True)
            await send_admin_message(
                bot,
                f"⏳ Paid order queued behind older pending stock orders.\n"
                f"Product: {product_name}\nQuantity: {quantity}\nUser: {user_id}\nRef: {ref_id}"
            )
            return

    try:
        items = await db.pop_stock(product_name, quantity)
    except Exception as exc:
        logger.exception("Could not pop stock for paid order %s: %s", ref_id, exc)
        await db.mark_order_pending_stock(ref_id, delivery_token=delivery_token)
        order["status"] = "pending_stock"
        await _delete_payment_msg_by_bot(bot, ref_id)
        await _send_pending_stock_notice(bot, user_id, order)
        await send_admin_message(
            bot,
            f"⚠️ Paid order could not be delivered automatically and was moved to pending stock.\n"
            f"Product: {product_name}\nQuantity: {quantity}\nUser: {user_id}\nRef: {ref_id}\nError: {exc}"
        )
        return

    if not items:
        await db.mark_order_pending_stock(ref_id, delivery_token=delivery_token)
        order["status"] = "pending_stock"
        await _delete_payment_msg_by_bot(bot, ref_id)
        await _send_pending_stock_notice(bot, user_id, order)
        await send_admin_message(
            bot,
            f"⚠️ Paid order is waiting for stock.\n"
            f"Product: {product_name}\nQuantity: {quantity}\nUser: {user_id}\nRef: {ref_id}\n\n"
            f"Add stock with /addstock {product_name}. The bot will auto-deliver pending paid orders first."
        )
        return

    was_pending_stock = order.get("status") == "pending_stock"
    updated = await db.update_order_status(ref_id, "delivered", items, delivery_token=delivery_token)
    if not updated:
        # This should be very rare because this worker holds the lock. Do not
        # send stock to the user if we could not attach those items to the order.
        logger.error("Could not finalize delivered order after stock pop ref=%s items=%s", ref_id, len(items))
        try:
            await db.restore_popped_stock_items(
                product_name,
                items,
                source="telegram_bot",
                note=f"Restored because paid order {ref_id} could not be finalized",
            )
            await db.clear_order_delivery_lock(ref_id, delivery_token=delivery_token)
        except Exception:
            logger.exception("Could not restore stock after delivery finalization failure ref=%s", ref_id)
        try:
            await send_admin_message(
                bot,
                f"🚨 Stock delivery finalization failed after stock was removed, so the stock was restored.\n"
                f"Product: {product_name}\nQuantity: {quantity}\nUser: {user_id}\nRef: {ref_id}\n"
                f"Please retry delivery after checking this order."
            )
        except Exception:
            pass
        return

    if _is_admin_created_order(order) and not was_pending_stock:
        await _send_admin_created_order_created_notice(bot, user_id, order)
    await _send_order_items(bot, user_id, order, items, from_pending=was_pending_stock)
    await _delete_payment_msg_by_bot(bot, ref_id)
    await notify_low_stock_if_needed(bot, product_name)

    # If enough stock remains for other paid waiting orders, clear them now.
    # This is a safety net for orders that became pending while stock was
    # already available, so they do not sit until the next manual stock upload.
    try:
        await process_pending_stock_orders(bot, product_name)
    except Exception as exc:
        logger.exception("Could not drain pending-stock queue after delivering %s: %s", ref_id, exc)

async def recover_stuck_wallet_orders(bot, limit: int = 100) -> dict:
    """Finalize wallet-paid orders that accidentally stayed plain pending.

    Wallet orders already deducted the user's balance, so they must not sit
    in the unpaid Pending state. On startup and after wallet checkout, this
    retries delivery; if stock is unavailable, complete_order moves them to
    pending_stock.
    """
    try:
        stuck_orders = await db.get_stuck_wallet_pending_orders(limit=limit)
    except AttributeError:
        return {"checked": 0, "finalized": 0, "still_pending": 0}

    finalized = 0
    still_pending = 0
    for order in stuck_orders:
        order_id = order.get("order_id")
        if not order_id:
            continue
        before_status = order.get("status")
        try:
            await complete_order(bot, int(order.get("user_id", 0) or 0), order_id)
        except Exception as exc:
            logger.exception("Wallet order recovery failed for %s: %s", order_id, exc)
            try:
                await db.mark_order_pending_stock(order_id)
                still_pending += 1
            except Exception:
                pass
            continue

        latest = await db.get_order(order_id)
        if latest and latest.get("status") != before_status:
            finalized += 1
        elif latest and latest.get("status") == "pending":
            # Safety net: a charged wallet order should never stay unpaid pending.
            await db.mark_order_pending_stock(order_id)
            still_pending += 1

    if finalized or still_pending:
        try:
            await send_admin_message(
                bot,
                f"🔁 Wallet order recovery checked {len(stuck_orders)} order(s).\n"
                f"Finalized: {finalized}\nMoved to pending stock: {still_pending}"
            )
        except Exception:
            pass
    return {"checked": len(stuck_orders), "finalized": finalized, "still_pending": still_pending}


async def process_pending_stock_orders(bot, product_name: str, *, limit: int = 100, max_passes: int = 3) -> dict:
    """Deliver paid pending-stock orders for a product, oldest first.

    The function refetches the queue for a few passes and stops at the first
    order that cannot be fulfilled or is actively locked by another worker. This
    keeps FIFO priority while also fixing stuck queues when stock is already
    available for more than one pending order.
    """
    delivered_orders = 0
    delivered_items = 0
    locked_orders = 0
    blocked_by_stock = 0
    restored_items = 0

    try:
        safe_passes = max(1, min(int(max_passes or 3), 10))
    except Exception:
        safe_passes = 3
    try:
        safe_limit = max(1, min(int(limit or 100), 500))
    except Exception:
        safe_limit = 100

    for _pass in range(safe_passes):
        pending_orders = await db.get_pending_stock_orders(product_name, limit=safe_limit)
        if not pending_orders:
            break

        pass_delivered = 0
        stop_pass = False
        for order in pending_orders:
            order_id = order.get("order_id")
            qty = int(order.get("quantity", 1) or 1)
            stock_now = await db.get_stock_count(product_name)
            if stock_now < qty:
                blocked_by_stock += 1
                stop_pass = True
                break

            claimed = await db.claim_order_delivery(order_id)
            if not claimed:
                # Do not skip an older locked paid order and deliver younger
                # orders first. The background watcher/startup recovery will
                # retry after the short delivery lock expires.
                locked_orders += 1
                stop_pass = True
                break
            delivery_token = claimed.get("delivery_lock_token")

            items: list[str] = []
            try:
                items = await db.pop_stock(product_name, qty)
            except Exception as exc:
                logger.exception("Could not pop stock for pending-stock order %s: %s", order_id, exc)
                await db.mark_order_pending_stock(order_id, delivery_token=delivery_token)
                stop_pass = True
                break

            if not items:
                await db.mark_order_pending_stock(order_id, delivery_token=delivery_token)
                blocked_by_stock += 1
                stop_pass = True
                break

            updated = await db.update_order_status(order_id, "delivered", items, delivery_token=delivery_token)
            if not updated:
                logger.error("Could not finalize pending-stock order after stock pop order=%s", order_id)
                try:
                    restored_items += await db.restore_popped_stock_items(
                        product_name,
                        items,
                        source="telegram_bot",
                        note=f"Restored because pending-stock order {order_id} could not be finalized",
                    )
                    await db.clear_order_delivery_lock(order_id, delivery_token=delivery_token)
                except Exception:
                    logger.exception("Could not restore stock after pending-stock finalization failure order=%s", order_id)
                try:
                    await send_admin_message(
                        bot,
                        f"🚨 Pending-stock finalization failed after stock was removed, so the stock was restored.\n"
                        f"Product: {product_name}\nQuantity: {qty}\nRef: {order_id}"
                    )
                except Exception:
                    pass
                stop_pass = True
                break

            delivered_orders += 1
            pass_delivered += 1
            delivered_items += len(items)
            try:
                await _send_order_items(bot, int(order["user_id"]), order, items, from_pending=True)
                await _delete_payment_msg_by_bot(bot, order_id)
            except Exception as exc:
                logger.exception("Could not notify user for pending delivery order=%s: %s", order_id, exc)

        if stop_pass or pass_delivered == 0:
            break

    if delivered_orders:
        try:
            await send_admin_message(
                bot,
                f"✅ Auto-delivered {delivered_orders} pending order(s) for {product_name} "
                f"using {delivered_items} stock item(s)."
            )
        except Exception:
            pass

    try:
        await notify_low_stock_if_needed(bot, product_name)
    except Exception:
        pass
    return {
        "orders_delivered": delivered_orders,
        "items_delivered": delivered_items,
        "locked_orders": locked_orders,
        "blocked_by_stock": blocked_by_stock,
        "restored_items": restored_items,
    }

async def _send_wallet_load_status(bot, user_id: int, pending: dict, *, already: bool = False):
    lang = pending.get("language") or await _lang_for_user(user_id)
    currency = pending.get("currency", "inr")
    load_amount = float(pending.get("load_amount", 0.0) or 0.0)
    user = await db.get_user(user_id) or {}
    title = tr(lang, "wallet_already_completed") if already else tr(lang, "wallet_completed")

    if currency == "inr":
        bal = float(user.get("wallet_inr", 0.0) or 0.0)
        await bot.send_message(
            user_id,
            f"{title}\n\n{tr(lang, 'wallet_added_inr', amount=f'{load_amount:.2f}')}\n{tr(lang, 'wallet_current_inr', balance=f'{bal:.2f}')}\n\n{tr(lang, 'wallet_use_wallet')}",
            parse_mode="Markdown"
        )
    else:
        bal = float(user.get("wallet_usdt", 0.0) or 0.0)
        await bot.send_message(
            user_id,
            f"{title}\n\n{tr(lang, 'wallet_added_usdt', amount=_format_wallet_usdt_display(load_amount))}\n{tr(lang, 'wallet_current_usdt', balance=_format_wallet_usdt_display(bal))}\n\n{tr(lang, 'wallet_use_wallet')}",
            parse_mode="Markdown"
        )

async def complete_wallet_load(bot, user_id: int, pending: dict):
    ref_id = pending.get("ref_id", "")
    credited_row = await db.mark_wallet_load_credited(ref_id)

    if not credited_row:
        # Already credited or not a wallet payment. Do not add balance again.
        await _delete_payment_msg_by_bot(bot, ref_id)
        latest = await db.get_pending_by_ref(ref_id) or pending
        await _send_wallet_load_status(bot, user_id, latest, already=True)
        return

    currency = credited_row.get("currency", "inr")
    load_amount = float(credited_row.get("load_amount", 0.0) or 0.0)

    if currency == "inr":
        await db.add_wallet_inr(user_id, load_amount)
    else:
        await db.add_wallet_usdt(user_id, load_amount)

    await _delete_payment_msg_by_bot(bot, ref_id)
    await _send_wallet_load_status(bot, user_id, credited_row, already=False)


# ───────────────────── PAYMENT REMINDERS / EXPIRY ─────────────────────

def schedule_payment_timer_task(context_or_application, ref_id: str):
    """Update the original payment message countdown in-place."""
    if not ref_id:
        return
    existing = _payment_timer_tasks.get(ref_id)
    if existing and not existing.done():
        return
    create_task = getattr(context_or_application, "create_task", None)
    coro = _payment_timer_worker(context_or_application, ref_id)
    if callable(create_task):
        _payment_timer_tasks[ref_id] = create_task(coro)
    else:
        _payment_timer_tasks[ref_id] = asyncio.create_task(coro)


async def _payment_timer_worker(context_or_application, ref_id: str):
    import time as _time
    last_text = None
    try:
        while True:
            pending = await db.get_pending_by_ref(ref_id)
            if not pending or pending.get("status") != "waiting":
                return
            template = str(pending.get("payment_message_template") or "")
            chat_id = pending.get("payment_chat_id")
            msg_id = pending.get("payment_msg_id")
            if not template or not chat_id or not msg_id:
                return
            created_at = float(pending.get("created_at") or _time.time())
            expire_at = created_at + max(1, PAYMENT_TIMEOUT_MINUTES) * 60
            remaining = max(0, int(expire_at - _time.time()))
            text = _render_payment_template(template, pending)
            if text != last_text:
                try:
                    kind = str(pending.get("payment_message_kind") or "text")
                    markup = _payment_keyboard_for_pending(pending)
                    if kind == "caption":
                        await context_or_application.bot.edit_message_caption(
                            chat_id=chat_id,
                            message_id=msg_id,
                            caption=text,
                            parse_mode="Markdown",
                            reply_markup=markup,
                        )
                    else:
                        await context_or_application.bot.edit_message_text(
                            chat_id=chat_id,
                            message_id=msg_id,
                            text=text,
                            parse_mode="Markdown",
                            reply_markup=markup,
                        )
                    last_text = text
                except Exception as exc:
                    logger.info("Could not update payment countdown ref=%s: %s", ref_id, exc)
            if remaining <= 0:
                return
            await asyncio.sleep(min(60, max(5, remaining)))
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.exception("Payment countdown worker crashed for ref=%s: %s", ref_id, exc)
    finally:
        _payment_timer_tasks.pop(ref_id, None)


def schedule_payment_notice_task(context_or_application, ref_id: str):
    """Schedule 20-minute reminder and payment-window expiry notice."""
    if not ref_id:
        return
    existing = _payment_notice_tasks.get(ref_id)
    if existing and not existing.done():
        return
    create_task = getattr(context_or_application, "create_task", None)
    coro = _payment_notice_worker(context_or_application, ref_id)
    if callable(create_task):
        _payment_notice_tasks[ref_id] = create_task(coro)
    else:
        _payment_notice_tasks[ref_id] = asyncio.create_task(coro)


async def _payment_notice_worker(context_or_application, ref_id: str):
    """Send reminder after PAYMENT_REMINDER_MINUTES and expire after PAYMENT_TIMEOUT_MINUTES.

    It only acts while the pending payment row is still status='waiting'. Once the user
    submits manual proof, payment is confirmed, or the order is delivered, the worker exits.
    """
    import time as _time

    try:
        while True:
            pending = await db.get_pending_by_ref(ref_id)
            if not pending or pending.get("status") != "waiting":
                return

            created_at = float(pending.get("created_at") or _time.time())
            now = _time.time()
            reminder_at = created_at + max(1, PAYMENT_REMINDER_MINUTES) * 60
            expire_at = created_at + max(1, PAYMENT_TIMEOUT_MINUTES) * 60

            if now < reminder_at:
                await asyncio.sleep(min(reminder_at - now, 60))
                continue

            if not pending.get("reminder_sent_at") and now < expire_at:
                reminded = await db.mark_payment_reminder_sent(ref_id)
                if reminded:
                    await _send_payment_reminder(context_or_application.bot, reminded)

            now = _time.time()
            if now < expire_at:
                await asyncio.sleep(min(expire_at - now, 60))
                continue

            await _expire_payment_session(context_or_application.bot, ref_id)
            return
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.exception("Payment notice worker crashed for ref=%s: %s", ref_id, exc)


async def _send_payment_reminder(bot, pending: dict):
    if await db.is_maintenance_mode():
        return
    user_id = int(pending.get("user_id", 0) or 0)
    if not user_id:
        return

    lang = pending.get("language") or await _lang_for_user(user_id)
    ref_id = pending.get("ref_id", "")
    pay_type = pending.get("pay_type", "order")
    remaining_minutes = max(1, PAYMENT_TIMEOUT_MINUTES - PAYMENT_REMINDER_MINUTES)

    if pay_type == "wallet":
        title = tr(lang, "payment_reminder_wallet_title")
        id_line = f"{tr(lang, 'wallet_topup_id')}: `{ref_id}`"
    else:
        title = tr(lang, "payment_reminder_order_title")
        id_line = f"{tr(lang, 'order_id')}: `{ref_id}`"

    try:
        await bot.send_message(
            user_id,
            f"{title}\n\n{id_line}\n\n{tr(lang, 'payment_reminder_body', minutes=remaining_minutes)}",
            parse_mode="Markdown",
        )
    except Exception:
        pass

async def _expire_payment_session(bot, ref_id: str):
    """Atomically expire a still-waiting payment and notify the user."""
    expired = await db.expire_pending_payment_if_waiting(ref_id)
    if not expired:
        return

    _payment_notice_tasks.pop(ref_id, None)
    timer_task = _payment_timer_tasks.pop(ref_id, None)
    if timer_task and not timer_task.done():
        timer_task.cancel()
    _usdt_tasks.pop(ref_id, None)

    if expired.get("pay_type") == "order":
        await db.update_order_status(ref_id, "expired")

    await _delete_payment_msg_by_bot(bot, ref_id)

    user_id = int(expired.get("user_id", 0) or 0)
    if not user_id:
        return
    lang = expired.get("language") or await _lang_for_user(user_id)

    if expired.get("pay_type") == "wallet":
        text = (
            f"{tr(lang, 'wallet_expired_title')}\n\n"
            f"{tr(lang, 'wallet_topup_id')}: `{ref_id}`\n"
            f"{tr(lang, 'payment_expired_body', minutes=PAYMENT_TIMEOUT_MINUTES)}"
        )
    else:
        text = (
            f"{tr(lang, 'order_expired_title')}\n\n"
            f"{tr(lang, 'order_id')}: `{ref_id}`\n"
            f"{tr(lang, 'order_expired_body', minutes=PAYMENT_TIMEOUT_MINUTES)}"
        )

    if await db.is_maintenance_mode():
        return

    try:
        await bot.send_message(user_id, text, parse_mode="Markdown")
    except Exception:
        pass

async def _expire_overdue_waiting_payments_with_notice(application) -> int:
    """Expire overdue waiting payment rows through the normal notifier path."""
    import time as _time

    expired = 0
    try:
        rows = await db.get_all_waiting_payments()
    except Exception as exc:
        logger.exception("Could not load waiting payments for notified expiry: %s", exc)
        return expired

    timeout_seconds = max(60, int(PAYMENT_TIMEOUT_MINUTES or 30) * 60)
    now = _time.time()
    for pending in rows:
        ref_id = pending.get("ref_id")
        if not ref_id:
            continue
        try:
            created_at = float(pending.get("created_at") or now)
        except (TypeError, ValueError):
            created_at = now
        if now - created_at < timeout_seconds:
            continue
        await _expire_payment_session(application.bot, str(ref_id))
        expired += 1
    return expired


async def resume_pending_payment_notices(application):
    """Resume reminder/expiry tasks for all waiting payments after a restart."""
    try:
        expired_with_notice = await _expire_overdue_waiting_payments_with_notice(application)
        startup_expiry_result = await db.expire_stale_unpaid_payments_and_orders(PAYMENT_TIMEOUT_MINUTES)
        if expired_with_notice or any(startup_expiry_result.values()):
            logger.info("Expired stale unpaid orders/payments on startup: notified=%s cleanup=%s", expired_with_notice, startup_expiry_result)
    except Exception as exc:
        logger.exception("Could not expire stale unpaid orders/payments on startup: %s", exc)
    rows = await db.get_all_waiting_payments()
    count = 0
    for pending in rows:
        ref_id = pending.get("ref_id")
        if not ref_id:
            continue
        schedule_payment_notice_task(application, ref_id)
        schedule_payment_timer_task(application, ref_id)
        count += 1
    if count:
        logger.info("Resumed payment reminder/expiry tasks: %s", count)



async def recover_pending_stock_orders(bot, limit: int = 200) -> dict:
    """Retry every product that has paid orders waiting for stock.

    This catches pending orders that were left waiting because a previous worker
    was locked/restarted or because stock already existed before the newest paid
    order joined the queue.
    """
    try:
        product_names = await db.get_pending_stock_product_names(limit=limit)
    except Exception as exc:
        logger.exception("Could not list pending-stock products for recovery: %s", exc)
        return {"products_checked": 0, "orders_delivered": 0, "items_delivered": 0}

    total_orders = 0
    total_items = 0
    checked = 0
    for product_name in product_names:
        checked += 1
        try:
            summary = await process_pending_stock_orders(bot, product_name)
        except Exception as exc:
            logger.exception("Pending-stock recovery failed for product=%s: %s", product_name, exc)
            continue
        total_orders += int(summary.get("orders_delivered", 0) or 0)
        total_items += int(summary.get("items_delivered", 0) or 0)

    if total_orders:
        try:
            await send_admin_message(
                bot,
                f"🔁 Pending-stock recovery delivered {total_orders} order(s) "
                f"using {total_items} stock item(s)."
            )
        except Exception:
            pass
    return {"products_checked": checked, "orders_delivered": total_orders, "items_delivered": total_items}


_pending_stock_recovery_task: asyncio.Task | None = None

async def start_pending_stock_recovery_watcher(application):
    """Start a durable pending-stock delivery safety net."""
    global _pending_stock_recovery_task
    if _pending_stock_recovery_task and not _pending_stock_recovery_task.done():
        return
    _pending_stock_recovery_task = application.create_task(_pending_stock_recovery_watcher(application))
    logger.info("✅ Pending-stock recovery watcher started.")


async def _pending_stock_recovery_watcher(application):
    while True:
        try:
            await recover_pending_stock_orders(application.bot)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("Pending-stock recovery watcher crashed: %s", exc)
        await asyncio.sleep(90)

_stale_unpaid_expiry_task: asyncio.Task | None = None

async def start_stale_unpaid_expiry_watcher(application):
    """Start a durable cleanup loop for unpaid orders/payments.

    The per-payment expiry tasks are the fast path, but this watcher is the safety
    net for restarts, local testing, or lost background tasks.
    """
    global _stale_unpaid_expiry_task
    if _stale_unpaid_expiry_task and not _stale_unpaid_expiry_task.done():
        return
    _stale_unpaid_expiry_task = application.create_task(_stale_unpaid_expiry_watcher(application))
    logger.info("✅ Stale unpaid order/payment expiry watcher started.")

async def _stale_unpaid_expiry_watcher(application):
    while True:
        try:
            expired_with_notice = await _expire_overdue_waiting_payments_with_notice(application)
            result = await db.expire_stale_unpaid_payments_and_orders(PAYMENT_TIMEOUT_MINUTES)
            if expired_with_notice or any(result.values()):
                logger.info("Expired stale unpaid orders/payments: notified=%s cleanup=%s", expired_with_notice, result)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("Stale unpaid expiry watcher crashed: %s", exc)
        await asyncio.sleep(60)


async def start_usdt_global_watcher(application):
    """Start one durable BEP20 watcher for all pending USDT payments.

    Per-payment tasks are still kept as a fast path, but this global watcher is
    the safety net that makes auto-verification work without the user pressing
    Check Payment. It scans the database every USDT_VERIFY_INTERVAL seconds,
    detects confirmed transfers, then calls the same completion logic used by
    the Check Payment button.
    """
    global _usdt_global_watcher_task

    if _usdt_global_watcher_task and not _usdt_global_watcher_task.done():
        return

    _usdt_global_watcher_task = application.create_task(_usdt_global_watcher(application))
    logger.info("✅ Global BEP20 auto-verification watcher started.")


async def _usdt_global_watcher(application):
    interval = max(5, int(USDT_VERIFY_INTERVAL or 30))

    while True:
        try:
            await _scan_waiting_usdt_payments(application)
            await _recover_confirmed_usdt_payments(application)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("Global BEP20 watcher cycle crashed: %s", exc)

        await asyncio.sleep(interval)


async def _scan_waiting_usdt_payments(application):
    rows = await db.get_all_pending_usdt()
    if not rows:
        return

    import time as _time
    now = _time.time()

    for pending in rows:
        ref_id = pending.get("ref_id")
        user_id = pending.get("user_id")
        unique_usdt = pending.get("unique_usdt")
        if not ref_id or not user_id or not unique_usdt:
            continue

        # Do not let very old waiting rows live forever. The expiry worker should
        # handle this too, but the watcher keeps it safe if the expiry task was lost.
        age = now - float(pending.get("created_at") or now)
        if age > PAYMENT_TIMEOUT_MINUTES * 60:
            await _expire_payment_session(application.bot, ref_id)
            continue

        try:
            network = _usdt_network_for_pending(pending)
            wallet = await _get_usdt_wallet_for_pending(pending)
            result = await check_usdt_received_detailed(float(unique_usdt), wallet_address=wallet, network=network)
        except Exception as exc:
            logger.exception("Global BEP20 check crashed ref=%s: %s", ref_id, exc)
            continue

        if not result.found:
            logger.info(
                "Global BEP20 check pending ref=%s amount=%s reason=%s",
                ref_id, unique_usdt, result.short_error_text(),
            )
            continue

        logger.info("✅ Global BEP20 watcher detected payment ref=%s amount=%s", ref_id, unique_usdt)
        await _on_usdt_confirmed(application, int(user_id), ref_id, result.tx or {})


async def _recover_confirmed_usdt_payments(application):
    """Complete confirmed BEP20 payments that were not delivered/credited yet."""
    rows = await db.get_confirmed_usdt_payments_needing_completion()
    for pending in rows:
        ref_id = pending.get("ref_id")
        user_id = pending.get("user_id")
        if not ref_id or not user_id:
            continue

        try:
            if pending.get("pay_type") == "wallet":
                await complete_wallet_load(application.bot, int(user_id), pending)
                continue

            order = await db.get_order(ref_id)
            if order and order.get("status") not in {"delivered", "pending_stock", "expired"}:
                await complete_order(application.bot, int(user_id), ref_id)
        except Exception as exc:
            logger.exception("Could not recover confirmed BEP20 payment ref=%s: %s", ref_id, exc)


async def resume_pending_usdt_payments(application):
    """Restart auto-verification tasks and recover confirmed BEP20 payments.

    Waiting rows get a polling worker again after restart. Confirmed rows from
    older/crashed runs are safely completed so the user does not need to press
    Check Payment manually.
    """
    rows = await db.get_all_pending_usdt()
    # pending_payments.created_at uses time.time(), not loop time.
    import time as _time
    wall_now = _time.time()
    started = 0
    expired = 0

    for pending in rows:
        ref_id = pending.get("ref_id")
        unique_usdt = pending.get("unique_usdt")
        user_id = pending.get("user_id")
        if not ref_id or not unique_usdt or not user_id:
            continue
        age = wall_now - float(pending.get("created_at") or wall_now)
        if age > PAYMENT_TIMEOUT_MINUTES * 60:
            await _expire_payment_session(application.bot, ref_id)
            expired += 1
            continue
        if ref_id in _usdt_tasks and not _usdt_tasks[ref_id].done():
            continue
        _usdt_tasks[ref_id] = application.create_task(
            _poll_usdt(application, int(user_id), ref_id, float(unique_usdt), await _get_usdt_wallet_for_pending(pending), network=_usdt_network_for_pending(pending))
        )
        started += 1

    recovered = 0
    for pending in await db.get_confirmed_usdt_payments_needing_completion():
        ref_id = pending.get("ref_id")
        user_id = pending.get("user_id")
        if not ref_id or not user_id:
            continue

        if pending.get("pay_type") == "wallet":
            application.create_task(complete_wallet_load(application.bot, int(user_id), pending))
            recovered += 1
            continue

        order = await db.get_order(ref_id)
        if order and order.get("status") not in {"delivered", "pending_stock", "expired"}:
            application.create_task(complete_order(application.bot, int(user_id), ref_id))
            recovered += 1

    if started or expired or recovered:
        logger.info(
            "Resumed USDT auto-check tasks: started=%s expired=%s recovered=%s",
            started, expired, recovered
        )


async def start_binance_global_watcher(application):
    """Start one durable Binance Pay watcher for all pending Binance payments."""
    global _binance_global_watcher_task

    if _binance_global_watcher_task and not _binance_global_watcher_task.done():
        return

    _binance_global_watcher_task = application.create_task(_binance_global_watcher(application))
    logger.info("✅ Global Binance Pay auto-verification watcher started.")


async def _binance_global_watcher(application):
    interval = max(5, int(USDT_VERIFY_INTERVAL or 30))

    while True:
        try:
            await _scan_waiting_binance_payments(application)
            await _recover_confirmed_binance_payments(application)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("Global Binance Pay watcher cycle crashed: %s", exc)

        await asyncio.sleep(interval)


async def _scan_waiting_binance_payments(application):
    rows = await db.get_all_pending_binance()
    if not rows:
        return

    import time as _time
    now = _time.time()
    active_rows = []
    for pending in rows:
        ref_id = pending.get("ref_id")
        if not ref_id:
            continue
        age = now - float(pending.get("created_at") or now)
        if age > PAYMENT_TIMEOUT_MINUTES * 60:
            await _expire_payment_session(application.bot, ref_id)
            continue
        active_rows.append(pending)

    if not active_rows:
        return

    oldest_created_at = min(float(row.get("created_at") or now) for row in active_rows)
    history = await fetch_binance_pay_history(int(max(0, oldest_created_at - 120) * 1000))
    if not history.ok:
        logger.warning("Global Binance Pay check skipped: %s", history.error)
        return

    used_ids = await db.get_used_binance_transaction_ids()
    for pending in active_rows:
        ref_id = pending.get("ref_id")
        user_id = pending.get("user_id")
        unique_usdt = pending.get("unique_usdt") or pending.get("expected_usdt")
        if not ref_id or not user_id or not unique_usdt:
            continue

        result = find_matching_binance_pay_transaction(
            history.transactions,
            unique_usdt,
            pending.get("created_at"),
            used_transaction_ids=used_ids,
        )
        if not result.found:
            logger.info(
                "Global Binance Pay check pending ref=%s amount=%s reason=%s",
                ref_id, unique_usdt, result.short_error_text(),
            )
            continue

        tx_id = str((result.transaction or {}).get("transactionId") or (result.transaction or {}).get("tranId") or "").strip()
        if tx_id:
            used_ids.add(tx_id)
        logger.info("✅ Global Binance Pay watcher detected payment ref=%s amount=%s tx=%s", ref_id, unique_usdt, tx_id)
        await _on_binance_confirmed(application, int(user_id), ref_id, result.transaction or {})


async def _recover_confirmed_binance_payments(application):
    """Complete auto-confirmed Binance Pay payments that were not delivered/credited yet."""
    rows = await db.get_confirmed_binance_payments_needing_completion()
    for pending in rows:
        ref_id = pending.get("ref_id")
        user_id = pending.get("user_id")
        if not ref_id or not user_id:
            continue

        try:
            if pending.get("pay_type") == "wallet":
                await complete_wallet_load(application.bot, int(user_id), pending)
                continue

            order = await db.get_order(ref_id)
            if order and order.get("status") not in {"delivered", "pending_stock", "expired"}:
                await complete_order(application.bot, int(user_id), ref_id)
        except Exception as exc:
            logger.exception("Could not recover confirmed Binance Pay payment ref=%s: %s", ref_id, exc)


async def resume_pending_binance_payments(application):
    """Recover Binance Pay payments already auto-confirmed before a restart."""
    await _recover_confirmed_binance_payments(application)


# ───────────────────────── HELPERS ───────────────────────────

async def _delete_payment_msg(context, ref_id: str):
    await _delete_payment_msg_by_bot(context.bot, ref_id)


async def _delete_payment_msg_by_bot(bot, ref_id: str):
    """Delete the original payment-details message.

    The in-memory map is fast, but it disappears after restarts.  The DB copy
    lets us still clean up the message when the user presses Check Payment later
    or when a paid pending-stock order is delivered after admin adds stock.
    """
    timer_task = _payment_timer_tasks.pop(ref_id, None)
    if timer_task and not timer_task.done():
        timer_task.cancel()

    info = _payment_msg_map.pop(ref_id, None)

    if not info:
        pending = await db.get_pending_by_ref(ref_id)
        if pending:
            chat_id = pending.get("payment_chat_id")
            msg_id = pending.get("payment_msg_id")
            if chat_id and msg_id:
                info = {"chat_id": chat_id, "msg_id": msg_id}

    if not info:
        return

    try:
        await bot.delete_message(info["chat_id"], info["msg_id"])
    except Exception as exc:
        logger.info("Could not delete payment message for ref=%s: %s", ref_id, exc)
    finally:
        try:
            await db.clear_pending_payment_message(ref_id)
        except Exception:
            pass
