"""Authenticated web administration UI."""
from __future__ import annotations

import secrets as pysecrets
from functools import wraps
from html import escape

from flask import Flask, abort, flash, redirect, render_template_string, request, session, url_for
from werkzeug.security import check_password_hash
from requests_oauthlib import OAuth1Session

from .config import load_settings
from .database import Database, now
from .flickr import ACCESS_TOKEN_URL, AUTHORIZE_URL, REQUEST_TOKEN_URL, FlickrClient
from .google_ui import account_email, flow_for, list_calendars, list_folders, test_calendar, test_folder
from .secrets import SecretStore
from .settings_store import SettingsStore, validate_settings

BASE = """
<!doctype html><title>drive-to-flickr</title><h1>drive-to-flickr</h1>
<nav><a href='/'>Dashboard</a> | <a href='/setup'>Setup Wizard</a> | <a href='/settings'>Settings</a> | <a href='/settings/google-account'>Google Account</a> | <a href='/settings/google-drive'>Google Drive</a> | <a href='/settings/calendar'>Calendar</a> | <a href='/settings/flickr'>Flickr</a></nav>
{% with messages = get_flashed_messages() %}{% for m in messages %}<p><strong>{{m}}</strong></p>{% endfor %}{% endwith %}
{{body|safe}}
"""

