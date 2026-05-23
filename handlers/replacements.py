"""User replacement/report flow.

Users can report a delivered account/item that is damaged or unusable.
The bot verifies that the submitted account/email/text belongs to one of the
user's delivered orders, then stores a replacement report for WebAdmin review.
"""

from __future__ import annotations

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

import database as db
from utils.i18n import tr
from utils.messages import compact_blank_lines, md_code

_report_flow: dict[int, dict] = {}


def is_report_flow_active(user_id: int) -> bool:
    return int(user_id or 0) in _report_flow


def clear_report_flow(user_id: int) -> None:
    _report_flow.pop(int(user_id or 0), None)


async def _user_lang(user_id: int) -> str:
    return await db.get_user_language(user_id)


def _report_cancel_keyboard(lang: str = "en") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(tr(lang, "btn_cancel"), callback_data="report:cancel")]
    ])


def _report_submit_keyboard(lang: str = "en") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(tr(lang, "report_submit_without_screenshot"), callback_data="report:submit")],
        [InlineKeyboardButton(tr(lang, "btn_cancel"), callback_data="report:cancel")],
    ])


async def _start_report(user_id: int, username: str = "") -> tuple[str, InlineKeyboardMarkup]:
    await db.upsert_user(user_id, username or "")
    _report_flow[user_id] = {"step": "item"}
    lang = await _user_lang(user_id)
    return tr(lang, "report_start"), _report_cancel_keyboard(lang)


async def cmd_report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or not update.message:
        return
    lang = await _user_lang(user.id)
    if await db.is_blocked(user.id):
        await update.message.reply_text(tr(lang, "blocked"))
        return
    text, keyboard = await _start_report(user.id, user.username or "")
    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=keyboard)


async def start_report_from_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        return
    user = query.from_user
    lang = await _user_lang(user.id)
    if await db.is_blocked(user.id):
        await query.edit_message_text(tr(lang, "blocked"))
        return
    text, keyboard = await _start_report(user.id, user.username or "")
    await query.edit_message_text(text, parse_mode="Markdown", reply_markup=keyboard)


async def _finish_report(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int, screenshot_file_id: str = "") -> None:
    state = _report_flow.get(user_id) or {}
    matched_items = state.get("matched_items") or []
    if not matched_items and state.get("matched"):
        matched_items = [state.get("matched") or {}]
    issue_text = str(state.get("issue_text") or "").strip()
    lang = await _user_lang(user_id)
    if not matched_items or not issue_text:
        clear_report_flow(user_id)
        target = update.callback_query.message if update.callback_query else update.message
        if target:
            await target.reply_text(tr(lang, "report_session_expired"))
        return

    username = (update.effective_user.username or "") if update.effective_user else ""
    report_id = await db.create_replacement_report(
        user_id=user_id,
        username=username,
        matched_items=matched_items,
        issue_text=issue_text,
        screenshot_file_id=screenshot_file_id,
    )

    clear_report_flow(user_id)
    first = matched_items[0]
    message_key = "report_submitted_many" if len(matched_items) > 1 else "report_submitted"
    text = tr(
        lang,
        message_key,
        report_id=report_id,
        order_id=first.get("order_id", "N/A"),
        product=first.get("product_name", "Product"),
        count=len(matched_items),
    )
    if update.callback_query:
        await update.callback_query.edit_message_text(text, parse_mode="Markdown")
    elif update.message:
        await update.message.reply_text(text, parse_mode="Markdown")


async def handle_report_callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        return
    await query.answer()
    user_id = query.from_user.id
    lang = await _user_lang(user_id)
    action = (query.data or "").split(":", 1)[1] if ":" in (query.data or "") else ""
    if action == "cancel":
        clear_report_flow(user_id)
        await query.edit_message_text(tr(lang, "report_cancelled"))
        return
    if action == "submit":
        await _finish_report(update, context, user_id, screenshot_file_id="")
        return


async def handle_report_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    if not update.effective_user or not update.message:
        return False
    user_id = update.effective_user.id
    state = _report_flow.get(user_id)
    if not state:
        return False

    lang = await _user_lang(user_id)
    text = (update.message.text or "").strip()
    if not text:
        await update.message.reply_text(tr(lang, "report_empty_text"), reply_markup=_report_cancel_keyboard(lang))
        return True

    if state.get("step") == "item":
        matched_items = await db.find_user_delivered_stock_items_for_report(user_id, text)
        if not matched_items:
            await update.message.reply_text(
                tr(lang, "report_item_not_found"),
                parse_mode="Markdown",
                reply_markup=_report_cancel_keyboard(lang),
            )
            return True

        reportable_items = [item for item in matched_items if not item.get("already_reported")]
        already_reported_items = [item for item in matched_items if item.get("already_reported")]
        if not reportable_items:
            first = already_reported_items[0] if already_reported_items else {}
            status = str(first.get("existing_report_status") or "pending").lower()
            key = "report_item_already_replaced" if status in {"replaced", "replacement_sent"} else "report_item_already_pending"
            clear_report_flow(user_id)
            await update.message.reply_text(
                tr(lang, key, report_id=first.get("existing_report_id") or "N/A"),
                parse_mode="Markdown",
            )
            return True

        state["matched"] = reportable_items[0]
        state["matched_items"] = reportable_items
        state["step"] = "issue"
        if len(reportable_items) == 1 and not already_reported_items:
            matched = reportable_items[0]
            reply = compact_blank_lines(tr(
                lang,
                "report_item_found",
                order_id=matched.get("order_id", "N/A"),
                product=matched.get("product_name", "Product"),
            ))
        else:
            item_lines = []
            for idx, matched in enumerate(reportable_items[:20], start=1):
                item_lines.append(
                    f"{idx}. `{matched.get('order_id', 'N/A')}` | {matched.get('product_name', 'Product')}"
                )
            skipped_note = ""
            if already_reported_items:
                skipped_note = tr(lang, "report_items_skipped_note", skipped=len(already_reported_items))
            reply = compact_blank_lines(tr(
                lang,
                "report_items_found",
                count=len(reportable_items),
                items="\n".join(item_lines),
                skipped_note=skipped_note,
            ))
        await update.message.reply_text(
            reply,
            parse_mode="Markdown",
            reply_markup=_report_cancel_keyboard(lang),
        )
        return True

    if state.get("step") == "issue":
        state["issue_text"] = text[:2000]
        state["step"] = "screenshot"
        await update.message.reply_text(
            tr(lang, "report_screenshot_prompt"),
            parse_mode="Markdown",
            reply_markup=_report_submit_keyboard(lang),
        )
        return True

    if state.get("step") == "screenshot":
        await update.message.reply_text(
            tr(lang, "report_waiting_screenshot_or_submit"),
            parse_mode="Markdown",
            reply_markup=_report_submit_keyboard(lang),
        )
        return True

    return False


async def handle_report_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    if not update.effective_user or not update.message:
        return False
    user_id = update.effective_user.id
    state = _report_flow.get(user_id)
    if not state:
        return False
    lang = await _user_lang(user_id)
    if state.get("step") != "screenshot":
        await update.message.reply_text(tr(lang, "report_send_issue_first"), reply_markup=_report_cancel_keyboard(lang))
        return True
    photos = update.message.photo or []
    if not photos:
        return True
    await _finish_report(update, context, user_id, screenshot_file_id=photos[-1].file_id)
    return True
