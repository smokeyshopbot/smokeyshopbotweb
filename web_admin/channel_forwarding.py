"""Send public channel posts through a private group with durable phase acknowledgements."""
import hashlib
import re
from datetime import datetime, timezone


def is_channel_post(job):
    return job['kind'] == 'text' and bool(job['payload'].get('channel_recipient'))


def validate_source(db, shop, payload):
    # Before the source is acknowledged, a corrected group setting can repair
    # a failed delivery. Afterwards the saved source ID is immutable.
    value = str(shop.get_secret_settings(db).get('channel_forward_source_chat_id') or
                payload.get('channel_forward_source_chat_id') or '').strip()
    if not re.fullmatch(r'-[1-9][0-9]{0,19}', value):
        return {'ok': False, 'status_code': 400,
                'error': 'Save a private forwarding group ID in Secret Settings → Notifications.'}
    if value == str(payload['chat_id']):
        return {'ok': False, 'status_code': 400, 'error': 'The forwarding group and destination channel must be different chats.'}
    payload['channel_forward_source_chat_id'] = int(value)
    fingerprint = hashlib.sha256(str(shop.get_bot_token()).encode()).hexdigest()
    cache = db.runtime_config.find_one({'key': 'channel_forwarding_group', 'chat_id': int(value),
                                       'token_fingerprint': fingerprint}) or {}
    checked = cache.get('updated_at')
    current = datetime.now(timezone.utc)
    if checked and 0 <= (current - checked.replace(tzinfo=timezone.utc)).total_seconds() < 300:
        return {'ok': True}
    status = shop._telegram_http_result('getChat', {'chat_id': int(value)})
    if not status.get('ok'):
        status = dict(status)
        status['error'] = f"Forwarding group {value}: {status.get('error', 'Could not access this group.')}"
        if 'chat not found' in status['error'].lower():
            status['error'] += ' Check the real group ID and add the bot whose token is saved in this WebAdmin.'
        return status
    chat = status.get('result') or {}
    if chat.get('type') not in {'group', 'supergroup'} or chat.get('username'):
        return {'ok': False, 'status_code': 400, 'error': 'The forwarding source must be a private group or supergroup.'}
    if chat.get('has_protected_content'):
        return {'ok': False, 'status_code': 400, 'error': 'Turn off content protection in the private forwarding group.'}
    db.runtime_config.update_one({'key': 'channel_forwarding_group'}, {'$set': {
        'chat_id': int(value), 'token_fingerprint': fingerprint, 'updated_at': current}}, upsert=True)
    return {'ok': True}


def send_source(shop, payload):
    data = {'chat_id': payload['channel_forward_source_chat_id'], 'text': payload['text'],
            'disable_notification': True}
    if payload.get('parse_mode'):
        data['parse_mode'] = payload['parse_mode']
    if payload.get('reply_markup'):
        data['reply_markup'] = payload['reply_markup']
    status = shop._telegram_http_result('sendMessage', data)
    if not status.get('ok'):
        status = dict(status)
        status['error'] = f"Forwarding group {data['chat_id']}: {status.get('error', 'Message send failed.')}"
    return status


def validate_channel(db, shop, payload):
    reference = str(payload['chat_id'])
    fingerprint = hashlib.sha256(str(shop.get_bot_token()).encode()).hexdigest()
    cache = db.runtime_config.find_one({'key': 'channel_forwarding_channel', 'reference': reference,
                                       'token_fingerprint': fingerprint}) or {}
    checked = cache.get('updated_at')
    current = datetime.now(timezone.utc)
    if checked and 0 <= (current - checked.replace(tzinfo=timezone.utc)).total_seconds() < 300:
        return {'ok': True}
    status = shop._telegram_http_result('getChat', {'chat_id': payload['chat_id']})
    if not status.get('ok'):
        status = dict(status)
        status['error'] = f"Destination channel {reference}: {status.get('error', 'Could not access this channel.')}"
        return status
    if (status.get('result') or {}).get('type') != 'channel':
        return {'ok': False, 'status_code': 400, 'error': 'The announcement destination must be a Telegram channel.'}
    db.runtime_config.update_one({'key': 'channel_forwarding_channel'}, {'$set': {
        'reference': reference, 'token_fingerprint': fingerprint, 'updated_at': current}}, upsert=True)
    return {'ok': True}


def forward_source(shop, job, payload):
    source = job.get('channel_source_result') or {}
    if not source.get('message_id'):
        return {'ok': False, 'error_type': 'MissingAcknowledgement',
                'error': 'The forwarding source has no acknowledged message ID; review this delivery.'}
    status = shop._telegram_http_result('forwardMessage', {
        'chat_id': payload['chat_id'],
        'from_chat_id': (source.get('chat') or {}).get('id') or payload['channel_forward_source_chat_id'],
        'message_id': source['message_id'], 'disable_notification': bool(payload.get('disable_notification', False))})
    if not status.get('ok'):
        status = dict(status)
        status['error'] = f"Destination channel {payload['chat_id']}: {status.get('error', 'Forwarding failed.')}"
    return status
