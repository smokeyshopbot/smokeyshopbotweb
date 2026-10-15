"""Reseller endpoints using the existing customer's USDT wallet."""
import functools
import uuid
from datetime import timedelta
from decimal import Decimal, InvalidOperation
from flask import flash, jsonify, redirect, request, url_for, g, make_response
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError
from web_admin.api_security import request_key, rate_limit, ensure_security_indexes
from utils.wallet_audit import change_wallet, event_id
from utils.stock_reservations import reserve_stock, release_stock
from werkzeug.exceptions import TooManyRequests
from utils.extra_i18n import api_message


def ensure_api_indexes(db):
    ensure_security_indexes(db)
    # Wallet references are scoped to one Telegram user; duplicate user rows
    # must never select an arbitrary wallet. Upgrade the older WebAdmin index.
    previous = db.users.index_information().get("user_id_1", {})
    if previous and not previous.get("unique"):
        duplicates = list(db.users.aggregate([
            {"$group": {"_id": "$user_id", "count": {"$sum": 1}}}, {"$match": {"count": {"$gt": 1}}}, {"$limit": 1}]))
        if duplicates: raise RuntimeError("Duplicate Telegram user IDs exist. Review their wallets before upgrading the user index.")
        db.users.drop_index("user_id_1")
    db.users.create_index("user_id", unique=True)
    # Idempotency is required even when optional dashboard indexes are off.
    db.users.create_index("api_key_hash", unique=True, sparse=True)
    db.orders.create_index([("api_buyer_user_id", 1), ("api_client_order_id", 1)], unique=True,
                           partialFilterExpression={"api_client_order_id": {"$type": "string"}})
    db.api_purchase_locks.create_index("key", unique=True)
    db.users.create_index([("wallet_usdt", 1), ("api_low_balance_next_at", 1)], name="api_low_balance_candidates",
                         partialFilterExpression={"api_request_count": {"$gt": 0}, "api_last_used_at": {"$exists": True}})
    db.users.create_index("api_low_balance_outbox.job_id", name="api_low_balance_outbox",
                         partialFilterExpression={"api_low_balance_outbox.job_id": {"$exists": True}})
    db.products.create_index([("stock_reservation_count", 1), ("_id", 1)], name="active_stock_reservations",
                             partialFilterExpression={"stock_reservation_count": {"$gt": 0}})


