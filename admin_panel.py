"""Convenience entry point for the web admin panel.

Run:
    python admin_panel.py

Or:
    python -m web_admin.app
"""

import os
from web_admin.app import create_app

app = create_app()

if __name__ == "__main__":
    host = os.getenv("ADMIN_PANEL_HOST", "0.0.0.0")
    port = int(os.getenv("PORT") or os.getenv("ADMIN_PANEL_PORT") or 8080)
    debug = os.getenv("ADMIN_PANEL_DEBUG", "0").lower() in {"1", "true", "yes"}
    app.run(host=host, port=port, debug=debug)
