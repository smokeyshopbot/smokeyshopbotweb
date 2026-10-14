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
