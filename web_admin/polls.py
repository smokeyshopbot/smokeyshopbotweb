"""Owner-only native survey management. Delivery runs in the Telegram worker."""
import csv
import io
import uuid
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from flask import abort, flash, redirect, render_template, request, Response, session, url_for
from utils.poll_validation import audience_query, parse_user_ids, validate_poll
from utils.message_emojis import plain_text
from web_admin.group_announcements import enqueue_poll, target
from web_admin.delivery_queue import enqueue_job


def ensure_poll_indexes(db):
    db.user_polls.create_index("campaign_id", unique=True)
    db.user_polls.create_index([("status", 1), ("ends_at", 1)])
    db.user_polls.create_index("results_unread")
    db.poll_deliveries.create_index([("campaign_id", 1), ("user_id", 1)], unique=True)
    db.poll_deliveries.create_index("telegram_poll_id", unique=True, sparse=True)
    db.poll_deliveries.create_index([("status", 1), ("updated_at", 1)])
    db.orders.create_index([("api_receipt_status", 1), ("api_receipt_next_attempt", 1)])


def poll_statistics_many(db, campaigns):
    """One aggregate query for a page of polls, not one query per option/user."""
    group = {"_id": "$campaign_id", "recipients": {"$sum": 1},
        "sent": {"$sum": {"$cond": [{"$ne": [{"$ifNull": ["$message_id", {"$ifNull": ["$telegram_poll_id", None]}]}, None]}, 1, 0]}},
        "voters": {"$sum": {"$cond": [{"$gt": [{"$size": {"$ifNull": ["$option_ids", []]}}, 0]}, 1, 0]}}}
    for key, statuses in {"failed": ["failed"], "queued": ["pending", "sending"], "close_failed": ["close_failed"]}.items():
        group[key] = {"$sum": {"$cond": [{"$in": ["$status", statuses]}, 1, 0]}}
    for index in range(10):
        group[f"option_{index}"] = {"$sum": {"$cond": [{"$in": [index, {"$ifNull": ["$option_ids", []]}]}, 1, 0]}}
    rows = db.poll_deliveries.aggregate([
        {"$match": {"campaign_id": {"$in": [c["campaign_id"] for c in campaigns]}}}, {"$group": group}])
    counts = {row["_id"]: row for row in rows}
    result = {}
    for campaign in campaigns:
        row = counts.get(campaign["campaign_id"], {})
        stats = {key: row.get(key, 0) for key in ("recipients", "sent", "failed", "queued", "close_failed", "voters")}
        final = campaign.get('final_results')
        if final:
            stats['voters'] = final['voters']
        stats["results"] = [{"text": text, "count": final['counts'][i] if final else row.get(f"option_{i}", 0),
            "percent": round(100 * (final['counts'][i] if final else row.get(f"option_{i}", 0)) / stats["voters"], 1) if stats["voters"] else 0}
            for i, text in enumerate(campaign["options"])]
        result[campaign["campaign_id"]] = stats
    return result


def poll_statistics(db, campaign):
    return poll_statistics_many(db, [campaign])[campaign["campaign_id"]]

def poll_deadline_text(campaign):
    ends = campaign.get('ends_at')
    if not ends:
        return ''
    try:
        zone = ZoneInfo(campaign.get('poll_timezone') or 'Asia/Kolkata')
    except (ValueError, ZoneInfoNotFoundError):
        zone = timezone.utc
    if ends.tzinfo is None:
        ends = ends.replace(tzinfo=timezone.utc)
    return ends.astimezone(zone).strftime('%Y-%m-%d %H:%M') + ' ' + str(zone)


def closing_settings(form):
    mode = form.get('closing_mode', 'duration')
    if mode == 'manual':
        return {'closing_mode': mode, 'duration_seconds': None, 'ends_at': None}
    if mode == 'duration':
        try:
            value = int(form.get('duration_value', '24'))
            unit = {'minutes': 60, 'hours': 3600, 'days': 86400}[form.get('duration_unit', 'hours')]
        except (ValueError, KeyError):
            raise ValueError('Enter a whole-number duration in minutes, hours, or days.')
        if not 60 <= value * unit <= 365 * 86400:
            raise ValueError('Poll duration must be between 1 minute and 365 days.')
        return {'closing_mode': mode, 'duration_seconds': value * unit, 'ends_at': None}
    if mode != 'at':
        raise ValueError('Choose a valid poll closing mode.')
    try:
        tz = ZoneInfo(str(form.get('poll_timezone') or 'Asia/Kolkata').strip())
        local = datetime.fromisoformat(form.get('close_at', ''))
        if local.tzinfo is not None:
            raise ValueError()
        ends = local.replace(tzinfo=tz).astimezone(timezone.utc)
        if ends.astimezone(tz).replace(tzinfo=None) != local:
            raise ValueError()
    except (ValueError, ZoneInfoNotFoundError):
        raise ValueError('Enter a valid closing date/time and timezone, such as Asia/Kolkata.')
    if ends <= datetime.now(timezone.utc):
        raise ValueError('Closing time must be in the future.')
    return {'closing_mode': mode, 'duration_seconds': None, 'ends_at': ends, 'poll_timezone': str(tz)}


