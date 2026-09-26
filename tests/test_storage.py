"""PNG/thumbnail storage and the disk guard."""

from __future__ import annotations

import dataclasses
import hashlib
import io
from datetime import UTC, datetime, timedelta

import pytest
from PIL import Image

from atelier import storage


def test_save_result_stores_the_png_as_received_and_a_thumbnail(settings, png_bytes):
    saved = storage.save_result(settings.data_dir, 42, png_bytes)

    png_path = settings.data_dir / saved.file_png
    thumb_path = settings.data_dir / saved.file_thumb
    assert png_path.read_bytes() == png_bytes  # stored exactly as received, no re-encoding
    assert saved.sha256 == hashlib.sha256(png_bytes).hexdigest()
    assert saved.bytes == len(png_bytes)
    assert saved.width == 64
    assert saved.height == 64

    with Image.open(thumb_path) as thumb:
        assert thumb.format == "WEBP"
        assert max(thumb.size) <= storage.THUMB_LONG_SIDE

    assert saved.file_png.parts[0] == "images"


def test_save_result_normalizes_16_bit_source_to_an_8_bit_thumbnail(settings):
    im = Image.new("I;16", (32, 32), 32768)
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    data = buf.getvalue()

    saved = storage.save_result(settings.data_dir, 7, data)

    assert (settings.data_dir / saved.file_png).read_bytes() == data  # the PNG itself is untouched
    with Image.open(settings.data_dir / saved.file_thumb) as thumb:
        assert thumb.mode == "RGB"
        # 32768 / 256 == 128: a mid-range 16-bit value must land near mid-gray, not clipped to white.
        r, g, b = thumb.getpixel((0, 0))
        assert 120 <= r <= 136
        assert r == g == b


def test_save_result_scales_a_full_range_16_bit_gradient_instead_of_clipping_to_white(settings):
    width = 256
    im = Image.new("I;16", (width, 8))
    for x in range(width):
        value = int(x / (width - 1) * 65535)
        for y in range(8):
            im.putpixel((x, y), value)
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    data = buf.getvalue()

    saved = storage.save_result(settings.data_dir, 8, data)
    with Image.open(settings.data_dir / saved.file_thumb) as thumb:
        thumb = thumb.convert("L")
        samples = [thumb.getpixel((x, 4)) for x in range(thumb.size[0])]
        mean = sum(samples) / len(samples)
    # A linear 0..65535 gradient scaled correctly averages to about half of 255, not near-white (250+).
    assert 100 <= mean <= 155


def test_save_result_raises_unstorable_result_for_garbage_bytes(settings):
    with pytest.raises(storage.UnstorableResult):
        storage.save_result(settings.data_dir, 1, b"not an image at all")


def test_save_result_raises_unstorable_result_for_a_decompression_bomb(settings, png_bytes, monkeypatch):
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 4)  # any real image now looks like a bomb
    with pytest.raises(storage.UnstorableResult):
        storage.save_result(settings.data_dir, 2, png_bytes)


def test_save_result_removes_the_png_if_the_thumbnail_write_fails(settings, png_bytes, monkeypatch):
    def broken_save(self, *args, **kwargs):
        raise OSError("simulated disk error while writing the thumbnail")

    monkeypatch.setattr(Image.Image, "save", broken_save)
    with pytest.raises(OSError):
        storage.save_result(settings.data_dir, 55, png_bytes)

    leftover = list((settings.data_dir / "images").rglob("job-55.*"))
    assert leftover == []  # the PNG must not survive without its thumbnail


def test_delete_files_removes_both_png_and_thumbnail(settings, png_bytes):
    saved = storage.save_result(settings.data_dir, 3, png_bytes)
    storage.delete_files(settings.data_dir, saved)
    assert not (settings.data_dir / saved.file_png).exists()
    assert not (settings.data_dir / saved.file_thumb).exists()


def test_remove_partial_files_deletes_this_and_last_months_leftovers_only(settings):
    now = datetime.now(UTC)
    previous = now.replace(day=1) - timedelta(days=1)
    current_dir = settings.data_dir / "images" / f"{now:%Y}" / f"{now:%m}"
    previous_dir = settings.data_dir / "images" / f"{previous:%Y}" / f"{previous:%m}"
    current_dir.mkdir(parents=True)
    previous_dir.mkdir(parents=True)

    current_png = current_dir / "job-9.png.tmp"
    current_thumb = current_dir / "job-9.thumb.webp.tmp"
    previous_png = previous_dir / "job-9.png.tmp"
    other_job = current_dir / "job-10.png.tmp"
    for path in (current_png, current_thumb, previous_png, other_job):
        path.write_bytes(b"partial")

    storage.remove_partial_files(settings.data_dir, 9)

    assert not current_png.exists()
    assert not current_thumb.exists()
    assert not previous_png.exists()
    assert other_job.exists()  # a different job's leftover in the same directory is untouched