def register_buyer_api_routes(app, shop):
    @app.post("/orders/<order_id>/retry-api-receipt")
    @shop.login_required
    @shop.owner_required
    @shop.csrf_required
    def retry_api_receipt(order_id):
        result = app.db.orders.update_one({"order_id": order_id.upper(), "api_purchase": True,
            "status": "delivered", "api_receipt_status": "failed"}, {"$set": {
                "api_receipt_status": "pending", "api_receipt_attempts": 0, "api_receipt_next_attempt": shop.utcnow()}})
        flash("Receipt queued for another attempt." if result.modified_count else "No failed receipt found.", "info")
        return redirect(url_for("order_detail", order_id=order_id.upper()))

    def guarded(fn):
        @functools.wraps(fn)
        def wrapped(*args, **kwargs):
            g.buyer_api_lang = request.accept_languages.best_match(["en", "es"], default="en")
            try:
                settings = shop.get_secret_settings(app.db)
                delay = rate_limit(app.db, "ip:" + (request.remote_addr or "unknown"), int(settings.get("api_ip_requests_per_minute") or 180))
                if delay:
                    response = jsonify({"success": False, "code": "rate_limited", "message": api_message("Too many requests. Try again later.", g.buyer_api_lang)})
                    response.status_code = 429
                    response.headers["Retry-After"] = str(delay)
                    return response
                response = make_response(fn(*args, **kwargs))
                data = response.get_json(silent=True)
                if isinstance(data, dict) and isinstance(data.get("message"), str):
                    data["message"] = api_message(data["message"], g.buyer_api_lang)
                    if "lang" in data: data["lang"] = g.buyer_api_lang
                    response.set_data(app.json.dumps(data))
                return response
            except TooManyRequests as error:
                response = jsonify({"success": False, "code": "rate_limited", "message": api_message("Too many requests. Try again later.", g.buyer_api_lang)})
                response.status_code = 429
                response.headers["Retry-After"] = str(error.retry_after)
                return response
            except Exception:
                app.logger.exception("Buyer API request failed")
                return shop.buyer_api_error(api_message("Service temporarily unavailable. Retry using the same client_order_id.", g.buyer_api_lang), 503)
        return wrapped

    def auth(payload=None):
        settings = shop.get_secret_settings(app.db)
        key = request_key(payload, str(settings.get("api_allow_legacy_query_keys", "1")) == "1")
        if not isinstance(key, str):
            return None, "Invalid API key"
        user, error = shop.authenticate_buyer_api_key(app.db, key)
        if user:
            g.buyer_api_lang = shop.lang_from_user(user)
            delay = rate_limit(app.db, "key:" + shop.buyer_api_key_hash(key), int(settings.get("api_key_requests_per_minute") or 60))
            if delay:
                raise TooManyRequests(retry_after=delay)
        return user, error

    def cached_order(order):
        response = order.get("api_purchase_response") or order.get("api_error_response")
        if response:
            return jsonify(response), int(order.get("api_response_status", 200))
        return jsonify({"success": False, "pending": True, "orderCode": order["order_id"],
                        "message": "Order is processing or requires review. Do not submit it with a new client_order_id."}), 202

    @app.get("/api/telegram-buyer/products")
    @guarded
    def buyer_api_products():
        user, error = auth()
        if not user:
            return shop.buyer_api_error(error, 401)
        language = shop.lang_from_user(user)
        custom_prices = {}
        for custom in app.db.user_product_prices.find({'user_id': int(user['user_id'])}):
            if custom.get('product_key'):
                custom_prices.setdefault(custom['product_key'], custom)
        sold_counts = {}
        for row in app.db.orders.aggregate([
            {'$match': {'status': 'delivered', 'is_replacement': {'$ne': True}}},
            {'$group': {'_id': '$product_name', 'count': {'$sum': 1}}}
        ]):
            key = str(row['_id'] or '').casefold()
            sold_counts[key] = sold_counts.get(key, 0) + row['count']
        rows = []
        for product in shop.get_products_with_availability(app.db):
            if product.get("api_enabled") is False:
                continue
            priced = shop.product_effective_price(product, user,
                custom_prices.get(shop.product_name_key(product['name']))) or product
            price = round(max(0, shop.safe_float(priced.get("price_usdt"), 0)), 6)
            stock = int(product.get("available_stock", 0))
            sold = sold_counts.get(product['name'].casefold(), 0)
            description = product.get(f"description_{language}") or product.get("description_en") or product.get("description") or ""
            product_id = shop.public_api_product_id(product)
            rows.append({"_id": product_id, "product_id": product_id, "product_name": product["name"], "product_name_raw": product["name"],
                         "pricing": price, "description": description, "description_raw": description,
                         "walletCurrency": "USDT", "walletPricing": price, "walletPricingText": shop.api_money_usdt(price), "usdPricing": price,
                         "isSlotProduct": False, "slotDurations": [], "requiresCustomerEmail": False, "requiresSlotMonths": False,
                         "quantityFixed": None, "slotPricingMode": None, "promotions": [], "stats": {"total": sold + stock, "sold": sold, "available": stock}})
        return jsonify({"success": True, "lang": language, "botSource": "primary", "walletCurrency": "USDT",
                        "requester": {"chatId": user["user_id"], "name": user.get("username", "")}, "products": rows})

    @app.get("/api/telegram-buyer/balance")
    @guarded
    def buyer_api_balance():
        user, error = auth()
        if not user:
            return shop.buyer_api_error(error, 401)
        balance = round(shop.safe_float(user.get("wallet_usdt"), 0), 6)
        return jsonify({"success": True, "lang": shop.lang_from_user(user), "botSource": "primary", "walletCurrency": "USDT",
                        "requester": {"chatId": user["user_id"], "name": user.get("username", "")},
                        "balance": balance, "balanceUsd": balance, "balanceText": shop.api_money_usdt(balance), "usdtBalance": balance,
                        "balanceVnd": 0, "usdRate": 1, "updatedAt": shop.utcnow().isoformat()})

    @app.get("/api/telegram-buyer/orders/<order_id>")
    @guarded
    def buyer_api_order(order_id):
        user, error = auth()
        if not user:
            return shop.buyer_api_error(error, 401)
        order = app.db.orders.find_one({"order_id": order_id.upper(), "api_buyer_user_id": user["user_id"], "api_purchase": True})
        if not order:
            return shop.buyer_api_error("Order not found", 404)
        return cached_order(order)

    @app.post("/api/telegram-buyer/purchase")
    @guarded
    def buyer_api_purchase():
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return shop.buyer_api_error("Send a JSON object", 400)
        user, error = auth(payload)
        if not user:
            return shop.buyer_api_error(error, 401)
        uid = int(user["user_id"])
        client_id = payload.get("client_order_id", "")
        if not isinstance(client_id, str) or len(client_id.strip()) > 120:
            return shop.buyer_api_error("client_order_id must be a string of at most 120 characters", 400)
        client_id = client_id.strip()
        if not client_id:
            return shop.buyer_api_error("client_order_id is required for every purchase", 400)
        product_id = payload.get("product_id")
        if not isinstance(product_id, str) or not product_id.strip():
            return shop.buyer_api_error("product_id is required", 400)
        value = payload.get("quantity", 1)
        try:
            if isinstance(value, bool):
                raise ValueError
            number = Decimal(str(value))
            if not number.is_finite() or number != number.to_integral_value() or number < 1 or number > 100000:
                raise ValueError
            quantity = int(number)
        except (InvalidOperation, ValueError, TypeError):
            return shop.buyer_api_error("quantity must be a positive whole number", 400)

        def existing_request():
            if not client_id:
                return None
            existing = app.db.orders.find_one({"api_buyer_user_id": uid, "api_client_order_id": client_id, "api_purchase": True})
            if existing:
                if existing.get("api_request_product_id") != product_id.strip() or existing.get("quantity") != quantity:
                    return shop.buyer_api_error("client_order_id already belongs to a different purchase", 409)
                return cached_order(existing)
            return None
        existing = existing_request()
        if existing is not None:
            return existing
        if shop.get_setting(app.db, "maintenance_mode", False):
            return shop.buyer_api_error("Shop is under maintenance. Try again later.", 503)
        product = shop.find_product_by_public_api_id(app.db, product_id.strip())
        if not product or product.get("enabled") is False or product.get("api_enabled") is False:
            return shop.buyer_api_error("Product not found", 404)
        minimum = shop.parse_positive_int(product.get("min_order_quantity"), 1, minimum=1)
        maximum = shop.parse_positive_int(product.get("max_order_quantity"), 100, minimum=1)
        if not minimum <= quantity <= maximum:
            return shop.buyer_api_error(f"Quantity must be between {minimum} and {maximum}", 400)
        priced = shop.effective_product_for_user(app.db, product, user) or product
        unit_price = Decimal(str(priced.get("price_usdt", 0)))
        if not unit_price.is_finite() or unit_price <= 0:
            return shop.buyer_api_error("Product USDT price is not configured", 400)
        amount = float((unit_price * quantity).quantize(Decimal("0.000001")))
        token = uuid.uuid4().hex
        lock_key = f"buyer:{uid}"
        now = shop.utcnow()
        try:
            app.db.api_purchase_locks.find_one_and_update({"key": lock_key, "$or": [
                {"expires_at": {"$lte": now}}, {"expires_at": {"$exists": False}}]},
                {"$set": {"key": lock_key, "owner": token, "expires_at": now + timedelta(minutes=10)}},
                upsert=True, return_document=ReturnDocument.AFTER)
        except DuplicateKeyError:
            return shop.buyer_api_error("Another purchase is processing. Retry using the same client_order_id.", 409)
        order = None
        items = []
        fulfillment = {"source": "none"}
        debit_path = ""
        committed = False
        def refund():
            # Charge and its recovery marker are changed in one user update.
            # Repeated rollback calls cannot refund the same debit twice.
            result = change_wallet(app.db, uid, "usdt", amount, reference="api-refund-" + order["order_id"], source="api_refund",
                conditions={debit_path: {"$exists": True}}, extra={"$unset": {debit_path: ""}})
            if result:
                app.db.orders.update_one({"_id": order["_id"]}, {"$set": {"wallet_refund_event_id": event_id(uid, "usdt", "api-refund-" + order["order_id"]), "wallet_refund_event_delta": amount}})
            return bool(result)
        try:
            existing = existing_request()
            if existing is not None:
                return existing
            available = shop.get_available_stock_count(app.db, product["name"])
            if available < quantity and not shop.normalize_supplier_fallbacks(product):
                return shop.buyer_api_error("Inventory not enough", 409)
            if available < quantity:
                pending_overhang = max(0, shop.get_pending_stock_quantity(app.db, product['name']) - shop.get_stock_count(app.db, product['name']))
                if pending_overhang and max(0, available + shop.get_external_supplier_available_stock(app.db, product) - pending_overhang) < quantity:
                    return shop.buyer_api_error("Inventory reserved for earlier paid orders", 409)
            order_id = uuid.uuid4().hex[:12].upper()
            order = {"order_id": order_id, "user_id": uid, "username": user.get("username", ""), "product_name": product["name"],
                     "quantity": quantity, "items": [], "payment_method": "api_wallet_usdt", "amount_inr": 0.0, "amount_usdt": amount,
                     "status": "api_processing", "created_at": now, "api_purchase": True, "api_delivery_mode": "json",
                     "api_buyer_user_id": uid, "api_product_id": shop.public_api_product_id(product), "api_request_product_id": product_id.strip(),
                     "api_stage": "created", "effective_price_group": priced.get("effective_price_group"),
                     "wallet_event_id": event_id(uid, "usdt", "api-purchase-" + order_id), "wallet_event_delta": -amount,
                     "effective_price_source": priced.get("effective_price_source"), **shop.product_order_txt_instruction_fields(product)}
            if client_id:
                order["api_client_order_id"] = client_id
            try:
                order["_id"] = app.db.orders.insert_one(order).inserted_id
            except DuplicateKeyError:
                existing = existing_request()
                return existing if existing is not None else shop.buyer_api_error("Order conflict. Please retry.", 409)
            debit_path = f"api_wallet_debits.{order_id}"
            reservation = reserve_stock(app.db, order_id, product["name"], quantity, uid,
                protected=True, partial=bool(shop.normalize_supplier_fallbacks(product)),
                pending_quantity=shop.get_pending_stock_quantity(app.db, product["name"]))
            if not reservation.get("ok"):
                response = {"success": False, "status": "failed", "code": "out_of_stock", "message": "Inventory not enough",
                            "orderCode": order_id, "client_order_id": client_id, "retryable": False}
                app.db.orders.update_one({"_id": order["_id"]}, {"$set": {"status": "failed", "api_stage": "rejected_before_charge",
                    "api_error_code": "out_of_stock", "api_error_message": "Inventory not enough", "failed_at": shop.utcnow(),
                    "api_error_response": response, "api_response_status": 409}})
                return jsonify(response), 409
            updated_user = change_wallet(app.db, uid, "usdt", -amount, reference="api-purchase-" + order_id, source="api_purchase",
                conditions={"api_enabled": {"$ne": False}, "blocked": {"$ne": True}, "wallet_usdt": {"$gte": amount}, debit_path: {"$exists": False}},
                extra={"$set": {debit_path: amount}})
            if not updated_user:
                latest_user = app.db.users.find_one({"user_id": uid}) or {}
                balance = round(shop.safe_float(latest_user.get("wallet_usdt"), 0), 6)
                disabled = not latest_user or latest_user.get("blocked") or latest_user.get("api_enabled") is False
                insufficient = balance < amount
                code = "api_access_disabled" if disabled else "insufficient_balance" if insufficient else "wallet_state_changed"
                message = "API access is disabled" if disabled else "Insufficient wallet balance" if insufficient else "Wallet or API access changed during purchase"
                status = 403 if disabled else 400 if insufficient else 409
                failed_at = shop.utcnow()
                response = {"success": False, "status": "failed", "code": code, "message": message,
                    "orderCode": order_id, "client_order_id": client_id, "walletCurrency": "USDT",
                    "balance": balance, "requiredBalance": amount, "shortfall": round(max(0, amount - balance), 6),
                    "failedAt": failed_at.isoformat(), "retryable": False, "retryWithNewClientOrderId": not disabled}
                app.db.orders.update_one({"_id": order["_id"]}, {"$set": {"status": "failed", "api_stage": "rejected_before_charge",
                    "api_error_code": code, "api_error_message": message, "failed_at": failed_at,
                    "api_error_response": response, "api_response_status": status}})
                release_stock(app.db, order_id, product["name"])
                return jsonify(response), status
            app.db.orders.update_one({"_id": order["_id"]}, {"$set": {"api_stage": "charged", "paid_at": shop.utcnow()}})
            fulfillment = shop.fulfill_product_items(app.db, product["name"], quantity, order_id=order_id, reserve_pending=True)
            items = fulfillment.get("items") or []
            if len(items) != quantity:
                if fulfillment.get("uncertain"):
                    app.db.orders.update_one({"_id": order["_id"]}, {"$set": {
                        "supplier_outcome_uncertain": True, "supplier_id": fulfillment.get("supplier_id"),
                        "supplier_request_id": fulfillment.get("client_order_id"),
                    }})
                raise ValueError("Inventory not enough")
            response = shop.build_buyer_purchase_response(app.db, order, updated_user, product, items)
            fields = {"status": "delivered", "api_stage": "complete", "items": items, "delivered_at": shop.utcnow(),
                      "payment_verified_at": shop.utcnow(), "api_purchase_response": response, "api_response_status": 200,
                      "api_balance_after": updated_user["wallet_usdt"], "api_receipt_status": "pending"}
            if fulfillment.get("source") == "supplier":
                fields.update({"external_supplier_delivery": True, "supplier_id": fulfillment.get("supplier_id"),
                               "supplier_alias": fulfillment.get("supplier_alias"), "supplier_product_id": fulfillment.get("supplier_product_id"),
                               "supplier_order_code": fulfillment.get("supplier_order_code"), "supplier_delivered_at": shop.utcnow()})
                fields.update(local_delivered_quantity=fulfillment.get("local_quantity", 0),
                              supplier_delivered_quantity=fulfillment.get("supplier_quantity", quantity))
            app.db.orders.update_one({"_id": order["_id"]}, {"$set": fields})
            committed = True
            # Optional bookkeeping must never refund an already delivered order.
            try:
                release_stock(app.db, order_id, product["name"], discard=True)
                app.db.users.update_one({"_id": user["_id"]}, {"$unset": {debit_path: ""}})
                shop.record_order_items_delivered_in_ledger(app.db, {**order, **fields}, items, source="api")
            except Exception:
                app.logger.exception("API order completed; bookkeeping needs review: %s", order_id)
            return jsonify(response)
        except Exception as exc:
            app.logger.exception("Buyer purchase needs rollback")
            if committed:
                return cached_order(app.db.orders.find_one({"_id": order["_id"]}) or order)
            if order and order.get("_id"):
                # A lost database acknowledgement may have committed delivery.
                latest = app.db.orders.find_one({"_id": order["_id"]}) or {}
                if latest.get("status") == "delivered":
                    return cached_order(latest)
                if items and fulfillment.get("source") == "local_stock" and not fulfillment.get("reserved_local"):
                    shop.restore_admin_send_stock_items(app.db, product["name"], items, username="api", stock_upload_kind="api_rollback", note=f"API rollback {order['order_id']}")
                if debit_path:
                    refund()
                response = {"success": False, "orderCode": order["order_id"], "message": "Supplier response is uncertain; wallet deduction was reversed. Contact support with this order ID." if fulfillment.get("uncertain") else "Inventory not enough" if isinstance(exc, ValueError) else "Purchase failed; wallet deduction was reversed."}
                app.db.orders.update_one({"_id": order["_id"]}, {"$set": {"status": "failed", "api_stage": "rolled_back", "api_error_response": response, "api_response_status": 409 if isinstance(exc, ValueError) else 503}})
                release_stock(app.db, order["order_id"], product["name"])
                return jsonify(response), 409 if isinstance(exc, ValueError) else 503
            return shop.buyer_api_error("Purchase failed. Retry using the same client_order_id.", 503)
        finally:
            try:
                app.db.api_purchase_locks.delete_one({"key": lock_key, "owner": token})
            except Exception:
                app.logger.exception("Could not release API purchase lock")
