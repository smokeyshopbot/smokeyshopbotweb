"""Persistent, owner-controlled reseller API maintenance and targeted notices."""
import uuid
from datetime import datetime, timezone

from flask import flash, redirect, request, url_for
from pymongo import ReturnDocument
from web_admin.api_runtime import invalidate
from web_admin.delivery_queue import enqueue_job

STATUS_KEY = 'buyer_api_status'


def status(db):
    doc = db.settings.find_one({'key': STATUS_KEY}) or {}
    value = doc.get('value') or {}
    return {'enabled': value.get('enabled') is not False, 'version': value.get('version', '')}


def notice_text(enabled, language='en'):
    if language == 'es':
        return ('✅ <b>API disponible de nuevo</b>\n\nLa API vuelve a estar habilitada. Ya puedes consultar productos y realizar pedidos con tu clave actual.' if enabled else
                '⚠️ <b>API en mantenimiento</b>\n\nLa API está deshabilitada por mantenimiento hasta nuevo aviso. Te avisaremos cuando vuelva a estar disponible. Puedes seguir comprando normalmente en el bot.')
    return ('✅ <b>API enabled again</b>\n\nThe API is available again. You can check products and place orders using your existing API key.' if enabled else
            '⚠️ <b>API maintenance</b>\n\nThe API is disabled for maintenance until further notice. We will notify you when it is available again. You can continue shopping normally in the bot.')


def change_status(db, enabled, expected_version, actor=''):
    """State and notification outbox commit atomically; stale forms cannot toggle."""
    db.settings.update_one({'_id': STATUS_KEY}, {'$setOnInsert': {
        'key': STATUS_KEY, 'value': {'enabled': True, 'version': ''}}}, upsert=True)
    version = uuid.uuid4().hex
    now = datetime.now(timezone.utc)
    query = {'_id': STATUS_KEY, 'value.version': expected_version, 'value.enabled': not enabled}
    doc = db.settings.find_one_and_update(query, {'$set': {
        'value': {'enabled': enabled, 'version': version}, 'updated_at': now,
        'updated_by': actor, 'pending_notices.' + version: {'enabled': enabled, 'created_at': now}}},
        return_document=ReturnDocument.AFTER)
    if doc:
        invalidate(db)
    return bool(doc)


def process_notices(db):
    doc = db.settings.find_one({'key': STATUS_KEY}) or {}
    for version, notice in (doc.get('pending_notices') or {}).items():
        if version == (doc.get('value') or {}).get('version'):
            enqueue_job(db, 'bulk', {'api_audience': True, 'api_status_version': version,
                'texts': {lang: notice_text(notice['enabled'], lang) for lang in ('en', 'es')},
                'parse_mode': 'HTML', 'maintenance_policy': 'continue',
                'notification': {'notification_type': 'api_status'}},
                job_id='api-status:' + version, priority=5)
        db.settings.update_one({'_id': doc['_id']}, {'$unset': {'pending_notices.' + version: ''}})


def notice_is_current(db, payload, snapshot=None):
    if not payload.get('api_status_version'):
        return True
    load = lambda: status(db)
    current = snapshot.get(('api_status',), load) if snapshot else load()
    return current['version'] == payload['api_status_version']


def register_routes(app, shop):
    @app.post('/api-maintenance')
    @shop.login_required
    @shop.owner_required
    @shop.csrf_required
    def api_maintenance_submit():
        enabled = request.form.get('enabled')
        if enabled not in {'0', '1'}:
            return 'Invalid API state', 400
        changed = change_status(app.db, enabled == '1', request.form.get('version', ''),
                                str(shop.session.get('admin_username') or 'owner'))
        if changed:
            flash('Reseller API ' + ('enabled.' if enabled == '1' else 'disabled for maintenance.')
                  + ' API users will be notified in the background.', 'success')
        else:
            flash('The API state has already changed. Review its current status below.', 'info')
        return redirect(url_for('secret_settings_form') + '#secret-security')
