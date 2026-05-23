"""Compatibility entry point for the WebAdmin service.

This file intentionally does NOT start Telegram polling. It exists because some
hosting setups may accidentally run ``python bot.py`` from the WebAdmin folder.
When that happens, start the WebAdmin Flask app instead of creating a second
Telegram getUpdates poller.

Correct production command:
    gunicorn admin_panel:app --bind 0.0.0.0:$PORT --workers 1 --threads 2 --timeout 120
"""

from __future__ import annotations

import logging
import os

from web_admin.app import create_app

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

app = create_app()


def main() -> None:
    logger.warning(
        "WebAdmin/sellingbot-main/bot.py was started. This compatibility wrapper "
        "will run WebAdmin only; it will not start Telegram polling."
    )
    host = os.getenv("ADMIN_PANEL_HOST", "0.0.0.0")
    port = int(os.getenv("PORT") or os.getenv("ADMIN_PANEL_PORT") or 8080)
    debug = os.getenv("ADMIN_PANEL_DEBUG", "0").lower() in {"1", "true", "yes"}
    app.run(host=host, port=port, debug=debug)


if __name__ == "__main__":
    main()
