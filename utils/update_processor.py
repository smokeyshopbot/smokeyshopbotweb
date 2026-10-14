"""Bounded parallel users, with ordered processing for each user's state."""
import asyncio
import inspect
import time
import logging

logger = logging.getLogger(__name__)
from telegram.ext import BaseUpdateProcessor


class OrderedUserProcessor(BaseUpdateProcessor):
    def __init__(self, maximum=8):
        super().__init__(maximum)
        self.locks = [asyncio.Lock() for _ in range(1024)]

    async def initialize(self):
        pass

    async def shutdown(self):
        pass

    async def do_process_update(self, update, coroutine):
        member = getattr(update, 'chat_member', None)
        user = member.new_chat_member.user if member else getattr(update, 'effective_user', None)
        key = int(user.id) if user else 0
        started = False
        queued_at = time.monotonic()
        try:
            async with self.locks[key % len(self.locks)]:
                started = True
                waited = time.monotonic() - queued_at
                if waited > 1:
                    logger.warning("Bot update %s waited %.2fs for a previous action to finish.",
                                   getattr(update, "update_id", None), waited)
                await coroutine
        finally:
            if not started and inspect.iscoroutine(coroutine):
                coroutine.close()
