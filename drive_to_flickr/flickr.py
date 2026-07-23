"""Flickr REST, upload, OAuth, and photoset handling."""
from __future__ import annotations

import hashlib
from pathlib import Path
from urllib.parse import parse_qs, urlencode
from xml.etree import ElementTree

import requests
from oauthlib.oauth1 import Client as OAuth1Client
from requests_oauthlib import OAuth1Session

from .matcher import normalize_album_name

API_URL = "https://www.flickr.com/services/rest/"
UPLOAD_URL = "https://up.flickr.com/services/upload/"
REQUEST_TOKEN_URL = "https://www.flickr.com/services/oauth/request_token"
AUTHORIZE_URL = "https://www.flickr.com/services/oauth/authorize"
ACCESS_TOKEN_URL = "https://www.flickr.com/services/oauth/access_token"

PRIVACY_MAP = {
    "public": {"is_public": "1", "is_friend": "0", "is_family": "0"},
    "private": {"is_public": "0", "is_friend": "0", "is_family": "0"},
    "friends": {"is_public": "0", "is_friend": "1", "is_family": "0"},
    "family": {"is_public": "0", "is_friend": "0", "is_family": "1"},
    "friends-and-family": {"is_public": "0", "is_friend": "1", "is_family": "1"},
}


class FlickrClient:
    """Minimal maintained direct Flickr API client using OAuth 1.0a and requests."""

    def __init__(self, api_key: str, api_secret: str, token: str, token_secret: str) -> None:
        self.api_key = api_key
        self.session = OAuth1Session(api_key, client_secret=api_secret, resource_owner_key=token, resource_owner_secret=token_secret)
        self.upload_signer = OAuth1Client(api_key, client_secret=api_secret, resource_owner_key=token, resource_owner_secret=token_secret)
        self.upload_session = requests.Session()

    @staticmethod
    def authorize(api_key: str, api_secret: str) -> tuple[str, str]:
        oauth = OAuth1Session(api_key, client_secret=api_secret, callback_uri="oob")
        tokens = oauth.fetch_request_token(REQUEST_TOKEN_URL)
        print(f"Open this URL, authorize with Flickr, then paste verifier:\n{oauth.authorization_url(AUTHORIZE_URL, perms='write')}")
        verifier = input("Verifier: ").strip()
        oauth = OAuth1Session(api_key, client_secret=api_secret, resource_owner_key=tokens["oauth_token"], resource_owner_secret=tokens["oauth_token_secret"], verifier=verifier)
        access = oauth.fetch_access_token(ACCESS_TOKEN_URL)
        return access["oauth_token"], access["oauth_token_secret"]

    def _call(self, method: str, **params: str) -> dict:
        payload = {"method": method, "api_key": self.api_key, "format": "json", "nojsoncallback": "1", **params}
        response = self.session.post(API_URL, data=payload, timeout=60)
        response.raise_for_status()
        data = response.json()
        if data.get("stat") != "ok":
            raise RuntimeError(f"Flickr API error {data.get('code')}: {data.get('message')}")
        return dict(data)

    def upload(self, path: Path, *, title: str, description: str | None, tags: tuple[str, ...], privacy: str, date_taken: str | None = None) -> str:
        data = {"api_key": self.api_key, "title": title, "description": description or "", "tags": " ".join(tags), **PRIVACY_MAP[privacy]}
        if date_taken:
            data["date_taken"] = date_taken
        # OAuth normally excludes multipart fields from its signature. Flickr's upload
        # API instead requires every form field except the photo bytes to be signed.
        # Sign the equivalent form-encoded body, then reuse only its Authorization
        # header on the actual multipart request.
        _, signed_headers, _ = self.upload_signer.sign(
            UPLOAD_URL,
            http_method="POST",
            body=urlencode(data),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        with path.open("rb") as fh:
            response = self.upload_session.post(
                UPLOAD_URL,
                data=data,
                files={"photo": fh},
                headers={"Authorization": signed_headers["Authorization"]},
                timeout=300,
            )
        try:
            root = ElementTree.fromstring(response.text)
        except ElementTree.ParseError:
            root = None
        error = root.find(".//err") if root is not None else None
        if error is not None:
            raise RuntimeError(
                f"Flickr upload failed (code {error.get('code', 'unknown')}): "
                f"{error.get('msg', 'Unknown Flickr error')}"
            )
        if not response.ok:
            oauth_problem = parse_qs(response.text).get("oauth_problem", [""])[0]
            detail = oauth_problem.replace("_", " ") if oauth_problem else response.reason
            raise RuntimeError(f"Flickr upload failed (HTTP {response.status_code}): {detail}")
        photo_id = root.findtext(".//photoid") if root is not None else None
        if not photo_id:
            raise RuntimeError("Flickr returned an unexpected upload response without a photo ID")
        return str(photo_id)

    def list_photosets(self) -> dict[str, tuple[str, str]]:
        data = self._call("flickr.photosets.getList")
        result: dict[str, tuple[str, str]] = {}
        for ps in data.get("photosets", {}).get("photoset", []):
            title = ps.get("title", {}).get("_content", "")
            result[normalize_album_name(title)] = (ps["id"], title)
        return result

    def create_photoset(self, title: str, primary_photo_id: str) -> str:
        data = self._call("flickr.photosets.create", title=title, primary_photo_id=primary_photo_id)
        return str(data["photoset"]["id"])

    def add_photo_to_photoset(self, photoset_id: str, photo_id: str) -> None:
        self._call("flickr.photosets.addPhoto", photoset_id=photoset_id, photo_id=photo_id)

    def find_uploaded_by_drive_id(self, drive_file_id: str) -> str | None:
        tag = f"drive-to-flickr:id={hashlib.sha256(drive_file_id.encode()).hexdigest()[:24]}"
        data = self._call("flickr.photos.search", user_id="me", tags=tag, extras="tags")
        photos = data.get("photos", {}).get("photo", [])
        return str(photos[0]["id"]) if photos else None


def drive_id_tag(drive_file_id: str) -> str:
    return f"drive-to-flickr:id={hashlib.sha256(drive_file_id.encode()).hexdigest()[:24]}"
