"""Custom emoji rendering with original Unicode fallbacks and unchanged actions."""
import copy
import html
import json
import re
from html.parser import HTMLParser

# IDs are strings: JavaScript numbers cannot safely represent Telegram IDs.
EMOJI_CHOICES = '🛍️ 🛒 📦 🆕 🔻 ⭐ ✏️ 🗑️ 🔎 😔 👋 🌐 🇬🇧 🇪🇸 📞 📢 🔄 ⬅️ ➡️ 🔙 ↩️ 👛 ➕ 💰 💵 💳 🇮🇳 🟡 🟣 🏦 🆔 👤 🔢 🧾 🔖 🔍 ✍️ 📸 ⏰ 🕒 ⏳ 1️⃣ 2️⃣ 3️⃣ ✅ ❌ ⚠️ ⛔ 🚫 ❓ 🚨 🎁 🔁 🛡️ 📄 📝 🔗 📋 💬 💸 👥 🏆 📊 📚 📨 🔔 🛠️'.split()
DEFAULT_MAPPING = {
    '🛍️': '5312361253610475399', '🛒': '5312361253610475399', '📦': '5303213613220121573',
    '🆕': '5382357040008021292', '🔻': '5447183459602669338', '⭐': '5278362086336908780',
    '✏️': '5395444784611480792', '🗑️': '5278597734012570966', '🔎': '5231012545799666522',
    '😔': '5280803324273115630', '👋': '5440431182602842059', '🌐': '5447410659077661506',
    '📞': '6301027265899661025', '📢': '5215668805199473901', '🔄': '5244758760429213978',
    '👛': '5472363448404809929', '➕': '5397916757333654639', '💰': '5197434882321567830',
    '💵': '5287231198098117669', '💳': '5807750375033278838', '🇮🇳': '5807750375033278838',
    '🏦': '5807750375033278838', '🆔': '5217811903685865303', '👤': '5278361622480438858',
    '🧾': '5444856076954520455', '🔖': '5841276284155467413', '🔍': '5231012545799666522',
    '✍️': '5217544005100794307', '📸': '5787632606484893320', '⏰': '5787488119490088755',
    '🕒': '5787488119490088755', '⏳': '5402117686320724687', '1️⃣': '6237686767550468103',
    '2️⃣': '6237961142946238268', '3️⃣': '6237808332304813709', '✅': '5206607081334906820',
    '❌': '5210952531676504517', '⚠️': '5447644880824181073', '⛔': '5260293700088511294',
    '🚫': '5240241223632954241', '❓': '5436113877181941026', '🚨': '5395695537687123235',
    '🎁': '5199749070830197566', '🔁': '5219934485113493317', '🛡️': '5251203410396458957',
    '📄': '5444856076954520455', '📝': '5837003105228558796', '🔗': '5271604874419647061',
    '📋': '5837003105228558796', '💬': '5472239203590888751', '💸': '5197434882321567830',
    '👥': '5278361622480438858', '🏆': '5188344996356448758', '📊': '5231200819986047254',
    '📚': '5231200819986047254', '📨': '5350421256627838238', '🔔': '5458603043203327669',
    '🛠️': '5415782537089793597',
}
DEFAULTS = {'premium_emojis_enabled': '0', 'premium_emoji_ids': json.dumps(DEFAULT_MAPPING, ensure_ascii=False)}
OUTPUT_METHODS = {'sendMessage', 'editMessageText', 'sendPhoto', 'sendVideo', 'sendAnimation', 'sendDocument',
                  'sendAudio', 'sendVoice', 'editMessageCaption', 'editMessageReplyMarkup', 'sendPoll', 'sendMediaGroup'}

def mapping(settings):
    raw = settings.get('premium_emoji_ids', DEFAULTS['premium_emoji_ids'])
    try:
        value = json.loads(raw) if isinstance(raw, str) else raw
        if not isinstance(value, dict):
            return {}
        return {emoji: str(value[emoji]) for emoji in EMOJI_CHOICES
                if emoji in value and re.fullmatch(r'[1-9][0-9]{0,19}', str(value[emoji]))}
    except (ValueError, TypeError):
        return {}

def form_settings(form, existing):
    saved = mapping(existing)
    for i, emoji in enumerate(EMOJI_CHOICES):
        key = f'premium_emoji_{i}'
        if key not in form:
            continue
        value = str(form.get(key) or '').strip()
        if value and not re.fullmatch(r'[1-9][0-9]{0,19}', value):
            raise ValueError(f'Enter a numeric Telegram emoji ID for {emoji}, or leave blank for its normal emoji.')
        if value:
            saved[emoji] = value
        else:
            saved.pop(emoji, None)
    return {'premium_emojis_enabled': '1' if form.get('premium_emojis_enabled') == '1' else '0',
            'premium_emoji_ids': json.dumps(saved, ensure_ascii=False)}

