"""Private voting presentation; totals contain no Telegram identities."""
from datetime import datetime, timezone
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

TEXT = {
    'en': {'title': '📊 Poll', 'single': 'Choose one option.',
           'multiple': 'Choose one or more options, then tap Submit vote.',
           'saved': 'Vote submitted.', 'closed': 'Poll ended',
           'refresh': 'Refresh', 'submit': 'Submit vote',
           'selected': 'Selection updated. Submit your vote when ready.',
           'choose': 'Select at least one answer, then submit your vote.',
           'locked': 'Your vote has already been submitted and cannot be changed.',
           'unavailable': 'This poll is unavailable for your account.',
           'busy': 'Please try again in a moment.', 'vote': 'Vote in the bot'},
    'es': {'title': '📊 Encuesta', 'single': 'Elige una opción.',
           'multiple': 'Elige una o varias opciones y pulsa Enviar voto.',
           'saved': 'Voto enviado.', 'closed': 'Encuesta finalizada',
           'refresh': 'Actualizar', 'submit': 'Enviar voto',
           'selected': 'Selección actualizada. Envía tu voto cuando esté listo.',
           'choose': 'Selecciona al menos una respuesta y envía tu voto.',
           'locked': 'Tu voto ya se ha enviado y no se puede cambiar.',
           'unavailable': 'Esta encuesta no está disponible para tu cuenta.',
           'busy': 'Inténtalo de nuevo en un momento.', 'vote': 'Votar en el bot'},
}

def words(language):
    return TEXT['es' if language == 'es' else 'en']

def utc(value):
    return value.replace(tzinfo=timezone.utc) if value and value.tzinfo is None else value

def voting_open(campaign, now=None):
    return (campaign.get('status') == 'active' and not campaign.get('close_requested')
            and (not campaign.get('ends_at') or utc(campaign['ends_at']) > (now or datetime.now(timezone.utc))))

def result_counts(rows, option_count):
    counts = [0] * option_count
    voters = 0
    for row in rows:
        choices = set(row.get('option_ids') or [])
        if choices:
            voters += 1
        for index in choices:
            if 0 <= index < option_count:
                counts[index] += 1
    return {'voters': voters, 'counts': counts}

def survey_text(campaign, language='en', choices=(), *, results=None):
    # Options stay on the buttons. Final results retain their compact layout.
    if results is None:
        w = words(language)
        return '\n\n'.join([w['title'], campaign['question'],
                            w['multiple'] if campaign.get('multiple') else w['single']])
    lines = [campaign['question']]
    for i, option in enumerate(campaign['options']):
        count = results['counts'][i]
        percent = round(100 * count / results['voters'], 1) if results['voters'] else 0
        lines.append(f'{i + 1}. {option} ({percent:g}%)')
    return '\n\n'.join(lines)

def vote_locked(delivery):
    # Votes recorded before this update are already submitted too.
    return bool(delivery.get('vote_locked_at') or delivery.get('option_ids'))

def survey_keyboard(campaign, language='en', choices=(), *, locked=False):
    cid = campaign['campaign_id']
    rows = [[InlineKeyboardButton(('✓ ' if i in choices else '') + f'{i+1}. ' + ' '.join(option.split()),
                                  callback_data=f'survey:{cid}:l' if locked else f'survey:{cid}:{i}')]
            for i, option in enumerate(campaign['options'])]
    if campaign.get('multiple') and not locked:
        rows.append([InlineKeyboardButton(words(language)['submit'], callback_data=f'survey:{cid}:s')])
    rows.append([InlineKeyboardButton(words(language)['refresh'], callback_data=f'survey:{cid}:r')])
    return InlineKeyboardMarkup(rows)
