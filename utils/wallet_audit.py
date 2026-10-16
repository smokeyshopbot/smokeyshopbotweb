"""Atomic wallet changes with a durable outbox and read-only reconciliation."""
import hashlib
from datetime import datetime, timezone
from decimal import Decimal
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

LEGACY_CREDIT_ERROR = "Legacy completion has no reliable wallet-event reference"


def legacy_credit_history(record):
    """An old completed credit has unknown provenance, not a proven shortfall."""
    if record.get("wallet_credit_version"):
        return False
    completed = (record.get("status") == "completed" or record.get("wallet_credited_at")
                 or record.get("refund_status") == "wallet_credited")
    return bool(completed and (record.get("wallet_credit_state") == "legacy_unverified" or
        (record.get("wallet_credit_state") == "review" and record.get("wallet_credit_error") == LEGACY_CREDIT_ERROR)))


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
    if not user.get("wallet_audit"):
        update["$set"]["wallet_audit"] = {"opening_inr": float(user.get("wallet_inr", 0) or 0),
            "opening_usdt": float(user.get("wallet_usdt", 0) or 0), "started_at": event["created_at"]}
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


def reconcile_user(db, user):
    uid = user["user_id"]
    if not user.get("wallet_audit"):
        opening_query = {"_id": user["_id"], "wallet_audit": {"$exists": False}}
        for field in ("wallet_inr", "wallet_usdt"):
            opening_query[field] = user[field] if field in user else {"$exists": False}
        db.users.update_one(opening_query,
            {"$set": {"wallet_audit": {"opening_inr": float(user.get("wallet_inr", 0) or 0),
                "opening_usdt": float(user.get("wallet_usdt", 0) or 0), "started_at": datetime.now(timezone.utc)}}})
        user = db.users.find_one({"_id": user["_id"]}) or user
    if not user.get("wallet_audit"): return None
    drain_wallet_outbox(db, user)
    user = db.users.find_one({"_id": user["_id"]})
    version = user.get("wallet_revision", 0)
    # Fetch financial fields once, rather than issuing one lookup for every
    # historical order/top-up. Item contents are never loaded by the audit.
    events = {row["_id"]: row for row in db.wallet_ledger.find({"user_id": uid}, {"delta": 1, "currency": 1, "reference": 1, "created_at": 1})}
    totals = {}
    for event in events.values():
        totals[event["currency"]] = totals.get(event["currency"], Decimal(0)) + Decimal(str(event["delta"]))
    final = db.users.find_one({"_id": user["_id"]})
    if final.get("wallet_revision", 0) != version or final.get("wallet_outbox"): return None
    report = {"checked_at": datetime.now(timezone.utc), "revision": version, "issues": [], "history_notes": []}
    for currency in ("inr", "usdt"):
        expected = Decimal(str(user["wallet_audit"].get("opening_" + currency, 0))) + totals.get(currency, Decimal(0))
        actual = Decimal(str(final.get("wallet_" + currency, 0) or 0))
        difference = actual - expected
        report[currency] = {"expected": float(expected), "actual": float(actual), "difference": float(difference)}
        if abs(difference) > Decimal("0.00001" if currency == "usdt" else "0.005") or actual < 0:
            report["issues"].append(currency + " balance mismatch")
    projection = {field: 1 for field in ("ref_id", "order_id", "wallet_event_id", "wallet_event_delta", "wallet_refund_event_id", "wallet_refund_event_delta",
        "wallet_credit_state", "wallet_credit_error", "wallet_credit_version", "status", "refund_status",
        "wallet_charge_applied_at", "api_stage", "wallet_credited_at", "wallet_adjusted_at", "refund_wallet_credited_at", "paid_at", "created_at")}
    records = list(db.pending_payments.find({"user_id": uid, "$or": [{"wallet_event_id": {"$exists": True}}, {"wallet_credit_state": {"$in": ["review", "legacy_unverified"]}}]}, projection))
    records += list(db.orders.find({"user_id": uid, "$or": [{"wallet_event_id": {"$exists": True}}, {"wallet_refund_event_id": {"$exists": True}}, {"wallet_credit_state": {"$in": ["review", "legacy_unverified"]}}]}, projection))
    references = set()
    cutoff = report["checked_at"].timestamp() - 300
    for record in records:
        legacy = legacy_credit_history(record)
        if legacy:
            report["history_notes"].append("Older completed credit has no reliable audit reference: " + str(record.get("ref_id") or record.get("order_id")))
        elif record.get("wallet_credit_state") == "review":
            report["issues"].append("Wallet credit requires verification: " + str(record.get("ref_id") or record.get("order_id")))
        for field, amount_field in (("wallet_event_id", "wallet_event_delta"), ("wallet_refund_event_id", "wallet_refund_event_delta")):
            identifier = record.get(field)
            if not identifier: continue
            references.add(identifier)
            charged = record.get("wallet_charge_applied_at") or record.get("api_stage") in {"charged", "complete", "rolled_back"}
            if record.get("order_id") and field == "wallet_event_id" and not charged: continue
            stamp = record.get("refund_wallet_credited_at") if field == "wallet_refund_event_id" else None
            stamp = stamp or record.get("wallet_credited_at") or record.get("wallet_adjusted_at") or record.get("wallet_charge_applied_at") or record.get("paid_at") or record.get("created_at")
            if stamp and stamp.replace(tzinfo=timezone.utc).timestamp() > cutoff: continue
            event = events.get(identifier)
            if legacy and not event:
                continue  # Keep the history gap visible without claiming money is missing.
            if not event or abs(Decimal(str(event["delta"])) - Decimal(str(record.get(amount_field, 0)))) > Decimal("0.00001"):
                report["issues"].append("Wallet source record needs review: " + str(record.get("ref_id") or record.get("order_id")))
    for event in events.values():
        stamp = event.get("created_at")
        if stamp and stamp.replace(tzinfo=timezone.utc).timestamp() <= cutoff and event["_id"] not in references:
            report["issues"].append("Wallet history record missing: " + event["reference"])
    for order in db.orders.find({"user_id": uid, "status": "api_processing"}):
        created = order.get("created_at")
        if created and (report["checked_at"] - created.replace(tzinfo=timezone.utc) if created.tzinfo is None else report["checked_at"] - created).total_seconds() > 600:
            report["issues"].append("API order needs review: " + order["order_id"])
    report["status"] = "review" if report["issues"] else "ok"
    latest = db.users.find_one({"_id": user["_id"]})
    if latest.get("wallet_revision", 0) != version or latest.get("wallet_outbox"): return None
    db.users.update_one({"_id": user["_id"], "wallet_revision": version} if "wallet_revision" in user else {"_id": user["_id"], "wallet_revision": {"$exists": False}},
        {"$set": {"wallet_reconciliation": report}})
    return report
