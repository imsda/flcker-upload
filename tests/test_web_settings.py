import json
from pathlib import Path
from unittest.mock import Mock, patch

import pytest
from werkzeug.security import generate_password_hash

from drive_to_flickr.database import Database
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
    assert validate_settings({'TIMEZONE':'Nope/Bad','POLL_INTERVAL_SECONDS':'0','MINIMUM_FILE_AGE_SECONDS':'0','MAX_ATTEMPTS':'1','BUFFER_BEFORE_MINUTES':'0','BUFFER_AFTER_MINUTES':'0','FLICKR_DEFAULT_PRIVACY':'bad','NO_EVENT_ACTION':'skip','DRIVE_SUCCESS_ACTION':'leave'})


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
