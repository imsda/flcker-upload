"""Authenticated web administration UI."""
from __future__ import annotations

import json
import logging
import os
import secrets as pysecrets
import threading
import time
from datetime import datetime, timedelta
from functools import wraps
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from flask import Flask, abort, flash, g, redirect, render_template, request, session, url_for
from requests_oauthlib import OAuth1Session
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash

from .config import load_settings
from .database import Database, now
from .flickr import ACCESS_TOKEN_URL, AUTHORIZE_URL, REQUEST_TOKEN_URL, FlickrClient
from .google_ui import GoogleOAuthConfigError, account_email, configured as google_oauth_configured, flow_for, list_calendars, list_folders, test_calendar, test_folder
from .health import worker_health
from .metadata import detect_media_kind
from .models import Status
from .secrets import SecretStore
from .settings_store import SettingsStore, validate_settings
from .sources import LocalFolderSource, SMBSource, clean_relative, friendly_smb_error, parse_smb_location

LOGGER = logging.getLogger(__name__)

LOGIN_MAX_FAILURES = 5
LOGIN_LOCKOUT_SECONDS = 15 * 60
LOCAL_BROWSE_LIMIT = 500


def default_browse_path() -> str:
    for candidate in ("/mnt", "/media", "/srv"):
        if Path(candidate).is_dir():
            return candidate
    return "/"


def browse_local(path: str) -> tuple[list[dict[str, str]], str, str | None]:
    """Subfolders of ``path`` for the folder picker, plus an error message and parent path."""
    target = Path(os.path.normpath(path or "/"))
    if not target.is_absolute():
        return [], "Enter an absolute path such as /mnt/nas/photos.", None
    parent = str(target.parent) if target.parent != target else None
    try:
        entries = sorted((e for e in os.scandir(target) if e.is_dir() and not e.name.startswith((".", "@"))), key=lambda e: e.name.lower())
    except FileNotFoundError:
        return [], f"{target} does not exist on this server.", parent
    except PermissionError:
        return [], f"The service account cannot read {target}.", parent
    except NotADirectoryError:
        return [], f"{target} is not a folder.", parent
    return [{"name": e.name, "path": e.path} for e in entries[:LOCAL_BROWSE_LIMIT]], "", parent


def source_configured(vals: dict[str, str]) -> bool:
    if vals.get("SOURCE_TYPE") == "local":
        return bool(vals.get("LOCAL_SOURCE_PATH"))
    if vals.get("SOURCE_TYPE") == "smb":
        return bool(vals.get("SMB_SERVER") and vals.get("SMB_SHARE"))
    return bool(vals.get("GOOGLE_DRIVE_FOLDER_ID"))


def smb_display(vals: dict[str, str]) -> str:
    if not (vals.get("SMB_SERVER") and vals.get("SMB_SHARE")):
        return ""
    folder = vals.get("SMB_FOLDER", "").strip("/").replace("/", "\\")
    return "\\\\" + vals["SMB_SERVER"] + "\\" + vals["SMB_SHARE"] + ("\\" + folder if folder else "")


def source_name(vals: dict[str, str]) -> str:
    if vals.get("SOURCE_TYPE") == "local":
        return vals.get("LOCAL_SOURCE_PATH") or ""
    if vals.get("SOURCE_TYPE") == "smb":
        return smb_display(vals)
    return vals.get("GOOGLE_DRIVE_FOLDER_NAME") or vals.get("GOOGLE_DRIVE_FOLDER_ID") or ""


