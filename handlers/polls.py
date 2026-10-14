"""Persistent private surveys, legacy native polls, and shared final results."""
import asyncio
import logging
import re
import uuid
from datetime import datetime, timezone, timedelta
from telegram.error import RetryAfter, BadRequest
from telegram.ext import ApplicationHandlerStop
import database as db
from utils.poll_validation import audience_query
from utils.survey import voting_open, survey_text, survey_keyboard, vote_locked, words
from utils.notification_limits import notifications_paused, pause_notifications
from utils.data_guard import guarded_async

logger = logging.getLogger(__name__)

async def handle_poll_answer(update, context):
    """Compatibility for polls sent before private surveys were introduced."""
    answer = update.poll_answer
    if not answer or not answer.user:
        return
    database = db.get_db()
    delivery = await database.poll_deliveries.find_one({'telegram_poll_id': answer.poll_id, 'user_id': answer.user.id})
    if not delivery:
        return
    campaign = await database.user_polls.find_one({'campaign_id': delivery['campaign_id']})
    if not campaign or campaign.get('format') == 'private_v2' or campaign.get('status') in {'closing', 'closed', 'close_failed'}:
        return
    choices = sorted(set(answer.option_ids))
    if any(i < 0 or i >= len(campaign['options']) for i in choices) or (not campaign.get('multiple') and len(choices) > 1):
        return
    await database.poll_deliveries.update_one({'_id': delivery['_id']}, {'$set': {
        'option_ids': choices, 'answered_at': datetime.now(timezone.utc), 'username': answer.user.username or '',
        'updated_at': datetime.now(timezone.utc)}})
    raise ApplicationHandlerStop

async def _edit(bot, delivery, campaign, *, results=None):
    locked = vote_locked(delivery)
    choices = delivery.get('option_ids', []) if locked else delivery.get('pending_option_ids', [])
    try:
        await bot.edit_message_text(chat_id=delivery['user_id'], message_id=delivery['message_id'],
            text=survey_text(campaign, delivery.get('language', 'en'), choices, results=results),
            reply_markup=None if results is not None else survey_keyboard(campaign, delivery.get('language', 'en'), choices, locked=locked))
    except BadRequest as exc:
        if 'message is not modified' not in str(exc).lower():
            raise

async def _refresh_current(bot, database, delivery, cid):
    # An older callback can finish editing after a newer vote commits. Always
    # rebuild the buttons from the persisted vote rather than that callback.
    delivery = await database.poll_deliveries.find_one({'_id': delivery['_id']}) or delivery
    fresh = await database.user_polls.find_one({'campaign_id': cid}) or {}
    if fresh.get('final_results'):
        await _edit(bot, delivery, fresh, results=fresh['final_results'])
    elif voting_open(fresh):
        await _edit(bot, delivery, fresh)
        # A close can overtake a slow Telegram edit. Repair that message with
        # the same immutable final snapshot, never stale voting buttons.
        latest = await database.user_polls.find_one({'campaign_id': cid}) or {}
        if latest.get('final_results'):
            await _edit(bot, delivery, latest, results=latest['final_results'])
        else:
            latest_delivery = await database.poll_deliveries.find_one({'_id': delivery['_id']}) or delivery
            if (latest_delivery.get('option_ids') != delivery.get('option_ids')
                    or latest_delivery.get('pending_option_ids') != delivery.get('pending_option_ids')
                    or vote_locked(latest_delivery) != vote_locked(delivery)):
                await _edit(bot, latest_delivery, latest)

