"""Group announcement preferences, including migration from the former channel relay."""
DEFAULTS = {'group_notifications_enabled': '0', 'group_notifications_chat_id': '',
            'group_notifications_language': 'en', 'group_notifications_stock': '1',
            'group_notifications_broadcast': '1', 'group_notifications_polls': '1',
            'group_notifications_products': '1', 'group_notifications_prices': '1'}


def settings(raw):
    raw = raw or {}
    result = {}
    for key, default in DEFAULTS.items():
        if key in raw:
            value = raw[key]
        elif key == 'group_notifications_chat_id':
            # Carry over the group, never the old destination channel.
            value = raw.get('channel_forward_source_chat_id') or raw.get('channel_emoji_test_source_chat_id') or default
        else:
            value = raw.get(key.replace('group_notifications_', 'channel_notifications_'), default)
        result[key] = str(value if value is not None else default).strip()
    return result


def enabled(raw, payload):
    current = settings(raw)
    kind = (payload.get('notification') or {}).get('notification_type', '')
    category = {'stock': 'stock', 'restock': 'stock', 'new_stock': 'stock',
                'broadcast': 'broadcast', 'poll': 'polls', 'poll_result': 'polls',
                'new_product': 'products', 'product': 'products', 'price_drop': 'prices', 'price_change': 'prices', 'sale': 'prices'}.get(kind)
    if not category and payload.get('deep_link_poll_id'):
        category = 'polls'
    if not category and payload.get('cleanup_previous_stock'):
        category = 'stock'
    return current['group_notifications_enabled'] == '1' and (
        not category or current['group_notifications_' + category] == '1')
