"""Owner controls for timed sales and product/wallet reward coupons."""
import secrets
import html
import uuid
from datetime import datetime, timezone, timedelta
from bson import ObjectId
from flask import flash, redirect, render_template, request, url_for
from pymongo.errors import DuplicateKeyError
from utils.promotions import active_sale, amount, utc
from utils.coupons import normalize_code, coupon_id, recover
from utils.i18n import tr
from utils.product_emojis import label
from web_admin.delivery_queue import enqueue_job
from web_admin.group_announcements import enqueue_notice


def now():
    return datetime.now(timezone.utc)


def integer(value, title, minimum, maximum):
    try:
        number = int(str(value))
    except (TypeError, ValueError):
        raise ValueError(title + ' must be a whole number.') from None
    if not minimum <= number <= maximum:
        raise ValueError(f'{title} must be between {minimum} and {maximum}.')
    return number


def services(db, shop):
    return {'pending_quantity': lambda name: shop.get_pending_stock_quantity(db, name),
            'external_stock': lambda product: shop.get_external_supplier_available_stock(db, product),
            'fulfill': lambda name, quantity, **kw: shop.fulfill_product_items(db, name, quantity, reserve_pending=True, **kw),
            'record_delivery': lambda order, items: shop.record_order_items_delivered_in_ledger(db, order, items, source='coupon')}


def recover_coupons(db, shop):
    if db.coupons.find_one({'pending_count': {'$gt': 0}}, {'_id': 1}):
        return recover(db, services(db, shop))
    return 0


def process_sales(db, shop):
    for product in db.products.find({'sale.notice_pending': True}).limit(20):
        sale = active_sale(product)
        if sale and product.get('enabled') is not False:
            currencies = shop.get_active_payment_currencies(db)
            texts = {}
            for lang in ('en', 'es'):
                lines = [tr(lang, 'sale_started'), '', html.escape(label(product), quote=False)]
                for currency in ('usdt', 'inr'):
                    field = 'price_' + currency
                    if currency in currencies and sale.get(field) is not None:
                        formatter = shop.money_usdt_price if currency == 'usdt' else shop.money_inr_price
                        lines.append('💰 <s>' + html.escape(formatter(product.get(field) or 0)) + '</s> → ' + html.escape(formatter(sale[field])))
                lines.extend(['', tr(lang, 'sale_until', end=utc(sale['ends_at']).strftime('%d %b %Y, %H:%M UTC'))])
                texts[lang] = '\n'.join(lines)
            batch = 'sale:' + sale['id']
            payload = {'texts': texts, 'parse_mode': 'HTML', 'product_name': product['name'],
                'markups': {lang: shop.product_notify_markup(product['name'], lang, product['_id']) for lang in ('en', 'es')},
                'sale_id': sale['id'], 'sale_expires_at': sale['ends_at'], 'maintenance_policy': 'pause',
                'admin_ids': shop.get_admin_ids(db), 'notification': {'notification_type': 'sale',
                    'notification_batch_id': batch, 'notification_title': 'Sale: ' + product['name'],
                    'notification_source': 'web_admin_sale', 'notification_product_name': product['name'],
                    'notification_target_kind': 'customer'}}
            enqueue_job(db, 'bulk', payload, job_id=batch, priority=10)
            enqueue_notice(db, shop, payload, 'prices')
        db.products.update_one({'_id': product['_id'], 'sale.id': (product.get('sale') or {}).get('id')},
                               {'$set': {'sale.notice_pending': False}})


