"""Per-message emoji placeholders, independent of the shop's global mappings."""
import copy
import json
import re
from html.parser import HTMLParser

# Common examples retained for compatibility; fallback entry is unrestricted.
FALLBACKS = ('⭐', '🔥', '🎉', '💎', '📦', '🎁', '✅', '❌', '📊', '💬', '🔔', '🛒')
# Unicode 17.0 Extended_Pictographic ranges; no emoji package is needed at runtime.
_PICTOGRAPH = r'[\u00a9\u00ae\u203c\u2049\u2122\u2139\u2194-\u2199\u21a9-\u21aa\u231a-\u231b\u2328\u23cf\u23e9-\u23f3\u23f8-\u23fa\u24c2\u25aa-\u25ab\u25b6\u25c0\u25fb-\u25fe\u2600-\u2604\u260e\u2611\u2614-\u2615\u2618\u261d\u2620\u2622-\u2623\u2626\u262a\u262e-\u262f\u2638-\u263a\u2640\u2642\u2648-\u2653\u265f-\u2660\u2663\u2665-\u2666\u2668\u267b\u267e-\u267f\u2692-\u2697\u2699\u269b-\u269c\u26a0-\u26a1\u26a7\u26aa-\u26ab\u26b0-\u26b1\u26bd-\u26be\u26c4-\u26c5\u26c8\u26ce-\u26cf\u26d1\u26d3-\u26d4\u26e9-\u26ea\u26f0-\u26f5\u26f7-\u26fa\u26fd\u2702\u2705\u2708-\u270d\u270f\u2712\u2714\u2716\u271d\u2721\u2728\u2733-\u2734\u2744\u2747\u274c\u274e\u2753-\u2755\u2757\u2763-\u2764\u2795-\u2797\u27a1\u27b0\u27bf\u2934-\u2935\u2b05-\u2b07\u2b1b-\u2b1c\u2b50\u2b55\u3030\u303d\u3297\u3299\U0001f004\U0001f02c-\U0001f02f\U0001f094-\U0001f09f\U0001f0af-\U0001f0b0\U0001f0c0\U0001f0cf-\U0001f0d0\U0001f0f6-\U0001f0ff\U0001f170-\U0001f171\U0001f17e-\U0001f17f\U0001f18e\U0001f191-\U0001f19a\U0001f1ae-\U0001f1e5\U0001f201-\U0001f20f\U0001f21a\U0001f22f\U0001f232-\U0001f23a\U0001f23c-\U0001f23f\U0001f249-\U0001f25f\U0001f266-\U0001f321\U0001f324-\U0001f393\U0001f396-\U0001f397\U0001f399-\U0001f39b\U0001f39e-\U0001f3f0\U0001f3f3-\U0001f3f5\U0001f3f7-\U0001f3fa\U0001f400-\U0001f4fd\U0001f4ff-\U0001f53d\U0001f549-\U0001f54e\U0001f550-\U0001f567\U0001f56f-\U0001f570\U0001f573-\U0001f57a\U0001f587\U0001f58a-\U0001f58d\U0001f590\U0001f595-\U0001f596\U0001f5a4-\U0001f5a5\U0001f5a8\U0001f5b1-\U0001f5b2\U0001f5bc\U0001f5c2-\U0001f5c4\U0001f5d1-\U0001f5d3\U0001f5dc-\U0001f5de\U0001f5e1\U0001f5e3\U0001f5e8\U0001f5ef\U0001f5f3\U0001f5fa-\U0001f64f\U0001f680-\U0001f6c5\U0001f6cb-\U0001f6d2\U0001f6d5-\U0001f6e5\U0001f6e9\U0001f6eb-\U0001f6f0\U0001f6f3-\U0001f6ff\U0001f7da-\U0001f7ff\U0001f80c-\U0001f80f\U0001f848-\U0001f84f\U0001f85a-\U0001f85f\U0001f888-\U0001f88f\U0001f8ae-\U0001f8af\U0001f8bc-\U0001f8bf\U0001f8c2-\U0001f8cf\U0001f8d9-\U0001f8ff\U0001f90c-\U0001f93a\U0001f93c-\U0001f945\U0001f947-\U0001f9ff\U0001fa58-\U0001fa5f\U0001fa6e-\U0001faff\U0001fc00-\U0001fffd]'
_ATOM = _PICTOGRAPH + r'[\ufe0e\ufe0f]?[\U0001f3fb-\U0001f3ff]?'
EMOJI_PATTERN = (r'(?:\U0001f3f4[\U000e0020-\U000e007e]{1,16}\U000e007f|'
                 r'[\U0001f1e6-\U0001f1ff]{2}|[#*0-9]\ufe0f?\u20e3|'
                 + _ATOM + r'(?:\u200d' + _ATOM + r')*)')
