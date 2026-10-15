"""Shared MongoDB stock holds. Items and their hold move in one product update.

The generator core is identical for PyMongo and Motor. Uncertain/paid work is
never released just because a clock expired; only unpaid checkouts expire.
"""
import re
from datetime import datetime, timezone, timedelta

PAID_PAYMENT = {"confirmed", "completed", "approved"}
REVIEW_PAYMENT = {"upi_submitted", "binance_submitted", "usdt_manual_submitted"}
TERMINAL = {"cancelled", "expired", "rejected", "failed"}


def now(): return datetime.now(timezone.utc)


def operation(collection, method, *args, **kwargs): return collection, method, args, kwargs


def _run(db, generator):
    value = None
    while True:
        try: collection, method, args, kwargs = generator.send(value)
        except StopIteration as result: return result.value
        if method == "find_many":
            limit = kwargs.pop("limit", 50)
            value = list(db[collection].find(*args, **kwargs).sort('_id', 1).limit(limit))
        else: value = getattr(db[collection], method)(*args, **kwargs)


async def _arun(db, generator):
    value = None
    while True:
        try: collection, method, args, kwargs = generator.send(value)
        except StopIteration as result: return result.value
        if method == "find_many":
            limit = kwargs.pop("limit", 50)
            value = await db[collection].find(*args, **kwargs).sort('_id', 1).limit(limit).to_list(length=limit)
        else: value = await getattr(db[collection], method)(*args, **kwargs)


def _reserve(order_id, product_name, quantity, user_id, *, minutes=30, partial=False, protected=False, pending_quantity=0):
    order_id = str(order_id).strip().upper()
    if not re.fullmatch(r"[A-Z0-9_-]{1,80}", order_id) or quantity < 1:
        raise ValueError("Invalid stock reservation")
    query = {"name": re.compile("^" + re.escape(product_name) + "$", re.I)}
    field = "stock_reservations." + order_id
    for _ in range(12):
        product = yield operation("products", "find_one", query)
        if not product: return {"ok": False, "reason": "out_of_stock"}
        holds = product.get("stock_reservations") or {}
        old = holds.get(order_id)
        if product.get("enabled") is False and not (old and protected):
            return {"ok": False, "reason": "out_of_stock"}
        if old and (old.get("user_id") != user_id or old.get("quantity") != quantity):
            raise ValueError("Stock reservation reference conflict")
        if not protected:
            for identifier, hold in holds.items():
                if identifier != order_id and hold.get("user_id") == user_id and not hold.get("protected"):
                    return {"ok": False, "reason": "active_checkout"}
        existing = list((old or {}).get("items") or [])
        stock = list(product.get("stock") or [])
        skip = max(0, int(pending_quantity))
        take = min(max(0, quantity-len(existing)), max(0, len(stock)-skip))
        if not partial and len(existing)+take < quantity:
            return {"ok": False, "reason": "out_of_stock"}
        record = {"user_id": user_id, "quantity": quantity, "items": existing + stock[skip:skip+take],
                  "created_at": (old or {}).get("created_at") or now(),
                  "expires_at": (old or {}).get("expires_at") or now()+timedelta(minutes=max(1, int(minutes))),
                  "protected": bool(protected or (old or {}).get("protected"))}
        cas = {"_id": product["_id"], "name": product["name"], "stock": product.get("stock") or [],
               "stock_reservations": product["stock_reservations"] if "stock_reservations" in product else {"$exists": False}}
        if not old: cas["enabled"] = {"$ne": False}
        result = yield operation("products", "update_one", cas, {"$set": {
            "stock": stock[:skip] + stock[skip+take:], field: record},
            "$inc": {"stock_reservation_count": 0 if old else 1}})
        if result.matched_count:
            yield operation("orders", "update_one", {"order_id": order_id}, {"$set": {
                "stock_reservation_enabled": True, "stock_reserved_quantity": len(record["items"]),
                "stock_reservation_protected": record["protected"],
                "stock_reservation_expires_at": record["expires_at"]}, "$unset": {"stock_reservation_released_at": ""}})
            return {"ok": True, **record, "reserved_local": True}
    return {"ok": False, "reason": "stock_busy"}


