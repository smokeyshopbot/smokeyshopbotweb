"""Reuse idle Telegram connections with the pinned python-telegram-bot 21.5."""
import httpx
from telegram.request import HTTPXRequest


class WarmTelegramRequest(HTTPXRequest):
    def _build_client(self):
        # PTB 21.5 builds its client here; preserve its timeouts, proxy and transport.
        options = dict(self._client_kwargs)
        limits = options['limits']
        options['limits'] = httpx.Limits(
            max_connections=limits.max_connections,
            max_keepalive_connections=limits.max_keepalive_connections,
            keepalive_expiry=120.0,
        )
        return httpx.AsyncClient(**options)