async def open_private_survey(update, context, campaign_id):
    database = db.get_db()
    user = update.effective_user
    campaign = await database.user_polls.find_one({'campaign_id': campaign_id, 'format': 'private_v2'})
    profile = await database.users.find_one({'user_id': user.id}) or {}
    lang = 'es' if profile.get('language') == 'es' else 'en'
    w = words(lang)
    reply = update.effective_message.reply_text
    if not campaign or campaign.get('status') not in {'queued', 'preparing', 'active', 'closing', 'closed', 'close_failed'} or profile.get('blocked'):
        await reply(w['unavailable'])
        return
    existing = await database.poll_deliveries.find_one({'campaign_id': campaign_id, 'user_id': user.id})
    deadline_passed = campaign.get('ends_at') and campaign['ends_at'].replace(tzinfo=timezone.utc) <= datetime.now(timezone.utc)
    if campaign.get('status') in {'closing', 'closed', 'close_failed'} or campaign.get('close_requested') or deadline_passed:
        await reply(survey_text(campaign, lang, results=campaign['final_results']) if campaign.get('final_results') else w['closed'])
        return
    if not await database.users.find_one({'$and': [audience_query(campaign['audience']), {'user_id': user.id}]}):
        await reply(w['unavailable'])
        return
    if existing and existing.get('message_id') and existing.get('status') == 'sent':
        try:
            await _refresh_current(context.bot, database, existing, campaign_id)
            await reply('Use the voting buttons on your poll message above.' if lang == 'en' else 'Usa los botones de tu encuesta anterior.')
        except Exception:
            await reply(w['busy'])
        return
    if existing and existing.get('status') in {'failed', 'sending', 'closed', 'close_failed'}:
        await reply(w['busy'])
        return
    now = datetime.now(timezone.utc)
    await database.poll_deliveries.update_one({'campaign_id': campaign_id, 'user_id': user.id}, {'$setOnInsert': {
        'campaign_id': campaign_id, 'user_id': user.id, 'username': user.username or '', 'language': lang,
        'status': 'pending', 'option_ids': [], 'created_at': now, 'updated_at': now}}, upsert=True)
    await reply('Your poll is queued and will arrive shortly.' if lang == 'en' else 'Tu encuesta está en cola y llegará en breve.')

async def handle_survey_callback(update, context):
    query = update.callback_query
    match = re.fullmatch(r'survey:([A-F0-9]{12}):([0-9]|r|s|l)', str(query.data or ''))
    if not match:
        return
    cid, selection = match.groups()
    database = db.get_db()
    uid = update.effective_user.id
    delivery = await database.poll_deliveries.find_one({'campaign_id': cid, 'user_id': uid})
    profile = await database.users.find_one({'user_id': uid}) or {}
    lang = 'es' if profile.get('language') == 'es' else 'en'
    w = words(lang)
    campaign = await database.user_polls.find_one({'campaign_id': cid, 'format': 'private_v2'})
    if (not campaign or not delivery or delivery.get('message_id') != query.message.message_id
            or query.message.chat.id != uid or profile.get('blocked')):
        await query.answer(w['unavailable'], show_alert=True)
        raise ApplicationHandlerStop
    if not voting_open(campaign):
        await query.answer(w['closed'], show_alert=True)
        if campaign.get('final_results'):
            await _edit(context.bot, delivery, campaign, results=campaign['final_results'])
        raise ApplicationHandlerStop
    if selection == 'r':
        await query.answer()
        await _refresh_current(context.bot, database, delivery, cid)
        raise ApplicationHandlerStop
    if vote_locked(delivery) or selection == 'l':
        await query.answer(w['locked'], show_alert=True)
        await _refresh_current(context.bot, database, delivery, cid)
        raise ApplicationHandlerStop
    index = int(selection) if selection != 's' else None
    if (delivery.get('status') != 'sent' or (index is not None and index >= len(campaign['options']))
            or (selection == 's' and not campaign.get('multiple'))):
        await query.answer(w['unavailable'], show_alert=True)
        raise ApplicationHandlerStop
    # The close snapshot waits for accepted votes to commit. No vote enters
    # after its deadline, even if the queue worker is offline.
    now = datetime.now(timezone.utc)
    token = uuid.uuid4().hex
    lock = await database.user_polls.update_one({'campaign_id': cid, 'status': 'active', 'close_requested': {'$ne': True},
        '$and': [{'$or': [{'ends_at': {'$exists': False}}, {'ends_at': None}, {'ends_at': {'$gt': now}}]},
                 {'$or': [{'vote_lock_until': {'$exists': False}}, {'vote_lock_until': {'$lte': now}}]}]},
        {'$set': {'vote_lock_owner': token, 'vote_lock_until': now + timedelta(seconds=15)}})
    if not lock.modified_count:
        await query.answer(w['busy'], show_alert=True)
        raise ApplicationHandlerStop
    outcome = 'busy'
    try:
        delivery = await database.poll_deliveries.find_one({'_id': delivery['_id']})
        if vote_locked(delivery):
            outcome = 'locked'
        else:
            choices = set(delivery.get('pending_option_ids') or [])
            if campaign.get('multiple') and index is not None:
                choices.symmetric_difference_update({index})
                fields = {'pending_option_ids': sorted(choices), 'updated_at': now}
                outcome = 'selected'
            elif selection == 's' and not choices:
                fields = None
                outcome = 'choose'
            else:
                if not campaign.get('multiple'):
                    choices = {index}
                fields = {'option_ids': sorted(choices), 'pending_option_ids': [],
                          'vote_locked_at': now, 'username': update.effective_user.username or '',
                          'answered_at': now, 'updated_at': now}
                outcome = 'saved'
            if fields is not None:
                # Persist the immutable answer atomically, before updating
                # Telegram. Old buttons and simultaneous callbacks cannot replace it.
                written = await database.poll_deliveries.update_one(
                    {'_id': delivery['_id'], 'status': 'sent', 'option_ids': [],
                     'vote_locked_at': {'$exists': False}}, {'$set': fields})
                if not written.modified_count:
                    outcome = 'locked'
    finally:
        await database.user_polls.update_one({'campaign_id': cid, 'vote_lock_owner': token},
            {'$unset': {'vote_lock_owner': '', 'vote_lock_until': ''}})
    await query.answer(w[outcome], show_alert=outcome in {'locked', 'choose', 'busy'})
    try:
        await _refresh_current(context.bot, database, delivery, cid)
    except RetryAfter as exc:
        await pause_notifications(database, exc.retry_after)
    except Exception:
        logger.warning('Vote saved; poll message refresh failed for campaign %s', cid)
    raise ApplicationHandlerStop

