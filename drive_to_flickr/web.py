"""Authenticated web administration UI."""
from __future__ import annotations

import secrets as pysecrets
from functools import wraps

from flask import Flask, abort, flash, redirect, render_template, request, session, url_for
from requests_oauthlib import OAuth1Session
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash

from .config import load_settings
from .database import Database, now
from .flickr import ACCESS_TOKEN_URL, AUTHORIZE_URL, REQUEST_TOKEN_URL, FlickrClient
from .google_ui import GoogleOAuthConfigError, account_email, configured as google_oauth_configured, flow_for, list_calendars, list_folders, test_calendar, test_folder
from .models import Status
from .secrets import SecretStore
from .settings_store import SettingsStore, validate_settings


def create_app() -> Flask:
    settings = load_settings(require_credentials=False)
    app = Flask(__name__)
    app.secret_key = settings.web_secret_key or pysecrets.token_hex(32)
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)
    db = Database(settings.database_path)
    store = SettingsStore(db)
    secret_store = SecretStore(settings.secret_store_path)

    def csrf() -> str:
        session.setdefault("csrf", pysecrets.token_urlsafe(32))
        return str(session["csrf"])

    @app.context_processor
    def inject_globals():
        return {"csrf_token": csrf, "app_name": "Drive to Flickr", "app_subtitle": "Automated Google Drive → Flickr media publishing"}

    def check_csrf() -> None:
        if request.method == "POST" and request.form.get("csrf") != session.get("csrf"):
            abort(403)

    def login_required(fn):
        @wraps(fn)
        def wrapper(*a, **kw):
            if not settings.admin_password_hash:
                return render_template("bootstrap_required.html"), 503
            if not session.get("auth"):
                return redirect(url_for("login"))
            check_csrf()
            return fn(*a, **kw)
        return wrapper

    def google_callback_url() -> str:
        base = (store.get("PUBLIC_BASE_URL") or settings.public_base_url).strip().rstrip("/")
        if base:
            return base + url_for("google_callback")
        return url_for("google_callback", _external=True)

    def flickr_callback_url() -> str:
        base = (store.get("PUBLIC_BASE_URL") or settings.public_base_url).strip().rstrip("/")
        if base:
            return base + url_for("flickr_callback")
        return url_for("flickr_callback", _external=True)

    def callback_warnings(callback: str) -> list[str]:
        if callback.startswith("http://") and not callback.startswith(("http://localhost", "http://127.0.0.1")):
            return ["Google OAuth usually requires HTTPS callback URLs unless you are using localhost for local development."]
        return []

    def friendly_google_error(exc: Exception) -> str:
        text = str(exc)
        if "accessNotConfigured" in text or "SERVICE_DISABLED" in text or "it is disabled" in text:
            if "drive.googleapis.com" in text:
                return "Google Drive API is disabled for this Google Cloud project. Enable the Google Drive API in Google Cloud Console, wait a few minutes, then refresh this page."
            if "calendar-json.googleapis.com" in text or "calendar/v3" in text:
                return "Google Calendar API is disabled for this Google Cloud project. Enable the Google Calendar API in Google Cloud Console, wait a few minutes, then refresh this page."
            return "A required Google API is disabled for this Google Cloud project. Enable it in Google Cloud Console, wait a few minutes, then try again."
        if "redirect_uri_mismatch" in text:
            return "Google rejected the callback URL (redirect_uri_mismatch). Copy the exact OAuth Callback URL shown here into the Google Cloud Console Authorized Redirect URIs."
        if "access_denied" in text:
            return "Google authorization was denied. Click Connect Google Account and approve the requested permissions to continue."
        if isinstance(exc, GoogleOAuthConfigError):
            return text
        return text or "Google OAuth could not be completed. Check the OAuth application configuration and try again."

    def dashboard_data() -> dict:
        vals = store.all_public()
        google_connected = secret_store.has("google_token_json")
        flickr_connected = secret_store.has("flickr_oauth_token")
        return {"vals": vals, "google_connected": google_connected, "flickr_connected": flickr_connected,
                "drive_connected": bool(vals.get("GOOGLE_DRIVE_FOLDER_ID")), "calendar_connected": bool(vals.get("GOOGLE_CALENDAR_ID")),
                "worker_status": "Running" if vals.get("WORKER_HEARTBEAT") else "Unknown"}

    def upload_metrics() -> dict[str, int]:
        counts = {status: count for status, count in db.status_counts()}
        with db.connect() as conn:
            today = conn.execute("SELECT COUNT(*) c FROM processed_files WHERE status=? AND date(updated_at)=date('now')", (Status.COMPLETE,)).fetchone()["c"]
            week = conn.execute("SELECT COUNT(*) c FROM processed_files WHERE status=? AND datetime(updated_at)>=datetime('now','-7 days')", (Status.COMPLETE,)).fetchone()["c"]
        pending = sum(counts.get(s, 0) for s in [Status.DISCOVERED, Status.DOWNLOADING, Status.DOWNLOADED, Status.MATCHING_CALENDAR, Status.UPLOADING, Status.UPLOADED, Status.ALBUM_ASSIGNED, Status.RETRY])
        return {"Uploads Today": today, "Uploads This Week": week, "Pending": pending, "Failed": counts.get(Status.FAILED, 0), "Manual Review": counts.get(Status.MANUAL_REVIEW, 0), "Total Uploaded": counts.get(Status.COMPLETE, 0)}

    def recent_activity() -> list[dict]:
        with db.connect() as conn:
            return [dict(r) for r in conn.execute("SELECT google_drive_name, media_captured_at, calendar_event_title, flickr_photoset_id, status, updated_at FROM processed_files ORDER BY updated_at DESC LIMIT 10")]

    def config_warnings() -> list[dict[str, str]]:
        d = dashboard_data(); vals = d["vals"]
        warnings = []
        if not google_oauth_configured(secret_store, settings.google_credentials_file): warnings.append({"title": "Google OAuth Not Configured", "message": "Google OAuth not configured", "detail": "Add your Google OAuth Client ID and Client Secret before connecting Google services.", "action": "Configure OAuth", "href": url_for("google_account")})
        if not d["google_connected"]: warnings.append({"title": "Google Account Not Connected", "message": "Google account is not connected.", "detail": "Connect a Google account to browse shared Drive folders and Calendars.", "action": "Connect Google", "href": url_for("google_account")})
        if not vals.get("GOOGLE_DRIVE_FOLDER_ID"): warnings.append({"title": "Drive Folder Missing", "message": "No Google Drive folder has been selected.", "detail": "Choose the Drive folder that Drive to Flickr should watch for new media.", "action": "Select Drive Folder", "href": url_for("drive_page")})
        if not vals.get("GOOGLE_CALENDAR_ID"): warnings.append({"title": "Calendar Missing", "message": "No Calendar has been selected.", "detail": "Select the Google Calendar used to map capture times to Flickr albums.", "action": "Select Calendar", "href": url_for("calendar_page")})
        if not d["flickr_connected"]: warnings.append({"title": "Flickr Disconnected", "message": "Flickr has not been connected.", "detail": "Connect Flickr so processed Drive media can be published to albums.", "action": "Connect Flickr", "href": url_for("flickr_page")})
        if d["worker_status"] != "Running": warnings.append({"title": "Worker Status Unknown", "message": "Worker status is not confirmed as running.", "detail": "Restart or verify the background service if publishing has stopped.", "action": "View Status", "href": url_for("dashboard")})
        return warnings

    @app.get("/login")
    def login():
        return render_template("login.html")

    @app.post("/login")
    def do_login():
        check_csrf()
        if request.form.get("username") == settings.admin_username and check_password_hash(settings.admin_password_hash, request.form.get("password", "")):
            session["auth"] = True
            return redirect(url_for("dashboard"))
        abort(403)

    @app.get("/")
    @login_required
    def dashboard():
        return render_template("dashboard.html", **dashboard_data(), metrics=upload_metrics(), activity=recent_activity(), warnings=config_warnings())

    @app.route("/settings", methods=["GET", "POST"])
    @login_required
    def settings_page():
        keys = ["PUBLIC_BASE_URL","TIMEZONE","POLL_INTERVAL_SECONDS","MINIMUM_FILE_AGE_SECONDS","MAX_ATTEMPTS","LOG_LEVEL","BUFFER_BEFORE_MINUTES","BUFFER_AFTER_MINUTES","REQUIRE_FLICKR_MARKER","NO_EVENT_ACTION","UNASSIGNED_ALBUM","DRIVE_SUCCESS_ACTION","DRIVE_SUCCESS_FOLDER","DRIVE_FAILED_FOLDER","FLICKR_DEFAULT_PRIVACY","GLOBAL_TAGS"]
        if request.method == "POST":
            vals = {k: request.form.get(k, "") for k in keys}; vals["REQUIRE_FLICKR_MARKER"] = "true" if request.form.get("REQUIRE_FLICKR_MARKER") else "false"
            errs = validate_settings(store.all_public() | vals)
            if errs: flash("; ".join(errs), "error")
            else: store.update(vals); flash("Settings saved", "success")
        return render_template("settings/general.html", vals=store.all_public())

    @app.get("/settings/google-account")
    @login_required
    def google_account():
        callback = google_callback_url()
        return render_template("settings/google_account.html", vals=store.all_public(), callback=callback, callback_warnings=callback_warnings(callback), app_configured=google_oauth_configured(secret_store, settings.google_credentials_file), google_connected=secret_store.has("google_token_json"), client_id=secret_store.get("google_client_id"), has_secret=secret_store.has("google_client_secret"))

    @app.post("/settings/google-account/oauth-settings")
    @login_required
    def google_oauth_settings_save():
        client_id = request.form.get("client_id", "").strip(); client_secret = request.form.get("client_secret", "").strip()
        if not client_id: flash("Missing Client ID", "error")
        elif not client_secret and not secret_store.has("google_client_secret"): flash("Missing Client Secret", "error")
        else:
            secret_store.set("google_client_id", client_id)
            if client_secret: secret_store.set("google_client_secret", client_secret)
            flash("Google OAuth application settings saved", "success")
        return redirect(url_for("google_account"))

    @app.post("/settings/google-account/oauth-settings/clear")
    @login_required
    def google_oauth_settings_clear(): secret_store.delete("google_client_id", "google_client_secret"); flash("Google OAuth application settings cleared", "success"); return redirect(url_for("google_account"))

    @app.post("/settings/google-account/connect")
    @login_required
    def google_connect():
        try:
            state = pysecrets.token_urlsafe(24); db.save_oauth_state(state, "google")
            flow = flow_for(settings.google_credentials_file, secret_store, google_callback_url(), state)
            auth_url, _ = flow.authorization_url(access_type="offline", include_granted_scopes="true", prompt="consent")
            return redirect(auth_url)
        except Exception as exc: flash(friendly_google_error(exc), "error"); return redirect(url_for("google_account"))

    @app.get("/oauth/google/callback")
    @login_required
    def google_callback():
        if request.args.get("error"):
            flash(friendly_google_error(Exception(request.args.get("error_description") or request.args.get("error", ""))), "error"); return redirect(url_for("google_account"))
        ok, _ = db.pop_oauth_state(request.args.get("state", ""), "google")
        if not ok: abort(403)
        try:
            flow = flow_for(settings.google_credentials_file, secret_store, google_callback_url(), request.args.get("state", "")); flow.fetch_token(authorization_response=request.url)
            secret_store.set("google_token_json", flow.credentials.to_json()); store.set("GOOGLE_ACCOUNT_EMAIL", account_email(secret_store)); store.set("GOOGLE_LAST_API_CHECK", now()); flash("Google account connected", "success")
        except Exception as exc: flash(friendly_google_error(exc), "error")
        return redirect(url_for("google_account"))

    @app.post("/settings/google-account/test")
    @login_required
    def google_test():
        try: store.set("GOOGLE_ACCOUNT_EMAIL", account_email(secret_store)); store.set("GOOGLE_LAST_API_CHECK", now()); flash("Google API check succeeded", "success")
        except Exception as exc: flash(friendly_google_error(exc), "error")
        return redirect(url_for("google_account"))

    @app.post("/settings/google-account/disconnect")
    @login_required
    def google_disconnect(): secret_store.delete("google_token_json", "google_refresh_token"); store.set("GOOGLE_ACCOUNT_EMAIL", ""); flash("Google disconnected", "success"); return redirect(url_for("google_account"))

    @app.get("/settings/google-drive")
    @login_required
    def drive_page():
        parent = request.args.get("parent") or None
        folders = []
        if secret_store.has("google_token_json"):
            try:
                folders = list_folders(secret_store, parent)
            except Exception as exc:
                app.logger.warning("Unable to list Google Drive folders: %s", exc)
                flash(friendly_google_error(exc), "error")
        return render_template("settings/google_drive.html", vals=store.all_public(), folders=folders, parent=parent)

    @app.post("/settings/google-drive/select")
    @login_required
    def drive_select():
        try:
            test_folder(secret_store, request.form["id"])
            store.set("GOOGLE_DRIVE_FOLDER_ID", request.form["id"])
            store.set("GOOGLE_DRIVE_FOLDER_NAME", request.form["name"])
            flash("Drive folder selected", "success")
        except Exception as exc:
            app.logger.warning("Unable to select Google Drive folder: %s", exc)
            flash(friendly_google_error(exc), "error")
        return redirect(url_for("drive_page"))

    @app.post("/settings/google-drive/test")
    @login_required
    def drive_test():
        try:
            meta = test_folder(secret_store, store.get("GOOGLE_DRIVE_FOLDER_ID"))
            flash("Drive folder accessible: " + meta.get("name", ""), "success")
        except Exception as exc:
            app.logger.warning("Unable to access Google Drive folder: %s", exc)
            flash(friendly_google_error(exc), "error")
        return redirect(url_for("drive_page"))

    @app.get("/settings/calendar")
    @login_required
    def calendar_page():
        cals = []
        if secret_store.has("google_token_json"):
            try:
                cals = list_calendars(secret_store)
            except Exception as exc:
                app.logger.warning("Unable to list Google Calendars: %s", exc)
                flash(friendly_google_error(exc), "error")
        return render_template("settings/calendar.html", vals=store.all_public(), calendars=cals)

    @app.post("/settings/calendar/select")
    @login_required
    def cal_select():
        try:
            test_calendar(secret_store, request.form["id"])
            store.set("GOOGLE_CALENDAR_ID", request.form["id"])
            store.set("GOOGLE_CALENDAR_NAME", request.form["name"])
            flash("Calendar selected", "success")
        except Exception as exc:
            app.logger.warning("Unable to select Google Calendar: %s", exc)
            flash(friendly_google_error(exc), "error")
        return redirect(url_for("calendar_page"))

    @app.post("/settings/calendar/test")
    @login_required
    def cal_test():
        try:
            meta = test_calendar(secret_store, store.get("GOOGLE_CALENDAR_ID"))
            flash("Calendar accessible: " + meta.get("summary", ""), "success")
        except Exception as exc:
            app.logger.warning("Unable to access Google Calendar: %s", exc)
            flash(friendly_google_error(exc), "error")
        return redirect(url_for("calendar_page"))

    @app.route("/settings/flickr", methods=["GET", "POST"])
    @login_required
    def flickr_page():
        if request.method == "POST":
            store.set("FLICKR_API_KEY", request.form.get("api_key", ""))
            if request.form.get("api_secret"): secret_store.set("flickr_api_secret", request.form["api_secret"])
            flash("Flickr API settings saved", "success")
        return render_template("settings/flickr.html", vals=store.all_public(), flickr_connected=secret_store.has("flickr_oauth_token"), has_secret=secret_store.has("flickr_api_secret"))

    @app.post("/settings/flickr/connect")
    @login_required
    def flickr_connect():
        oauth = OAuth1Session(store.get("FLICKR_API_KEY"), client_secret=secret_store.get("flickr_api_secret"), callback_uri=flickr_callback_url())
        tok = oauth.fetch_request_token(REQUEST_TOKEN_URL); state = pysecrets.token_urlsafe(24); db.save_oauth_state(state, "flickr", tok["oauth_token_secret"]); session["flickr_state"] = state
        return redirect(oauth.authorization_url(AUTHORIZE_URL, perms="write"))

    @app.get("/oauth/flickr/callback")
    @login_required
    def flickr_callback():
        state = session.pop("flickr_state", ""); ok, verifier_secret = db.pop_oauth_state(state, "flickr")
        if not ok or not verifier_secret: abort(403)
        oauth = OAuth1Session(store.get("FLICKR_API_KEY"), client_secret=secret_store.get("flickr_api_secret"), resource_owner_key=request.args["oauth_token"], resource_owner_secret=verifier_secret, verifier=request.args["oauth_verifier"])
        access = oauth.fetch_access_token(ACCESS_TOKEN_URL); secret_store.set("flickr_oauth_token", access["oauth_token"]); secret_store.set("flickr_oauth_token_secret", access["oauth_token_secret"]); store.set("FLICKR_USERNAME", access.get("username", "")); store.set("FLICKR_LAST_API_CHECK", now()); flash("Flickr connected", "success"); return redirect(url_for("flickr_page"))

    @app.post("/settings/flickr/test")
    @login_required
    def flickr_test(): FlickrClient(store.get("FLICKR_API_KEY"), secret_store.get("flickr_api_secret"), secret_store.get("flickr_oauth_token"), secret_store.get("flickr_oauth_token_secret")).list_photosets(); store.set("FLICKR_LAST_API_CHECK", now()); flash("Flickr API check succeeded", "success"); return redirect(url_for("flickr_page"))

    @app.post("/settings/flickr/disconnect")
    @login_required
    def flickr_disconnect(): secret_store.delete("flickr_oauth_token", "flickr_oauth_token_secret"); store.set("FLICKR_USERNAME", ""); flash("Flickr disconnected", "success"); return redirect(url_for("flickr_page"))

    @app.get("/setup")
    @login_required
    def setup():
        return render_template("setup/index.html", vals=store.all_public(), callback=google_callback_url(), app_configured=google_oauth_configured(secret_store, settings.google_credentials_file), google_connected=secret_store.has("google_token_json"), flickr_connected=secret_store.has("flickr_oauth_token"))

    @app.post("/setup/finish")
    @login_required
    def setup_finish():
        if not (secret_store.has("google_token_json") and store.get("GOOGLE_DRIVE_FOLDER_ID") and store.get("GOOGLE_CALENDAR_ID") and secret_store.has("flickr_oauth_token")):
            flash("Setup cannot be marked complete until Google, Drive, Calendar, and Flickr are configured.", "error"); return redirect(url_for("setup"))
        store.set("SETUP_COMPLETE", "true"); flash("Setup complete", "success"); return redirect(url_for("dashboard"))

    @app.post("/setup/test-scan")
    @login_required
    def setup_scan(): flash("Run `drive-to-flickr dry-run` on the server to perform a test scan.", "info"); return redirect(url_for("setup"))

    return app


def main() -> None:
    settings = load_settings(require_credentials=False)
    create_app().run(host=settings.web_bind, port=settings.web_port)
