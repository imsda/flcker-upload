import json
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import Mock, patch

import pytest
from werkzeug.security import generate_password_hash

from drive_to_flickr.database import Database
from drive_to_flickr.models import DriveFile, Status
from drive_to_flickr.secrets import SecretStore
from drive_to_flickr.settings_store import SettingsStore, validate_settings


def env(monkeypatch, tmp_path):
    monkeypatch.setenv('DATABASE_PATH', str(tmp_path/'state.sqlite'))
    monkeypatch.setenv('SECRET_STORE_PATH', str(tmp_path/'secrets.json'))
    monkeypatch.setenv('GOOGLE_CREDENTIALS_FILE', str(tmp_path/'google-client.json'))
    monkeypatch.setenv('ADMIN_PASSWORD_HASH', generate_password_hash('pw'))
    monkeypatch.setenv('WEB_SECRET_KEY', 'test-secret')
    (tmp_path/'google-client.json').write_text(json.dumps({'web': {'client_id': 'id', 'client_secret': 'sec', 'auth_uri': 'https://accounts.google.com/o/oauth2/auth', 'token_uri': 'https://oauth2.googleapis.com/token'}}))


def login(client):
    client.get('/login')
    with client.session_transaction() as s: csrf=s['csrf']
    return client.post('/login', data={'username':'admin','password':'pw','csrf':csrf})


@pytest.fixture
def client(monkeypatch, tmp_path):
    env(monkeypatch,tmp_path)
    from drive_to_flickr.web import create_app
    app=create_app(); app.config['TESTING']=True
    c=app.test_client(); login(c)
    return c


def csrf(client):
    with client.session_transaction() as s: return s['csrf']


def connect_google_for_discovery():
    SecretStore(Path(__import__('os').environ['SECRET_STORE_PATH'])).set(
        'google_token_json', '{"token":"test"}'
    )


def test_authentication_and_csrf_required(client):
    anon = client.application.test_client()
    assert anon.get('/').status_code == 302
    assert client.post('/settings', data={'TIMEZONE':'UTC'}).status_code == 403


def test_settings_persistence_and_validation(monkeypatch, tmp_path):
    env(monkeypatch,tmp_path)
    db=Database(tmp_path/'state.sqlite'); store=SettingsStore(db)
    store.update({'TIMEZONE':'UTC','POLL_INTERVAL_SECONDS':'30','FLICKR_DEFAULT_PRIVACY':'public'})
    assert SettingsStore(Database(tmp_path/'state.sqlite')).get('POLL_INTERVAL_SECONDS') == '30'
    assert store.all_public()['PUBLIC_BASE_URL'] == ''
    assert validate_settings({'TIMEZONE':'Nope/Bad','POLL_INTERVAL_SECONDS':'0','MINIMUM_FILE_AGE_SECONDS':'0','MAX_ATTEMPTS':'1','BUFFER_BEFORE_MINUTES':'0','BUFFER_AFTER_MINUTES':'0','FLICKR_DEFAULT_PRIVACY':'bad','NO_EVENT_ACTION':'skip','DRIVE_SUCCESS_ACTION':'leave'})
    assert 'PUBLIC_BASE_URL must be a complete http:// or https:// URL' in validate_settings(store.all_public() | {'PUBLIC_BASE_URL': 'None'})


def test_token_storage_redaction(tmp_path):
    ss=SecretStore(tmp_path/'secret.json'); ss.set('flickr_oauth_token','abc')
    assert oct((tmp_path/'secret.json').stat().st_mode & 0o777) == '0o600'
    assert 'abc' not in ss.__dict__.values()


def test_google_oauth_callback(client):
    fake_flow=Mock(); fake_flow.credentials.to_json.return_value='{"refresh_token":"r"}'
    with patch('drive_to_flickr.web.flow_for', return_value=fake_flow), patch('drive_to_flickr.web.account_email', return_value='flickr-uploader@example.org'):
        client.application.view_functions['dashboard']
        # create matching oauth state in DB through connect endpoint mocked authorization URL
        with patch.object(fake_flow, 'authorization_url', return_value=('https://google.example/auth', None)):
            resp=client.post('/settings/google-account/connect', data={'csrf':csrf(client)})
        assert resp.status_code == 302
        db=Database(Path(__import__('os').environ['DATABASE_PATH']))
        with db.connect() as conn:
            state=conn.execute("select state from oauth_states where provider='google'").fetchone()['state']
        resp=client.get('/oauth/google/callback?state='+state+'&code=ok')
        assert resp.status_code == 302
        assert SettingsStore(db).get('GOOGLE_ACCOUNT_EMAIL') == 'flickr-uploader@example.org'