def _pattern(ids):
    variants = {}
    for emoji, identifier in ids.items():
        variants[emoji] = identifier
        variants[emoji.replace('\ufe0f', '')] = identifier
    return re.compile('|'.join(re.escape(s) for s in sorted(variants, key=len, reverse=True))), variants

def _units(text):
    return len(text.encode('utf-16-le')) // 2

def _entities(text, entities, ids):
    original = [e.to_dict() if hasattr(e, 'to_dict') else dict(e) for e in (entities or [])]
    pattern, variants = _pattern(ids)
    protected = [(e['offset'], e['offset'] + e['length']) for e in original
                 if e.get('type') in {'code', 'pre', 'custom_emoji', 'text_link', 'url'}]
    # Do not alter credentials, literal code, existing custom emoji or URLs.
    for link in re.finditer(r'https?://\S+', text):
        protected.append((_units(text[:link.start()]), _units(text[:link.end()])))
    added = []
    for hit in pattern.finditer(text):
        start = _units(text[:hit.start()]); end = _units(text[:hit.end()])
        if any(start < b and end > a for a, b in protected):
            continue
        added.append({'type': 'custom_emoji', 'offset': start, 'length': end - start,
                      'custom_emoji_id': variants[hit.group()]})
    return sorted(original + added, key=lambda e: (e['offset'], -e['length'])), bool(added)

def _legacy_markdown(text):
    """Convert the legacy syntax actually used by this bot to Telegram HTML."""
    out = []; i = 0
    while i < len(text):
        if text[i] == '\\' and i + 1 < len(text) and text[i + 1] in '_*`[':
            out.append(html.escape(text[i + 1])); i += 2; continue
        delimiter = '```' if text.startswith('```', i) else text[i] if text[i] in '*_`' else None
        if delimiter:
            end = i + len(delimiter)
            while True:
                end = text.find(delimiter, end)
                if end < 0:
                    return None
                if end == 0 or text[end - 1] != '\\':
                    break
                end += len(delimiter)
            content = text[i + len(delimiter):end]
            if delimiter == '```':
                language = ''
                if '\n' in content:
                    first, rest = content.split('\n', 1)
                    if re.fullmatch(r'[a-zA-Z0-9_+-]*', first):
                        language = first; content = rest
                out.append('<pre><code' + (f' class="language-{language}"' if language else '') + '>' + html.escape(content) + '</code></pre>')
            else:
                tag = {'*': 'b', '_': 'i', '`': 'code'}[delimiter]
                if delimiter != '`':
                    content = re.sub(r'\\([_*`\[])', r'\1', content)
                out.append(f'<{tag}>' + html.escape(content) + f'</{tag}>')
            i = end + len(delimiter); continue
        if text[i] == '[':
            link = re.match(r'\[([^\]]+)\]\(([^\s)]+)\)', text[i:])
            if link:
                out.append('<a href="' + html.escape(link[2], quote=True) + '">' + html.escape(link[1]) + '</a>')
                i += len(link[0]); continue
        out.append(html.escape(text[i])); i += 1
    return ''.join(out)

class _HTMLRenderer(HTMLParser):
    def __init__(self, ids):
        super().__init__(convert_charrefs=False)
        self.pattern, self.variants = _pattern(ids)
        self.parts = []; self.protected = []; self.changed = False
    def handle_starttag(self, tag, attrs):
        self.parts.append(self.get_starttag_text())
        if tag in {'code', 'pre', 'tg-emoji', 'a'}:
            self.protected.append(tag)
    def handle_endtag(self, tag):
        self.parts.append(f'</{tag}>')
        if self.protected and self.protected[-1] == tag:
            self.protected.pop()
    def handle_data(self, data):
        if self.protected:
            self.parts.append(data); return
        pieces = re.split(r'(https?://\S+)', data)
        for piece in pieces:
            if piece.startswith(('https://', 'http://')):
                self.parts.append(piece); continue
            def replace(hit):
                self.changed = True
                return '<tg-emoji emoji-id="' + self.variants[hit.group()] + '">' + hit.group() + '</tg-emoji>'
            self.parts.append(self.pattern.sub(replace, piece))
    def handle_entityref(self, name): self.parts.append('&' + name + ';')
    def handle_charref(self, name): self.parts.append('&#' + name + ';')
    def handle_comment(self, data): self.parts.append('<!--' + data + '-->')

