# drive-to-flickr

`drive-to-flickr` is a production-oriented Python 3.12+ Linux service that polls a configured Google Drive folder, downloads supported photo/video files, determines their capture time, matches that time against a dedicated Google Calendar, uploads the media to Flickr, and assigns it to a Flickr album/photoset.

## Architecture

The service is split into modules: `config` validates environment settings, `database` owns SQLite state, `drive` uses the official Google Drive API, `calendar` uses the official Google Calendar API, `metadata` shells out to ExifTool, `matcher` parses event descriptions and selects albums, `flickr` calls Flickr REST/upload APIs directly with OAuth 1.0a, and `processor` coordinates the idempotent pipeline.

## API implementation decision

This project uses the Google Drive API directly instead of rclone. Google documents Drive API file listing, search, and media download behavior, and the Google Calendar API exposes event listing through official REST/client-library surfaces. Flickr's own API page notes that third-party API kits are not maintained by Flickr, so this service uses direct `requests`/OAuth calls rather than abandoned Flickr Python wrappers. See the current official references checked during implementation: Google Drive Python quickstart, Drive `files.list`/download docs, Google Calendar events list docs, and Flickr REST/upload/photosets/OAuth docs.

## Google Cloud setup

1. Create a Google Cloud project.
2. Enable Google Drive API and Google Calendar API.
3. Create an OAuth desktop client and download it as `/etc/drive-to-flickr/google-client.json`.
4. Share the watched Drive folder and the dedicated calendar with the Google account used for OAuth.

## Google Drive and Calendar setup

Create a Drive folder for incoming media and copy its folder ID into `GOOGLE_DRIVE_FOLDER_ID`. Create a dedicated calendar, for example **Flickr Albums**, and put its ID in `GOOGLE_CALENDAR_ID`. Only this configured calendar is searched.

## Flickr API/OAuth setup

Create a Flickr app, copy the API key/secret into the environment file, then run headless OAuth with `drive-to-flickr auth-flickr`. Flickr photosets require a primary photo when created, so the first uploaded media item becomes the primary photo for a newly-created album.

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

Edit `/etc/drive-to-flickr/drive-to-flickr.env` and keep it mode `0600` because it contains OAuth tokens and API secrets.

## First-run authentication

On a headless server:

```bash
sudo -u flickruploader /opt/drive-to-flickr/.venv/bin/drive-to-flickr auth-google
sudo -u flickruploader /opt/drive-to-flickr/.venv/bin/drive-to-flickr auth-flickr
```

The commands print URLs to open on another machine and then prompt for verifier codes. Store Flickr tokens in the environment file.

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

See `.env.example` for all settings. Important defaults: `POLL_INTERVAL_SECONDS=120`, `MINIMUM_FILE_AGE_SECONDS=60`, `NO_EVENT_ACTION=unassigned`, `UNASSIGNED_ALBUM=Unassigned Uploads`, `FLICKR_DEFAULT_PRIVACY=private`, and zero-minute global buffers.

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

Do not hard-code credentials. Keep environment and token files mode `0600`, owned by `flickruploader`. The service never logs API keys, OAuth tokens, or token secrets. Original Drive files are never deleted automatically; successful files may optionally be moved.
