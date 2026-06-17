"""Create MongoDB indexes for the WebAdmin panel.

Run once after setting MONGO_URI and DB_NAME:
    python init_indexes.py
"""

from __future__ import annotations

import os

from dotenv import load_dotenv
from pymongo import MongoClient

load_dotenv()


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


mongo_uri = os.getenv("MONGO_URI", "").strip()
if not mongo_uri:
    raise SystemExit("Missing MONGO_URI. Put it in .env or your environment first.")

db_name = (os.getenv("DB_NAME") or os.getenv("MONGO_DB_NAME") or "shopbot").strip() or "shopbot"
MONGO_TLS_ALLOW_INVALID_CERTIFICATES = _env_bool("MONGO_TLS_ALLOW_INVALID_CERTIFICATES", True)
client = MongoClient(
    mongo_uri,
    tls=True,
    tlsAllowInvalidCertificates=MONGO_TLS_ALLOW_INVALID_CERTIFICATES,
    serverSelectionTimeoutMS=10000,
    connectTimeoutMS=10000,
    socketTimeoutMS=20000,
    maxPoolSize=5,
)
db = client[db_name]

# Import after load_dotenv so web_admin.app sees the same environment values.
from web_admin.app import ensure_admin_indexes  # noqa: E402

ensure_admin_indexes(db)
print(f"Indexes created/verified for database: {db_name}")
