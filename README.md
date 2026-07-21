# drive-to-flickr

`drive-to-flickr` is a production-oriented Python 3.12+ Linux service that polls a configured Google Drive folder, downloads supported photo/video files, determines their capture time, matches that time against a dedicated Google Calendar, uploads the media to Flickr, and assigns it to a Flickr album/photoset.

## Architecture

The service is split into modules: `config` validates environment settings, `database` owns SQLite state, `drive` uses the official Google Drive API, `calendar` uses the official Google Calendar API, `metadata` shells out to ExifTool, `matcher` parses event descriptions and selects albums, `flickr` calls Flickr REST/upload APIs directly with OAuth 1.0a, and `processor` coordinates the idempotent pipeline.

## API implementation decision

This project uses the Google Drive API directly instead of rclone. Google documents Drive API file listing, search, and media download behavior, and the Google Calendar API exposes event listing through official REST/client-library surfaces. Flickr's own API page notes that third-party API kits are not maintained by Flickr, so this service uses direct `requests`/OAuth calls rather than abandoned Flickr Python wrappers. See the current official references checked during implementation: Google Drive Python quickstart, Drive `files.list`/download docs, Google Calendar events list docs, and Flickr REST/upload/photosets/OAuth docs.

## Recommended web setup

Normal application configuration is now managed in the authenticated web UI at `http://127.0.0.1:8080/setup`. The recommended deployment model is:

1. Create or use a dedicated Google account, for example `flickr-uploader@example.org`.
2. Share the desired Google Drive upload folder with that account. The account does not need to own the folder.
3. Share or add the desired Google Calendar to that account. The account does not need to own the calendar.
4. Open the drive-to-flickr web UI.
5. Connect the Google account with standard OAuth 2.0 Web Server authorization.
6. Select the Drive folder from My Drive, shared folders, or accessible Shared Drives.
7. Select the Calendar from calendars visible in the authenticated account's Calendar list.
8. Configure and connect Flickr.
9. Run **Test Configuration** to perform read-only access checks for Google Account, Drive, Calendar, and Flickr.
10. Start the worker.

The worker records a heartbeat in the application database while it runs. The dashboard reports it as **Running** when recent, **Stale** when the worker has stopped reporting, or **Not started** when no heartbeat has been recorded.

The UI stores normal settings in SQLite and stores OAuth/API secrets separately in a filesystem-protected secret file. OAuth tokens are never displayed in HTML or JSON responses. Existing `GOOGLE_DRIVE_FOLDER_ID` and `GOOGLE_CALENDAR_ID` environment variables are still honored/imported for backward compatibility.

## Minimal bootstrap settings

Only settings that protect or start the web server must be supplied outside the UI:

* `ADMIN_PASSWORD_HASH`: password hash for the admin login, for example generated with `python -c "from werkzeug.security import generate_password_hash; print(generate_password_hash('change-me'))"`.
* `WEB_SECRET_KEY`: a random Flask session secret.
* Optional `PUBLIC_BASE_URL`: the public HTTPS base URL used to generate OAuth callback URLs, for example `https://flickr-upload.example.org`. If unset, Flask generates the external URL from the current request.
* Optional legacy `GOOGLE_CREDENTIALS_FILE`: path to an existing Google OAuth web-client JSON, default `/etc/drive-to-flickr/google-client.json`. New installs should paste the Client ID and Client Secret into the web UI instead.
* Optional path/binding settings: `DATABASE_PATH`, `SECRET_STORE_PATH`, `STAGING_DIR`, `WEB_BIND` (defaults to `127.0.0.1`), and `WEB_PORT` (defaults to `8080`).

## Google Cloud setup

Google requires an OAuth client to identify this application, but you no longer need to download or install a `google-client.json` file for normal setup. Create the OAuth application in Google Cloud, then paste the credentials into drive-to-flickr's web UI.

1. Create a Google Cloud project.
2. Enable the Google Drive API.
3. Enable the Google Calendar API.
4. Configure the Google Auth consent screen for your deployment and add the scopes requested by drive-to-flickr.
5. Create **OAuth Client ID -> Web Application**.
6. Open drive-to-flickr Settings -> Google Account and copy the displayed OAuth Callback URL, such as `https://flickr-upload.example.org/oauth/google/callback`, into Google's **Authorized Redirect URIs**. Set `PUBLIC_BASE_URL` first if the app is behind a reverse proxy or has a stable public URL.
7. Copy the Google Client ID and Client Secret into Settings -> Google Account -> Google OAuth Application, then click **Save OAuth Settings**. The Client Secret is stored in the SecretStore and is masked after saving.
8. Click **Connect Google Account**.
9. Sign in to Google normally and approve the Drive and Calendar permissions.
10. Choose the shared Drive folder in Settings -> Google Drive. The connected Google account only needs access to the folder; it does not need to own it.
11. Choose the shared Calendar in Settings -> Calendar.

Existing deployments may keep using `GOOGLE_CREDENTIALS_FILE` as a legacy fallback. UI-configured Client ID/Secret values take precedence over the JSON file.

## Google Drive and Calendar setup

Use the Settings -> Google Drive folder browser and Settings -> Calendar selector instead of manually copying IDs. The browser uses the official Drive API with all-drives support so folders shared with the authenticated account and accessible Shared Drives are selectable. Calendar selection uses the authenticated account's CalendarList.