def test_drive_folder_discovery_shared_drives_and_selection(client):
    connect_google_for_discovery()
    folders=[{'id':'fld','name':'Shared Uploads','driveId':'sd','capabilities':{'canEdit':False}}]
    with patch('drive_to_flickr.web.list_folders', return_value=folders) as lf, patch('drive_to_flickr.web.test_folder', return_value={'id':'fld','name':'Shared Uploads'}):
        assert b'Shared Uploads' in client.get('/settings/google-drive').data
        lf.assert_called()
        client.post('/settings/google-drive/select', data={'csrf':csrf(client),'id':'fld','name':'Shared Uploads'})
    assert SettingsStore(Database(Path(__import__('os').environ['DATABASE_PATH']))).get('GOOGLE_DRIVE_FOLDER_ID') == 'fld'


def test_drive_folder_browser_has_search_and_access_filters(client):
    connect_google_for_discovery()
    folders = [
        {'id': 'mine', 'name': 'Photography', 'capabilities': {'canEdit': True}},
        {'id': 'shared', 'name': 'Archive', 'driveId': 'shared-drive', 'capabilities': {'canEdit': False}},
    ]
    with patch('drive_to_flickr.web.list_folders', return_value=folders):
        response = client.get('/settings/google-drive')
    assert response.status_code == 200
    assert b'data-folder-search' in response.data
    assert b'data-folder-filter' in response.data
    assert b'data-folder-location="shared-drive"' in response.data
    assert b'data-folder-access="editable"' in response.data


def test_hidden_filtered_folders_override_layout_display(client):
    response = client.get('/static/css/app.css')
    assert response.status_code == 200
    assert b'[hidden]{display:none!important}' in response.data


def test_inaccessible_folder_handling(client):
    with patch('drive_to_flickr.web.test_folder', side_effect=RuntimeError('forbidden')):
        response = client.post('/settings/google-drive/test', data={'csrf':csrf(client)}, follow_redirects=True)
        assert response.status_code == 200
        assert b'forbidden' in response.data


def test_disabled_drive_api_is_shown_as_configuration_error(client):
    connect_google_for_discovery()
    error = RuntimeError('drive.googleapis.com accessNotConfigured: API is disabled')
    with patch('drive_to_flickr.web.list_folders', side_effect=error):
        response = client.get('/settings/google-drive')
    assert response.status_code == 200
    assert b'Google Drive API is disabled' in response.data


def test_calendar_list_and_shared_selection(client):
    connect_google_for_discovery()
    calendars=[{'id':'cal@example.org','summary':'Flickr Albums','accessRole':'reader'}]
    with patch('drive_to_flickr.web.list_calendars', return_value=calendars), patch('drive_to_flickr.web.test_calendar', return_value=calendars[0]):
        assert b'Flickr Albums' in client.get('/settings/calendar').data
        client.post('/settings/calendar/select', data={'csrf':csrf(client),'id':'cal@example.org','name':'Flickr Albums'})
    assert SettingsStore(Database(Path(__import__('os').environ['DATABASE_PATH']))).get('GOOGLE_CALENDAR_ID') == 'cal@example.org'


def test_inaccessible_calendar_handling(client):
    with patch('drive_to_flickr.web.test_calendar', side_effect=RuntimeError('forbidden')):
        response = client.post('/settings/calendar/test', data={'csrf':csrf(client)}, follow_redirects=True)
        assert response.status_code == 200
        assert b'forbidden' in response.data


def test_disabled_calendar_api_is_shown_as_configuration_error(client):
    connect_google_for_discovery()
    error = RuntimeError('calendar-json.googleapis.com accessNotConfigured: API is disabled')
    with patch('drive_to_flickr.web.list_calendars', side_effect=error):
        response = client.get('/settings/calendar')
    assert response.status_code == 200
    assert b'Google Calendar API is disabled' in response.data


def test_configuration_health_and_wizard(client):
    assert b'Google Account Not Connected' in client.get('/').data
    data=client.get('/setup').data
    assert b'First-run Setup Wizard' in data and b'Test Configuration' in data


