"""Optional product logo markers resolved only at the Telegram request boundary."""
import copy
import re

MARKER = re.compile(r'\ue000([1-9][0-9]{0,19})\ue001 ')
TOKEN = re.compile(r'\ue000([1-9][0-9]{0,19})\ue001 |\ue002([^\ue003]*)\ue003')
KEY = 'premium_emoji_id'


def identifier(product):
    value = str((product or {}).get(KEY) or '').strip()
    return value if re.fullmatch(r'[1-9][0-9]{0,19}', value) else ''


def prefix(product):
    value = identifier(product)
    return '\ue000' + value + '\ue001 ' if value else ''


def label(product, name=None):
    return prefix(product) + str(name if name is not None else (product or {}).get('name') or '')


def normal_prefix(value):
    """Show the old product/status icon only when premium mode is disabled."""
    return '\ue002' + str(value) + '\ue003'


def product_heading(text):
    """Format the optional title icon and preserve a line before the quote."""
    # An invisible word joiner keeps this spacer line nonempty through quote
    # formatting. It stays outside the quote; its contents remain unchanged.
    text = re.sub(r'\n{2,}(?=<blockquote>)', lambda _: '\n\u2060\n', text)
    beginning = '📦 '
    return normal_prefix(beginning) + text[len(beginning):] if text.startswith(beginning) else text


def units(value):
    return len(value.encode('utf-16-le')) // 2


def render_text(payload, key, entity_key, enabled, markdown_converter):
    text = payload.get(key)
    if not isinstance(text, str) or not TOKEN.search(text):
        return False
    mode = str(payload.get('parse_mode') or '').lower()
    if mode == 'markdown' and not payload.get(entity_key) and enabled:
        converted = markdown_converter(text)
        if converted is not None:
            text = converted
            payload['parse_mode'] = 'HTML'
            mode = 'html'
        else:
            enabled = False
    if mode == 'html' and not payload.get(entity_key):
        payload[key] = TOKEN.sub(lambda hit: ('<tg-emoji emoji-id="' + hit[1] + '">🏷️</tg-emoji> ' if enabled else '') if hit[1] else ('' if enabled else hit[2]), text)
        return enabled
    # Explicit entities use UTF-16 offsets. Adjust every existing entity when
    # markers expand to a real emoji or disappear in normal mode.
    parts = []; replacements = []; emojis = []; cursor = 0; offset = 0
    for hit in TOKEN.finditer(text):
        piece = text[cursor:hit.start()]
        parts.append(piece); offset += units(piece)
        replacement = ('🏷️ ' if enabled else '') if hit[1] else ('' if enabled else hit[2])
        if enabled and hit[1]:
            emojis.append({'type':'custom_emoji', 'offset':offset, 'length':units('🏷️'), 'custom_emoji_id':hit[1]})
        replacements.append((units(text[:hit.start()]), units(text[:hit.end()]), units(replacement)))
        parts.append(replacement); offset += units(replacement); cursor = hit.end()
    parts.append(text[cursor:])
    def position(value):
        delta = 0
        for start, end, length in replacements:
            if value >= end: delta += length - (end-start)
            elif value > start: return start + delta + min(value-start, length)
            else: break
        return value + delta
    adjusted = []
    for raw in payload.get(entity_key) or []:
        entity = raw.to_dict() if hasattr(raw, 'to_dict') else dict(raw)
        start, end = position(entity['offset']), position(entity['offset'] + entity['length'])
        if end > start:
            entity.update(offset=start, length=end-start); adjusted.append(entity)
    payload[key] = ''.join(parts)
    if adjusted or emojis:
        payload[entity_key] = sorted(adjusted+emojis,key=lambda e:(e['offset'],-e['length']))
        payload.pop('parse_mode',None)
    elif entity_key in payload:
        payload[entity_key] = []
    return enabled


def render_payload(data, enabled, markdown_converter):
    # Preserve the original object unless a product logo is present.
    markup = data.get('reply_markup')
    if hasattr(markup, 'to_dict'): markup = markup.to_dict()
    import json
    if isinstance(markup,str):
        try: markup=json.loads(markup)
        except ValueError: markup=None
    has_marker = any(isinstance(data.get(key),str) and TOKEN.search(data[key]) for key in ('text','caption'))
    has_button = isinstance(markup,dict) and any(TOKEN.search(str(b.get('text') or '')) for row in markup.get('inline_keyboard',[]) for b in row)
    if not has_marker and not has_button:
        return data, False
    payload=dict(data); changed=False
    changed |= render_text(payload,'text','entities',enabled,markdown_converter)
    changed |= render_text(payload,'caption','caption_entities',enabled,markdown_converter)
    if has_button:
        markup=copy.deepcopy(markup)
        for row in markup['inline_keyboard']:
            for button in row:
                text = str(button.get('text') or '')
                if TOKEN.search(text):
                    hit = MARKER.search(text)
                    button['text'] = TOKEN.sub(lambda token: '' if token[1] or enabled else token[2], text)
                    if enabled:
                        changed = True
                        if hit:
                            button['icon_custom_emoji_id'] = hit[1]
        payload['reply_markup']=markup
    return payload, changed
