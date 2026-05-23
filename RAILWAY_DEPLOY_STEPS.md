# Railway deploy steps

This copy is safe for GitHub: it does not include a real `.env` file.
Keep `.env.example` only as a template.

## 1. Push this folder to GitHub

The repository root should contain these files directly:

- `admin_panel.py`
- `requirements.txt`
- `Procfile`
- `.env.example`
- `.gitignore`
- `web_admin/`

Do not upload a real `.env` file.

## 2. Add Railway variables

In Railway, open your project/service, then add these variables:

```env
MONGO_URI=your_mongodb_atlas_uri
DB_NAME=shopbot
ADMIN_PANEL_SECRET_KEY=make_a_long_random_secret
ADMIN_PANEL_DEBUG=0
ADMIN_PANEL_COOKIE_SECURE=1
ADMIN_CACHE_TTL_SECONDS=15
ADMIN_DASHBOARD_CACHE_TTL_SECONDS=30
ADMIN_LIVE_STATE_CACHE_TTL_SECONDS=10
ADMIN_LIVE_STATE_REFRESH_MS=15000
ADMIN_LIVE_FULL_REFRESH=0
ADMIN_AUTO_INDEXES=0
MONGO_SERVER_SELECTION_TIMEOUT_MS=10000
MONGO_CONNECT_TIMEOUT_MS=10000
MONGO_SOCKET_TIMEOUT_MS=20000
MONGO_MAX_POOL_SIZE=10
```

Optional only for first login, if your app has not yet stored admin credentials in MongoDB:

```env
ADMIN_PANEL_USERNAME=admin
ADMIN_PANEL_PASSWORD=change_this_password
```

## 3. Railway start command

The included `Procfile` already uses:

```bash
gunicorn admin_panel:app --bind 0.0.0.0:$PORT --workers 1 --threads 2 --timeout 120
```

If Railway asks for a start command, paste the same command.

## 4. Run MongoDB indexes once

Run this once locally or from a Railway shell if available:

```bash
python init_indexes.py
```

It uses `MONGO_URI` and `DB_NAME` from environment variables.

## 5. If `.env` was already pushed before

Treat the MongoDB URI/password as leaked. Create a new MongoDB Atlas database user/password, update Railway with the new URI, then delete the old database user.