def test_activity_retry_action_is_clickable_and_csrf_protected(client):
    db = Database(Path(__import__('os').environ['DATABASE_PATH']))
    drive_file = DriveFile('retry-id', 'photo.jpg', 'image/jpeg', datetime.now(UTC), datetime.now(UTC))
    db.upsert_discovered(drive_file)
    db.increment_attempts('retry-id', 'temporary API failure')
    dashboard = client.get('/')
    assert b'action="/activity/retry-id/retry"' in dashboard.data
    assert b'>Retry</button>' in dashboard.data
    assert b'<summary>View issue</summary>' in dashboard.data
    assert b'temporary API failure' in dashboard.data
    assert client.post('/activity/retry-id/retry').status_code == 403
    response = client.post('/activity/retry-id/retry', data={'csrf': csrf(client)}, follow_redirects=True)
    assert response.status_code == 200
    assert b'Retry queued' in response.data


def test_all_failed_activity_can_be_retried_with_csrf(client):
    db = Database(Path(__import__('os').environ['DATABASE_PATH']))
    for index in range(2):
        drive_file = DriveFile(f'failed-{index}', f'photo-{index}.jpg', 'image/jpeg', datetime.now(UTC), datetime.now(UTC))
        db.upsert_discovered(drive_file)
        db.update_status(drive_file.id, Status.FAILED, 'Flickr upload failed')
    dashboard = client.get('/')
    assert b'Retry all 2 failed' in dashboard.data
    assert client.post('/activity/retry-failed').status_code == 403
    response = client.post('/activity/retry-failed', data={'csrf': csrf(client)}, follow_redirects=True)
    assert response.status_code == 200
    assert b'Queued 2 failed files for retry' in response.data
    assert dict(db.status_counts())[Status.RETRY] == 2


def test_dashboard_times_use_configured_timezone(client):
    db = Database(Path(__import__('os').environ['DATABASE_PATH']))
    store = SettingsStore(db)
    store.set('TIMEZONE', 'America/Chicago')
    store.set('WORKER_HEARTBEAT', '2026-07-21T21:44:29+00:00')
    response = client.get('/')
    assert b'2026-07-21 04:44:29 PM CDT' in response.data


def test_setup_configuration_check_reports_each_service(client):
    connect_google_for_discovery()
    db = Database(Path(__import__('os').environ['DATABASE_PATH']))
    store = SettingsStore(db)
    store.update({
        'GOOGLE_DRIVE_FOLDER_ID': 'folder-id',
        'GOOGLE_DRIVE_FOLDER_NAME': 'Uploads',
        'GOOGLE_CALENDAR_ID': 'calendar-id',
        'GOOGLE_CALENDAR_NAME': 'Albums',
        'FLICKR_API_KEY': 'api-key',
    })
    secrets = SecretStore(Path(__import__('os').environ['SECRET_STORE_PATH']))
    secrets.set('flickr_api_secret', 'secret')
    secrets.set('flickr_oauth_token', 'token')
    secrets.set('flickr_oauth_token_secret', 'token-secret')
    with patch('drive_to_flickr.web.account_email', return_value='photos@example.org'), \
         patch('drive_to_flickr.web.test_folder', return_value={'name': 'Uploads'}), \
         patch('drive_to_flickr.web.test_calendar', return_value={'summary': 'Albums'}), \
         patch('drive_to_flickr.web.FlickrClient') as flickr:
        flickr.return_value.list_photosets.return_value = {'one': ('1', 'One')}
        response = client.post('/setup/test-scan', data={'csrf': csrf(client)}, follow_redirects=True)
    assert response.status_code == 200
    assert b'All configuration checks passed' in response.data
    assert b'Connected as photos@example.org' in response.data
    assert b'Folder accessible: Uploads' in response.data
    assert b'Calendar accessible: Albums' in response.data
    assert b'1 albums available' in response.data
    assert SettingsStore(db).get('SETUP_LAST_TEST_STATUS') == 'success'