def register_promotions(app, shop):
    app.db.coupons.create_index([('pending_count', 1)])
    app.db.coupon_redemptions.create_index([('user_id', 1), ('created_at', -1)])
    app.db.coupon_redemptions.create_index([('coupon_id', 1), ('created_at', -1)])
    app.db.products.create_index([('sale.notice_pending', 1)])

    @app.post('/products/<path:name>/sale')
    @shop.login_required
    @shop.owner_required
    @shop.csrf_required
    def update_product_sale(name):
        product = app.db.products.find_one({'name': shop.name_regex(name)})
        if not product:
            flash('Product not found.', 'error')
            return redirect(url_for('products'))
        if request.form.get('action') == 'stop':
            app.db.products.update_one({'_id': product['_id']}, {'$set': {'sale.enabled': False, 'sale.notice_pending': False}})
            shop.log_admin_action(app.db, 'sale_stopped', product['name'])
            flash('Sale stopped. New checkouts use the regular price.', 'success')
            return redirect(url_for('product_manage', name=product['name']))
        try:
            duration = integer(request.form.get('duration', '24'), 'Sale duration', 1, 525600)
            factor = {'minutes': 1, 'hours': 60, 'days': 1440}.get(request.form.get('duration_unit', 'hours'))
            if factor is None or duration * factor > 525600:
                raise ValueError('Sale duration must be between one minute and one year.')
            sale = {'id': uuid.uuid4().hex, 'enabled': True, 'starts_at': now(),
                    'ends_at': now() + timedelta(minutes=duration * factor), 'notice_pending': True}
            for currency in ('inr', 'usdt'):
                field = 'price_' + currency
                raw = str(request.form.get('sale_' + field) or '').strip()
                if raw:
                    price = amount(raw)
                    if price >= float(product.get(field) or 0):
                        raise ValueError('Sale prices must be lower than the regular product price.')
                    sale[field] = price
            if not any('price_' + currency in sale for currency in ('inr', 'usdt')):
                raise ValueError('Enter at least one sale price.')
            if product.get('enabled') is False:
                raise ValueError('Enable this product before starting its sale.')
        except ValueError as error:
            flash(str(error), 'error')
            return redirect(url_for('product_manage', name=product['name']))
        app.db.products.update_one({'_id': product['_id']}, {'$set': {'sale': sale}})
        process_sales(app.db, shop)
        shop.log_admin_action(app.db, 'sale_started', product['name'] + ' ends=' + sale['ends_at'].isoformat())
        flash('Sale started. Notifications are queued and the regular price returns automatically when it ends.', 'success')
        return redirect(url_for('product_manage', name=product['name']))

    @app.get('/coupons')
    @shop.login_required
    @shop.owner_required
    def coupons():
        rows = list(app.db.coupons.find({}).sort('created_at', -1).limit(100))
        for row in rows:
            row['expired'] = bool(utc(row.get('expires_at')) and utc(row['expires_at']) <= now())
        history = list(app.db.coupon_redemptions.find({}).sort('created_at', -1).limit(100))
        return render_template('coupons.html', coupons=rows, redemptions=history,
            products=list(app.db.products.find({'enabled': {'$ne': False}}, {'name': 1}).sort('name', 1)))

    @app.post('/coupons/create')
    @shop.login_required
    @shop.owner_required
    @shop.csrf_required
    def create_coupon():
        try:
            code = normalize_code(request.form.get('code') or 'SMO-' + secrets.token_hex(8).upper())
            kind = request.form.get('kind')
            if kind not in {'product', 'wallet'}:
                raise ValueError('Choose a product or wallet reward.')
            hours = integer(request.form.get('expiry_hours', '24'), 'Expiry hours', 0, 8760)
            document = {'_id': coupon_id(code), 'code': code, 'kind': kind, 'enabled': True,
                'max_uses': integer(request.form.get('max_uses', '1'), 'Total redemption limit', 1, 10000),
                'claimed_count': 0, 'pending_count': 0, 'claimed_users': {}, 'pending_claims': {},
                'created_at': now(), 'created_by': shop.current_admin_username(),
                'expires_at': now() + timedelta(hours=hours) if hours else None}
            if kind == 'wallet':
                currency = request.form.get('currency')
                if currency not in {'inr', 'usdt'}:
                    raise ValueError('Choose INR or USDT.')
                document.update(currency=currency, amount=amount(request.form.get('amount')))
            else:
                try:
                    product = app.db.products.find_one({'_id': ObjectId(request.form.get('product_id')), 'enabled': {'$ne': False}})
                except Exception:
                    product = None
                if not product:
                    raise ValueError('Choose an enabled product.')
                document.update(product_id=product['_id'], product_name=product['name'],
                    quantity=integer(request.form.get('quantity', '1'), 'Reward quantity', 1, 100))
            app.db.coupons.insert_one(document)
        except (ValueError, DuplicateKeyError) as error:
            flash('That coupon code already exists.' if isinstance(error, DuplicateKeyError) else str(error), 'error')
            return redirect(url_for('coupons'))
        shop.log_admin_action(app.db, 'coupon_created', 'kind=' + kind + ' limit=' + str(document['max_uses']))
        flash('Coupon created. Share its code with the intended users; they can use /redeem CODE.', 'success')
        return redirect(url_for('coupons'))

    @app.post('/coupons/<identifier>/toggle')
    @shop.login_required
    @shop.owner_required
    @shop.csrf_required
    def toggle_coupon(identifier):
        app.db.coupons.update_one({'_id': identifier}, {'$set': {'enabled': request.form.get('enabled') == '1'}})
        shop.log_admin_action(app.db, 'coupon_toggled', identifier[:12])
        flash('Coupon availability updated. Rewards already claimed will still be completed.', 'success')
        return redirect(url_for('coupons'))

    @app.post('/coupons/redemptions/<identifier>/retry')
    @shop.login_required
    @shop.owner_required
    @shop.csrf_required
    def retry_coupon(identifier):
        app.db.coupon_redemptions.update_one({'_id': identifier, 'status': {'$in': ['review', 'pending_stock', 'processing']}},
            {'$set': {'status': 'processing', 'next_attempt_at': now()}})
        shop.log_admin_action(app.db, 'coupon_retry_requested', identifier[:12])
        flash('Safe retry requested. Uncertain vendor purchases still require verification before delivery.', 'success')
        return redirect(url_for('coupons'))