EMOJI = re.compile(EMOJI_PATTERN)
TOKEN = re.compile(r'\[\[emoji:([1-9][0-9]{0,19})\|(' + EMOJI_PATTERN + r')\]\]', re.I)


def is_emoji(value):
    return isinstance(value, str) and 0 < units(value) <= 64 and bool(EMOJI.fullmatch(value))


def markdown_text(text, converter):
    """Keep emoji keycaps such as *️⃣ out of the Markdown syntax parser."""
    if not TOKEN.search(text):
        return converter(text)
    prefix = '\ue100'
    while prefix in text:
        prefix += '\ue100'
    tokens = []
    def protect(hit):
        tokens.append(hit.group())
        return prefix + str(len(tokens) - 1) + '\ue101'
    converted = converter(TOKEN.sub(protect, text))
    if converted is None:
        return None
    return re.sub(re.escape(prefix) + r'(\d+)\ue101', lambda hit: tokens[int(hit[1])], converted)


def plain_text(value):
    return TOKEN.sub(lambda hit: hit[2], str(value or ''))


def validate(value, *, max_icons=None):
    text = str(value or '')
    if re.search(r'\[\[emoji:', TOKEN.sub('', text), re.I):
        raise ValueError('Invalid premium emoji placeholder. Use Insert premium emoji to add it again.')
    if any(not is_emoji(hit[2]) for hit in TOKEN.finditer(text)):
        raise ValueError('Invalid premium emoji placeholder. Paste exactly one normal fallback emoji.')
    if max_icons is not None and len(TOKEN.findall(text)) > max_icons:
        raise ValueError('Each poll answer supports one premium emoji. Questions and broadcasts can contain several.')
    return text


def units(value):
    return len(value.encode('utf-16-le')) // 2


class _HTMLRenderer(HTMLParser):
    def __init__(self, enabled):
        super().__init__(convert_charrefs=False)
        self.enabled = enabled
        self.parts = []
        self.protected = []
        self.changed = False

    def handle_starttag(self, tag, attrs):
        self.parts.append(self.get_starttag_text())
        if tag in {'code', 'pre', 'a', 'tg-emoji'}:
            self.protected.append(tag)

    def handle_endtag(self, tag):
        self.parts.append(f'</{tag}>')
        if self.protected and self.protected[-1] == tag:
            self.protected.pop()

    def handle_data(self, data):
        if self.protected:
            self.parts.append(data)
            return
        def replace(hit):
            if self.enabled:
                self.changed = True
                return '<tg-emoji emoji-id="' + hit[1] + '">' + hit[2] + '</tg-emoji>'
            return hit[2]
        self.parts.append(TOKEN.sub(replace, data))

    def handle_entityref(self, name): self.parts.append('&' + name + ';')
    def handle_charref(self, name): self.parts.append('&#' + name + ';')
    def handle_comment(self, data): self.parts.append('<!--' + data + '-->')


