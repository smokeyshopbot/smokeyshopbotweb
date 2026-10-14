"""A queued forwarding experiment; normal channel announcements are unchanged."""
from collections import Counter
from datetime import timedelta
import re
import uuid

from flask import flash, redirect, url_for
from pymongo.errors import DuplicateKeyError
from utils.premium_emojis import mapping
from web_admin.delivery_queue import enqueue_job, now


def _emoji_counts(message):
    return Counter(str(entity.get('custom_emoji_id')) for entity in message.get('entities', [])
                   if entity.get('type') == 'custom_emoji')


def _button_urls(message):
    return {button['url'] for row in (message.get('reply_markup') or {}).get('inline_keyboard', [])
            for button in row if button.get('url')}


def test_state(db, job=None):
    if job is None:
        latest = db.operational_control.find_one({'_id': 'channel_emoji_test'}) or {}
        job = db.delivery_jobs.find_one({'job_id': latest.get('last_job_id')}) if latest.get('last_job_id') else None
    if not job:
        return None
    source = db.delivery_jobs.find_one({'job_id': job['payload']['source_job_id']}) or {}
    expected = Counter(job['payload']['expected_custom_ids'])
    expected_buttons = _button_urls(source.get('payload') or {})
    errors = [text for text in (source.get('error'), job.get('error')) if text]
    return {'job_id': job['job_id'], 'status': job['status'], 'source_status': source.get('status', 'missing'),
            'source_job_id': source.get('job_id'), 'error': ' '.join(dict.fromkeys(errors)),
            'source_sent': bool(source.get('acknowledged_at')), 'forward_sent': bool(job.get('acknowledged_at')),
            'source_emojis': bool(expected) and expected <= _emoji_counts(source.get('telegram_result') or {}),
            'forward_emojis': bool(expected) and expected <= _emoji_counts(job.get('telegram_result') or {}),
            'button_preserved': bool(expected_buttons) and expected_buttons <= _button_urls(job.get('telegram_result') or {})}


def preflight(shop, job):
    """Read-only target checks run in the worker, before marking a send started."""
    source = job['kind'] == 'emoji_test_source'
    result = shop._telegram_http_result('getChat', {'chat_id': job['payload']['chat_id']})
    if not result.get('ok'):
        return result
    chat = result.get('result') or {}
    if source and (chat.get('type') not in {'group', 'supergroup'} or chat.get('username')):
        return {'ok': False, 'status_code': 400, 'error': 'The forwarding test source must be a private group or supergroup.'}
    if source and chat.get('has_protected_content'):
        return {'ok': False, 'status_code': 400, 'error': 'Disable content protection in the source group so its test message can be forwarded.'}
    if not source and chat.get('type') != 'channel':
        return {'ok': False, 'status_code': 400, 'error': 'The forwarding test destination must be a channel.'}
    return {'ok': True}


def register_routes(app, shop):
    @app.post('/secret-settings/channel-emoji-test')
    @shop.login_required
    @shop.owner_required
    @shop.csrf_required
    def channel_emoji_test():
        settings = shop.get_secret_settings(app.db)
        source = settings.get('channel_emoji_test_source_chat_id', '')
        channel = settings.get('channel_notifications_chat_id') or settings.get('required_channel_id') or ''
        error = None
        if settings.get('premium_emojis_enabled') != '1':
            error = 'Enable premium emojis and save Secret Settings before running the forwarding test.'
        elif not settings.get('bot_token'):
            error = 'Save the Telegram bot token before running the forwarding test.'
        elif not re.fullmatch(r'-[1-9][0-9]{0,19}', source):
            error = 'Save a private source group ID before running the forwarding test.'
        elif not re.fullmatch(r'(?:@[A-Za-z][A-Za-z0-9_]{4,31}|-100[0-9]+)', channel):
            error = 'Save an announcement channel or required membership channel before running the forwarding test.'
        elif source == channel:
            error = 'The private source group and destination channel must be different chats.'
        ids = mapping(settings)
        expected = [ids[emoji] for emoji in ('📦', '✅') if emoji in ids]
        if not error and not expected:
            error = 'Save a premium emoji ID for 📦 or ✅ before running the forwarding test.'
        product = app.db.products.find_one({'enabled': {'$ne': False}}, sort=[('name', 1)])
        if not error and not product:
            error = 'Add an enabled product so the forwarding test can include a Buy product button.'
        back = url_for('secret_settings_form') + '#secret-notifications'
        if error:
            flash(error, 'error')
            return redirect(back)
        token = uuid.uuid4().hex
        try:
            app.db.operational_control.update_one({'_id': 'channel_emoji_test', '$or': [
                {'enqueue_until': {'$lte': now()}}, {'enqueue_until': {'$exists': False}}]},
                {'$set': {'enqueue_owner': token, 'enqueue_until': now() + timedelta(seconds=60)}}, upsert=True)
        except DuplicateKeyError:
            flash('A forwarding test is being queued. Please refresh shortly.', 'info')
            return redirect(back)
        try:
            # Check under the enqueue lease, so double clicks cannot post twice.
            if app.db.delivery_jobs.find_one({'kind': 'emoji_forward', 'status': {'$in': ['queued', 'working']}}):
                flash('A forwarding test is already queued. Open its delivery status below.', 'info')
                return redirect(back)
            jid = 'emoji-forward-test:' + token
            sid = jid + ':source'
            lang = settings.get('channel_notifications_language', 'en')
            text = ('📦 Prueba de reenvío de emojis premium\n✅ Comprueba que estos emojis sigan animados.'
                    if lang == 'es' else '📦 Premium emoji forwarding test\n✅ Check that these emojis stay animated.')
            # The forward waits for its source acknowledgement. Creating it
            # first also prevents an orphaned source from posting after a crash.
            enqueue_job(app.db, 'emoji_forward', {'chat_id': int(channel) if channel.startswith('-') else channel,
                'source_job_id': sid, 'expected_custom_ids': expected, 'text': text,
                'channel_recipient': True, 'admin_recipient': True,
                'notification': {'notification_type': 'emoji_test', 'notification_target_kind': 'channel',
                                 'notification_title': 'Premium emoji forwarding test'}}, job_id=jid, priority=6)
            app.db.operational_control.update_one({'_id': 'channel_emoji_test', 'enqueue_owner': token},
                {'$set': {'last_job_id': jid}})
            enqueue_job(app.db, 'emoji_test_source', {'chat_id': int(source), 'text': text, 'language': lang,
                'channel_recipient': True, 'admin_recipient': True, 'deep_link_product_id': str(product['_id']),
                'notification': {'notification_type': 'emoji_test', 'notification_target_kind': 'group',
                                 'notification_title': 'Premium emoji forwarding test source'}},
                job_id=sid, parent_id=jid, priority=5)
            shop.log_admin_action(app.db, 'channel_emoji_test_queued', jid)
        finally:
            app.db.operational_control.update_one({'_id': 'channel_emoji_test', 'enqueue_owner': token},
                {'$unset': {'enqueue_owner': '', 'enqueue_until': ''}})
        flash('Forwarding test queued. It posts one test message to the private group and forwards it to the channel. Refresh this section for results.', 'success')
        return redirect(back)
