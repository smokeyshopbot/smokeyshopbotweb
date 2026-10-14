"""Header authentication and shared, expiring API request counters."""
import hashlib
import math
import time
from datetime import datetime, timezone
from flask import request
from pymongo import ReturnDocument


def request_key(payload=None, allow_legacy=True):
    header = request.headers.get("Authorization", "").strip()
    key = request.headers.get("X-API-Key", "").strip()
    if header:
        scheme, _, bearer = header.partition(" ")
        if scheme.lower() != "bearer" or not bearer.strip(): return None
        if key and key != bearer.strip(): return None
        key = bearer.strip()
    body = payload.get("key", "") if isinstance(payload, dict) else ""
    query = request.args.get("key", "")
    if body and not isinstance(body, str): return None
    if key:
        if any(value and value != key for value in (body, query)): return None
        return key
    return body or (query if allow_legacy else "")


def rate_limit(db, identity, limit, window=60):
    if limit <= 0: return 0
    seconds = time.time()
    bucket = int(seconds // window)
    identifier = hashlib.sha256(f"{identity}:{bucket}".encode()).hexdigest()
    doc = db.api_rate_limits.find_one_and_update({"_id": identifier},
        {"$inc": {"count": 1}, "$setOnInsert": {"expires_at": datetime.fromtimestamp((bucket + 2) * window, timezone.utc)}},
        upsert=True, return_document=ReturnDocument.AFTER)
    return max(1, math.ceil((bucket + 1) * window - seconds)) if doc["count"] > limit else 0


def ensure_security_indexes(db):
    db.api_rate_limits.create_index("expires_at", expireAfterSeconds=0)