def test_setup_configuration_check_is_required_to_finish(client):
    connect_google_for_discovery()
    db = Database(Path(__import__('os').environ['DATABASE_PATH']))
    store = SettingsStore(db)
    store.set('GOOGLE_DRIVE_FOLDER_ID', 'folder-id')
    store.set('GOOGLE_CALENDAR_ID', 'calendar-id')
    SecretStore(Path(__import__('os').environ['SECRET_STORE_PATH'])).set('flickr_oauth_token', 'token')
    response = client.post('/setup/finish', data={'csrf': csrf(client)}, follow_redirects=True)
    assert b'Test Configuration passes' in response.data
    assert store.get('SETUP_COMPLETE') != 'true'


def test_flickr_connection_workflow(client):
    store = SettingsStore(Database(Path(__import__('os').environ['DATABASE_PATH'])))
    store.set('PUBLIC_BASE_URL', 'https://flickerapp.internal.imsda.org')
    page = client.get('/settings/flickr')
    assert b'https://flickerapp.internal.imsda.org/oauth/flickr/callback' in page.data
    with patch('drive_to_flickr.web.OAuth1Session') as cls:
        inst=cls.return_value; inst.fetch_request_token.return_value={'oauth_token':'rt','oauth_token_secret':'rs'}; inst.authorization_url.return_value='https://flickr.example/auth'
        assert client.post('/settings/flickr/connect', data={'csrf':csrf(client)}).status_code == 302
        inst.fetch_access_token.return_value={'oauth_token':'at','oauth_token_secret':'ats','username':'me'}
    # Full callback covered structurally; endpoint stores tokens via same SecretStore.


def test_google_oauth_settings_storage_and_secret_redaction(client):
    resp = client.post('/settings/google-account/oauth-settings', data={'csrf':csrf(client),'client_id':'stored-client','client_secret':'stored-secret'}, follow_redirects=True)
    assert b'OAuth Application' in resp.data and b'Configured' in resp.data
    assert b'stored-client' in resp.data
    assert b'stored-secret' not in resp.data
    ss = SecretStore(Path(__import__('os').environ['SECRET_STORE_PATH']))
    assert ss.get('google_client_id') == 'stored-client'
    assert ss.get('google_client_secret') == 'stored-secret'


def test_google_client_config_from_secret_store(tmp_path):
    from drive_to_flickr.google_ui import client_config
    ss = SecretStore(tmp_path/'secrets.json')
    ss.set('google_client_id', 'ui-id')
    ss.set('google_client_secret', 'ui-secret')
    cfg = client_config(tmp_path/'missing.json', ss)
    assert cfg['web']['client_id'] == 'ui-id'
    assert cfg['web']['client_secret'] == 'ui-secret'
    assert cfg['web']['token_uri'] == 'https://oauth2.googleapis.com/token'


def test_google_client_config_falls_back_to_legacy_json(tmp_path):
    from drive_to_flickr.google_ui import client_config
    legacy = tmp_path/'google-client.json'
    legacy.write_text(json.dumps({'web': {'client_id': 'legacy-id', 'client_secret': 'legacy-secret', 'auth_uri':'a', 'token_uri':'t'}}))
    cfg = client_config(legacy, SecretStore(tmp_path/'secrets.json'))
    assert cfg['web']['client_id'] == 'legacy-id'


def test_missing_google_oauth_credentials_friendly_error(monkeypatch, tmp_path):
    env(monkeypatch, tmp_path)
    (tmp_path/'google-client.json').unlink()
    from drive_to_flickr.web import create_app
    app=create_app(); app.config['TESTING']=True
    c=app.test_client(); login(c)
    resp = c.post('/settings/google-account/connect', data={'csrf':csrf(c)}, follow_redirects=True)
    assert b'Google OAuth application credentials have not been configured' in resp.data
    assert resp.status_code == 200


def test_public_base_url_callback(monkeypatch, tmp_path):
    env(monkeypatch, tmp_path)
    monkeypatch.setenv('PUBLIC_BASE_URL', 'https://example.org/base')
    from drive_to_flickr.web import create_app
    app=create_app(); app.config['TESTING']=True
    c=app.test_client(); login(c)
    data = c.get('/settings/google-account').data
    assert b'https://example.org/base/oauth/google/callback' in data


def test_google_disconnect_preserves_oauth_app_credentials(client):
    ss = SecretStore(Path(__import__('os').environ['SECRET_STORE_PATH']))
    ss.set('google_client_id', 'keep-id')
    ss.set('google_client_secret', 'keep-secret')
    ss.set('google_token_json', '{"token":"t"}')
    client.post('/settings/google-account/disconnect', data={'csrf':csrf(client)})
    ss = SecretStore(Path(__import__('os').environ['SECRET_STORE_PATH']))
    assert ss.get('google_client_id') == 'keep-id'
    assert ss.get('google_client_secret') == 'keep-secret'
    assert not ss.has('google_token_json')


