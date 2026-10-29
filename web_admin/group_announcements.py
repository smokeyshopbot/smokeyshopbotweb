"""Public notices share the persistent delivery queue; private financial data does not."""
import hashlib
import re
from datetime import datetime, timezone

from web_admin.delivery_queue import enqueue_job, STOCK_NOTICE_PRIORITY, is_stock_notice

from utils.group_notifications import DEFAULTS as GROUP_DEFAULTS

DEFAULTS = {**GROUP_DEFAULTS, 'required_channel_cache_seconds': '30'}
BOOL_KEYS = {key for key in GROUP_DEFAULTS if key not in {'group_notifications_chat_id', 'group_notifications_language'}}


def validate(settings):
    result = {key: str(settings.get(key, value) or '').strip() for key, value in DEFAULTS.items()}
    try:
        ttl = int(result['required_channel_cache_seconds'])
    except ValueError:
        raise ValueError('Membership cache seconds must be a whole number between 0 and 60.')
    if not 0 <= ttl <= 60:
        raise ValueError('Membership cache seconds must be between 0 and 60.')
    result['required_channel_cache_seconds'] = str(ttl)
    group = result['group_notifications_chat_id']
    if group and not re.fullmatch(r'(?:@[A-Za-z][A-Za-z0-9_]{4,31}|-[1-9][0-9]{0,19})', group):
        raise ValueError('Group announcements require a numeric group ID or a public @group username.')
    if result['group_notifications_enabled'] == '1' and not group:
        raise ValueError('Enter an announcement group before enabling group notifications.')
    if result['group_notifications_language'] not in {'en', 'es'}:
        raise ValueError('Choose English or Spanish for group announcements.')
    return result


def target(db, shop, category):
    settings = shop.get_secret_settings(db)
    if settings.get('group_notifications_enabled') != '1' or settings.get('group_notifications_' + category) != '1':
        return None
    group = str(settings.get('group_notifications_chat_id') or '').strip()
    if not re.fullmatch(r'(?:@[A-Za-z][A-Za-z0-9_]{4,31}|-[1-9][0-9]{0,19})', group):
        return None
    return {'chat_id': int(group) if group.startswith('-') else group,
            'language': settings.get('group_notifications_language', 'en'), 'group_recipient': True,
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
    notification['notification_target_kind'] = 'group'
    recipient.update(text=text, parse_mode=payload.get('parse_mode'), product_name=payload.get('product_name'),
                     cleanup_previous_stock=payload.get('cleanup_previous_stock', False), notification=notification)
    if payload.get('sale_id'):
        recipient.update(sale_id=payload['sale_id'], sale_expires_at=payload.get('sale_expires_at'))
    if payload.get('product_name'):
        product = db.products.find_one({'name': shop.name_regex(payload['product_name'])}, {'_id': 1})
        if product:
            recipient['deep_link_product_id'] = str(product['_id'])
    batch = notification.get('notification_batch_id') or hashlib.sha256(text.encode()).hexdigest()
    return enqueue_job(db, 'text', recipient, job_id='group:' + batch,
                       priority=STOCK_NOTICE_PRIORITY if is_stock_notice(recipient) else 20)


def enqueue_poll(db, shop, campaign):
    recipient = target(db, shop, 'polls')
    if not recipient:
        return
    from utils.survey import survey_text
    recipient.update(text=survey_text(campaign, recipient['language']),
                     deep_link_poll_id=campaign['campaign_id'],
                     notification={'notification_type': 'poll', 'notification_target_kind': 'group',
                                   'notification_title': campaign['question'], 'notification_source': 'web_admin_poll'})
    return enqueue_job(db, 'text', recipient, job_id='group-poll:' + campaign['campaign_id'], priority=15)


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
