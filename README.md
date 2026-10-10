# drive-to-flickr

`drive-to-flickr` is a production-oriented Python 3.12+ Linux service that polls a configured Google Drive folder **or a local/NAS folder**, downloads supported photo/video files, determines their capture time, matches that time against a dedicated Google Calendar, uploads the media to Flickr, and assigns it to a Flickr album/photoset.

## Architecture

The service is split into modules: `config` validates environment settings, `database` owns SQLite state, `sources` abstracts where media comes from (Google Drive or a local/NAS folder), `drive` uses the official Google Drive API, `calendar` uses the official Google Calendar API, `metadata` shells out to ExifTool, `matcher` parses event descriptions and selects albums, `flickr` calls Flickr REST/upload APIs directly with OAuth 1.0a, and `processor` coordinates the idempotent pipeline.

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

## Choosing the media source: Google Drive or a local NAS

Open **Settings → Source** and pick where media is pulled from:

* **Google Drive folder** (default): choose the folder on the Drive page.
* **Local / NAS folder**: the worker reads photos and videos straight from a folder on the server, such as a mounted NAS share. Files are read in place rather than copied to the staging area, and subfolders are included unless you untick **Include subfolders**. Hidden files and NAS metadata folders such as Synology's `@eaDir` are skipped.

The Calendar still comes from Google either way, so a Google account must be connected even when the source is a NAS.

To use a NAS, mount the share on the server first. Example `/etc/fstab` lines:

```text
# SMB/CIFS (Synology, QNAP, Windows shares). Keep credentials in a 0600 file.
//nas.local/photos  /mnt/nas/photos  cifs  credentials=/etc/drive-to-flickr/nas-credentials,uid=flickruploader,gid=flickruploader,ro,_netdev,nofail  0  0
# NFS
nas.local:/volume1/photos  /mnt/nas/photos  nfs  ro,_netdev,nofail  0  0
```

Then enter `/mnt/nas/photos` on the Source page, or browse to it and click **Use this folder**. Click **Test Source** to confirm the service can read it and see how many photos and videos it found.

If the share is unmounted, scans log an error and nothing is marked as processed, so files are picked up once the share is back. Each file is identified by its path relative to the source folder. Renaming or moving a file inside the share makes it look new.

**Moving files after upload.** To move uploaded files out of the watched folder, set **General → After successful upload** to `move` and fill in **Move uploaded files to** on the Source page. A move-to folder inside the watched folder is excluded from scanning. Moving needs a read-write mount (drop `ro` above), and the systemd unit must be allowed to write there:

```bash
sudo systemctl edit drive-to-flickr
# add:
# [Service]
# ReadWritePaths=/mnt/nas/photos /mnt/nas/photos-uploaded
```

Read-only access works with the shipped unit unchanged.

## Minimal bootstrap settings

Only settings that protect or start the web server must be supplied outside the UI:

* `ADMIN_PASSWORD_HASH`: password hash for the admin login, for example generated with `python -c "from werkzeug.security import generate_password_hash; print(generate_password_hash('change-me'))"`.
* `WEB_SECRET_KEY`: a random Flask session secret. Without it, every web UI restart signs everyone out.
* Optional `SESSION_COOKIE_SECURE=true`: only send the login cookie over HTTPS. Set this when the UI is served over HTTPS (for example behind a reverse proxy).
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
sudo mkdir -p /opt/flcker-upload
sudo cp -a . /opt/flcker-upload
cd /opt/flcker-upload
python3.12 -m venv .venv
. .venv/bin/activate
pip install -e .
sudo install -m 600 .env.example /etc/drive-to-flickr/drive-to-flickr.env
```

Create `/etc/drive-to-flickr/drive-to-flickr.env` with only the bootstrap settings listed above and keep it mode `0600`. Then open the setup wizard at `http://127.0.0.1:8080/setup`.

## First-run authentication

Start the web UI with `drive-to-flickr web` or `drive-to-flickr-web` (served by Waitress), log in with the bootstrap admin account, and follow the setup wizard: General Settings, Google OAuth Application, Connect Google Account, Select Google Drive Folder, Select Google Calendar, Connect Flickr, Test Configuration, and Finish. The normal Google sign-in/consent screen is still used to connect the Google user account. The legacy `auth-google` and `auth-flickr` CLI commands remain for existing deployments and recovery use.

