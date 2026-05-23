"""Safety stub for the WebAdmin copy.

Do not run Telegram polling from the WebAdmin service. The project contains a
separate Bot/sellingbot-main/bot.py for the single Telegram polling worker.

Correct commands:
    Bot service:      python bot.py
    WebAdmin service: python admin_panel.py

If WebAdmin runs bot.py by mistake, Telegram sees two getUpdates pollers and
raises: "Conflict: terminated by other getUpdates request".
"""

from __future__ import annotations

import sys


def main() -> None:
    print(
        "ERROR: WebAdmin/sellingbot-main/bot.py must not be used for polling.\n"
        "Run the Telegram bot from Bot/sellingbot-main with: python bot.py\n"
        "Run WebAdmin from WebAdmin/sellingbot-main with: python admin_panel.py",
        file=sys.stderr,
    )
    raise SystemExit(2)


if __name__ == "__main__":
    main()
