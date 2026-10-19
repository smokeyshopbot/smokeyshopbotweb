"""One request-boundary hook covers sends, edits, captions and keyboard updates."""
import time
from utils.update_context import record_timing
from telegram.ext import ExtBot
from telegram.error import BadRequest, Forbidden
from utils.premium_emojis import OUTPUT_METHODS, transform_payload, emoji_rejected, plain_product_payload

class PremiumEmojiBot(ExtBot):
    async def _emoji_settings(self):
        import database
        return await database.get_secret_settings()

    async def _emoji_do_post(self, endpoint, data, **kwargs):
        if endpoint not in OUTPUT_METHODS:
            return await super()._do_post(endpoint=endpoint, data=plain_product_payload(endpoint, data), **kwargs)
        try:
            settings = await self._emoji_settings()
        except Exception:
            settings = {}
        payload, changed = transform_payload(endpoint, data, settings)
        try:
            return await super()._do_post(endpoint=endpoint, data=payload, **kwargs)
        except (BadRequest, Forbidden) as error:
            if not changed or not emoji_rejected(error):
                raise
            # An explicit Telegram 400/403 is a rejection, so this cannot duplicate
            # an acknowledged send. Timeouts and rate limits are never retried here.
            return await super()._do_post(endpoint=endpoint, data=plain_product_payload(endpoint, data), **kwargs)

    async def _do_post(self, endpoint, data, **kwargs):
        started = time.monotonic()
        try:
            return await self._emoji_do_post(endpoint, data, **kwargs)
        finally:
            record_timing('telegram:' + endpoint, time.monotonic() - started)