async def _final_results(database, campaign):
    group = {'_id': None, 'voters': {'$sum': {'$cond': [{'$gt': [{'$size': {'$ifNull': ['$option_ids', []]}}, 0]}, 1, 0]}}}
    for index in range(len(campaign['options'])):
        group[str(index)] = {'$sum': {'$cond': [{'$in': [index, {'$ifNull': ['$option_ids', []]}]}, 1, 0]}}
    rows = await database.poll_deliveries.aggregate([{'$match': {'campaign_id': campaign['campaign_id']}}, {'$group': group}]).to_list(length=1)
    row = rows[0] if rows else {}
    return {'voters': row.get('voters', 0), 'counts': [row.get(str(i), 0) for i in range(len(campaign['options']))]}

async def _close_group(bot, database, campaign):
    from utils.group_notifications import settings as group_settings
    raw = await database.settings.find_one({'key': 'secret_settings'}) or {}
    preferences = group_settings(raw.get('value') or {})
    if preferences['group_notifications_enabled'] != '1' or preferences['group_notifications_polls'] != '1':
        return True
    jid = campaign.get('group_poll_job_id') or campaign.get('channel_poll_job_id')
    if not jid:
        return True
    job = await database.delivery_jobs.find_one({'job_id': jid}) or {}
    if job.get('kind') != 'text':
        return True
    payload = job.get('payload') or {}
    if payload.get('group_recipient'):
        result = job.get('telegram_result') or {}
    elif payload.get('channel_recipient') and job.get('channel_source_acknowledged_at'):
        result = job.get('channel_source_result') or {}
    else:
        result = {}
    message_id = result.get('message_id')
    if not message_id:
        if job.get('status') == 'queued':
            await database.delivery_jobs.update_one({'_id': job['_id'], 'status': 'queued'}, {'$set': {'status': 'cancelled'}})
            return True
        return job.get('status') in {'done', 'cancelled', 'failed', 'review'} or not job
    if campaign.get('group_results_sent'):
        return True
    try:
        await _edit(bot, {'user_id': (result.get('chat') or {}).get('id') or payload['chat_id'],
                          'message_id': message_id, 'language': payload.get('language', 'en')},
                    campaign, results=campaign['final_results'])
        await database.user_polls.update_one({'campaign_id': campaign['campaign_id']},
            {'$set': {'group_results_sent': True}, '$unset': {'group_results_error': ''}})
        return True
    except RetryAfter:
        raise
    except Exception as exc:
        attempts = campaign.get('group_close_attempts', 0) + 1
        campaign['group_close_attempts'] = attempts
        await database.user_polls.update_one({'campaign_id': campaign['campaign_id']}, {'$set': {
            'group_close_attempts': attempts, 'group_results_error': str(exc)[:300]}})
        return attempts >= 3