def _release(order_id, product_name, *, discard=False, expected=None):
    field = "stock_reservations." + str(order_id).strip().upper()
    for _ in range(12):
        product = yield operation("products", "find_one", {"name": re.compile("^"+re.escape(product_name)+"$", re.I)})
        hold = (product or {}).get("stock_reservations", {}).get(str(order_id).strip().upper())
        if not hold: return False
        if expected is not None and hold != expected: return False
        query = {"_id": product["_id"], field: hold}
        update = {"$unset": {field: ""}, "$inc": {"stock_reservation_count": -1}}
        if not discard:
            query["stock"] = product.get("stock") or []
            update["$set"] = {"stock": list(hold.get("items") or []) + list(product.get("stock") or [])}
        result = yield operation("products", "update_one", query, update)
        if result.modified_count:
            yield operation("orders", "update_one", {"order_id": str(order_id).strip().upper()},
                            {"$set": {"stock_reserved_quantity": 0, "stock_reservation_released_at": now()}})
            return True
    return False


def _sweep(limit=50):
    state = yield operation("operational_control", "find_one", {"_id": "stock_reservation_sweep"})
    query = {"stock_reservation_count": {"$gt": 0}}
    if state and state.get("cursor"):
        query["_id"] = {"$gt": state["cursor"]}
    products = yield operation("products", "find_many", query, limit=limit)
    released = 0
    for product in products:
        for order_id, hold in (product.get("stock_reservations") or {}).items():
            order = yield operation("orders", "find_one", {"order_id": order_id})
            if order and order.get("status") == "delivered":
                yield from _release(order_id, product["name"], discard=True, expected=hold)
                continue
            if order and order.get("status") in TERMINAL:
                released += bool((yield from _release(order_id, product["name"], expected=hold)))
                continue
            # A crash during debit/fulfilment needs review, not a timed stock release.
            if hold.get("protected"): continue
            pending = yield operation("pending_payments", "find_one", {"ref_id": order_id})
            if pending and pending.get("status") in PAID_PAYMENT | REVIEW_PAYMENT: continue
            if order and (order.get("wallet_charge_applied_at") or order.get("paid_at") or order.get("payment_verified_at")): continue
            if pending and pending.get("status") in TERMINAL:
                if order:
                    yield operation("orders", "update_one", {"_id": order["_id"], "status": "pending"},
                                    {"$set": {"status": "expired" if pending["status"] == "expired" else "failed"}})
                released += bool((yield from _release(order_id, product["name"], expected=hold)))
                continue
            expiry = hold["expires_at"]
            if expiry.tzinfo is None: expiry = expiry.replace(tzinfo=timezone.utc)
            if expiry > now(): continue
            if pending and pending.get("status") == "waiting":
                changed = yield operation("pending_payments", "update_one", {"_id": pending["_id"], "status": "waiting"},
                                          {"$set": {"status": "expired", "expired_at": now(), "stock_hold_expiry_notice_pending": True}})
                if not changed.modified_count: continue
            if order:
                changed = yield operation("orders", "update_one", {"_id": order["_id"], "status": "pending",
                    "paid_at": {"$exists": False}, "wallet_charge_applied_at": {"$exists": False}},
                    {"$set": {"status": "expired", "expired_at": now()}})
                if not changed.modified_count: continue
            released += bool((yield from _release(order_id, product["name"], expected=hold)))
    if products:
        yield operation("operational_control", "update_one", {"_id": "stock_reservation_sweep"},
                        {"$set": {"cursor": products[-1]["_id"]}}, upsert=True)
    elif state and state.get("cursor"):
        yield operation("operational_control", "update_one", {"_id": "stock_reservation_sweep"}, {"$unset": {"cursor": ""}})
    return released


def reserve_stock(db, *args, **kwargs): return _run(db, _reserve(*args, **kwargs))
async def reserve_stock_async(db, *args, **kwargs): return await _arun(db, _reserve(*args, **kwargs))
def release_stock(db, *args, **kwargs): return _run(db, _release(*args, **kwargs))
async def release_stock_async(db, *args, **kwargs): return await _arun(db, _release(*args, **kwargs))
def sweep_reservations(db, limit=50): return _run(db, _sweep(limit))
async def sweep_reservations_async(db, limit=50): return await _arun(db, _sweep(limit))
