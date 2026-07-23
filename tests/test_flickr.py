from pathlib import Path
from urllib.parse import parse_qs
from unittest.mock import Mock

import pytest

from drive_to_flickr.flickr import FlickrClient, UPLOAD_URL


def test_upload_signs_all_metadata_but_not_photo(tmp_path: Path):
    photo = tmp_path / "photo.jpg"
    photo.write_bytes(b"jpeg-data")
    client = FlickrClient("api-key", "api-secret", "token", "token-secret")
    client.upload_signer.sign = Mock(return_value=(UPLOAD_URL, {"Authorization": "OAuth signed"}, None))
    response = Mock(ok=True, text='<rsp stat="ok"><photoid>12345</photoid></rsp>')
    client.upload_session.post = Mock(return_value=response)

    photo_id = client.upload(
        photo,
        title="Camp",
        description="Summer photos",
        tags=("camp", "2026"),
        privacy="private",
        date_taken="2026-07-21T17:00:00-05:00",
    )

    assert photo_id == "12345"
    sign_call = client.upload_signer.sign.call_args
    signed_fields = parse_qs(sign_call.kwargs["body"], keep_blank_values=True)
    assert signed_fields == {
        "api_key": ["api-key"],
        "title": ["Camp"],
        "description": ["Summer photos"],
        "tags": ["camp 2026"],
        "is_public": ["0"],
        "is_friend": ["0"],
        "is_family": ["0"],
        "date_taken": ["2026-07-21T17:00:00-05:00"],
    }
    assert "photo" not in signed_fields
    post_call = client.upload_session.post.call_args
    assert post_call.kwargs["headers"] == {"Authorization": "OAuth signed"}
    assert post_call.kwargs["files"]["photo"].name == str(photo)


def test_upload_reports_safe_flickr_error(tmp_path: Path):
    photo = tmp_path / "photo.jpg"
    photo.write_bytes(b"jpeg-data")
    client = FlickrClient("api-key", "api-secret", "token", "token-secret")
    client.upload_signer.sign = Mock(return_value=(UPLOAD_URL, {"Authorization": "OAuth signed"}, None))
    client.upload_session.post = Mock(
        return_value=Mock(
            ok=False,
            status_code=401,
            reason="Unauthorized",
            text="oauth_problem=signature_invalid&debug_sbs=contains-sensitive-token",
        )
    )

    with pytest.raises(RuntimeError, match="HTTP 401.*signature invalid") as error:
        client.upload(photo, title="Photo", description=None, tags=(), privacy="private")
    assert "sensitive-token" not in str(error.value)
