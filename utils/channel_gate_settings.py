"""Validation shared by the Telegram membership gate and WebAdmin settings."""

import re
from urllib.parse import urlsplit


def channel_gate_enabled(value) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def validate_channel_settings(enabled, channel_id, join_url) -> dict:
    enabled = channel_gate_enabled(enabled)
    channel_id = str(channel_id or "").strip()
    join_url = str(join_url or "").strip()
    if channel_id and not (
        re.fullmatch(r"@[A-Za-z][A-Za-z0-9_]{4,31}", channel_id)
        or (re.fullmatch(r"-100[0-9]+", channel_id) and abs(int(channel_id)) < 2**52)
    ):
        raise ValueError("Required channel must be an @username or a numeric -100… channel ID.")
    if enabled and not channel_id:
        raise ValueError("Enter the required channel before enabling channel membership.")
    if not join_url and channel_id.startswith("@"):
        join_url = "https://t.me/" + channel_id[1:]
    if join_url:
        parsed = urlsplit(join_url)
        if not (
            parsed.scheme == "https"
            and parsed.netloc.lower() in {"t.me", "telegram.me"}
            and not parsed.query and not parsed.fragment
            and re.fullmatch(r"/(?:[A-Za-z][A-Za-z0-9_]{4,31}|\+[A-Za-z0-9_-]+|joinchat/[A-Za-z0-9_-]+)", parsed.path)
        ):
            raise ValueError("Channel join link must be an HTTPS Telegram channel or invite link.")
        if channel_id.startswith("@") and parsed.path.lower() != "/" + channel_id[1:].lower():
            # Invite links may also be used for a public channel.
            if not parsed.path.startswith(("/+", "/joinchat/")):
                raise ValueError("The public join link must match the required channel username.")
        join_url = "https://t.me" + parsed.path
    if enabled and not join_url:
        raise ValueError("A private channel requires a Telegram invite link.")
    return {
        "required_channel_enabled": "1" if enabled else "0",
        "required_channel_id": channel_id,
        "required_channel_join_url": join_url,
    }


def chat_member_is_joined(member) -> bool:
    return member.status in {"member", "administrator", "creator"} or (
        member.status == "restricted" and bool(getattr(member, "is_member", False))
    )

MAX_REQUIRED_CHATS = 10


def button_text(value, label="Button text"):
    value = str(value or "").strip()
    if len(value) > 64 or any(ord(char) < 32 for char in value):
        raise ValueError(label + " must be at most 64 characters on one line.")
    return value


def validate_join_settings(settings):
    """Preserve the original channel while validating additional required chats."""
    raw = settings.get("required_join_chats", [])
    if not isinstance(raw, list):
        raise ValueError("The required join list must be a list of groups/channels.")
    if len(raw) > MAX_REQUIRED_CHATS:
        raise ValueError("Add at most 10 required groups/channels in total.")
    enabled = channel_gate_enabled(settings.get("required_channel_enabled"))
    primary_id = str(settings.get("required_channel_id") or "").strip()
    primary = validate_channel_settings(
        enabled and (bool(primary_id) or not raw), primary_id,
        settings.get("required_channel_join_url"))
    seen = {primary_id.casefold()} if primary_id else set()
    chats = []
    for index, row in enumerate(raw, 1):
        if not isinstance(row, dict):
            raise ValueError("Invalid required join entry.")
        chat_id = str(row.get("chat_id") or "").strip()
        # Basic groups have negative IDs without the supergroup -100 prefix.
        if not re.fullmatch(r"(?:@[A-Za-z][A-Za-z0-9_]{4,31}|-[1-9][0-9]*)", chat_id) or (
                chat_id.startswith("-") and abs(int(chat_id)) >= 2**52):
            raise ValueError(f"Required chat {index}: enter an @username or a negative Telegram group/channel ID.")
        if chat_id.casefold() in seen:
            raise ValueError("Each required group/channel must appear only once.")
        seen.add(chat_id.casefold())
        # Reuse strict Telegram join-link validation for basic groups as well.
        checked_id = chat_id if chat_id.startswith(("@", "-100")) else "-1001"
        checked = validate_channel_settings(True, checked_id, row.get("join_url"))
        chats.append({"chat_id": chat_id, "join_url": checked["required_channel_join_url"],
                      "button_text": button_text(row.get("button_text")),
                      "button_text_es": button_text(row.get("button_text_es"), "Spanish button text")})
    if len(chats) + bool(primary_id) > MAX_REQUIRED_CHATS:
        raise ValueError("Add at most 10 required groups/channels in total.")
    return {**primary, "required_channel_enabled": "1" if enabled else "0",
            "required_channel_button_text": button_text(settings.get("required_channel_button_text")),
            "required_channel_button_text_es": button_text(settings.get("required_channel_button_text_es"), "Spanish button text"),
            "required_join_chats": chats}


def required_chats(settings):
    checked = validate_join_settings(settings)
    chats = []
    if checked["required_channel_id"]:
        chats.append({"chat_id": checked["required_channel_id"],
                      "join_url": checked["required_channel_join_url"],
                      "button_text": checked["required_channel_button_text"],
                      "button_text_es": checked["required_channel_button_text_es"],
                      "primary": True})
    return chats + checked["required_join_chats"]

