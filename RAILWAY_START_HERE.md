# Railway deploy: WebAdmin service

Upload/push the contents of this folder as the root of the Railway service.
The root must contain `admin_panel.py`, `requirements.txt`, `railway.json`, and `Procfile` directly.

Start command:

```bash
gunicorn admin_panel:app --bind 0.0.0.0:$PORT --workers 1 --threads 2 --timeout 120
```

Required Railway variables:

```env
MONGO_URI=your_mongodb_atlas_uri
MONGO_DB_NAME=shopbot
ADMIN_PANEL_USERNAME=admin
ADMIN_PANEL_PASSWORD=change_this_password
ADMIN_PANEL_SECRET_KEY=make_a_long_random_secret
```

After WebAdmin opens, save the bot token and admin/tester IDs in Secret Settings, or keep `BOT_TOKEN` and `ADMIN_IDS` on the bot service.
