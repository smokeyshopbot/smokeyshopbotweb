"""Owner catalogue previews and explicit imports; never import stock credentials."""
import hashlib
import json
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_UP
from urllib.parse import urlsplit

import requests
from bson import ObjectId
from flask import abort, flash, redirect, render_template, request, url_for
from pymongo.errors import DuplicateKeyError


class CatalogueError(ValueError):
    pass


def fetch_catalogue(supplier, shop):
    url = shop.supplier_products_url(supplier.get('base_url', ''))
    parsed = urlsplit(url)
    if parsed.scheme not in {'http', 'https'} or not parsed.hostname or parsed.username:
        raise CatalogueError('Vendor URL must be an HTTP or HTTPS address without embedded credentials.')
    key = str(supplier.get('api_key') or '').strip()
    if not key or any(ord(char) < 32 for char in key):
        raise CatalogueError('Vendor API key is empty or contains invalid characters.')
    with requests.get(url, headers={'Authorization': 'Bearer ' + key},
                      params={'key': key} if supplier.get('legacy_query_auth', True) else {},
                      timeout=(3, 8), stream=True, allow_redirects=False) as response:
        if response.status_code != 200:
            raise CatalogueError(f'Vendor returned HTTP {response.status_code}. Check its URL, API access and key.')
        chunks, size = [], 0
        for chunk in response.iter_content(65536):
            size += len(chunk)
            if size > 2 * 1024 * 1024:
                raise CatalogueError('Vendor catalogue exceeds the 2 MB preview limit.')
            chunks.append(chunk)
        data = json.loads(b''.join(chunks))
    if not isinstance(data, dict) or data.get('success') is False or not isinstance(data.get('products'), list):
        raise CatalogueError('Vendor did not return a compatible product catalogue.')
    rows, seen = [], set()
    if len(data['products']) > 1000:
        raise CatalogueError('Vendor catalogue exceeds the 1,000 product preview limit.')
    for row in data['products']:
        if not isinstance(row, dict):
            continue
        product_id = str(row.get('product_id') or row.get('_id') or '').strip()
        name = str(row.get('product_name') or row.get('product_name_raw') or '').strip()
        if not product_id or len(product_id) > 200 or not name or product_id in seen:
            continue
        seen.add(product_id)
        try:
            price = Decimal(str(row.get('walletPricing', row.get('pricing', 0))))
            if not price.is_finite() or price < 0 or price > 1000000:
                continue
            stock = max(0, int((row.get('stats') or {}).get('available') or 0))
        except (ValueError, TypeError, InvalidOperation):
            continue
        supported = (row.get('walletCurrency', 'USDT') == 'USDT'
                     and not row.get('isSlotProduct') and not row.get('requiresCustomerEmail')
                     and not row.get('requiresSlotMonths') and not row.get('quantityFixed'))
        rows.append({'product_id': product_id, 'name': name[:180], 'price': str(price),
                     'selling_price': str(max(Decimal('0.01'), price.quantize(Decimal('0.01'), rounding=ROUND_UP))),
                     'stock': stock, 'description': str(row.get('description') or '')[:4000],
                     'supported': supported})
    return rows


