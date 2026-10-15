"""Admin mount path, per-account TOTP and short-lived server-side challenges."""
from __future__ import annotations

import base64
import hashlib
import hmac
import io
import os
import re
import secrets
import time
from datetime import datetime, timedelta, timezone

import pyotp
import qrcode
from cryptography.fernet import Fernet, InvalidToken
from flask import abort, flash, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash
from werkzeug.wrappers import Response
from web_admin.domain_routing import (RECOVERY_ENV, hostname, origin_hostname,
                                      routing_settings, validate_settings as validate_domains)

DEFAULT_PATH = "/admin"
PUBLIC_API = "/api/telegram-buyer"
AUTH_ENDPOINTS = {"login_form", "login_submit", "login_two_factor", "login_two_factor_submit"}
COMMON_ENDPOINTS = AUTH_ENDPOINTS | {"admin_security", "admin_2fa_start", "admin_2fa_enable",
                                     "admin_2fa_disable", "admin_2fa_recovery"}


def normalize_path(value, *, allow_root=False):
    value = str(value or "").strip()
    if value == "/" and allow_root:
        return ""
    value = value.strip("/")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{2,79}", value) or value.lower() in {"api", "static"}:
        raise ValueError("Admin path must contain 3–80 letters, numbers, hyphens or underscores, for example /shop-control-7x9. Do not enter a domain or full URL.")
    return "/" + value


def active_path(db, settings=None):
    override = os.getenv("ADMIN_PANEL_PATH")
    if override is not None and override.strip():
        return normalize_path(override, allow_root=True)
    if settings is None:
        settings = routing_settings(db)
    return normalize_path(settings.get("admin_panel_path") or DEFAULT_PATH)


class AdminPathMiddleware:
    def __init__(self, application, db):
        self.application, self.db = application, db

    def __call__(self, environ, start_response):
        path = environ.get("PATH_INFO", "/")
        # Unbranded process-liveness probe for Railway's internal healthcheck host.
        if path == "/healthz" and environ.get("REQUEST_METHOD") in {"GET", "HEAD"}:
            return Response("OK", content_type="text/plain", headers={"Cache-Control": "no-store"})(environ, start_response)
        is_api = path == PUBLIC_API or path.startswith(PUBLIC_API + "/")
        try:
            settings = routing_settings(self.db)
            if os.getenv(RECOVERY_ENV, "").strip() != "1" and settings.get("domain_separation_enabled", "0") != "0":
                domains = validate_domains(settings)
                actual_host = hostname(environ.get("HTTP_HOST"))
                expected_host = origin_hostname(domains["buyer_api_endpoint_url" if is_api else "admin_panel_url"])
                if actual_host != expected_host:
                    return Response("Not found", status=404, content_type="text/plain",
                                    headers={"Cache-Control": "no-store"})(environ, start_response)
        except ValueError:
            return Response("Not found", status=404, content_type="text/plain",
                            headers={"Cache-Control": "no-store"})(environ, start_response)
        except Exception:
            return Response("Service temporarily unavailable", status=503)(environ, start_response)
        # Only the reseller namespace stays public. /api/live-state is admin-only.
        if is_api:
            return self.application(environ, start_response)
        try:
            prefix = active_path(self.db, settings)
        except Exception:
            return Response("Service temporarily unavailable", status=503)(environ, start_response)
        if prefix and path != prefix and not path.startswith(prefix + "/"):
            return Response("Not found", status=404, content_type="text/plain")(environ, start_response)
        environ["webadmin.mount"] = prefix
        environ["webadmin.base"] = environ.get("SCRIPT_NAME", "").rstrip("/")
        environ["SCRIPT_NAME"] = environ.get("SCRIPT_NAME", "").rstrip("/") + prefix
        environ["PATH_INFO"] = path[len(prefix):] or "/"
        # A prefixed API alias would leak the admin path into integrations.
        if environ["PATH_INFO"] == PUBLIC_API or environ["PATH_INFO"].startswith(PUBLIC_API + "/"):
            return Response("Not found", status=404)(environ, start_response)
        return self.application(environ, start_response)