def create_app() -> Flask:
    settings = load_settings(require_credentials=False)
    app = Flask(__name__)
    if not settings.web_secret_key:
        LOGGER.warning("WEB_SECRET_KEY is not set; using a temporary key, so logins will reset whenever the web UI restarts")
    app.secret_key = settings.web_secret_key or pysecrets.token_hex(32)
    app.config.update(
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        # Enable when the UI is only reached over HTTPS (recommended behind a reverse proxy).
        SESSION_COOKIE_SECURE=os.getenv("SESSION_COOKIE_SECURE", "").strip().lower() in {"1", "true", "yes", "on"},
        PERMANENT_SESSION_LIFETIME=timedelta(hours=12),
    )
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)
    db = Database(settings.database_path)
    store = SettingsStore(db)
    secret_store = SecretStore(settings.secret_store_path)
    login_failures: dict[str, list[float]] = {}
    login_lock = threading.Lock()

    def recent_failures(ip: str) -> list[float]:
        cutoff = time.monotonic() - LOGIN_LOCKOUT_SECONDS
        with login_lock:
            attempts = [t for t in login_failures.get(ip, []) if t > cutoff]
            login_failures[ip] = attempts
            return attempts

    def csrf() -> str:
        session.setdefault("csrf", pysecrets.token_urlsafe(32))
        return str(session["csrf"])

    @app.context_processor
    def inject_globals():
        return {"csrf_token": csrf, "app_name": "Drive to Flickr", "app_subtitle": "Automated Google Drive / NAS → Flickr media publishing"}

    @app.template_filter("local_datetime")
    def local_datetime(value: str | None) -> str:
        if not value:
            return "—"
        try:
            parsed = datetime.fromisoformat(value)
            if "display_timezone" not in g:
                g.display_timezone = ZoneInfo(store.get("TIMEZONE", "UTC"))
            timezone = g.display_timezone
            return parsed.astimezone(timezone).strftime("%Y-%m-%d %I:%M:%S %p %Z")
        except (ValueError, ZoneInfoNotFoundError):
            return value

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
        try:
            poll_interval = int(vals.get("POLL_INTERVAL_SECONDS", "120"))
        except ValueError:
            poll_interval = 120
        try:
            display_timezone = ZoneInfo(vals.get("TIMEZONE", "UTC"))
        except ZoneInfoNotFoundError:
            display_timezone = ZoneInfo("UTC")
        health = worker_health(vals.get("WORKER_HEARTBEAT", ""), poll_interval, display_timezone=display_timezone)
        return {"vals": vals, "google_connected": google_connected, "flickr_connected": flickr_connected,
                "drive_connected": bool(vals.get("GOOGLE_DRIVE_FOLDER_ID")), "calendar_connected": bool(vals.get("GOOGLE_CALENDAR_ID")),
                "source_configured": source_configured(vals), "source_name": source_name(vals), "source_is_local": vals.get("SOURCE_TYPE") == "local", "source_type": vals.get("SOURCE_TYPE") or "google_drive",
                "worker_health": health}

    def upload_metrics() -> dict[str, int]:
        counts = {status: count for status, count in db.status_counts()}
        with db.connect() as conn:
            today = conn.execute("SELECT COUNT(*) c FROM processed_files WHERE status=? AND date(updated_at)=date('now')", (Status.COMPLETE,)).fetchone()["c"]
            week = conn.execute("SELECT COUNT(*) c FROM processed_files WHERE status=? AND datetime(updated_at)>=datetime('now','-7 days')", (Status.COMPLETE,)).fetchone()["c"]
        pending = sum(counts.get(s, 0) for s in [Status.DISCOVERED, Status.DOWNLOADING, Status.DOWNLOADED, Status.MATCHING_CALENDAR, Status.UPLOADING, Status.UPLOADED, Status.ALBUM_ASSIGNED, Status.RETRY])
        return {"Uploads Today": today, "Uploads This Week": week, "Pending": pending, "Failed": counts.get(Status.FAILED, 0), "Manual Review": counts.get(Status.MANUAL_REVIEW, 0), "Total Uploaded": counts.get(Status.COMPLETE, 0)}

    def recent_activity() -> list[dict]:
        with db.connect() as conn:
            return [dict(r) for r in conn.execute("SELECT google_drive_file_id, google_drive_name, media_captured_at, calendar_event_title, flickr_photoset_id, status, attempts, last_error, updated_at FROM processed_files ORDER BY updated_at DESC LIMIT 10")]

    def config_warnings(d: dict) -> list[dict[str, str]]:
        vals = d["vals"]
        warnings = []
        if not google_oauth_configured(secret_store, settings.google_credentials_file): warnings.append({"title": "Google OAuth Not Configured", "message": "Google OAuth not configured", "detail": "Add your Google OAuth Client ID and Client Secret before connecting Google services.", "action": "Configure OAuth", "href": url_for("google_account")})
        if not d["google_connected"]: warnings.append({"title": "Google Account Not Connected", "message": "Google account is not connected.", "detail": "Connect a Google account to browse shared Drive folders and Calendars.", "action": "Connect Google", "href": url_for("google_account")})
        if vals.get("SOURCE_TYPE") == "smb" and not d["source_configured"]: warnings.append({"title": "Network Share Missing", "message": "No NAS share has been set.", "detail": "Enter the NAS server, share name and login so Drive to Flickr can read new media.", "action": "Set Media Source", "href": url_for("source_page")})
        if d["source_is_local"] and not vals.get("LOCAL_SOURCE_PATH"): warnings.append({"title": "Local Folder Missing", "message": "No local/NAS folder has been set.", "detail": "Enter the mounted NAS folder that Drive to Flickr should watch for new media.", "action": "Set Media Source", "href": url_for("source_page")})
        if d["source_type"] == "google_drive" and not vals.get("GOOGLE_DRIVE_FOLDER_ID"): warnings.append({"title": "Drive Folder Missing", "message": "No Google Drive folder has been selected.", "detail": "Choose the Drive folder that Drive to Flickr should watch for new media.", "action": "Select Drive Folder", "href": url_for("drive_page")})
        if not vals.get("GOOGLE_CALENDAR_ID"): warnings.append({"title": "Calendar Missing", "message": "No Calendar has been selected.", "detail": "Select the Google Calendar used to map capture times to Flickr albums.", "action": "Select Calendar", "href": url_for("calendar_page")})
        if not d["flickr_connected"]: warnings.append({"title": "Flickr Disconnected", "message": "Flickr has not been connected.", "detail": "Connect Flickr so processed Drive media can be published to albums.", "action": "Connect Flickr", "href": url_for("flickr_page")})
        if d["worker_health"]["status"] != "Running": warnings.append({"title": "Publisher Worker " + d["worker_health"]["status"], "message": "The publisher worker is not reporting as running.", "detail": "Start or restart the drive-to-flickr worker service. " + d["worker_health"]["detail"], "action": "", "href": ""})
        return warnings

    @app.get("/login")
    def login():
        return render_template("login.html")

    @app.post("/login")
    def do_login():
        check_csrf()
        ip = request.remote_addr or "unknown"
        if len(recent_failures(ip)) >= LOGIN_MAX_FAILURES:
            return render_template("login.html", error="Too many failed sign-in attempts. Wait 15 minutes and try again."), 429
        if settings.admin_password_hash and pysecrets.compare_digest(request.form.get("username", ""), settings.admin_username) and check_password_hash(settings.admin_password_hash, request.form.get("password", "")):
            with login_lock:
                login_failures.pop(ip, None)
            session.clear()  # new session on login so a pre-login session ID can't be reused
            session.permanent = True
            session["auth"] = True
            session["csrf"] = pysecrets.token_urlsafe(32)
            return redirect(url_for("dashboard"))
        with login_lock:
            login_failures.setdefault(ip, []).append(time.monotonic())
        LOGGER.warning("Failed admin sign-in from %s", ip)
        return render_template("login.html", error="Incorrect username or password."), 401

    @app.post("/logout")
    @login_required
    def logout():
        session.clear()
        return redirect(url_for("login"))

    @app.get("/")
    @login_required
    def dashboard():
        data = dashboard_data()
        return render_template("dashboard.html", **data, metrics=upload_metrics(), activity=recent_activity(), warnings=config_warnings(data))

    @app.post("/activity/<file_id>/retry")
    @login_required
    def retry_activity(file_id: str):
        if db.queue_retry(file_id):
            flash("Retry queued. The publisher worker will process the file during its next scan.", "success")
        else:
            flash("This file is not currently eligible for retry.", "error")
        return redirect(url_for("dashboard", _anchor="activity"))

    @app.post("/activity/retry-failed")
    @login_required
    def retry_failed_activity():
        count = db.queue_all_failed()
        if count:
            flash(f"Queued {count} failed files for retry.", "success")
        else:
            flash("There are no failed files to retry.", "info")
        return redirect(url_for("dashboard", _anchor="activity"))

    @app.route("/settings", methods=["GET", "POST"])
    @login_required
    def settings_page():
        keys = ["PUBLIC_BASE_URL","TIMEZONE","POLL_INTERVAL_SECONDS","WORKER_CONCURRENCY","MINIMUM_FILE_AGE_SECONDS","MAX_ATTEMPTS","LOG_LEVEL","BUFFER_BEFORE_MINUTES","BUFFER_AFTER_MINUTES","REQUIRE_FLICKR_MARKER","NO_EVENT_ACTION","UNASSIGNED_ALBUM","DRIVE_SUCCESS_ACTION","DRIVE_SUCCESS_FOLDER","DRIVE_FAILED_FOLDER","FLICKR_DEFAULT_PRIVACY","GLOBAL_TAGS"]
        if request.method == "POST":
            vals = {k: request.form.get(k, "") for k in keys}; vals["REQUIRE_FLICKR_MARKER"] = "true" if request.form.get("REQUIRE_FLICKR_MARKER") else "false"
            errs = validate_settings(store.all_public() | vals)
            if errs: flash("; ".join(errs), "error")
            else: store.update(vals); flash("Settings saved", "success")
        return render_template("settings/general.html", vals=store.all_public())

    @app.route("/settings/source", methods=["GET", "POST"])
    @login_required
    def source_page():
        if request.method == "POST":
            server, share, folder = request.form.get("SMB_SERVER", "").strip(), request.form.get("SMB_SHARE", "").strip(), request.form.get("SMB_FOLDER", "").strip()
            if server.lower().startswith("smb://") or server.startswith(("\\\\", "//")):
                # Accept a pasted address like smb://nas/Photos/Events or \\nas\Photos\Events.
                server, pasted_share, pasted_folder = parse_smb_location(server)
                share, folder = share or pasted_share, folder or pasted_folder
            vals = {
                "SOURCE_TYPE": request.form.get("SOURCE_TYPE", "google_drive"),
                "LOCAL_SOURCE_PATH": request.form.get("LOCAL_SOURCE_PATH", "").strip(),
                "LOCAL_SOURCE_RECURSIVE": "true" if request.form.get("LOCAL_SOURCE_RECURSIVE") else "false",
                "LOCAL_SUCCESS_FOLDER": request.form.get("LOCAL_SUCCESS_FOLDER", "").strip(),
                "LOCAL_FAILED_FOLDER": request.form.get("LOCAL_FAILED_FOLDER", "").strip(),
                "SMB_SERVER": server,
                "SMB_SHARE": share,
                "SMB_FOLDER": folder,
                "SMB_USERNAME": request.form.get("SMB_USERNAME", "").strip(),
                "SMB_PORT": request.form.get("SMB_PORT", "").strip() or "445",
                "SMB_RECURSIVE": "true" if request.form.get("SMB_RECURSIVE") else "false",
                "SMB_SUCCESS_FOLDER": request.form.get("SMB_SUCCESS_FOLDER", "").strip(),
                "SMB_FAILED_FOLDER": request.form.get("SMB_FAILED_FOLDER", "").strip(),
            }
            errs = validate_settings(store.all_public() | vals)
            if errs:
                flash("; ".join(errs), "error")
            else:
                for key in ("SMB_FOLDER", "SMB_SUCCESS_FOLDER", "SMB_FAILED_FOLDER"):
                    vals[key] = clean_relative(vals[key])
                store.update(vals)
                if request.form.get("SMB_PASSWORD"):
                    secret_store.set("smb_password", request.form["SMB_PASSWORD"])
                flash("Media source saved. The worker uses it from its next scan.", "success")
            return redirect(url_for("source_page"))
        vals = store.all_public()
        browse_path = request.args.get("browse") or vals.get("LOCAL_SOURCE_PATH") or default_browse_path()
        local_dirs, browse_error, browse_parent = browse_local(browse_path)
        smb_browse = clean_relative(request.args.get("smb_browse", "")) if ".." not in request.args.get("smb_browse", "") else ""
        smb_dirs: list[dict[str, str]] = []
        smb_error = ""
        if vals.get("SOURCE_TYPE") == "smb" and vals.get("SMB_SERVER") and vals.get("SMB_SHARE"):
            try:
                smb_dirs = smb_source(vals).list_folders(smb_browse)
            except Exception as exc:
                app.logger.warning("Unable to browse SMB share: %s", exc)
                smb_error = friendly_smb_error(exc)
        smb_parent = smb_browse.rsplit("/", 1)[0] if "/" in smb_browse else ("" if smb_browse else None)
        return render_template("settings/source.html", vals=vals, browse_path=browse_path, local_dirs=local_dirs, browse_error=browse_error, browse_parent=browse_parent,
                               smb_browse=smb_browse, smb_dirs=smb_dirs, smb_error=smb_error, smb_parent=smb_parent, smb_display=smb_display(vals), has_smb_password=secret_store.has("smb_password"))

    def smb_source(vals: dict[str, str]) -> SMBSource:
        return SMBSource(vals.get("SMB_SERVER", ""), vals.get("SMB_SHARE", ""), vals.get("SMB_FOLDER", ""), username=vals.get("SMB_USERNAME", ""), password=secret_store.get("smb_password"), port=int(vals.get("SMB_PORT") or 445), recursive=vals.get("SMB_RECURSIVE") != "false", exclude=(vals.get("SMB_SUCCESS_FOLDER", ""), vals.get("SMB_FAILED_FOLDER", "")))

    def check_smb_source(vals: dict[str, str]) -> str:
        media = [f for f in smb_source(vals).list_media_files() if detect_media_kind(Path(f.name)) is not None]
        return f"Share readable: {len(media)} photo/video file(s) found in {smb_display(vals)}"

    @app.post("/settings/source/select-smb")
    @login_required
    def source_select_smb():
        try:
            folder = clean_relative(request.form.get("path", ""))
        except ValueError as exc:
            flash(str(exc), "error"); return redirect(url_for("source_page"))
        vals = {"SOURCE_TYPE": "smb", "SMB_FOLDER": folder}
        errs = validate_settings(store.all_public() | vals)
        if errs:
            flash("; ".join(errs), "error"); return redirect(url_for("source_page"))
        store.update(vals)
        flash("Now pulling media from " + smb_display(store.all_public()), "success")
        return redirect(url_for("source_page", smb_browse=folder))

    @app.post("/settings/source/select-local")
    @login_required
    def source_select_local():
        vals = {"SOURCE_TYPE": "local", "LOCAL_SOURCE_PATH": request.form.get("path", "").strip()}
        errs = validate_settings(store.all_public() | vals)
        if errs:
            flash("; ".join(errs), "error")
        else:
            store.update(vals)
            flash(f"Now pulling media from {vals['LOCAL_SOURCE_PATH']}", "success")
        return redirect(url_for("source_page"))

    def check_local_source(vals: dict[str, str]) -> str:
        source = LocalFolderSource(Path(vals.get("LOCAL_SOURCE_PATH", "")), recursive=vals.get("LOCAL_SOURCE_RECURSIVE") != "false")
        media = [f for f in source.list_media_files() if detect_media_kind(Path(f.name)) is not None]
        return f"Folder readable: {len(media)} photo/video file(s) found"

    @app.post("/settings/source/test")
    @login_required
    def source_test():
        vals = store.all_public()
        try:
            if vals.get("SOURCE_TYPE") == "local":
                if not vals.get("LOCAL_SOURCE_PATH"):
                    raise ValueError("No local/NAS folder has been set.")
                flash(check_local_source(vals), "success")
            elif vals.get("SOURCE_TYPE") == "smb":
                if not (vals.get("SMB_SERVER") and vals.get("SMB_SHARE")):
                    raise ValueError("Enter the NAS server and share name, then save.")
                flash(check_smb_source(vals), "success")
            else:
                meta = test_folder(secret_store, vals.get("GOOGLE_DRIVE_FOLDER_ID", ""))
                flash("Drive folder accessible: " + meta.get("name", ""), "success")
        except Exception as exc:
            app.logger.warning("Media source check failed: %s", exc)
            if vals.get("SOURCE_TYPE") == "smb":
                message = friendly_smb_error(exc)
            elif vals.get("SOURCE_TYPE") == "local":
                message = str(exc)
            else:
                message = friendly_google_error(exc)
            flash(message, "error")
        return redirect(url_for("source_page"))

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
        return render_template("settings/flickr.html", vals=store.all_public(), callback=flickr_callback_url(), flickr_connected=secret_store.has("flickr_oauth_token"), has_secret=secret_store.has("flickr_api_secret"))

    @app.post("/settings/flickr/connect")
    @login_required
    def flickr_connect():
        try:
            oauth = OAuth1Session(store.get("FLICKR_API_KEY"), client_secret=secret_store.get("flickr_api_secret"), callback_uri=flickr_callback_url())
            tok = oauth.fetch_request_token(REQUEST_TOKEN_URL); state = pysecrets.token_urlsafe(24); db.save_oauth_state(state, "flickr", tok["oauth_token_secret"]); session["flickr_state"] = state
            return redirect(oauth.authorization_url(AUTHORIZE_URL, perms="write"))
        except Exception as exc:
            app.logger.warning("Flickr connect failed: %s", exc)
            flash("Flickr rejected the request. Check the API key and secret, then try again.", "error")
            return redirect(url_for("flickr_page"))

    @app.get("/oauth/flickr/callback")
    @login_required
    def flickr_callback():
        state = session.pop("flickr_state", ""); ok, verifier_secret = db.pop_oauth_state(state, "flickr")
        if not ok or not verifier_secret: abort(403)
        if not request.args.get("oauth_token") or not request.args.get("oauth_verifier"):
            flash("Flickr authorization was cancelled or denied.", "error"); return redirect(url_for("flickr_page"))
        try:
            oauth = OAuth1Session(store.get("FLICKR_API_KEY"), client_secret=secret_store.get("flickr_api_secret"), resource_owner_key=request.args["oauth_token"], resource_owner_secret=verifier_secret, verifier=request.args["oauth_verifier"])
            access = oauth.fetch_access_token(ACCESS_TOKEN_URL)
        except Exception as exc:
            app.logger.warning("Flickr token exchange failed: %s", exc)
            flash("Flickr authorization could not be completed. Try connecting again.", "error"); return redirect(url_for("flickr_page"))
        secret_store.set("flickr_oauth_token", access["oauth_token"]); secret_store.set("flickr_oauth_token_secret", access["oauth_token_secret"]); store.set("FLICKR_USERNAME", access.get("username", "")); store.set("FLICKR_LAST_API_CHECK", now()); flash("Flickr connected", "success"); return redirect(url_for("flickr_page"))

    @app.post("/settings/flickr/test")
    @login_required
    def flickr_test():
        try:
            FlickrClient(store.get("FLICKR_API_KEY"), secret_store.get("flickr_api_secret"), secret_store.get("flickr_oauth_token"), secret_store.get("flickr_oauth_token_secret")).list_photosets()
            store.set("FLICKR_LAST_API_CHECK", now()); flash("Flickr API check succeeded", "success")
        except Exception as exc:
            app.logger.warning("Flickr API check failed: %s", exc)
            flash("Flickr API check failed. Reconnect Flickr or check the API key and secret.", "error")
        return redirect(url_for("flickr_page"))

    @app.post("/settings/flickr/disconnect")
    @login_required
    def flickr_disconnect(): secret_store.delete("flickr_oauth_token", "flickr_oauth_token_secret"); store.set("FLICKR_USERNAME", ""); flash("Flickr disconnected", "success"); return redirect(url_for("flickr_page"))

    @app.get("/setup")
    @login_required
    def setup():
        vals = store.all_public()
        try:
            test_results = json.loads(vals.get("SETUP_TEST_RESULTS", "[]"))
        except (TypeError, ValueError):
            test_results = []
        return render_template("setup/index.html", vals=vals, source_configured=source_configured(vals), source_name=source_name(vals), callback=google_callback_url(), app_configured=google_oauth_configured(secret_store, settings.google_credentials_file), google_connected=secret_store.has("google_token_json"), flickr_connected=secret_store.has("flickr_oauth_token"), test_results=test_results)

    @app.post("/setup/finish")
    @login_required
    def setup_finish():
        vals = store.all_public()
        if not (secret_store.has("google_token_json") and source_configured(vals) and vals.get("GOOGLE_CALENDAR_ID") and secret_store.has("flickr_oauth_token") and vals.get("SETUP_LAST_TEST_STATUS") == "success"):
            flash("Setup cannot be marked complete until Google, the media source, Calendar, and Flickr are configured and Test Configuration passes.", "error"); return redirect(url_for("setup"))
        store.set("SETUP_COMPLETE", "true"); flash("Setup complete", "success"); return redirect(url_for("dashboard"))

    @app.post("/setup/test-scan")
    @login_required
    def setup_scan():
        vals = store.all_public()
        results: list[dict[str, object]] = []

        def check(name: str, fn, success_detail) -> None:
            try:
                value = fn()
                detail = success_detail(value)
                results.append({"name": name, "ok": True, "detail": detail})
            except Exception as exc:
                app.logger.warning("Setup check failed for %s: %s", name, exc)
                results.append({"name": name, "ok": False, "detail": "Connection check failed. Review this service's settings and try again."})

        if secret_store.has("google_token_json"):
            check("Google Account", lambda: account_email(secret_store), lambda email: f"Connected as {email}")
        else:
            results.append({"name": "Google Account", "ok": False, "detail": "Connect a Google account first."})

        folder_id = vals.get("GOOGLE_DRIVE_FOLDER_ID", "")
        if vals.get("SOURCE_TYPE") == "smb":
            if vals.get("SMB_SERVER") and vals.get("SMB_SHARE"):
                try:
                    results.append({"name": "Network Share (SMB)", "ok": True, "detail": check_smb_source(vals)})
                except Exception as exc:
                    app.logger.warning("Setup check failed for SMB: %s", exc)
                    results.append({"name": "Network Share (SMB)", "ok": False, "detail": friendly_smb_error(exc)})
            else:
                results.append({"name": "Network Share (SMB)", "ok": False, "detail": "Enter the NAS server and share first."})
        elif vals.get("SOURCE_TYPE") == "local":
            if vals.get("LOCAL_SOURCE_PATH"):
                check("Mounted Folder", lambda: check_local_source(vals), lambda detail: detail)
            else:
                results.append({"name": "Mounted Folder", "ok": False, "detail": "Set a local/NAS folder first."})
        elif folder_id:
            check("Google Drive", lambda: test_folder(secret_store, folder_id), lambda meta: f"Folder accessible: {meta.get('name', folder_id)}")
        else:
            results.append({"name": "Google Drive", "ok": False, "detail": "Select a Drive folder first."})

        calendar_id = vals.get("GOOGLE_CALENDAR_ID", "")
        if calendar_id:
            check("Google Calendar", lambda: test_calendar(secret_store, calendar_id), lambda meta: f"Calendar accessible: {meta.get('summary', calendar_id)}")
        else:
            results.append({"name": "Google Calendar", "ok": False, "detail": "Select a Calendar first."})

        if secret_store.has("flickr_oauth_token"):
            check("Flickr", lambda: FlickrClient(vals.get("FLICKR_API_KEY", ""), secret_store.get("flickr_api_secret"), secret_store.get("flickr_oauth_token"), secret_store.get("flickr_oauth_token_secret")).list_photosets(), lambda albums: f"Connection succeeded; {len(albums)} albums available.")
        else:
            results.append({"name": "Flickr", "ok": False, "detail": "Connect Flickr first."})

        passed = all(bool(result["ok"]) for result in results)
        store.set("SETUP_TEST_RESULTS", json.dumps(results))
        store.set("SETUP_LAST_TEST_STATUS", "success" if passed else "failed")
        store.set("SETUP_LAST_TEST_AT", now())
        flash("All configuration checks passed." if passed else "Some configuration checks failed. Review the results below.", "success" if passed else "error")
        return redirect(url_for("setup"))

    return app


def main() -> None:
    from waitress import serve  # type: ignore[import-untyped]

    settings = load_settings(require_credentials=False)
    serve(create_app(), host=settings.web_bind, port=settings.web_port, threads=8)
