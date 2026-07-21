"""Filesystem-protected secret storage for OAuth credentials."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

SECRET_KEYS = {
    "google_client_secret",
    "google_refresh_token",
    "google_token_json",
    "flickr_api_secret",
    "flickr_oauth_token",
    "flickr_oauth_token_secret",
    "flickr_request_token_secret",
}


class SecretStore:
    """Small JSON secret store protected by file permissions.

    This intentionally keeps secrets separate from normal configuration rows and
    never exposes values through web serializers. Operators can place this file
    on an encrypted volume if encryption-at-rest is required by policy.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self._write({})
        else:
            os.chmod(self.path, 0o600)

    def _read(self) -> dict[str, Any]:
        try:
            return dict(json.loads(self.path.read_text()))
        except (json.JSONDecodeError, FileNotFoundError):
            return {}

    def _write(self, data: dict[str, Any]) -> None:
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, indent=2, sort_keys=True))
        os.chmod(tmp, 0o600)
        tmp.replace(self.path)
        os.chmod(self.path, 0o600)

    def get(self, key: str, default: str = "") -> str:
        return str(self._read().get(key, default) or default)

    def set(self, key: str, value: str) -> None:
        if key not in SECRET_KEYS:
            raise KeyError(f"Unsupported secret key: {key}")
        data = self._read()
        data[key] = value
        self._write(data)

    def delete(self, *keys: str) -> None:
        data = self._read()
        for key in keys:
            data.pop(key, None)
        self._write(data)

    def has(self, key: str) -> bool:
        return bool(self.get(key))