def now():
    return datetime.now(timezone.utc)


def digest(value):
    return hashlib.sha256(str(value).encode()).hexdigest()


def cipher(app):
    key = os.getenv("ADMIN_2FA_ENCRYPTION_KEY", "").strip()
    if key:
        return Fernet(key.encode())
    # The existing persistent panel secret is the default encryption root.
    root = "webadmin-totp-v1:" + str(app.config["SECRET_KEY"])
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(root.encode()).digest()))


def principal(shop, identifier):
    if identifier == "owner":
        username, password, hashed = shop.get_admin_login_config(shop.current_app.db)
        return {"id": "owner", "username": username, "password": password,
                "hashed": hashed, "role": "owner"} if password else None
    for account in shop.get_admin_accounts(shop.current_app.db):
        if "staff:" + account["id"] == identifier and account.get("enabled", True):
            return {"id": identifier, "username": account["username"], "password": account["password_hash"],
                    "hashed": True, "role": account["role"]}
    return None


def password_ok(identity, password):
    if not identity or not identity.get("password"):
        return False
    return (check_password_hash(identity["password"], password) if identity["hashed"]
            else hmac.compare_digest(identity["password"].encode(), password.encode()))


def credential_stamp(app, identity):
    data = "\0".join(str(identity[k]) for k in ("id", "username", "password", "role"))
    return hmac.new(str(app.config["SECRET_KEY"]).encode(), data.encode(), hashlib.sha256).hexdigest()


def security_state(app, identifier):
    return app.db.admin_security.find_one({"_id": identifier}) or {}


def rate_keys(username):
    # Forwarded headers are not trusted here; configure a trusted proxy at deployment.
    window = int(time.time()) // 900
    return [("user:" + digest(str(username).strip().lower()) + ":" + str(window), 5),
            ("ip:" + digest(request.remote_addr or "unknown") + ":" + str(window), 20)]


def locked_seconds(app, username):
    remaining = 900 - int(time.time()) % 900
    for key, limit in rate_keys(username):
        record = app.db.admin_auth_limits.find_one({"_id": key}) or {}
        if int(record.get("count", 0)) >= limit:
            return remaining
    return 0


def failed_attempt(app, username):
    for key, _ in rate_keys(username):
        app.db.admin_auth_limits.update_one({"_id": key}, {"$inc": {"count": 1},
            "$setOnInsert": {"expires_at": now() + timedelta(minutes=30)}}, upsert=True)


def clear_attempts(app, username):
    # A successful account login must not clear failures from other accounts on an IP.
    app.db.admin_auth_limits.delete_one({"_id": rate_keys(username)[0][0]})


def consume_factor(app, state, code):
    if not state.get("enabled"):
        return False
    code = str(code or "").strip().replace(" ", "").replace("-", "")
    match = {"_id": state["_id"], "enabled": True, "version": state["version"]}
    if re.fullmatch(r"[0-9]{6}", code):
        try:
            totp = pyotp.TOTP(cipher(app).decrypt(state["secret"].encode()).decode())
        except (InvalidToken, ValueError, KeyError):
            return False
        step = int(time.time()) // 30
        for candidate in (step, step - 1, step + 1):
            if hmac.compare_digest(totp.at(candidate * 30), code):
                result = app.db.admin_security.update_one({**match, "last_step": {"$lt": candidate}},
                    {"$set": {"last_step": candidate}})
                return bool(result.modified_count)
        return False
    if not re.fullmatch(r"[a-fA-F0-9]{32}", code):
        return False
    hashed = digest(code.lower())
    result = app.db.admin_security.update_one({**match, "recovery_hashes": hashed},
        {"$pull": {"recovery_hashes": hashed}})
    return bool(result.modified_count)