## Flickr API/OAuth setup

Create a Flickr app, enter its API key/secret on Settings -> Flickr, then click Connect Flickr. The legacy `drive-to-flickr auth-flickr` CLI remains available. Flickr photosets require a primary photo when created, so the first uploaded media item becomes the primary photo for a newly-created album.

## Calendar metadata syntax

Calendar descriptions can contain:

```text
FLICKR: true
Album: Camp Meeting 2026
Privacy: public
Tags: camp-meeting, rooted-together, 2026
Description: Optional Flickr description
Buffer Before: 360
Buffer After: 360
```

Supported fields are `FLICKR`, `Album`, `Privacy`, `Tags`, `Description`, `Buffer Before`, and `Buffer After`. If `REQUIRE_FLICKR_MARKER=true`, only events with `FLICKR: true` qualify. `FLICKR: false` always excludes an event.

## Example event

Title: `Camp Meeting 2026`

Start: `2026-06-02`

End: `2026-06-07`

Description:

```text
FLICKR: true
Album: Camp Meeting 2026
Privacy: public
Tags: camp-meeting, rooted-together, 2026
Buffer Before: 360
Buffer After: 360
```

A photo captured on June 4, 2026 at 7:42 PM in `America/Chicago` matches the multi-day event and is uploaded into the Flickr album `Camp Meeting 2026`.

## Installation

```bash
sudo apt-get update
sudo apt-get install -y python3.12 python3.12-venv exiftool
sudo mkdir -p /opt/drive-to-flickr
sudo cp -a . /opt/drive-to-flickr
cd /opt/drive-to-flickr
python3.12 -m venv .venv
. .venv/bin/activate
pip install -e .
sudo install -m 600 .env.example /etc/drive-to-flickr/drive-to-flickr.env
```

Create `/etc/drive-to-flickr/drive-to-flickr.env` with only the bootstrap settings listed above and keep it mode `0600`. Then open the setup wizard at `http://127.0.0.1:8080/setup`.

## First-run authentication

Start the web UI with `drive-to-flickr web` or `drive-to-flickr-web`, log in with the bootstrap admin account, and follow the setup wizard: General Settings, Google OAuth Application, Connect Google Account, Select Google Drive Folder, Select Google Calendar, Connect Flickr, Test Configuration, and Finish. The normal Google sign-in/consent screen is still used to connect the Google user account. The legacy `auth-google` and `auth-flickr` CLI commands remain for existing deployments and recovery use.

## Running manually

```bash
drive-to-flickr test-drive
drive-to-flickr test-calendar '2026-06-04T19:42:00-05:00'
drive-to-flickr test-flickr
drive-to-flickr scan
```

## Dry-run/testing

```bash
drive-to-flickr dry-run
# or
drive-to-flickr scan --dry-run
```

Dry-run prints filename, capture timestamp, matching event, intended album, privacy, and tags without uploading or modifying Drive/Flickr.

## systemd setup

```bash
./scripts/install-systemd.sh
sudo systemctl enable --now drive-to-flickr
sudo journalctl -u drive-to-flickr -f
```

The unit runs as `flickruploader`, uses `/opt/drive-to-flickr`, `/var/lib/drive-to-flickr`, and `/etc/drive-to-flickr`, and enables hardening such as `NoNewPrivileges`, `PrivateTmp`, `ProtectSystem=strict`, and `ProtectHome=true`.

## Configuration

Settings -> General, Google Account, Google Drive, Calendar, Flickr, and No Event Behavior are the primary configuration surfaces. SQLite table `app_settings` persists normal settings. See `.env.example` for legacy environment variables and overrides. Important defaults: `POLL_INTERVAL_SECONDS=120`, `MINIMUM_FILE_AGE_SECONDS=60`, `NO_EVENT_ACTION=unassigned`, `UNASSIGNED_ALBUM=Unassigned Uploads`, `FLICKR_DEFAULT_PRIVACY=private`, and zero-minute global buffers.

## Duplicate prevention and recovery

The primary identity is Google Drive file ID, stored uniquely in SQLite. Checksums are stored when Drive provides them. Every upload includes a hashed Drive-ID machine tag so `reconcile` and retry paths can search Flickr before re-uploading after a crash. If a crash happens after Flickr upload but before SQLite completion, the next attempt searches for that tag and reuses the existing Flickr photo ID where possible, then finishes photoset assignment. This minimizes duplicate uploads, although Flickr search indexing latency can delay reconciliation.

## Troubleshooting

* Install ExifTool if metadata extraction falls back to Drive timestamps.
* Verify `/etc/drive-to-flickr` and `/var/lib/drive-to-flickr` are owned by `flickruploader` and not world-readable.
* Use `drive-to-flickr status` for SQLite status counts.
* Use `journalctl -u drive-to-flickr` for structured processing logs.

## Database location

SQLite defaults to `/var/lib/drive-to-flickr/state.sqlite`. Tables are automatically initialized at startup.

## Security considerations

Do not hard-code credentials. Keep environment, Google client, and secret-store files mode `0600`, owned by `flickruploader`. The service never logs or renders API keys, OAuth tokens, token secrets, refresh tokens, or client secrets. Original Drive files are never deleted automatically; successful files may optionally be moved.