def register_poll_routes(app, login_required, owner_required, csrf_required, log_admin_action, shop=None):
    def find_campaign(campaign_id):
        campaign = app.db.user_polls.find_one({"campaign_id": campaign_id})
        if not campaign:
            abort(404)
        return campaign

    @app.get("/polls")
    @login_required
    @owner_required
    def polls():
        campaigns = list(app.db.user_polls.find({}).sort("created_at", -1).limit(100))
        stats = poll_statistics_many(app.db, campaigns)
        for campaign in campaigns:
            campaign["stats"] = stats[campaign["campaign_id"]]
            campaign['deadline_text'] = poll_deadline_text(campaign)
        return render_template("polls.html", campaigns=campaigns,
                               maintenance=bool((app.db.settings.find_one({"key": "maintenance_mode"}) or {}).get("value")))

    @app.post("/polls/create")
    @login_required
    @owner_required
    @csrf_required
    def create_user_poll():
        try:
            question, options, multiple = validate_poll(
                request.form.get("question"), request.form.getlist('answer') or request.form.get("options", "").splitlines(),
                request.form.get("multiple") == "1", max_option_length=250)
            closing = closing_settings(request.form)
            audience = {"language": request.form.get("language", "all"),
                        "pricing_group": request.form.get("pricing_group", "all"),
                        "user_ids": parse_user_ids(request.form.get("user_ids", ""))}
            query = audience_query(audience)
        except ValueError as exc:
            flash(str(exc), "error")
            return redirect(url_for("polls"))
        if app.db.users.count_documents(query) == 0 and not (shop and target(app.db, shop, 'polls')):
            flash("No active users match this audience.", "error")
            return redirect(url_for("polls"))
        campaign_id = uuid.uuid4().hex[:12].upper()
        app.db.user_polls.insert_one({
            "campaign_id": campaign_id, "question": question, "options": options,
            "multiple": multiple, "audience": audience, "status": "draft", "format": "private_v2", **closing,
            "created_by": session.get("admin_username", "owner"),
            "created_at": datetime.now(timezone.utc),
        })
        log_admin_action(app.db, "poll_created", campaign_id)
        flash("Poll draft created. Review it, then press Send poll.", "success")
        return redirect(url_for("user_poll_detail", campaign_id=campaign_id))

    @app.get("/polls/<campaign_id>")
    @login_required
    @owner_required
    def user_poll_detail(campaign_id):
        campaign = find_campaign(campaign_id)
        campaign['deadline_text'] = poll_deadline_text(campaign)
        try:
            page = max(1, int(request.args.get("page", 1)))
        except ValueError:
            page = 1
        selection = request.args.get('option', '0')
        query = {'campaign_id': campaign_id}
        if selection == 'unanswered':
            query['option_ids'] = []
        elif selection != 'delivery':
            try:
                index = int(selection)
            except ValueError:
                abort(400)
            if not 0 <= index < len(campaign['options']):
                abort(400)
            query['option_ids'] = index
            selection = str(index)
        total_rows = app.db.poll_deliveries.count_documents(query)
        page = min(page, max(1, (total_rows + 49) // 50))
        rows = list(app.db.poll_deliveries.find(query).sort("user_id", 1).skip((page - 1) * 50).limit(50))
        for row in rows:
            row["answers"] = [campaign["options"][i] for i in row.get("option_ids", []) if 0 <= i < len(campaign["options"])]
        return render_template("poll_detail.html", campaign=campaign,
                               stats=poll_statistics(app.db, campaign), rows=rows, page=page, selection=selection, total_rows=total_rows,
                               audience_count=app.db.users.count_documents(audience_query(campaign["audience"])))

    @app.post("/polls/<campaign_id>/send")
    @login_required
    @owner_required
    @csrf_required
    def send_user_poll(campaign_id):
        campaign = find_campaign(campaign_id)
        now = datetime.now(timezone.utc)
        ends = campaign.get('ends_at')
        if ends and (ends.replace(tzinfo=timezone.utc) if ends.tzinfo is None else ends) <= now:
            flash('The closing time has passed. Update the timer before sending.', 'error')
            return redirect(url_for('user_poll_detail', campaign_id=campaign_id))
        # Older unsent drafts also use private voting when first sent.
        fields = {'status': 'queued', 'queued_at': now, 'format': 'private_v2'}
        if campaign.get('duration_seconds'):
            fields['ends_at'] = now + timedelta(seconds=campaign['duration_seconds'])
        result = app.db.user_polls.update_one({"campaign_id": campaign_id, "status": "draft"},
            {"$set": fields})
        if result.modified_count and shop:
            queued = enqueue_poll(app.db, shop, find_campaign(campaign_id))
            if queued:
                app.db.user_polls.update_one({'campaign_id': campaign_id}, {'$set': {'group_poll_job_id': queued['job_id']}})
        flash("Poll queued. The Telegram bot sends it in the background; maintenance pauses delivery." if result.modified_count else "This poll has already been queued or closed.", "success" if result.modified_count else "info")
        return redirect(url_for("user_poll_detail", campaign_id=campaign_id))

    @app.post("/polls/<campaign_id>/close")
    @login_required
    @owner_required
    @csrf_required
    def close_user_poll(campaign_id):
        campaign = find_campaign(campaign_id)
        if campaign["status"] == "close_failed":
            app.db.poll_deliveries.update_many({"campaign_id": campaign_id, "status": "close_failed"},
                {"$set": {"status": "sent", "close_attempts": 0}})
            app.db.user_polls.update_one({'campaign_id': campaign_id}, {'$set': {'group_close_attempts': 0},
                '$unset': {'group_results_error': ''}})
        app.db.user_polls.update_one({"campaign_id": campaign_id, "status": {"$nin": ["closed", "draft"]}},
            {"$set": {"close_requested": True, "close_requested_at": datetime.now(timezone.utc),
                      **({'status': 'closing'} if campaign['status'] == 'close_failed' else {})}})
        log_admin_action(app.db, "poll_close_requested", campaign_id)
        flash("Close requested. The bot will publish shared final results in the background.", "info")
        return redirect(url_for("user_poll_detail", campaign_id=campaign_id))

    @app.post('/polls/<campaign_id>/schedule')
    @login_required
    @owner_required
    @csrf_required
    def schedule_user_poll(campaign_id):
        campaign = find_campaign(campaign_id)
        if campaign['status'] not in {'draft', 'queued', 'preparing', 'active'} or campaign.get('close_requested'):
            abort(409)
        try:
            fields = closing_settings(request.form)
            if fields.get('duration_seconds') and campaign['status'] != 'draft':
                fields['ends_at'] = datetime.now(timezone.utc) + timedelta(seconds=fields['duration_seconds'])
            app.db.user_polls.update_one({'campaign_id': campaign_id, 'status': campaign['status'], 'close_requested': {'$ne': True}}, {'$set': fields})
            log_admin_action(app.db, 'poll_timer_updated', campaign_id)
            flash('Closing timer saved. For an active poll, a new duration starts now.', 'success')
        except ValueError as exc:
            flash(str(exc), 'error')
        return redirect(url_for('user_poll_detail', campaign_id=campaign_id))

    @app.post('/polls/<campaign_id>/reviewed')
    @login_required
    @owner_required
    @csrf_required
    def review_user_poll(campaign_id):
        find_campaign(campaign_id)
        app.db.user_polls.update_one({'campaign_id': campaign_id, 'results_unread': True},
            {'$set': {'results_unread': False, 'results_reviewed_at': datetime.now(timezone.utc)}})
        if shop:
            shop.clear_admin_cache()
        return redirect(url_for('user_poll_detail', campaign_id=campaign_id))
    @app.get("/polls/<campaign_id>/responses.csv")
    @login_required
    @owner_required
    def export_poll_responses(campaign_id):
        campaign = find_campaign(campaign_id)
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["User ID", "Username", "Answers", "Delivery status"])
        def cell(value):
            value = str(value)
            return "'" + value if value.startswith(("=", "+", "-", "@", "\t", "\r")) else value
        for row in app.db.poll_deliveries.find({"campaign_id": campaign_id}).sort("user_id", 1):
            answers = " | ".join(plain_text(campaign["options"][i]) for i in row.get("option_ids", []) if 0 <= i < len(campaign["options"]))
            writer.writerow([row["user_id"], cell(row.get("username", "")), cell(answers), row["status"]])
        return Response(output.getvalue(), mimetype="text/csv", headers={"Content-Disposition": f"attachment; filename=poll-{campaign_id}.csv"})