@guarded_async(lambda: db.get_db())
async def process_poll_queue(bot, *, batch_size=20):
    database = db.get_db()
    now = datetime.now(timezone.utc)
    remaining = batch_size
    # Closing and the badge still run during maintenance or Telegram throttling.
    await database.user_polls.update_many({'status': {'$in': ['queued', 'preparing', 'active']},
        '$and': [{'$or': [{'ends_at': {'$lte': now, '$ne': None}}, {'close_requested': True}]},
                 {'$or': [{'vote_lock_until': {'$exists': False}}, {'vote_lock_until': {'$lte': now}}]}]},
        {'$set': {'status': 'closing', 'close_requested_at': now}})
    await database.poll_deliveries.update_many({'status': 'sending', 'updated_at': {'$lt': now - timedelta(minutes=2)}},
        {'$set': {'status': 'failed', 'error': 'Delivery interrupted; review before resending.', 'updated_at': now}})
    async for campaign in database.user_polls.find({'status': 'closing'}):
        cid = campaign['campaign_id']
        await database.poll_deliveries.update_many({'campaign_id': cid, 'status': 'pending'}, {'$set': {'status': 'cancelled'}})
        if not campaign.get('final_results'):
            campaign['final_results'] = await _final_results(database, campaign)
            await database.user_polls.update_one({'campaign_id': cid}, {'$set': {
                'final_results': campaign['final_results'], 'results_ready_at': now, 'results_unread': True}})
        if await notifications_paused(database):
            continue
        async for delivery in database.poll_deliveries.find({'campaign_id': cid, 'status': 'sent'}).limit(batch_size):
            if remaining <= 0:
                break
            remaining -= 1
            try:
                if campaign.get('format') == 'private_v2':
                    await _edit(bot, delivery, campaign, results=campaign['final_results'])
                else:
                    if not delivery.get('native_stopped'):
                        await bot.stop_poll(chat_id=delivery['user_id'], message_id=delivery['message_id'])
                        await database.poll_deliveries.update_one({'_id': delivery['_id']}, {'$set': {'native_stopped': True}})
                    await bot.send_message(chat_id=delivery['user_id'], text=survey_text(campaign, delivery.get('language', 'en'), results=campaign['final_results']))
                fields = {'status': 'closed', 'telegram_closed_at': now}
            except RetryAfter as exc:
                await pause_notifications(database, exc.retry_after)
                return
            except Exception as exc:
                attempts = int(delivery.get('close_attempts', 0)) + 1
                fields = {'close_attempts': attempts, 'error': str(exc)[:300]}
                if attempts >= 3:
                    fields['status'] = 'close_failed'
            await database.poll_deliveries.update_one({'_id': delivery['_id']}, {'$set': fields})
            await asyncio.sleep(0.1)
        try:
            group_done = await _close_group(bot, database, campaign) if remaining > 0 else not (campaign.get('group_poll_job_id') or campaign.get('channel_poll_job_id'))
            if (campaign.get('group_poll_job_id') or campaign.get('channel_poll_job_id')) and remaining > 0:
                remaining -= 1
        except RetryAfter as exc:
            await pause_notifications(database, exc.retry_after)
            return
        if group_done and not await database.poll_deliveries.count_documents({'campaign_id': cid, 'status': {'$in': ['sent', 'sending']}}):
            failures = await database.poll_deliveries.count_documents({'campaign_id': cid, 'status': 'close_failed'})
            await database.user_polls.update_one({'campaign_id': cid, 'status': 'closing'}, {'$set': {
                'status': 'close_failed' if failures or campaign.get('group_close_attempts', 0) >= 3 else 'closed', 'closed_at': now}})
    if remaining <= 0 or await notifications_paused(database) or await db.is_maintenance_mode():
        return
    async for campaign in database.user_polls.find({'status': {'$in': ['queued', 'preparing']}}).limit(1):
        cid = campaign['campaign_id']
        await database.user_polls.update_one({'campaign_id': cid, 'status': 'queued'}, {'$set': {'status': 'preparing'}})
        query = audience_query(campaign['audience'])
        if campaign.get('prepared_through'):
            query = {'$and': [query, {'user_id': {'$gt': campaign['prepared_through']}}]}
        users = await database.users.find(query).sort('user_id', 1).limit(201).to_list(length=201)
        for user in users[:200]:
            await database.poll_deliveries.update_one({'campaign_id': cid, 'user_id': user['user_id']}, {'$setOnInsert': {
                'campaign_id': cid, 'user_id': user['user_id'], 'username': user.get('username', ''),
                'language': user.get('language', 'en'), 'status': 'pending', 'option_ids': [], 'created_at': now, 'updated_at': now}}, upsert=True)
        fields = {'prepared_through': users[min(199, len(users)-1)]['user_id']} if users else {}
        if len(users) <= 200:
            fields.update(status='active', started_at=now)
        await database.user_polls.update_one({'campaign_id': cid, 'status': 'preparing', 'close_requested': {'$ne': True}}, {'$set': fields})
    async for campaign in database.user_polls.find({'status': 'active'}):
        if remaining <= 0:
            break
        cid = campaign['campaign_id']
        async for delivery in database.poll_deliveries.find({'campaign_id': cid, 'status': 'pending'}).limit(remaining):
            if remaining <= 0:
                break
            fresh = await database.user_polls.find_one({'campaign_id': cid}) or {}
            if not voting_open(fresh):
                break
            user = await database.users.find_one({'user_id': delivery['user_id']}) or {}
            if user.get('blocked'):
                await database.poll_deliveries.update_one({'_id': delivery['_id']}, {'$set': {'status': 'cancelled'}})
                continue
            claim = await database.poll_deliveries.update_one({'_id': delivery['_id'], 'status': 'pending'},
                {'$set': {'status': 'sending', 'updated_at': datetime.now(timezone.utc)}})
            if not claim.modified_count:
                continue
            remaining -= 1
            try:
                if campaign.get('format') == 'private_v2':
                    message = await bot.send_message(chat_id=delivery['user_id'], text=survey_text(fresh, delivery.get('language', 'en')),
                                                     reply_markup=survey_keyboard(fresh, delivery.get('language', 'en')))
                    fields = {'status': 'sent', 'message_id': message.message_id, 'sent_at': datetime.now(timezone.utc)}
                else:
                    message = await bot.send_poll(chat_id=delivery['user_id'], question=campaign['question'], options=campaign['options'],
                        is_anonymous=False, allows_multiple_answers=campaign.get('multiple', False), type='regular')
                    fields = {'status': 'sent', 'telegram_poll_id': message.poll.id, 'message_id': message.message_id, 'sent_at': datetime.now(timezone.utc)}
            except RetryAfter as exc:
                await database.poll_deliveries.update_one({'_id': delivery['_id']}, {'$set': {'status': 'pending', 'updated_at': datetime.now(timezone.utc)}})
                await pause_notifications(database, exc.retry_after)
                return
            except Exception as exc:
                fields = {'status': 'failed', 'error': str(exc)[:300]}
            fields['updated_at'] = datetime.now(timezone.utc)
            await database.poll_deliveries.update_one({'_id': delivery['_id']}, {'$set': fields})
            await asyncio.sleep(0.1)

async def start_poll_watcher(application):
    database = db.get_db()
    await database.user_polls.create_index('campaign_id', unique=True)
    await database.user_polls.create_index([('status', 1), ('ends_at', 1)])
    await database.poll_deliveries.create_index([('campaign_id', 1), ('user_id', 1)], unique=True)
    await database.poll_deliveries.create_index('telegram_poll_id', unique=True, sparse=True)
    await database.delivery_control.create_index('key', unique=True)
    async def worker():
        while True:
            try:
                await process_poll_queue(application.bot)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception('Poll queue processing failed')
            await asyncio.sleep(3)
    application.bot_data['poll_task'] = asyncio.create_task(worker())
