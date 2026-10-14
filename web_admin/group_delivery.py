"""Validate public/private announcement groups without forwarding to a channel."""
import hashlib
from datetime import datetime, timezone


def validate_group(db, shop, chat_id):
    reference = str(chat_id)
    fingerprint = hashlib.sha256(str(shop.get_bot_token()).encode()).hexdigest()
    current = datetime.now(timezone.utc)
    saved = db.runtime_config.find_one({'key': 'group_announcements_target', 'reference': reference,
                                       'token_fingerprint': fingerprint}) or {}
    checked = saved.get('updated_at')
    if checked and 0 <= (current - checked.replace(tzinfo=timezone.utc)).total_seconds() < 300:
        return {'ok': True}
    status = shop._telegram_http_result('getChat', {'chat_id': chat_id})
    if not status.get('ok'):
        status = dict(status)
        status['error'] = f"Announcement group {reference}: {status.get('error', 'Could not access this group.')}"
        if 'chat not found' in status['error'].lower():
            status['error'] += ' Check the group ID or username and add the bot configured in this WebAdmin.'
        return status
    if (status.get('result') or {}).get('type') not in {'group', 'supergroup'}:
        return {'ok': False, 'status_code': 400, 'error': 'The announcement destination must be a public or private group, not a channel.'}
    # Content protection is allowed: this feature sends directly to the group.
    db.runtime_config.update_one({'key': 'group_announcements_target'}, {'$set': {
        'reference': reference, 'token_fingerprint': fingerprint, 'updated_at': current}}, upsert=True)
    return {'ok': True}
