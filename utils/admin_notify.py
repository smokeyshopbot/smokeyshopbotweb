"""Telegram admin notification helpers.

Telegram admin panel/notifications were removed. WebAdmin is the only admin
panel, so these helpers intentionally no-op to keep delivery flows simple.
"""


async def send_admin_message(bot, text: str, **kwargs) -> int:
    return 0


async def send_admin_photo(bot, photo: str, caption: str | None = None, **kwargs) -> int:
    return 0