def create_app() -> Flask:
    settings = load_settings(require_credentials=False)
    app = Flask(__name__)
    app.secret_key = settings.web_secret_key or pysecrets.token_hex(32)
    db = Database(settings.database_path)
    store = SettingsStore(db)
    secret_store = SecretStore(settings.secret_store_path)

    def page(body: str):
        return render_template_string(BASE, body=body)

    def csrf() -> str:
        session.setdefault("csrf", pysecrets.token_urlsafe(32))
        return str(session["csrf"])

    def check_csrf() -> None:
        if request.method == "POST" and request.form.get("csrf") != session.get("csrf"):
            abort(403)

    def login_required(fn):
        @wraps(fn)
        def wrapper(*a, **kw):
            if not settings.admin_password_hash:
                return page("<h2>Bootstrap required</h2><p>Set ADMIN_PASSWORD_HASH and WEB_SECRET_KEY before using the admin UI.</p>"), 503
            if not session.get("auth"):
                return redirect(url_for("login"))
            check_csrf()
            return fn(*a, **kw)
        return wrapper

    def form(action: str, inner: str) -> str:
        return f"<form method='post' action='{action}'><input type='hidden' name='csrf' value='{csrf()}'>{inner}</form>"

    @app.get('/login')
    def login():
        return page(form('/login', "<h2>Login</h2><input name='username'><input name='password' type='password'><button>Login</button>"))

    @app.post('/login')
    def do_login():
        check_csrf()
        if request.form.get('username') == settings.admin_username and check_password_hash(settings.admin_password_hash, request.form.get('password','')):
            session['auth'] = True
            return redirect(url_for('dashboard'))
        abort(403)

    @app.get('/')
    @login_required
    def dashboard():
        vals = store.all_public()
        warnings=[]
        if not secret_store.has('google_token_json'): warnings.append('Google authorization expired or missing')
        if not vals.get('GOOGLE_DRIVE_FOLDER_ID'): warnings.append('Configured Drive folder is not selected or no longer accessible')
        if not vals.get('GOOGLE_CALENDAR_ID'): warnings.append('Configured Calendar is not selected or no longer accessible')
        if not secret_store.has('flickr_oauth_token'): warnings.append('Flickr authorization failed or missing')
        body = "<h2>Dashboard</h2><table>" + "".join(f"<tr><th>{k}</th><td>{v}</td></tr>" for k,v in {
            'Google Account':'Connected' if secret_store.has('google_token_json') else 'Not connected',
            'Google Drive':'Connected' if vals.get('GOOGLE_DRIVE_FOLDER_ID') else 'Not selected',
            'Watched Folder': vals.get('GOOGLE_DRIVE_FOLDER_NAME') or vals.get('GOOGLE_DRIVE_FOLDER_ID') or 'Not selected',
            'Google Calendar': vals.get('GOOGLE_CALENDAR_NAME') or vals.get('GOOGLE_CALENDAR_ID') or 'Not selected',
            'Flickr':'Connected' if secret_store.has('flickr_oauth_token') else 'Not connected',
            'Worker':'Running when systemd service is active',}.items()) + "</table>"
        if warnings: body += '<h3>Warnings</h3><ul>' + ''.join(f'<li>{escape(w)}</li>' for w in warnings) + '</ul>'
        return page(body)

    @app.route('/settings', methods=['GET','POST'])
    @login_required
    def settings_page():
        if request.method == 'POST':
            vals={k:request.form.get(k,'') for k in ['TIMEZONE','POLL_INTERVAL_SECONDS','MINIMUM_FILE_AGE_SECONDS','MAX_ATTEMPTS','LOG_LEVEL','BUFFER_BEFORE_MINUTES','BUFFER_AFTER_MINUTES','REQUIRE_FLICKR_MARKER','NO_EVENT_ACTION','UNASSIGNED_ALBUM','DRIVE_SUCCESS_ACTION','DRIVE_SUCCESS_FOLDER','DRIVE_FAILED_FOLDER','FLICKR_DEFAULT_PRIVACY','GLOBAL_TAGS']}
            errs=validate_settings(store.all_public()|vals)
            if errs: flash('; '.join(errs))
            else: store.update(vals); flash('Settings saved')
        v=store.all_public()
        fields=''.join(f"<label>{k}<input name='{k}' value='{escape(v.get(k,''))}'></label><br>" for k in ['TIMEZONE','POLL_INTERVAL_SECONDS','MINIMUM_FILE_AGE_SECONDS','MAX_ATTEMPTS','LOG_LEVEL','BUFFER_BEFORE_MINUTES','BUFFER_AFTER_MINUTES','REQUIRE_FLICKR_MARKER','NO_EVENT_ACTION','UNASSIGNED_ALBUM','DRIVE_SUCCESS_ACTION','DRIVE_SUCCESS_FOLDER','DRIVE_FAILED_FOLDER','FLICKR_DEFAULT_PRIVACY','GLOBAL_TAGS'])
        return page('<h2>Main Settings</h2>'+form('/settings', fields+'<button>Save</button>'))

    @app.get('/settings/google-account')
    @login_required
    def google_account():
        v=store.all_public(); status='Connected' if secret_store.has('google_token_json') else 'Not connected'
        buttons=form('/settings/google-account/connect','<button>Connect Google Account</button>')+form('/settings/google-account/test','<button>Test Connection</button>')+form('/settings/google-account/disconnect','<button>Disconnect</button>')
        return page(f"<h2>Google Account</h2><p>Email: {escape(v.get('GOOGLE_ACCOUNT_EMAIL',''))}</p><p>Status: {status}</p><p>Last successful API check: {escape(v.get('GOOGLE_LAST_API_CHECK',''))}</p>{buttons}")

    @app.post('/settings/google-account/connect')
    @login_required
    def google_connect():
        state=pysecrets.token_urlsafe(24); db.save_oauth_state(state,'google')
        flow=flow_for(settings.google_credentials_file, secret_store, url_for('google_callback', _external=True), state)
        auth_url,_=flow.authorization_url(access_type='offline', include_granted_scopes='true', prompt='consent')
        return redirect(auth_url)

    @app.get('/oauth/google/callback')
    @login_required
    def google_callback():
        state=request.args.get('state','')
        ok, _ = db.pop_oauth_state(state,'google')
        if not ok: abort(403)
        flow=flow_for(settings.google_credentials_file, secret_store, url_for('google_callback', _external=True), state)
        flow.fetch_token(authorization_response=request.url)
        secret_store.set('google_token_json', flow.credentials.to_json())
        email=account_email(secret_store); store.set('GOOGLE_ACCOUNT_EMAIL', email); store.set('GOOGLE_LAST_API_CHECK', now())
        flash('Google account connected')
        return redirect(url_for('google_account'))

    @app.post('/settings/google-account/test')
    @login_required
    def google_test():
        store.set('GOOGLE_ACCOUNT_EMAIL', account_email(secret_store)); store.set('GOOGLE_LAST_API_CHECK', now()); flash('Google API check succeeded'); return redirect(url_for('google_account'))

    @app.post('/settings/google-account/disconnect')
    @login_required
    def google_disconnect():
        secret_store.delete('google_token_json','google_refresh_token'); store.set('GOOGLE_ACCOUNT_EMAIL',''); flash('Google disconnected'); return redirect(url_for('google_account'))

    @app.get('/settings/google-drive')
    @login_required
    def drive_page():
        folders=list_folders(secret_store, request.args.get('parent') or None) if secret_store.has('google_token_json') else []
        rows=''.join(f"<li>{escape(f['name'])} <a href='/settings/google-drive?parent={f['id']}'>Open</a> "+form('/settings/google-drive/select', f"<input type='hidden' name='id' value='{f['id']}'><input type='hidden' name='name' value='{escape(f['name'])}'><button>Select</button>")+'</li>' for f in folders)
        return page(f"<h2>Google Drive</h2><p>Selected: {escape(store.get('GOOGLE_DRIVE_FOLDER_NAME') or store.get('GOOGLE_DRIVE_FOLDER_ID'))}</p><a href='/settings/google-drive'>Refresh folders</a><ul>{rows}</ul>"+form('/settings/google-drive/test','<button>Test access</button>'))

    @app.post('/settings/google-drive/select')
    @login_required
    def drive_select():
        test_folder(secret_store, request.form['id']); store.set('GOOGLE_DRIVE_FOLDER_ID', request.form['id']); store.set('GOOGLE_DRIVE_FOLDER_NAME', request.form['name']); flash('Drive folder selected'); return redirect(url_for('drive_page'))

    @app.post('/settings/google-drive/test')
    @login_required
    def drive_test():
        meta=test_folder(secret_store, store.get('GOOGLE_DRIVE_FOLDER_ID')); flash('Drive folder accessible: '+meta.get('name','')); return redirect(url_for('drive_page'))

    @app.get('/settings/calendar')
    @login_required
    def calendar_page():
        cals=list_calendars(secret_store) if secret_store.has('google_token_json') else []
        rows=''.join(f"<li>{escape(c.get('summary',''))} <small>advanced ID: {escape(c.get('id',''))}</small> "+form('/settings/calendar/select', f"<input type='hidden' name='id' value='{escape(c.get('id',''))}'><input type='hidden' name='name' value='{escape(c.get('summary',''))}'><button>Select</button>")+'</li>' for c in cals)
        return page(f"<h2>Calendar</h2><p>Selected: {escape(store.get('GOOGLE_CALENDAR_NAME') or store.get('GOOGLE_CALENDAR_ID'))}</p><a href='/settings/calendar'>Refresh Calendars</a><ul>{rows}</ul>"+form('/settings/calendar/test','<button>Test Calendar</button>'))

    @app.post('/settings/calendar/select')
    @login_required
    def cal_select():
        test_calendar(secret_store, request.form['id']); store.set('GOOGLE_CALENDAR_ID', request.form['id']); store.set('GOOGLE_CALENDAR_NAME', request.form['name']); flash('Calendar selected'); return redirect(url_for('calendar_page'))

    @app.post('/settings/calendar/test')
    @login_required
    def cal_test():
        meta=test_calendar(secret_store, store.get('GOOGLE_CALENDAR_ID')); flash('Calendar accessible: '+meta.get('summary','')); return redirect(url_for('calendar_page'))

    @app.route('/settings/flickr', methods=['GET','POST'])
    @login_required
    def flickr_page():
        if request.method=='POST':
            store.set('FLICKR_API_KEY', request.form.get('api_key',''))
            if request.form.get('api_secret'): secret_store.set('flickr_api_secret', request.form['api_secret'])
            flash('Flickr API settings saved')
        status='Connected' if secret_store.has('flickr_oauth_token') else 'Not connected'
        body=f"<h2>Flickr</h2><p>API Key: {escape(store.get('FLICKR_API_KEY'))}</p><p>API Secret: {'••••••' if secret_store.has('flickr_api_secret') else 'Not set'}</p><p>Status: {status}</p><p>Username: {escape(store.get('FLICKR_USERNAME'))}</p>"
        body += form('/settings/flickr', "<input name='api_key' placeholder='API Key'><input name='api_secret' placeholder='API Secret' type='password'><button>Save API Settings</button>")
        body += form('/settings/flickr/connect','<button>Connect Flickr</button>')+form('/settings/flickr/test','<button>Test Connection</button>')+form('/settings/flickr/disconnect','<button>Disconnect</button>')
        return page(body)

    @app.post('/settings/flickr/connect')
    @login_required
    def flickr_connect():
        oauth=OAuth1Session(store.get('FLICKR_API_KEY'), client_secret=secret_store.get('flickr_api_secret'), callback_uri=url_for('flickr_callback', _external=True))
        tok=oauth.fetch_request_token(REQUEST_TOKEN_URL); state=pysecrets.token_urlsafe(24); db.save_oauth_state(state,'flickr',tok['oauth_token_secret']); session['flickr_request_token']=tok['oauth_token']; session['flickr_state']=state
        return redirect(oauth.authorization_url(AUTHORIZE_URL, perms='write'))

    @app.get('/oauth/flickr/callback')
    @login_required
    def flickr_callback():
        state=session.pop('flickr_state',''); ok, verifier_secret=db.pop_oauth_state(state,'flickr')
        if not ok or not verifier_secret: abort(403)
        oauth=OAuth1Session(store.get('FLICKR_API_KEY'), client_secret=secret_store.get('flickr_api_secret'), resource_owner_key=request.args['oauth_token'], resource_owner_secret=verifier_secret, verifier=request.args['oauth_verifier'])
        access=oauth.fetch_access_token(ACCESS_TOKEN_URL); secret_store.set('flickr_oauth_token', access['oauth_token']); secret_store.set('flickr_oauth_token_secret', access['oauth_token_secret']); store.set('FLICKR_USERNAME', access.get('username','')); store.set('FLICKR_LAST_API_CHECK', now()); flash('Flickr connected'); return redirect(url_for('flickr_page'))

    @app.post('/settings/flickr/test')
    @login_required
    def flickr_test():
        FlickrClient(store.get('FLICKR_API_KEY'), secret_store.get('flickr_api_secret'), secret_store.get('flickr_oauth_token'), secret_store.get('flickr_oauth_token_secret')).list_photosets(); store.set('FLICKR_LAST_API_CHECK', now()); flash('Flickr API check succeeded'); return redirect(url_for('flickr_page'))

    @app.post('/settings/flickr/disconnect')
    @login_required
    def flickr_disconnect():
        secret_store.delete('flickr_oauth_token','flickr_oauth_token_secret'); store.set('FLICKR_USERNAME',''); flash('Flickr disconnected'); return redirect(url_for('flickr_page'))

    @app.get('/setup')
    @login_required
    def setup():
        done=store.get('SETUP_COMPLETE')=='true'
        body="<h2>First-run Setup Wizard</h2><ol><li>General Settings</li><li>Connect Google Account</li><li>Select Google Drive Folder</li><li>Select Google Calendar</li><li>Connect Flickr</li><li>Test Configuration</li><li>Finish</li></ol>"
        body += f"<p>Google: {'Connected' if secret_store.has('google_token_json') else 'Not connected'}<br>Drive: {'Connected' if store.get('GOOGLE_DRIVE_FOLDER_ID') else 'Not connected'}<br>Calendar: {'Connected' if store.get('GOOGLE_CALENDAR_ID') else 'Not connected'}<br>Flickr: {'Connected' if secret_store.has('flickr_oauth_token') else 'Not connected'}</p>"
        body += form('/setup/finish','<button>Finish</button>')+form('/setup/test-scan','<button>Run Test Scan</button>')
        return page(body)

    @app.post('/setup/finish')
    @login_required
    def setup_finish(): store.set('SETUP_COMPLETE','true'); flash('Setup complete'); return redirect(url_for('dashboard'))

    @app.post('/setup/test-scan')
    @login_required
    def setup_scan(): flash('Run `drive-to-flickr dry-run` on the server to perform a test scan.'); return redirect(url_for('setup'))

    return app


def main() -> None:
    settings = load_settings(require_credentials=False)
    create_app().run(host=settings.web_bind, port=settings.web_port)