def _text(payload, key, entity_key, enabled, markdown_converter, parse_key='parse_mode'):
    text = payload.get(key)
    if not isinstance(text, str) or not TOKEN.search(text):
        return False
    mode = str(payload.get(parse_key) or '').lower()
    original = [entity.to_dict() if hasattr(entity, 'to_dict') else dict(entity) for entity in (payload.get(entity_key) or [])]
    if mode == 'markdown' and not original:
        # A normal * keycap must stay an emoji even when premium mode is off.
        converted = markdown_converter(text)
        if converted is None:
            payload[key] = plain_text(text)
            return False
        text = converted
        payload[parse_key] = 'HTML'
        mode = 'html'
    if mode == 'html' and not original:
        renderer = _HTMLRenderer(enabled)
        renderer.feed(text)
        renderer.close()
        payload[key] = ''.join(renderer.parts)
        return renderer.changed
    # Existing formatting and all custom-emoji offsets use UTF-16 code units.
    protected = [(e['offset'], e['offset'] + e['length']) for e in original
                 if e.get('type') in {'code', 'pre', 'custom_emoji', 'text_link', 'url'}]
    for link in re.finditer(r'https?://\S+', text):
        protected.append((units(text[:link.start()]), units(text[:link.end()])))
    parts, replacements, emojis = [], [], []
    cursor = offset = 0
    for hit in TOKEN.finditer(text):
        start, end = units(text[:hit.start()]), units(text[:hit.end()])
        if any(start < b and end > a for a, b in protected):
            continue
        piece = text[cursor:hit.start()]
        parts.append(piece)
        offset += units(piece)
        fallback = hit[2]
        if enabled:
            emojis.append({'type': 'custom_emoji', 'offset': offset, 'length': units(fallback), 'custom_emoji_id': hit[1]})
        replacements.append((start, end, units(fallback)))
        parts.append(fallback)
        offset += units(fallback)
        cursor = hit.end()
    parts.append(text[cursor:])
    def position(value):
        delta = 0
        for start, end, length in replacements:
            if value >= end:
                delta += length - (end - start)
            elif value > start:
                return start + delta + min(value - start, length)
            else:
                break
        return value + delta
    adjusted = []
    for entity in original:
        start, end = position(entity['offset']), position(entity['offset'] + entity['length'])
        if end > start:
            entity.update(offset=start, length=end - start)
            adjusted.append(entity)
    payload[key] = ''.join(parts)
    if adjusted or emojis:
        payload[entity_key] = sorted(adjusted + emojis, key=lambda e: (e['offset'], -e['length']))
        payload.pop(parse_key, None)
    elif entity_key in payload:
        payload[entity_key] = []
    return bool(emojis)


def render_payload(method, data, enabled, markdown_converter):
    # The original payload remains reusable for queue retries and live toggles.
    def contains(value):
        if isinstance(value, str):
            return bool(TOKEN.search(value))
        if hasattr(value, 'to_dict'):
            value = value.to_dict()
        if isinstance(value, dict):
            return any(contains(item) for item in value.values())
        if isinstance(value, (list, tuple)):
            return any(contains(item) for item in value)
        return False
    if not contains(data):
        return data, False
    payload = dict(data)
    changed = False
    changed |= _text(payload, 'text', 'entities', enabled, markdown_converter)
    changed |= _text(payload, 'caption', 'caption_entities', enabled, markdown_converter)
    markup = payload.get('reply_markup')
    if hasattr(markup, 'to_dict'):
        markup = markup.to_dict()
    if isinstance(markup, str):
        try: markup = json.loads(markup)
        except ValueError: markup = None
    if isinstance(markup, dict) and any(TOKEN.search(str(button.get('text') or ''))
            for row in markup.get('inline_keyboard', []) for button in row):
        markup = copy.deepcopy(markup)
        for row in markup['inline_keyboard']:
            for button in row:
                text = str(button.get('text') or '')
                first = TOKEN.search(text)
                if not first:
                    continue
                if enabled and not button.get('icon_custom_emoji_id'):
                    button['icon_custom_emoji_id'] = first[1]
                    text = text[:first.start()] + text[first.end():]
                    changed = True
                button['text'] = re.sub(r' {2,}', ' ', plain_text(text)).strip() or first[2]
        payload['reply_markup'] = markup
    if method == 'sendPoll':
        changed |= _text(payload, 'question', 'question_entities', enabled, markdown_converter, 'question_parse_mode')
        options = []
        for raw in payload.get('options', []):
            option = raw.to_dict() if hasattr(raw, 'to_dict') else dict(raw) if isinstance(raw, dict) else {'text': raw}
            changed |= _text(option, 'text', 'text_entities', enabled, markdown_converter, 'text_parse_mode')
            options.append(option)
        payload['options'] = options
    if method == 'sendMediaGroup':
        media = []
        for raw in payload.get('media', []):
            item = raw.to_dict() if hasattr(raw, 'to_dict') else dict(raw)
            changed |= _text(item, 'caption', 'caption_entities', enabled, markdown_converter)
            media.append(item)
        payload['media'] = media
    return payload, changed
