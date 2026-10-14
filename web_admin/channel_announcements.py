"""Public notices share the persistent delivery queue; private financial data does not."""
import hashlib
import re
from datetime import datetime, timezone

from web_admin.delivery_queue import enqueue_job

DEFAULTS = {'channel_notifications_enabled': '0', 'channel_notifications_chat_id': '',
            'channel_notifications_language': 'en', 'channel_notifications_stock': '1',
            'channel_notifications_broadcast': '1', 'channel_notifications_polls': '1',
            'channel_notifications_products': '1', 'channel_notifications_prices': '1',
            'required_channel_cache_seconds': '30', 'channel_forward_source_chat_id': ''}
BOOL_KEYS = {key for key in DEFAULTS if key.startswith('channel_notifications_')
             and key not in {'channel_notifications_chat_id', 'channel_notifications_language'}}


def validate(settings):
    result = {key: str(settings.get(key, value) or '').strip() for key, value in DEFAULTS.items()}
    source = result['channel_forward_source_chat_id']
    if source and not re.fullmatch(r'-[1-9][0-9]{0,19}', source):
        raise ValueError('The channel forwarding source must be a numeric private group ID, such as -1001234567890.')
    try:
        ttl = int(result['required_channel_cache_seconds'])
    except ValueError:
        raise ValueError('Membership cache seconds must be a whole number between 0 and 60.')
    if not 0 <= ttl <= 60:
        raise ValueError('Membership cache seconds must be between 0 and 60.')
    result['required_channel_cache_seconds'] = str(ttl)
    target = result['channel_notifications_chat_id'] or str(settings.get('required_channel_id') or '').strip()
    if result['channel_notifications_enabled'] == '1' and not re.fullmatch(r'(?:@[A-Za-z][A-Za-z0-9_]{4,31}|-100[0-9]+)', target):
        raise ValueError('Channel announcements require an @channel username or numeric -100… channel ID.')
    if result['channel_notifications_enabled'] == '1' and not source:
        raise ValueError('Channel announcements require a private forwarding group ID.')
    if result['channel_notifications_language'] not in {'en', 'es'}:
        raise ValueError('Choose English or Spanish for channel announcements.')
    return result


def target(db, shop, category):
    settings = shop.get_secret_settings(db)
    if settings.get('channel_notifications_enabled') != '1' or settings.get('channel_notifications_' + category) != '1':
        return None
    channel = str(settings.get('channel_notifications_chat_id') or settings.get('required_channel_id') or '').strip()
    if not re.fullmatch(r'(?:@[A-Za-z][A-Za-z0-9_]{4,31}|-100[0-9]+)', channel):
        return None
    return {'chat_id': int(channel) if channel.startswith('-100') else channel,
            'language': settings.get('channel_notifications_language', 'en'), 'channel_recipient': True,
            'channel_forward_source_chat_id': settings.get('channel_forward_source_chat_id', ''),
            'maintenance_policy': 'pause'}


def enqueue_notice(db, shop, payload, category):
    recipient = target(db, shop, category)
    if not recipient:
        return
    language = recipient['language']
    text = payload['texts'].get(language) or payload['texts'].get('en')
    if not text:
        return
    notification = dict(payload.get('notification') or {})
    notification['notification_target_kind'] = 'channel'
    recipient.update(text=text, parse_mode=payload.get('parse_mode'), product_name=payload.get('product_name'),
                     cleanup_previous_stock=payload.get('cleanup_previous_stock', False), notification=notification)
    if payload.get('product_name'):
        product = db.products.find_one({'name': shop.name_regex(payload['product_name'])}, {'_id': 1})
        if product:
            recipient['deep_link_product_id'] = str(product['_id'])
    batch = notification.get('notification_batch_id') or hashlib.sha256(text.encode()).hexdigest()
    return enqueue_job(db, 'text', recipient, job_id='channel:' + batch, priority=15)


def enqueue_poll(db, shop, campaign):
    recipient = target(db, shop, 'polls')
    if not recipient:
        return
    if campaign.get('format') == 'private_v2':
        from utils.survey import survey_text
        recipient.update(text=survey_text(campaign, recipient['language']),
                         deep_link_poll_id=campaign['campaign_id'],
                         notification={'notification_type': 'poll', 'notification_target_kind': 'channel',
                                       'notification_title': campaign['question'], 'notification_source': 'web_admin_poll'})
        return enqueue_job(db, 'text', recipient, job_id='channel-poll:' + campaign['campaign_id'], priority=15)
    recipient.update(campaign_id=campaign['campaign_id'], question=campaign['question'],
                     options=campaign['options'], multiple=campaign.get('multiple', False),
                     notification={'notification_type': 'poll', 'notification_target_kind': 'channel',
                                   'notification_title': campaign['question'], 'notification_source': 'web_admin_poll'})
    return enqueue_job(db, 'channel_poll', recipient, job_id='channel-poll:' + campaign['campaign_id'], priority=15)


def bot_username(db, shop):
    fingerprint = hashlib.sha256(str(shop.get_bot_token()).encode()).hexdigest()
    saved = db.runtime_config.find_one({'key': 'telegram_bot_username', 'token_fingerprint': fingerprint}) or {}
    updated = saved.get('updated_at')
    if updated and updated.tzinfo is None:
        updated = updated.replace(tzinfo=timezone.utc)
    if saved.get('value') and updated and 0 <= (datetime.now(timezone.utc) - updated).total_seconds() < 3600:
        return str(saved['value'])
    status = shop._telegram_http_result('getMe', {})
    username = str((status.get('result') or {}).get('username') or '')
    if not status.get('ok') or not re.fullmatch(r'[A-Za-z0-9_]{5,32}', username):
        return None
    db.runtime_config.update_one({'key': 'telegram_bot_username'}, {'$set': {
        'value': username, 'token_fingerprint': fingerprint, 'updated_at': datetime.now(timezone.utc)}}, upsert=True)
    return username