## Running manually

```bash
drive-to-flickr test-source      # lists files in the configured Drive or local/NAS folder (alias: test-drive)
drive-to-flickr test-calendar '2026-06-04T19:42:00-05:00'
drive-to-flickr test-flickr
drive-to-flickr scan
drive-to-flickr retry            # re-queue every failed file, then scan
drive-to-flickr reconcile        # record Flickr photo IDs for uploads that finished but were not recorded
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

The provided unit uses `/opt/flcker-upload`, `/var/lib/drive-to-flickr`, and `/etc/drive-to-flickr`, and enables hardening such as `NoNewPrivileges`, `PrivateTmp`, `ProtectSystem=strict`, and `ProtectHome=true`. Adjust `User` and `Group` in the unit to match the account that owns the application and protected secret store on an existing deployment.

## Processing, retries and performance

* The worker reloads its settings before every scan, so changes made in the web UI take effect without a restart.
* Up to `WORKER_CONCURRENCY` files (General → Parallel uploads; default 3, max 8) are downloaded, matched and uploaded at the same time. Album creation is serialised so parallel uploads never create duplicate albums.
* Calendar events are fetched once per capture day per scan rather than once per photo. The query covers the day before and after, so buffers that cross midnight still match.
* Uploads stream from disk instead of being loaded into memory, which matters for large videos.
* A failed file is retried with exponential backoff (1, 2, 4, 8… minutes, capped at one hour) without holding up other files. After `MAX_ATTEMPTS` it is marked `failed` and left alone until you click **Retry**. Files in `manual_review` are also left alone until retried.
* With `NO_EVENT_ACTION=skip`, files with no matching event are checked against the calendar again once an hour.
* Staged Google Drive downloads are deleted as soon as each file finishes, and leftovers from a crash are removed when the worker starts.

## Configuration

Settings -> General, Google Account, Google Drive, Calendar, Flickr, and No Event Behavior are the primary configuration surfaces. SQLite table `app_settings` persists normal settings. See `.env.example` for legacy environment variables and overrides. Important defaults: `SOURCE_TYPE=google_drive`, `WORKER_CONCURRENCY=3`, `POLL_INTERVAL_SECONDS=120`, `MINIMUM_FILE_AGE_SECONDS=60`, `NO_EVENT_ACTION=unassigned`, `UNASSIGNED_ALBUM=Unassigned Uploads`, `FLICKR_DEFAULT_PRIVACY=private`, and zero-minute global buffers.

## Duplicate prevention and recovery

The primary identity is Google Drive file ID, stored uniquely in SQLite. Checksums are stored when Drive provides them. Every upload includes a hashed file-ID machine tag so `reconcile` and retry paths can search Flickr before re-uploading after a crash. The search runs only for files with an earlier attempt, so first uploads skip that extra API call. If a crash happens after Flickr upload but before SQLite completion, the next attempt searches for that tag and reuses the existing Flickr photo ID where possible, then finishes photoset assignment. This minimizes duplicate uploads, although Flickr search indexing latency can delay reconciliation.

## Troubleshooting

* Install ExifTool if metadata extraction falls back to Drive timestamps.
* Verify `/etc/drive-to-flickr` and `/var/lib/drive-to-flickr` are owned by `flickruploader` and not world-readable.
* Use `drive-to-flickr status` for SQLite status counts.
* Use `journalctl -u drive-to-flickr` for structured processing logs.

## Database location

SQLite defaults to `/var/lib/drive-to-flickr/state.sqlite`. Tables are automatically initialized at startup.

## Security considerations

Do not hard-code credentials. Keep environment, Google client, and secret-store files mode `0600`, owned by `flickruploader`. The service never logs or renders API keys, OAuth tokens, token secrets, refresh tokens, or client secrets. Original Drive and NAS files are never deleted automatically; successful files may optionally be moved. Failed admin sign-ins are rate-limited (5 attempts per 15 minutes per IP), and sessions expire after 12 hours.