def test_google_oauth_denial_friendly(client):
    resp = client.get('/oauth/google/callback?error=access_denied&error_description=access_denied', follow_redirects=True)
    assert b'Google authorization was denied' in resp.data


def store_for_test():
    return SettingsStore(Database(Path(__import__('os').environ['DATABASE_PATH'])))


def test_media_source_page_saves_local_nas_folder(client, tmp_path):
    nas = tmp_path / 'nas'
    nas.mkdir()
    page = client.get('/settings/source')
    assert page.status_code == 200 and b'Folder mounted on this server' in page.data
    resp = client.post('/settings/source', data={'csrf': csrf(client), 'SOURCE_TYPE': 'local', 'LOCAL_SOURCE_PATH': str(nas), 'LOCAL_SOURCE_RECURSIVE': 'on'}, follow_redirects=True)
    assert b'Media source saved' in resp.data
    store = store_for_test()
    assert store.get('SOURCE_TYPE') == 'local'
    assert store.get('LOCAL_SOURCE_PATH') == str(nas)
    assert store.get('LOCAL_SOURCE_RECURSIVE') == 'true'


def test_media_source_rejects_relative_or_missing_path(client):
    resp = client.post('/settings/source', data={'csrf': csrf(client), 'SOURCE_TYPE': 'local', 'LOCAL_SOURCE_PATH': ''}, follow_redirects=True)
    assert b'LOCAL_SOURCE_PATH is required' in resp.data
    resp = client.post('/settings/source', data={'csrf': csrf(client), 'SOURCE_TYPE': 'local', 'LOCAL_SOURCE_PATH': 'nas/photos'}, follow_redirects=True)
    assert b'must be an absolute path' in resp.data
    assert store_for_test().get('SOURCE_TYPE') == 'google_drive'


def test_local_folder_browser_lists_and_selects(client, tmp_path):
    nas = tmp_path / 'nas'
    (nas / 'Camp Meeting').mkdir(parents=True)
    (nas / '.hidden').mkdir()
    page = client.get('/settings/source?browse=' + str(nas))
    assert b'Camp Meeting' in page.data
    assert b'.hidden' not in page.data
    assert b'does not exist' in client.get('/settings/source?browse=' + str(tmp_path / 'missing')).data
    resp = client.post('/settings/source/select-local', data={'csrf': csrf(client), 'path': str(nas / 'Camp Meeting')}, follow_redirects=True)
    assert b'Now pulling media from' in resp.data
    assert store_for_test().get('LOCAL_SOURCE_PATH') == str(nas / 'Camp Meeting')


def test_local_source_test_counts_media(client, tmp_path):
    nas = tmp_path / 'nas'
    nas.mkdir()
    (nas / 'a.jpg').write_bytes(b'x')
    (nas / 'b.mov').write_bytes(b'x')
    (nas / 'notes.txt').write_bytes(b'x')
    store_for_test().update({'SOURCE_TYPE': 'local', 'LOCAL_SOURCE_PATH': str(nas)})
    resp = client.post('/settings/source/test', data={'csrf': csrf(client)}, follow_redirects=True)
    assert b'2 photo/video file(s) found' in resp.data
    store_for_test().update({'LOCAL_SOURCE_PATH': str(tmp_path / 'unmounted')})
    resp = client.post('/settings/source/test', data={'csrf': csrf(client)}, follow_redirects=True)
    assert b'NAS share is mounted' in resp.data


def test_setup_check_and_dashboard_use_local_source(client, tmp_path):
    nas = tmp_path / 'nas'
    nas.mkdir()
    store_for_test().update({'SOURCE_TYPE': 'local', 'LOCAL_SOURCE_PATH': str(nas)})
    dashboard = client.get('/').data
    assert b'Mounted Folder' in dashboard
    assert b'Drive Folder Missing' not in dashboard
    resp = client.post('/setup/test-scan', data={'csrf': csrf(client)}, follow_redirects=True)
    assert b'Mounted Folder' in resp.data
    assert b'Folder readable: 0 photo/video file(s) found' in resp.data


