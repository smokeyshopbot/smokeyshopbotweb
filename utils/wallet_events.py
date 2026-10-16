"""Atomic wallet changes with durable, idempotent transaction records."""
import hashlib
from datetime import datetime, timezone
from decimal import Decimal
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError


def event_id(user_id, currency, reference):
    return hashlib.sha256(f"{user_id}:{currency}:{reference}".encode()).hexdigest()


def _prepare(user, currency, delta, reference, source, extra):
    if currency not in {"inr", "usdt"} or not Decimal(str(delta)).is_finite():
        raise ValueError("Invalid wallet currency or amount")
    field = "wallet_" + currency
    before = float(user.get(field, 0) or 0)
    after = float(Decimal(str(before)) + Decimal(str(delta)))
    identifier = event_id(user["user_id"], currency, reference)
    event = {"_id": identifier, "user_id": user["user_id"], "currency": currency, "delta": float(delta),
        "reference": str(reference), "source": source, "before": before, "after": after, "created_at": datetime.now(timezone.utc)}
    update = {key: dict(value) for key, value in (extra or {}).items()}
    update.setdefault("$inc", {}).update({field: float(delta), "wallet_revision": 1})
    update.setdefault("$set", {})["wallet_outbox." + identifier] = event
    conditions = [{"_id": user["_id"]}, {field: before} if field in user else {field: {"$exists": False}},
        {"wallet_revision": user["wallet_revision"]} if "wallet_revision" in user else {"wallet_revision": {"$exists": False}}]
    return identifier, event, conditions, update


def change_wallet(db, user_id, currency, delta, *, reference, source, conditions=None, extra=None):
    identifier = event_id(user_id, currency, reference)
    existing = db.wallet_ledger.find_one({"_id": identifier})
    if existing:
        if existing["delta"] != float(delta): raise ValueError("Wallet reference reused for a different amount")
        return db.users.find_one({"user_id": user_id})
    for _ in range(12):
        user = db.users.find_one({"user_id": user_id})
        if not user: return None
        existing = db.wallet_ledger.find_one({"_id": identifier})
        if existing:
            if existing["delta"] != float(delta): raise ValueError("Wallet reference reused for a different amount")
            return db.users.find_one({"user_id": user_id})
        if identifier in user.get("wallet_outbox", {}):
            if user["wallet_outbox"][identifier]["delta"] != float(delta): raise ValueError("Wallet reference conflict")
            drain_wallet_outbox(db, user)
            return db.users.find_one({"user_id": user_id})
        identifier, event, query, update = _prepare(user, currency, delta, reference, source, extra)
        if conditions: query.append(conditions)
        result = db.users.find_one_and_update({"$and": query}, update, return_document=ReturnDocument.AFTER)
        if result:
            try: drain_wallet_outbox(db, result)
            except Exception: pass  # Durable outbox remains on the atomically changed user.
            return result
        if conditions and not db.users.find_one({"$and": [{"user_id": user_id}, conditions]}): return None
    raise RuntimeError("Wallet is busy; retry the same reference")


async def change_wallet_async(db, user_id, currency, delta, *, reference, source, conditions=None, extra=None):
    identifier = event_id(user_id, currency, reference)
    existing = await db.wallet_ledger.find_one({"_id": identifier})
    if existing:
        if existing["delta"] != float(delta): raise ValueError("Wallet reference reused for a different amount")
        return await db.users.find_one({"user_id": user_id})
    for _ in range(12):
        user = await db.users.find_one({"user_id": user_id})
        if not user: return None
        existing = await db.wallet_ledger.find_one({"_id": identifier})
        if existing:
            if existing["delta"] != float(delta): raise ValueError("Wallet reference reused for a different amount")
            return await db.users.find_one({"user_id": user_id})
        if identifier in user.get("wallet_outbox", {}):
            if user["wallet_outbox"][identifier]["delta"] != float(delta): raise ValueError("Wallet reference conflict")
            await drain_wallet_outbox_async(db, user)
            return await db.users.find_one({"user_id": user_id})
        identifier, event, query, update = _prepare(user, currency, delta, reference, source, extra)
        if conditions: query.append(conditions)
        result = await db.users.find_one_and_update({"$and": query}, update, return_document=ReturnDocument.AFTER)
        if result:
            try: await drain_wallet_outbox_async(db, result)
            except Exception: pass
            return result
        if conditions and not await db.users.find_one({"$and": [{"user_id": user_id}, conditions]}): return None
    raise RuntimeError("Wallet is busy; retry the same reference")


def drain_wallet_outbox(db, user):
    for identifier, event in user.get("wallet_outbox", {}).items():
        try: db.wallet_ledger.insert_one(event)
        except DuplicateKeyError: pass
        db.users.update_one({"_id": user["_id"], "wallet_outbox." + identifier: event}, {"$unset": {"wallet_outbox." + identifier: ""}})


async def drain_wallet_outbox_async(db, user):
    for identifier, event in user.get("wallet_outbox", {}).items():
        try: await db.wallet_ledger.insert_one(event)
        except DuplicateKeyError: pass
        await db.users.update_one({"_id": user["_id"], "wallet_outbox." + identifier: event}, {"$unset": {"wallet_outbox." + identifier: ""}})