def register_vendor_catalog_routes(app, shop):
    app.db.vendor_catalog_snapshots.create_index('expires_at', expireAfterSeconds=0)
    app.db.products.create_index('vendor_import_key', unique=True, sparse=True)

    def vendor(supplier_id):
        if not ObjectId.is_valid(supplier_id):
            abort(404)
        value = app.db.supplier_apis.find_one({'_id': ObjectId(supplier_id), 'enabled': {'$ne': False}})
        if not value:
            abort(404)
        return value

    @app.get('/supplier-apis/<supplier_id>/products')
    @shop.login_required
    @shop.owner_required
    def vendor_products(supplier_id):
        supplier = vendor(supplier_id)
        snapshot = app.db.vendor_catalog_snapshots.find_one({'_id': supplier_id})
        now = datetime.now(timezone.utc)
        expiry = (snapshot or {}).get('expires_at')
        if expiry and expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=timezone.utc)
        if not snapshot or not expiry or expiry <= now or request.args.get('refresh') == '1':
            try:
                rows = fetch_catalogue(supplier, shop)
            except Exception as error:
                message = str(error) if isinstance(error, CatalogueError) else 'Could not load vendor products. Check the connection and try again.'
                flash(message, 'error')
                return redirect(url_for('supplier_apis'))
            snapshot = {'_id': supplier_id, 'revision': uuid.uuid4().hex, 'rows': rows,
                        'expires_at': now + timedelta(minutes=15)}
            app.db.vendor_catalog_snapshots.replace_one({'_id': supplier_id}, snapshot, upsert=True)
        return render_template('vendor_products.html', supplier=supplier, snapshot=snapshot)

    @app.post('/supplier-apis/<supplier_id>/products/import')
    @shop.login_required
    @shop.owner_required
    @shop.csrf_required
    def import_vendor_products(supplier_id):
        vendor(supplier_id)
        snapshot = app.db.vendor_catalog_snapshots.find_one({'_id': supplier_id, 'revision': request.form.get('revision')})
        expiry = (snapshot or {}).get('expires_at')
        if expiry and expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=timezone.utc)
        if not expiry or expiry <= datetime.now(timezone.utc):
            flash('Preview expired or changed. Load the catalogue again before importing.', 'error')
            return redirect(url_for('vendor_products', supplier_id=supplier_id, refresh=1))
        selected = list(dict.fromkeys(request.form.getlist('selected')))
        if not selected or len(selected) > 100:
            flash('Select between 1 and 100 products.', 'error')
            return redirect(url_for('vendor_products', supplier_id=supplier_id))
        planned = []
        try:
            for index in selected:
                number = int(index)
                if number < 0 or number >= len(snapshot['rows']):
                    raise ValueError()
                row = snapshot['rows'][number]
                price = Decimal(str(request.form.get('price_' + index, '')))
                if not row['supported'] or not price.is_finite() or not Decimal('0.01') <= price <= 1000000 or price != price.quantize(Decimal('0.01')):
                    raise ValueError()
                planned.append((row, float(price)))
        except (ValueError, InvalidOperation):
            flash('Select supported USDT stock products and enter selling prices of at least 0.01 USDT, with up to two decimal places.', 'error')
            return redirect(url_for('vendor_products', supplier_id=supplier_id))
        added, skipped = 0, 0
        for row, price in planned:
            identity = hashlib.sha256((supplier_id + ':' + row['product_id']).encode()).hexdigest()
            duplicate = app.db.products.find_one({'$or': [
                {'vendor_import_key': identity}, {'name': shop.name_regex(row['name'])},
                {'supplier_fallbacks': {'$elemMatch': {'supplier_id': supplier_id, 'supplier_product_id': row['product_id']}}}]}, {'_id': 1})
            if duplicate:
                skipped += 1
                continue
            document = {'name': row['name'], 'enabled': True, 'price_usdt': price, 'price_inr': 0,
                        'stock': [], 'created_at': datetime.now(timezone.utc), 'warranty_days': 0,
                        'api_enabled': True, 'api_product_id': 'vendor-' + identity[:24],
                        'vendor_import_key': identity, 'description_en': row['description'],
                        'external_supplier_fallback_enabled': True,
                        'supplier_fallbacks': [{'supplier_id': supplier_id, 'supplier_product_id': row['product_id'],
                                                'priority': 1, 'enabled': True}]}
            try:
                result = app.db.products.update_one({'vendor_import_key': identity}, {'$setOnInsert': document}, upsert=True)
                added += int(result.upserted_id is not None)
                skipped += int(result.upserted_id is None)
            except DuplicateKeyError:
                skipped += 1
        shop.log_admin_action(app.db, 'vendor_products_imported', f'vendor={supplier_id} added={added} skipped={skipped}')
        flash(f'Imported {added} product(s); skipped {skipped} existing product(s). Existing prices and stock were kept.', 'success')
        return redirect(url_for('products'))