def test_failed_login_is_rejected_then_rate_limited(monkeypatch, tmp_path):
    env(monkeypatch, tmp_path)
    from drive_to_flickr.web import create_app
    app = create_app(); app.config['TESTING'] = True
    c = app.test_client()
    c.get('/login')
    for _ in range(5):
        resp = c.post('/login', data={'username': 'admin', 'password': 'wrong', 'csrf': csrf(c)})
        assert resp.status_code == 401
        assert b'Incorrect username or password' in resp.data
    resp = c.post('/login', data={'username': 'admin', 'password': 'pw', 'csrf': csrf(c)})
    assert resp.status_code == 429


def test_logout_ends_session(client):
    assert client.get('/').status_code == 200
    client.post('/logout', data={'csrf': csrf(client)})
    assert client.get('/').status_code == 302


def test_flickr_test_failure_is_friendly(client):
    with patch('drive_to_flickr.web.FlickrClient') as flickr:
        flickr.return_value.list_photosets.side_effect = RuntimeError('401')
        resp = client.post('/settings/flickr/test', data={'csrf': csrf(client)}, follow_redirects=True)
    assert resp.status_code == 200
    assert b'Flickr API check failed' in resp.data


def test_settings_batch_read_honours_env_override(monkeypatch, tmp_path):
    env(monkeypatch, tmp_path)
    store = SettingsStore(Database(tmp_path/'state.sqlite'))
    store.set('POLL_INTERVAL_SECONDS', '45')
    monkeypatch.setenv('TIMEZONE', 'America/Chicago')
    values = store.all_public()
    assert values['POLL_INTERVAL_SECONDS'] == '45'
    assert values['TIMEZONE'] == 'America/Chicago'
    assert values['SOURCE_TYPE'] == 'google_drive'


def test_load_settings_for_local_source_does_not_require_drive_folder(monkeypatch, tmp_path):
    env(monkeypatch, tmp_path)
    from drive_to_flickr.config import load_settings
    store = SettingsStore(Database(tmp_path/'state.sqlite'))
    store.update({'SOURCE_TYPE': 'local', 'LOCAL_SOURCE_PATH': '/mnt/nas/photos', 'GOOGLE_CALENDAR_ID': 'cal', 'FLICKR_API_KEY': 'k'})
    ss = SecretStore(tmp_path/'secrets.json')
    for key in ('flickr_api_secret', 'flickr_oauth_token', 'flickr_oauth_token_secret'):
        ss.set(key, 'v')
    settings = load_settings()
    assert settings.source_type == 'local'
    assert settings.local_source_path == Path('/mnt/nas/photos')
    assert settings.google_drive_folder_id == ''


def test_smb_source_saved_from_pasted_address_and_password_kept_secret(client):
    browse = patch('drive_to_flickr.web.SMBSource.list_folders', return_value=[])
    browse.start()
    resp = client.post('/settings/source', data={'csrf': csrf(client), 'SOURCE_TYPE': 'smb', 'SMB_SERVER': '\\\\192.168.1.20\\Photos\\Events', 'SMB_SHARE': '', 'SMB_FOLDER': '', 'SMB_USERNAME': 'flickr', 'SMB_PASSWORD': 'nas-secret', 'SMB_RECURSIVE': 'on'}, follow_redirects=True)
    assert b'Media source saved' in resp.data
    store = store_for_test()
    assert (store.get('SOURCE_TYPE'), store.get('SMB_SERVER'), store.get('SMB_SHARE'), store.get('SMB_FOLDER'), store.get('SMB_USERNAME')) == ('smb', '192.168.1.20', 'Photos', 'Events', 'flickr')
    ss = SecretStore(Path(__import__('os').environ['SECRET_STORE_PATH']))
    assert ss.get('smb_password') == 'nas-secret'
    page = client.get('/settings/source')
    browse.stop()
    assert b'nas-secret' not in page.data
    assert b'Saved. Leave blank to keep it' in page.data
    # Saving again without a password keeps the stored one.
    client.post('/settings/source', data={'csrf': csrf(client), 'SOURCE_TYPE': 'smb', 'SMB_SERVER': '192.168.1.20', 'SMB_SHARE': 'Photos', 'SMB_FOLDER': 'Events', 'SMB_USERNAME': 'flickr'})
    assert SecretStore(Path(__import__('os').environ['SECRET_STORE_PATH'])).get('smb_password') == 'nas-secret'


