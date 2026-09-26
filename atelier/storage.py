"""PNG and thumbnail storage, plus the disk guard.

The PNG is stored exactly as Modal returned it. Only the thumbnail is normalized to 8-bit, because WebP
cannot carry a 16-bit source image the way PNG can.
"""

from __future__ import annotations

import hashlib
import io
import os
import shutil
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from PIL import Image, UnidentifiedImageError

from atelier.config import Settings

THUMB_LONG_SIDE = 512

_DECODE_ERRORS = (Image.DecompressionBombError, UnidentifiedImageError, OSError, ValueError)

# Pillow clips these modes to white on a plain convert("RGB"): they hold values up to 65535 (or floats),
# so they need an explicit /256 scale down to 8-bit before the mode conversion.
_HIGH_BITDEPTH_MODES = ("I", "F")


class UnstorableResult(Exception):
    """Raised for bytes that can never be stored as an image, so the poller can fail just that job."""


class DiskGuardError(Exception):
    """Raised when the disk guard refuses a new batch."""


@dataclass(frozen=True, slots=True)
class SavedImage:
    """Paths are relative to data_dir, so the database survives data_dir moving between environments."""

    file_png: Path
    file_thumb: Path
    width: int
    height: int
    bytes: int
    sha256: str


@dataclass(frozen=True, slots=True)
class DiskStatus:
    used_bytes: int
    cap_bytes: int
    free_bytes: int
    total_bytes: int
    refusal: str | None


def _atomic_write(path: Path, data: bytes) -> None:
    """Write, fsync, then rename. The temp file is always cleaned up: os.replace() removes it on
    success, and the finally block removes it if the write, fsync or rename itself failed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    try:
        with open(tmp, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def _make_thumbnail(im: Image.Image) -> Image.Image:
    if im.mode.startswith("I;16") or im.mode in _HIGH_BITDEPTH_MODES:
        # A plain convert("RGB") on these modes clips every value above 255 to white. Rescale the
        # 0..65535 (or float) range down to 0..255 first, so the thumbnail looks like the source.
        im = im.convert("I").point(lambda v: v * (1 / 256)).convert("L")
    thumb = im.convert("RGB")
    thumb.thumbnail((THUMB_LONG_SIDE, THUMB_LONG_SIDE))
    return thumb


def save_result(data_dir: Path, job_id: int, data: bytes) -> SavedImage:
    """Verify the bytes are a usable image, then write the PNG as received plus a normalized WebP
    thumbnail, both atomically. Raises UnstorableResult for bytes that will never decode, such as an
    unidentified image or Pillow's decompression-bomb guard."""
    try:
        with Image.open(io.BytesIO(data)) as probe:
            probe.verify()
        with Image.open(io.BytesIO(data)) as im:
            width, height = im.size
            thumb = _make_thumbnail(im)
    except _DECODE_ERRORS as exc:
        raise UnstorableResult(f"not a usable image: {exc}") from exc

    now = datetime.now(UTC)
    rel_dir = Path("images") / f"{now:%Y}" / f"{now:%m}"
    file_png = rel_dir / f"job-{job_id}.png"
    file_thumb = rel_dir / f"job-{job_id}.thumb.webp"

    _atomic_write(data_dir / file_png, data)

    try:
        thumb_buf = io.BytesIO()
        thumb.save(thumb_buf, format="WEBP")
        _atomic_write(data_dir / file_thumb, thumb_buf.getvalue())
    except Exception:
        (data_dir / file_png).unlink(missing_ok=True)  # never leave the PNG behind without its thumbnail
        raise

    return SavedImage(
        file_png=file_png,
        file_thumb=file_thumb,
        width=width,
        height=height,
        bytes=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
    )


def delete_files(data_dir: Path, saved: SavedImage) -> None:
    """Removes a completed result's files, e.g. when a job was cancelled before it could be recorded."""
    (data_dir / saved.file_png).unlink(missing_ok=True)
    (data_dir / saved.file_thumb).unlink(missing_ok=True)


def _job_month_dirs(data_dir: Path, now: datetime | None = None) -> tuple[Path, Path]:
    """This month's and last month's images/YYYY/MM directories: the only places save_result() could
    have left a temporary file for a job, since it always names paths from the current UTC date."""
    now = now or datetime.now(UTC)
    previous = now.replace(day=1) - timedelta(days=1)  # any day in the previous month
    return (
        data_dir / "images" / f"{now:%Y}" / f"{now:%m}",
        data_dir / "images" / f"{previous:%Y}" / f"{previous:%m}",
    )


def remove_partial_files(data_dir: Path, job_id: int) -> None:
    """Deletes leftover temporary files for a job whose result could not be stored. Targets only the
    job's own known paths (no directory walk), in case the failure happened just before a month rolled
    over."""
    for directory in _job_month_dirs(data_dir):
        for name in (f"job-{job_id}.png.tmp", f"job-{job_id}.thumb.webp.tmp"):
            (directory / name).unlink(missing_ok=True)


def disk_status(conn: sqlite3.Connection, settings: Settings) -> DiskStatus:
    """Reports used bytes (SUM(images.bytes)), the cap, and volume free/total bytes from the real
    filesystem, plus a refusal reason when free space is below the floor or used bytes reach the cap."""
    row = conn.execute("SELECT COALESCE(SUM(bytes), 0) AS used FROM images").fetchone()
    used_bytes = row["used"]
    cap_bytes = settings.data_cap_gb * 2**30
    min_free_bytes = settings.min_free_gb * 2**30

    settings.data_dir.mkdir(parents=True, exist_ok=True)
    total_bytes, _, free_bytes = shutil.disk_usage(settings.data_dir)

    refusal = None
    if free_bytes < min_free_bytes:
        refusal = f"only {free_bytes // 2**20} MB free on the volume, below the {settings.min_free_gb} GB floor"
    elif used_bytes >= cap_bytes:
        refusal = f"image storage is at {used_bytes // 2**20} MB, at or over the {settings.data_cap_gb} GB cap"

    return DiskStatus(
        used_bytes=used_bytes,
        cap_bytes=cap_bytes,
        free_bytes=free_bytes,
        total_bytes=total_bytes,
        refusal=refusal,
    )
