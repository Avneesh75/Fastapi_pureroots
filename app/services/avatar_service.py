"""Customer profile photos.

Pipeline for ``POST /auth/me/avatar``:

1. read at most ``AVATAR_MAX_BYTES`` (+1) bytes — anything larger is rejected *before* decoding;
2. decode with Pillow; only real JPEG / PNG / WebP images pass (the file name and the client's
   content type are ignored, so a renamed PDF or a GIF called ``.png`` is refused);
3. apply the EXIF orientation, then rebuild the pixels into a fresh image so no metadata
   (EXIF, GPS, XMP, ICC, text chunks) survives;
4. centre-crop to a square, resize to 512×512 and encode as WebP;
5. write ``avatars/<user_id>/<random>.webp`` under ``MEDIA_ROOT`` and remove the previous photo.

The stored value is the path relative to ``MEDIA_ROOT``; the API exposes
``{PUBLIC_MEDIA_BASE}/{path}`` as ``avatar_url``.
"""
from __future__ import annotations

import io
import logging
import os
import secrets
import warnings
from pathlib import Path
from typing import BinaryIO

from PIL import Image, ImageOps, UnidentifiedImageError
from sqlalchemy.orm import Session

from app.config import Settings
from app.core.exceptions import FileTooLargeError, InvalidFileError
from app.models.user import User

logger = logging.getLogger(__name__)

AVATAR_DIR = "avatars"
AVATAR_SIZE = 512
WEBP_QUALITY = 85
ALLOWED_FORMATS = {"JPEG", "PNG", "WEBP"}
# Refuse absurd canvases before Pillow allocates memory for them (a 2 MB PNG can declare 50k×50k).
MAX_PIXELS = 40_000_000
CHUNK = 64 * 1024

NOT_AN_IMAGE = "Please upload a JPEG, PNG or WebP image."
IMAGE_TOO_BIG = "That image's dimensions are too large. Please upload a smaller photo."
IMAGE_TOO_SMALL = "That image is too small. Please upload a photo at least 64×64 pixels."
MIN_SIDE = 64


def too_large_message(max_bytes: int) -> str:
    return f"Photos must be {max_bytes / 1_000_000:g} MB or smaller."


def read_limited(stream: BinaryIO, max_bytes: int) -> bytes:
    """Read the upload, stopping (and failing) as soon as it exceeds ``max_bytes``."""
    buffer = io.BytesIO()
    total = 0
    while True:
        chunk = stream.read(CHUNK)
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            raise FileTooLargeError(too_large_message(max_bytes))
        buffer.write(chunk)
    if total == 0:
        raise InvalidFileError(NOT_AN_IMAGE)
    return buffer.getvalue()


def process_image(data: bytes) -> bytes:
    """Validate and normalise raw upload bytes → 512×512 metadata-free WebP bytes."""
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            # 1st pass: cheap structural check.
            with Image.open(io.BytesIO(data)) as probe:
                if probe.format not in ALLOWED_FORMATS:
                    raise InvalidFileError(NOT_AN_IMAGE)
                width, height = probe.size
                if width * height > MAX_PIXELS:
                    raise InvalidFileError(IMAGE_TOO_BIG)
                probe.verify()
            # 2nd pass: verify() leaves the image unusable, so decode again for real.
            with Image.open(io.BytesIO(data)) as img:
                img.load()
                img = ImageOps.exif_transpose(img)
                return _to_webp(img)
    except InvalidFileError:
        raise
    except (UnidentifiedImageError, Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise InvalidFileError(NOT_AN_IMAGE) from exc
    except (OSError, SyntaxError, ValueError, EOFError) as exc:  # truncated / corrupt files
        raise InvalidFileError(NOT_AN_IMAGE) from exc


def _to_webp(img: Image.Image) -> bytes:
    if min(img.size) < MIN_SIDE:
        raise InvalidFileError(IMAGE_TOO_SMALL)
    has_alpha = img.mode in ("RGBA", "LA", "PA") or (img.mode == "P" and "transparency" in img.info)
    mode = "RGBA" if has_alpha else "RGB"
    img = img.convert(mode)
    img = ImageOps.fit(img, (AVATAR_SIZE, AVATAR_SIZE), method=Image.Resampling.LANCZOS, centering=(0.5, 0.5))
    # Rebuild from raw pixels: the new image carries no .info (EXIF, XMP, ICC, PNG text...).
    clean = Image.frombytes(mode, img.size, img.tobytes())
    out = io.BytesIO()
    clean.save(out, format="WEBP", quality=WEBP_QUALITY, method=4)
    return out.getvalue()


# --------------------------------------------------------------------------- storage


def _media_root(settings: Settings) -> Path:
    return Path(settings.media_root).resolve()


def _user_dir(settings: Settings, user: User) -> Path:
    return _media_root(settings) / AVATAR_DIR / str(user.id)


def _safe_path(settings: Settings, user: User, relative: str | None) -> Path | None:
    """Absolute path of a stored avatar, only if it really lives in this user's avatar folder."""
    if not relative:
        return None
    candidate = (_media_root(settings) / relative).resolve()
    user_dir = _user_dir(settings, user).resolve()
    if candidate.parent != user_dir:
        logger.warning("Ignoring avatar path outside the user's folder: %r", relative)
        return None
    return candidate


def _remove_file(path: Path | None) -> None:
    if path is None:
        return
    try:
        path.unlink(missing_ok=True)
    except OSError:
        logger.warning("Could not delete old avatar %s", path, exc_info=True)


def save_avatar(db: Session, user: User, stream: BinaryIO, settings: Settings) -> User:
    data = read_limited(stream, settings.avatar_max_bytes)
    webp = process_image(data)

    folder = _user_dir(settings, user)
    folder.mkdir(parents=True, exist_ok=True)
    name = f"{secrets.token_hex(12)}.webp"
    target = folder / name
    tmp = folder / f".{name}.tmp"
    tmp.write_bytes(webp)
    os.replace(tmp, target)

    previous = _safe_path(settings, user, user.avatar)
    user.avatar = f"{AVATAR_DIR}/{user.id}/{name}"
    try:
        db.commit()
    except Exception:
        db.rollback()
        _remove_file(target)
        raise
    db.refresh(user)
    if previous is not None and previous != target:
        _remove_file(previous)
    logger.info("Avatar updated for user %s", user.id)
    return user


def delete_avatar(db: Session, user: User, settings: Settings) -> User:
    previous = _safe_path(settings, user, user.avatar)
    if user.avatar is not None:
        user.avatar = None
        db.commit()
        db.refresh(user)
    _remove_file(previous)
    try:  # tidy up the now-empty avatars/<user_id>/ folder
        _user_dir(settings, user).rmdir()
    except OSError:
        pass  # missing, or not empty
    return user