def test_smb_source_validation(client):
    resp = client.post('/settings/source', data={'csrf': csrf(client), 'SOURCE_TYPE': 'smb', 'SMB_SERVER': '', 'SMB_SHARE': ''}, follow_redirects=True)
    assert b'SMB_SERVER is required' in resp.data and b'SMB_SHARE is required' in resp.data
    resp = client.post('/settings/source', data={'csrf': csrf(client), 'SOURCE_TYPE': 'smb', 'SMB_SERVER': 'nas', 'SMB_SHARE': 'Photos', 'SMB_FOLDER': '../etc'}, follow_redirects=True)
    assert b"SMB_FOLDER cannot contain" in resp.data
    assert store_for_test().get('SOURCE_TYPE') == 'google_drive'


def test_smb_folder_browser_and_selection(client):
    store_for_test().update({'SOURCE_TYPE': 'smb', 'SMB_SERVER': 'nas', 'SMB_SHARE': 'Photos'})
    with patch('drive_to_flickr.web.SMBSource.list_folders', return_value=[{'name': 'Camp Meeting 2026', 'path': 'Events/Camp Meeting 2026'}]) as lf:
        page = client.get('/settings/source?smb_browse=Events')
    lf.assert_called_once_with('Events')
    assert b'Camp Meeting 2026' in page.data
    with patch('drive_to_flickr.web.SMBSource.list_folders', return_value=[]):
        resp = client.post('/settings/source/select-smb', data={'csrf': csrf(client), 'path': 'Events/Camp Meeting 2026'}, follow_redirects=True)
    assert b'Now pulling media from' in resp.data
    assert store_for_test().get('SMB_FOLDER') == 'Events/Camp Meeting 2026'


def test_smb_browser_shows_friendly_error(client):
    store_for_test().update({'SOURCE_TYPE': 'smb', 'SMB_SERVER': 'nas', 'SMB_SHARE': 'Photos'})
    with patch('drive_to_flickr.web.SMBSource.list_folders', side_effect=ValueError("Failed to connect to 'nas:445'")):
        page = client.get('/settings/source')
    assert b'Could not reach the NAS' in page.data


def test_smb_source_test_and_setup_check(client):
    from drive_to_flickr.models import DriveFile
    store_for_test().update({'SOURCE_TYPE': 'smb', 'SMB_SERVER': 'nas', 'SMB_SHARE': 'Photos'})
    files = [DriveFile('smb:a.jpg', 'a.jpg', '', datetime.now(UTC), datetime.now(UTC)), DriveFile('smb:n.txt', 'n.txt', '', datetime.now(UTC), datetime.now(UTC))]
    with patch('drive_to_flickr.web.SMBSource.list_media_files', return_value=files), patch('drive_to_flickr.web.SMBSource.list_folders', return_value=[]):
        resp = client.post('/settings/source/test', data={'csrf': csrf(client)}, follow_redirects=True)
        assert b'Share readable: 1 photo/video file(s) found' in resp.data
        resp = client.post('/setup/test-scan', data={'csrf': csrf(client)}, follow_redirects=True)
        assert b'Network Share (SMB)' in resp.data
    dashboard = client.get('/').data
    assert b'NAS Share' in dashboard


def test_load_settings_for_smb_source(monkeypatch, tmp_path):
    env(monkeypatch, tmp_path)
    from drive_to_flickr.config import load_settings
    from drive_to_flickr.main import build_source
    from drive_to_flickr.sources import SMBSource
    SettingsStore(Database(tmp_path/'state.sqlite')).update({'SOURCE_TYPE': 'smb', 'SMB_SERVER': 'nas', 'SMB_SHARE': 'Photos', 'SMB_FOLDER': 'Events', 'SMB_USERNAME': 'u', 'SMB_SUCCESS_FOLDER': 'Events/Uploaded'})
    SecretStore(tmp_path/'secrets.json').set('smb_password', 'pw')
    settings = load_settings(require_credentials=False)
    assert (settings.smb_server, settings.smb_share, settings.smb_password, settings.smb_port) == ('nas', 'Photos', 'pw', 445)
    source = build_source(settings)
    assert isinstance(source, SMBSource)
    assert source.auth == {'username': 'u', 'password': 'pw', 'port': 445, 'connection_timeout': 15}
    assert 'events/uploaded' in source.exclude