def test_remove_partial_files_does_not_touch_an_older_months_directory(settings):
    # A directory more than a month old (not "this month" or "last month") must never be scanned:
    # remove_partial_files targets only the two known paths, never a tree walk.
    stale = datetime.now(UTC) - timedelta(days=95)
    stale_dir = settings.data_dir / "images" / f"{stale:%Y}" / f"{stale:%m}"
    stale_dir.mkdir(parents=True)
    stale_file = stale_dir / "job-9.png.tmp"
    stale_file.write_bytes(b"partial")

    storage.remove_partial_files(settings.data_dir, 9)

    assert stale_file.exists()


def test_remove_partial_files_is_a_no_op_without_an_images_directory(settings):
    storage.remove_partial_files(settings.data_dir, 123)  # must not raise


def test_atomic_write_fsyncs_before_replacing(settings, monkeypatch):
    target = settings.data_dir / "out.bin"
    calls: list[str] = []
    real_fsync = storage.os.fsync

    def spy_fsync(fd):
        calls.append("fsync")
        return real_fsync(fd)

    monkeypatch.setattr(storage.os, "fsync", spy_fsync)
    storage._atomic_write(target, b"hello")

    assert calls == ["fsync"]
    assert target.read_bytes() == b"hello"
    assert not target.with_name(target.name + ".tmp").exists()


def test_atomic_write_removes_the_temp_file_when_the_write_fails(settings, monkeypatch):
    target = settings.data_dir / "out.bin"

    def broken_fsync(fd):
        raise OSError("simulated disk error")

    monkeypatch.setattr(storage.os, "fsync", broken_fsync)
    with pytest.raises(OSError):
        storage._atomic_write(target, b"hello")

    assert not target.exists()
    assert not target.with_name(target.name + ".tmp").exists()


def test_disk_status_reports_used_bytes_from_the_images_table(conn, settings):
    conn.execute(
        "INSERT INTO batches (created_at, model_id, kind, base_params_json, count) VALUES (0, 'm', 'generate', '{}', 1)"
    )
    conn.execute(
        "INSERT INTO jobs (batch_id, model_id, backend_id, kind, params_json, graph_json, status, created_at) "
        "VALUES (1, 'm', 'qwen21-uc', 'generate', '{}', '{}', 'done', 0)"
    )
    conn.execute(
        "INSERT INTO images (job_id, model_id, file_png, file_thumb, width, height, bytes, sha256, created_at) "
        "VALUES (1, 'm', 'a.png', 'a.webp', 8, 8, 12345, 'sha', 0)"
    )
    status = storage.disk_status(conn, settings)
    assert status.used_bytes == 12345


def _seed_one_image(conn, image_bytes: int) -> None:
    conn.execute(
        "INSERT INTO batches (created_at, model_id, kind, base_params_json, count) VALUES (0, 'm', 'generate', '{}', 1)"
    )
    conn.execute(
        "INSERT INTO jobs (batch_id, model_id, backend_id, kind, params_json, graph_json, status, created_at) "
        "VALUES (1, 'm', 'qwen21-uc', 'generate', '{}', '{}', 'done', 0)"
    )
    conn.execute(
        "INSERT INTO images (job_id, model_id, file_png, file_thumb, width, height, bytes, sha256, created_at) "
        "VALUES (1, 'm', 'a.png', 'a.webp', 8, 8, ?, 'sha', 0)",
        (image_bytes,),
    )


def test_disk_status_refuses_when_used_equals_the_cap_exactly(conn, settings):
    cap_bytes = 1 * 2**30
    _seed_one_image(conn, cap_bytes)
    tiny_cap_settings = dataclasses.replace(settings, data_cap_gb=1)
    status = storage.disk_status(conn, tiny_cap_settings)
    assert status.refusal is not None
    assert "cap" in status.refusal


def test_disk_status_allows_one_byte_under_the_cap(conn, settings):
    cap_bytes = 1 * 2**30
    _seed_one_image(conn, cap_bytes - 1)
    tiny_cap_settings = dataclasses.replace(settings, data_cap_gb=1)
    status = storage.disk_status(conn, tiny_cap_settings)
    assert status.refusal is None


def test_disk_status_refuses_at_the_free_floor(conn, settings):
    # A floor set far above any real machine's free space trips deterministically off the real
    # filesystem, with no need to fake shutil.disk_usage itself.
    huge_floor_settings = dataclasses.replace(settings, min_free_gb=100_000_000)
    status = storage.disk_status(conn, huge_floor_settings)
    assert status.refusal is not None
    assert "free" in status.refusal


def test_disk_status_allows_when_free_space_is_comfortably_above_the_floor(conn, settings):
    modest_floor_settings = dataclasses.replace(settings, min_free_gb=1)
    status = storage.disk_status(conn, modest_floor_settings)
    assert status.refusal is None