def recovery_codes():
    raw = [secrets.token_hex(16) for _ in range(10)]
    return ["-".join(value[i:i+8] for i in range(0, 32, 8)) for value in raw], [digest(value) for value in raw]


def complete_login(app, shop, identity, state):
    session.clear()
    session.update(admin_logged_in=True, admin_role=identity["role"], admin_username=identity["username"],
                   admin_principal=identity["id"], admin_credentials=credential_stamp(app, identity),
                   admin_factor_version=state.get("version", ""), csrf=secrets.token_urlsafe(32))
    clear_attempts(app, identity["username"])
    flash("Logged in successfully.", "success")
    return redirect(url_for(shop.role_home_endpoint(identity["role"])))


def install(app, shop):
    for collection in ("admin_login_challenges", "admin_auth_setups", "admin_auth_limits"):
        app.db[collection].create_index("expires_at", expireAfterSeconds=0)
    cipher(app)  # Invalid explicit encryption keys fail startup, never disable verification.
    active_path(app.db)  # Validate the override before serving requests.
    app.wsgi_app = AdminPathMiddleware(app.wsgi_app, app.db)
    shop.COMMON_ADMIN_ENDPOINTS.update(COMMON_ENDPOINTS)

    @app.before_request
    def validate_admin_factor_session():
        if (request.endpoint or "").startswith("buyer_api_") or request.endpoint == "static":
            return
        if not session.get("admin_logged_in"):
            return
        identifier = session.get("admin_principal")
        if not identifier:
            # Old pre-upgrade cookies may continue only until this account enables MFA.
            identifier = "owner" if session.get("admin_role", "owner") == "owner" else ""
            if not identifier:
                for account in shop.get_admin_accounts(app.db):
                    if account["username"].lower() == str(session.get("admin_username", "")).lower():
                        identifier = "staff:" + account["id"]
                        break
            state = security_state(app, identifier) if identifier else {}
            invalid = bool(state.get("enabled"))
        else:
            identity = principal(shop, identifier)
            state = security_state(app, identifier)
            invalid = (not identity or session.get("admin_credentials") != credential_stamp(app, identity)
                       or session.get("admin_factor_version", "") != state.get("version", ""))
        if invalid:
            session.clear()
            flash("Please log in again to verify your account security.", "info")
            return redirect(url_for("login_form"))

    @app.after_request
    def private_admin_responses(response):
        if not (request.endpoint or "").startswith("buyer_api_"):
            response.headers["Cache-Control"] = "no-store"
            response.headers["Referrer-Policy"] = "no-referrer"
            response.headers["X-Frame-Options"] = "DENY"
        return response

    @app.context_processor
    def security_context():
        return {"admin_mount_path": request.environ.get("webadmin.mount", ""),
                "admin_base_path": request.environ.get("webadmin.base", ""),
                "admin_path_override": bool(os.getenv("ADMIN_PANEL_PATH", "").strip()),
                "admin_domain_override": os.getenv(RECOVERY_ENV, "").strip() == "1"}

    @app.get("/login")
    def login_form():
        if shop._is_logged_in():
            return redirect(url_for(shop.role_home_endpoint()))
        return render_template("login.html")

    @app.post("/login")
    @shop.csrf_required
    def login_submit():
        username, password = request.form.get("username", "").strip(), request.form.get("password", "")
        if locked_seconds(app, username):
            flash("Too many login attempts. Try again in 15 minutes.", "error")
            return redirect(url_for("login_form"))
        owner = principal(shop, "owner")
        identity = owner if owner and hmac.compare_digest(username.encode(), owner["username"].encode()) and password_ok(owner, password) else None
        if not identity:
            for account in shop.get_admin_accounts(app.db):
                if account.get("enabled", True) and account["username"].lower() == username.lower():
                    candidate = principal(shop, "staff:" + account["id"])
                    if password_ok(candidate, password):
                        identity = candidate
                    break
        if not identity:
            failed_attempt(app, username)
            flash("Invalid username or password.", "error")
            return redirect(url_for("login_form"))
        state = security_state(app, identity["id"])
        if not state.get("enabled"):
            return complete_login(app, shop, identity, state)
        session.clear()
        token = secrets.token_urlsafe(32)
        session.update(admin_login_challenge=token, csrf=secrets.token_urlsafe(32))
        app.db.admin_login_challenges.insert_one({"_id": digest(token), "principal": identity["id"],
            "credentials": credential_stamp(app, identity), "version": state["version"], "attempts": 0,
            "expires_at": now() + timedelta(minutes=5)})
        return redirect(url_for("login_two_factor"))

    def pending_login():
        token = session.get("admin_login_challenge", "")
        challenge = app.db.admin_login_challenges.find_one({"_id": digest(token), "expires_at": {"$gt": now()}, "attempts": {"$lt": 5}}) if token else None
        identity = principal(shop, challenge["principal"]) if challenge else None
        state = security_state(app, identity["id"]) if identity else {}
        if (not identity or not state.get("enabled") or state.get("version") != challenge["version"]
                or challenge["credentials"] != credential_stamp(app, identity)):
            session.pop("admin_login_challenge", None)
            return None, None, None
        return challenge, identity, state

    @app.get("/login/authenticator")
    def login_two_factor():
        challenge, _, _ = pending_login()
        if not challenge:
            flash("Your login expired. Enter your username and password again.", "info")
            return redirect(url_for("login_form"))
        return render_template("login_two_factor.html")

    @app.post("/login/authenticator")
    @shop.csrf_required
    def login_two_factor_submit():
        challenge, identity, state = pending_login()
        if not challenge:
            return redirect(url_for("login_form"))
        if locked_seconds(app, identity["username"]):
            session.clear()
            flash("Too many login attempts. Try again in 15 minutes.", "error")
            return redirect(url_for("login_form"))
        attempt = app.db.admin_login_challenges.update_one({"_id": challenge["_id"], "attempts": {"$lt": 5}, "expires_at": {"$gt": now()}}, {"$inc": {"attempts": 1}})
        if attempt.modified_count and consume_factor(app, state, request.form.get("code")):
            if app.db.admin_login_challenges.delete_one({"_id": challenge["_id"]}).deleted_count:
                return complete_login(app, shop, identity, state)
        failed_attempt(app, identity["username"])
        flash("Code is invalid, expired or already used. Use the next code or an unused recovery code.", "error")
        return redirect(url_for("login_two_factor"))

    def current_identity():
        identifier = session.get("admin_principal")
        if not identifier and shop.is_owner_role():
            identifier = "owner"
        identity = principal(shop, identifier)
        if not identity:
            abort(403)
        return identity

    def reauthenticate(identity, require_factor=True):
        if locked_seconds(app, identity["username"]):
            flash("Too many verification attempts. Try again in 15 minutes.", "error")
            return False
        state = security_state(app, identity["id"])
        valid = password_ok(identity, request.form.get("current_password", ""))
        if valid and require_factor and state.get("enabled"):
            valid = consume_factor(app, state, request.form.get("code"))
        if not valid:
            failed_attempt(app, identity["username"])
            flash("Could not verify your password and security code. Wait for a fresh authenticator code if the last code was already used.", "error")
        return valid

    def render_security(identity, **extra):
        state = security_state(app, identity["id"])
        setup = None
        token = session.get("admin_setup_token", "")
        if token and not state.get("enabled"):
            setup = app.db.admin_auth_setups.find_one({"_id": digest(token), "principal": identity["id"], "expires_at": {"$gt": now()}})
        qr, manual_key = "", ""
        if setup:
            manual_key = cipher(app).decrypt(setup["secret"].encode()).decode()
            uri = pyotp.TOTP(manual_key).provisioning_uri(name=identity["username"], issuer_name="Smokey Shop Admin")
            output = io.BytesIO()
            qrcode.make(uri).save(output, format="PNG")
            qr = base64.b64encode(output.getvalue()).decode()
        return render_template("admin_security.html", security=state, identity=identity, qr=qr,
                               manual_key=manual_key, recovery_count=len(state.get("recovery_hashes", [])), **extra)

    @app.get("/account-security")
    @shop.login_required
    def admin_security():
        return render_security(current_identity())

    @app.post("/account-security/setup")
    @shop.login_required
    @shop.csrf_required
    def admin_2fa_start():
        identity = current_identity()
        state = security_state(app, identity["id"])
        if state.get("enabled"):
            flash("Disable your existing authenticator with a valid code before replacing it.", "info")
        elif reauthenticate(identity, require_factor=False):
            if not state:
                app.db.admin_security.update_one({"_id": identity["id"]}, {"$setOnInsert": {
                    "enabled": False, "version": secrets.token_hex(16)}}, upsert=True)
            state = security_state(app, identity["id"])
            old = session.pop("admin_setup_token", "")
            if old:
                app.db.admin_auth_setups.delete_one({"_id": digest(old)})
            token = secrets.token_urlsafe(32)
            session["admin_setup_token"] = token
            # Upgrade legacy cookies before exposing any new authenticator material.
            session.update(admin_principal=identity["id"], admin_credentials=credential_stamp(app, identity),
                           admin_factor_version=state["version"])
            app.db.admin_auth_setups.insert_one({"_id": digest(token), "principal": identity["id"],
                "credentials": credential_stamp(app, identity), "version": state["version"],
                "secret": cipher(app).encrypt(pyotp.random_base32().encode()).decode(),
                "expires_at": now() + timedelta(minutes=10)})
        return redirect(url_for("admin_security"))

    @app.post("/account-security/enable")
    @shop.login_required
    @shop.csrf_required
    def admin_2fa_enable():
        identity = current_identity()
        token = session.get("admin_setup_token", "")
        setup = app.db.admin_auth_setups.find_one({"_id": digest(token), "principal": identity["id"], "expires_at": {"$gt": now()}}) if token else None
        if not setup or setup["credentials"] != credential_stamp(app, identity):
            flash("Authenticator setup expired. Start setup again.", "error")
            return redirect(url_for("admin_security"))
        code = request.form.get("code", "").strip()
        secret = cipher(app).decrypt(setup["secret"].encode()).decode()
        step = int(time.time()) // 30
        accepted = next((candidate for candidate in (step, step-1, step+1)
                         if re.fullmatch(r"[0-9]{6}", code) and hmac.compare_digest(pyotp.TOTP(secret).at(candidate * 30), code)), None)
        if locked_seconds(app, identity["username"]) or accepted is None:
            failed_attempt(app, identity["username"])
            flash("Authenticator code is invalid or expired.", "error")
            return redirect(url_for("admin_security"))
        codes, hashes = recovery_codes()
        version = secrets.token_hex(16)
        result = app.db.admin_security.update_one({"_id": identity["id"], "enabled": {"$ne": True}, "version": setup["version"]},
            {"$set": {"enabled": True, "secret": setup["secret"], "last_step": accepted,
                      "version": version, "recovery_hashes": hashes, "enabled_at": now()}})
        if not result.modified_count:
            flash("Account security changed. Start setup again.", "error")
            return redirect(url_for("admin_security"))
        app.db.admin_auth_setups.delete_one({"_id": setup["_id"]})
        session.pop("admin_setup_token", None)
        session["admin_factor_version"] = version
        clear_attempts(app, identity["username"])
        flash("Two-factor authentication is enabled. Save your recovery codes now.", "success")
        return render_security(identity, recovery_codes=codes)

    @app.post("/account-security/disable")
    @shop.login_required
    @shop.csrf_required
    def admin_2fa_disable():
        identity = current_identity()
        state = security_state(app, identity["id"])
        if state.get("enabled") and reauthenticate(identity):
            version = secrets.token_hex(16)
            result = app.db.admin_security.update_one({"_id": identity["id"], "version": state["version"]},
                {"$set": {"enabled": False, "version": version}, "$unset": {"secret": "", "last_step": "", "recovery_hashes": ""}})
            if result.modified_count:
                session["admin_factor_version"] = version
                flash("Two-factor authentication is disabled for your account.", "info")
        return redirect(url_for("admin_security"))

    @app.post("/account-security/recovery-codes")
    @shop.login_required
    @shop.csrf_required
    def admin_2fa_recovery():
        identity = current_identity()
        state = security_state(app, identity["id"])
        if state.get("enabled") and reauthenticate(identity):
            codes, hashes = recovery_codes()
            version = secrets.token_hex(16)
            result = app.db.admin_security.update_one({"_id": identity["id"], "version": state["version"]},
                {"$set": {"recovery_hashes": hashes, "version": version}})
            if result.modified_count:
                session["admin_factor_version"] = version
                flash("New recovery codes created. All previous recovery codes are invalid.", "success")
                return render_security(identity, recovery_codes=codes)
        return redirect(url_for("admin_security"))

    @app.post("/admin-security/domains")
    @shop.login_required
    @shop.owner_required
    @shop.csrf_required
    def admin_domains_submit():
        target = url_for("secret_settings_form") + "#secret-security"
        try:
            domains = validate_domains({
                "admin_panel_url": request.form.get("admin_panel_url", ""),
                "buyer_api_endpoint_url": request.form.get("buyer_api_endpoint_url", ""),
                "domain_separation_enabled": request.form.get("domain_separation_enabled", "0"),
            })
            if (domains["domain_separation_enabled"] == "1"
                    and origin_hostname(domains["admin_panel_url"]) != hostname(request.environ.get("HTTP_HOST"))):
                raise ValueError("Open this panel on the configured admin domain before enabling separation. Connect its DNS and verify HTTPS first. To change an active admin domain, disable separation, open the new domain, then enable it again.")
        except ValueError as error:
            flash(str(error), "error")
            return redirect(target)
        if not reauthenticate(current_identity()):
            return redirect(target)
        # One atomic update; general settings saves cannot overwrite these fields.
        app.db.settings.update_one({"key": "secret_settings"}, {"$set": {
            **{"value." + key: value for key, value in domains.items()}, "updated_at": now()}}, upsert=True)
        flash("Domain settings saved. Separate admin/API domains are "
              + ("enabled." if domains["domain_separation_enabled"] == "1" else "disabled.")
              + " The bot updates its advertised API URL shortly; DNS is configured separately in Railway.", "success")
        return redirect(target)

    @app.post("/admin-security/path")
    @shop.login_required
    @shop.owner_required
    @shop.csrf_required
    def admin_path_submit():
        try:
            new_path = normalize_path(request.form.get("admin_panel_path"))
        except ValueError as error:
            flash(str(error), "error")
            return redirect(url_for("secret_settings_form") + "#secret-security")
        if os.getenv("ADMIN_PANEL_PATH", "").strip():
            flash("ADMIN_PANEL_PATH in .env overrides this setting. Remove that override and restart WebAdmin to manage the path here.", "error")
            return redirect(url_for("secret_settings_form") + "#secret-security")
        if not reauthenticate(current_identity()):
            return redirect(url_for("secret_settings_form") + "#secret-security")
        app.db.settings.update_one({"key": "secret_settings"}, {"$set": {
            "value.admin_panel_path": new_path, "updated_at": now()}}, upsert=True)
        flash("Admin path saved. Update your bookmark; the previous path is now closed.", "success")
        return redirect(request.environ.get("webadmin.base", "") + new_path + "/secret-settings#secret-security")