def _text(payload, key, entity_key, ids, parse_key='parse_mode'):
    text = payload.get(key)
    if not isinstance(text, str) or not text:
        return False
    mode = str(payload.get(parse_key) or '').lower()
    if payload.get(entity_key) or not mode:
        entities, changed = _entities(text, payload.get(entity_key), ids)
        if changed:
            payload[entity_key] = entities; payload.pop(parse_key, None)
        return changed
    if mode not in {'markdown', 'html'}:
        return False
    converted = _legacy_markdown(text) if mode == 'markdown' else text
    if converted is None:
        return False
    renderer = _HTMLRenderer(ids)
    renderer.feed(converted); renderer.close()
    if renderer.changed:
        payload[key] = ''.join(renderer.parts); payload[parse_key] = 'HTML'
    return renderer.changed

def _keyboard(markup, ids):
    if hasattr(markup, 'to_dict'):
        markup = markup.to_dict()
    if isinstance(markup, str):
        try: markup = json.loads(markup)
        except ValueError: return markup, False
    if not isinstance(markup, dict) or not markup.get('inline_keyboard'):
        return markup, False  # Reply-keyboard text is also user input; preserve it.
    result = copy.deepcopy(markup); changed = False
    pattern, variants = _pattern(ids)
    for row in result['inline_keyboard']:
        for button in row:
            if button.get('icon_custom_emoji_id'):
                continue
            text = str(button.get('text') or '')
            hit = pattern.search(text)
            if hit:
                before, after = text[:hit.start()].rstrip(), text[hit.end():].lstrip()
                label = ' '.join(part for part in (before, after) if part)
                if label:
                    button.update(text=label, icon_custom_emoji_id=variants[hit.group()]); changed = True
    return result, changed

def _transform_standard_payload(method, data, settings):
    """Return a shallow copy only when enabled; never mutate stored queue payloads."""
    if method not in OUTPUT_METHODS or str(settings.get('premium_emojis_enabled', '0')) not in {'1', 'True'}:
        return data, False
    ids = mapping(settings)
    if not ids:
        return data, False
    payload = dict(data); changed = False
    changed |= _text(payload, 'text', 'entities', ids)
    changed |= _text(payload, 'caption', 'caption_entities', ids)
    if 'reply_markup' in payload:
        markup, updated = _keyboard(payload['reply_markup'], ids)
        if updated: payload['reply_markup'] = markup
        changed |= updated
    if method == 'sendPoll':
        changed |= _text(payload, 'question', 'question_entities', ids, 'question_parse_mode')
        options = []
        for raw in payload.get('options', []):
            option = raw.to_dict() if hasattr(raw, 'to_dict') else dict(raw) if isinstance(raw, dict) else {'text': raw}
            changed |= _text(option, 'text', 'text_entities', ids, 'text_parse_mode')
            options.append(option)
        payload['options'] = options
    if method == 'sendMediaGroup':
        media = []
        for raw in payload.get('media', []):
            item = raw.to_dict() if hasattr(raw, 'to_dict') else dict(raw)
            changed |= _text(item, 'caption', 'caption_entities', ids)
            media.append(item)
        payload['media'] = media
    return (payload, True) if changed else (data, False)

def transform_payload(method, data, settings):
    if method not in OUTPUT_METHODS:
        return data, False
    from utils.product_emojis import render_payload
    enabled = str(settings.get('premium_emojis_enabled', '0')) in {'1', 'True'}
    prepared, product_changed = render_payload(data, enabled, _legacy_markdown)
    rendered, standard_changed = _transform_standard_payload(method, prepared, settings)
    return rendered, product_changed or standard_changed


def plain_product_payload(method, data):
    """Never leak internal logo markers on an emoji rejection or disabled mode."""
    return transform_payload(method, data, {'premium_emojis_enabled':'0'})[0]

def emoji_rejected(error):
    text = str(error).casefold()
    return any(term in text for term in ('custom emoji', 'custom_emoji', 'emoji_id', 'emoji id', 'emoji document', "can't parse entities", 'cannot parse entities'))

def multipart_data(payload):
    return {key: json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else value
            for key, value in payload.items()}

def file_positions(files):
    positions = []
    for value in (files or {}).values():
        content = value[1] if isinstance(value, tuple) else value
        if hasattr(content, 'read'):
            try:
                positions.append((content, content.tell()))
                if not content.seekable(): return None
            except (AttributeError, OSError): return None
    return positions

def rewind_files(positions):
    for file, offset in positions:
        file.seek(offset)
