"""Profile photo upload/delete (POST/DELETE /auth/me/avatar) and the extended profile fields."""
import io
from collections.abc import Iterator
from datetime import date
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.core.clock import IST, utcnow
from app.models.user import User
from app.schemas.auth import age_on
from tests.factories import auth_headers, create_user

ME = "/api/v1/auth/me"
AVATAR = "/api/v1/auth/me/avatar"
EXIF_ORIENTATION = 0x0112
EXIF_MAKE = 0x010F


@pytest.fixture
def media_root(tmp_path: Path, test_app: FastAPI) -> Iterator[Path]:
    """Point MEDIA_ROOT at a temporary folder (and use a small size limit) for these tests."""
    root = tmp_path / "media"
    settings: Settings = get_settings().model_copy(update={"media_root": root, "avatar_max_bytes": 300_000})
    test_app.dependency_overrides[get_settings] = lambda: settings
    yield root
    test_app.dependency_overrides.pop(get_settings, None)


@pytest.fixture
def user(db: Session) -> User:
    return create_user(db, email="aarav@testmail.in", full_name="Aarav Sharma")


def image_bytes(fmt: str = "JPEG", size: tuple[int, int] = (800, 600), color=(200, 120, 40), exif=None, **save) -> bytes:
    mode = "RGBA" if fmt == "PNG" and save.pop("alpha", False) else "RGB"
    img = Image.new(mode, size, color if mode == "RGB" else (*color, 128))
    # Paint the left half differently so we can check the crop / rotation.
    img.paste((10, 10, 10) if mode == "RGB" else (10, 10, 10, 255), (0, 0, size[0] // 2, size[1]))
    out = io.BytesIO()
    if exif is not None:
        save["exif"] = exif
    img.save(out, format=fmt, **save)
    return out.getvalue()


def upload(client: TestClient, user: User, data: bytes, name: str = "photo.jpg", ctype: str = "image/jpeg"):
    return client.post(AVATAR, files={"file": (name, data, ctype)}, headers=auth_headers(user))


def stored_file(media_root: Path, body: dict) -> Path:
    url = body["data"]["avatar_url"]
    assert url.startswith("/media/avatars/")
    return media_root / url.removeprefix("/media/")


# --------------------------------------------------------------------------- upload


def test_valid_jpeg_becomes_512_webp(client: TestClient, user: User, media_root: Path, db: Session) -> None:
    response = upload(client, user, image_bytes("JPEG", (1200, 800)))
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["message"] == "Profile photo updated."
    path = stored_file(media_root, body)
    assert path.parent == media_root / "avatars" / str(user.id)
    assert path.suffix == ".webp"
    with Image.open(path) as saved:
        assert saved.format == "WEBP"
        assert saved.size == (512, 512)
    db.refresh(user)
    assert user.avatar == f"avatars/{user.id}/{path.name}"
    # The profile reflects it too.
    me = client.get(ME, headers=auth_headers(user)).json()["data"]
    assert me["avatar_url"] == body["data"]["avatar_url"]
    assert me["initials"] == "AS"
    assert "avatar" not in me


def test_png_metadata_is_stripped_and_orientation_applied(client: TestClient, user: User, media_root: Path) -> None:
    exif = Image.Exif()
    exif[EXIF_MAKE] = "SpyCam"
    exif[EXIF_ORIENTATION] = 6  # rotate 90° CW on display
    raw = image_bytes("PNG", (600, 300), exif=exif.tobytes())
    with Image.open(io.BytesIO(raw)) as check:  # sanity: the source really carries EXIF
        assert check.getexif().get(EXIF_MAKE) == "SpyCam"

    response = upload(client, user, raw, name="me.png", ctype="image/png")
    assert response.status_code == 200, response.text
    path = stored_file(media_root, response.json())
    data = path.read_bytes()
    assert b"SpyCam" not in data
    with Image.open(path) as saved:
        assert saved.size == (512, 512)
        assert not saved.getexif()
        assert "exif" not in saved.info and "xmp" not in saved.info and "icc_profile" not in saved.info
        # 600×300 with a dark left half, rotated 90° CW → dark TOP half; centre-crop keeps both.
        top = saved.convert("RGB").getpixel((256, 20))
        bottom = saved.convert("RGB").getpixel((256, 490))
        assert sum(top) < 100 < sum(bottom)


def test_webp_with_alpha_is_accepted(client: TestClient, user: User, media_root: Path) -> None:
    raw = image_bytes("WEBP", (300, 300))
    response = upload(client, user, raw, name="a.webp", ctype="image/webp")
    assert response.status_code == 200, response.text
    raw = image_bytes("PNG", (300, 300), alpha=True)
    response = upload(client, user, raw, name="a.png", ctype="image/png")
    assert response.status_code == 200, response.text
    with Image.open(stored_file(media_root, response.json())) as saved:
        assert saved.mode == "RGBA"


@pytest.mark.parametrize(
    ("data", "name", "ctype"),
    [
        (b"%PDF-1.7 definitely not an image" * 20, "photo.jpg", "image/jpeg"),  # spoofed extension + type
        (b"<svg xmlns='http://www.w3.org/2000/svg'/>", "photo.png", "image/png"),
        (b"", "empty.jpg", "image/jpeg"),
    ],
)
def test_non_images_are_rejected(client: TestClient, user: User, media_root: Path, data, name, ctype) -> None:
    response = upload(client, user, data, name=name, ctype=ctype)
    assert response.status_code == 400, response.text
    assert response.json()["error"]["code"] == "INVALID_FILE"
    assert not (media_root / "avatars").exists() or not any((media_root / "avatars").rglob("*.webp"))


def test_disallowed_formats_and_truncated_files_are_rejected(client: TestClient, user: User, media_root: Path) -> None:
    gif = image_bytes("GIF", (200, 200))
    assert upload(client, user, gif, name="fake.png", ctype="image/png").status_code == 400
    bmp = image_bytes("BMP", (200, 200))
    assert upload(client, user, bmp, name="x.jpg").status_code == 400
    jpeg = image_bytes("JPEG", (400, 400))
    truncated = jpeg[: len(jpeg) // 3]
    assert upload(client, user, truncated).status_code == 400
    tiny = image_bytes("JPEG", (20, 20))
    assert upload(client, user, tiny).status_code == 400


def test_oversize_upload_is_rejected_before_decoding(client: TestClient, user: User, media_root: Path) -> None:
    big = b"\xff\xd8\xff" + b"0" * 400_000  # over the 300 kB test limit; not even a valid JPEG
    response = upload(client, user, big)
    assert response.status_code == 413
    body = response.json()
    assert body["error"]["code"] == "FILE_TOO_LARGE"
    assert "0.3 MB" in body["error"]["message"]


def test_replacing_deletes_previous_file(client: TestClient, user: User, media_root: Path) -> None:
    first = stored_file(media_root, upload(client, user, image_bytes()).json())
    second = stored_file(media_root, upload(client, user, image_bytes(color=(1, 200, 1))).json())
    assert first != second
    assert not first.exists()
    assert second.exists()
    assert len(list((media_root / "avatars" / str(user.id)).iterdir())) == 1


def test_missing_file_field_is_422(client: TestClient, user: User, media_root: Path) -> None:
    response = client.post(AVATAR, data={"nope": "1"}, headers=auth_headers(user))
    assert response.status_code == 422


# --------------------------------------------------------------------------- delete / auth


def test_delete_removes_file_and_clears_field(client: TestClient, user: User, media_root: Path, db: Session) -> None:
    path = stored_file(media_root, upload(client, user, image_bytes()).json())
    response = client.delete(AVATAR, headers=auth_headers(user))
    assert response.status_code == 200
    assert response.json()["data"]["avatar_url"] is None
    assert not path.exists()
    assert not path.parent.exists()  # empty per-user folder removed
    db.refresh(user)
    assert user.avatar is None
    # Idempotent.
    assert client.delete(AVATAR, headers=auth_headers(user)).status_code == 200


def test_delete_never_touches_files_outside_the_user_folder(
    client: TestClient, user: User, media_root: Path, db: Session
) -> None:
    victim = media_root / "catalog" / "keep.jpg"
    victim.parent.mkdir(parents=True)
    victim.write_bytes(b"x")
    user.avatar = "catalog/keep.jpg"
    db.commit()
    assert client.delete(AVATAR, headers=auth_headers(user)).status_code == 200
    assert victim.exists()


def test_avatar_endpoints_require_auth(client: TestClient, media_root: Path) -> None:
    assert client.post(AVATAR, files={"file": ("a.jpg", image_bytes(), "image/jpeg")}).status_code == 401
    assert client.delete(AVATAR).status_code == 401


def test_cookie_auth_upload_needs_csrf_header(raw_client: TestClient, user: User, media_root: Path) -> None:
    from app.core.security import create_access_token

    raw_client.cookies.set("pr_access", create_access_token(user.id, user.token_version))
    response = raw_client.post(AVATAR, files={"file": ("a.jpg", image_bytes(), "image/jpeg")})
    assert response.status_code == 403


# --------------------------------------------------------------------------- profile fields


def _years_ago(years: int, days: int = 0) -> date:
    today = utcnow().astimezone(IST).date()
    try:
        d = today.replace(year=today.year - years)
    except ValueError:  # 29 Feb
        d = today.replace(year=today.year - years, day=28)
    return date.fromordinal(d.toordinal() - days)


def test_me_returns_profile_fields(client: TestClient, user: User) -> None:
    data = client.get(ME, headers=auth_headers(user)).json()["data"]
    assert data["avatar_url"] is None
    assert data["date_of_birth"] is None
    assert data["gender"] is None
    assert data["initials"] == "AS"


def test_patch_profile_fields(client: TestClient, user: User) -> None:
    dob = _years_ago(30)
    response = client.patch(ME, json={"date_of_birth": dob.isoformat(), "gender": "Female"}, headers=auth_headers(user))
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["date_of_birth"] == dob.isoformat()
    assert data["gender"] == "female"
    assert data["full_name"] == "Aarav Sharma"  # untouched

    # Sending only one field leaves the others alone; null clears.
    response = client.patch(ME, json={"gender": None}, headers=auth_headers(user))
    data = response.json()["data"]
    assert data["gender"] is None and data["date_of_birth"] == dob.isoformat()
    response = client.patch(ME, json={"date_of_birth": None, "gender": "prefer_not"}, headers=auth_headers(user))
    data = response.json()["data"]
    assert data["date_of_birth"] is None and data["gender"] == "prefer_not"


@pytest.mark.parametrize(
    "body",
    [
        {"gender": "robot"},
        {"gender": ""},
        {"date_of_birth": "not-a-date"},
        {"date_of_birth": "2999-01-01"},  # future
    ],
)
def test_patch_rejects_invalid_values(client: TestClient, user: User, body: dict) -> None:
    response = client.patch(ME, json=body, headers=auth_headers(user))
    assert response.status_code == 422, response.text
    error = response.json()["error"]
    assert error["code"] == "VALIDATION_ERROR"
    assert error["details"][0]["field"] in body


def test_patch_age_bounds(client: TestClient, user: User) -> None:
    headers = auth_headers(user)
    too_young = _years_ago(13, days=-1)  # turns 13 tomorrow
    assert client.patch(ME, json={"date_of_birth": too_young.isoformat()}, headers=headers).status_code == 422
    exactly_13 = _years_ago(13)
    assert client.patch(ME, json={"date_of_birth": exactly_13.isoformat()}, headers=headers).status_code == 200
    too_old = _years_ago(121)
    response = client.patch(ME, json={"date_of_birth": too_old.isoformat()}, headers=headers)
    assert response.status_code == 422
    assert "13 and 120" in response.json()["error"]["message"]


def test_age_on() -> None:
    assert age_on(date(2000, 3, 1), date(2026, 2, 28)) == 25
    assert age_on(date(2000, 3, 1), date(2026, 3, 1)) == 26


def test_read_limited_stops_at_the_limit() -> None:
    from app.core.exceptions import FileTooLargeError
    from app.services.avatar_service import read_limited

    assert read_limited(io.BytesIO(b"x" * 100), 100) == b"x" * 100
    with pytest.raises(FileTooLargeError):
        read_limited(io.BytesIO(b"x" * 101), 100)
