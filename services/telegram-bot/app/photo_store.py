"""
Persists photos taken during /identify to disk + DB, for a future
training/dataset feature. Not used for anything at identify-time itself.
"""
from __future__ import annotations

import logging
import os
import uuid
from datetime import datetime, timezone

log = logging.getLogger(__name__)

PHOTO_STORAGE_DIR = os.environ.get("PHOTO_STORAGE_DIR", "/data/identify-photos")


def _write_file(data: bytes, suffix: str = ".jpg") -> str:
    """Write photo bytes to PHOTO_STORAGE_DIR, one subdir per day. Returns
    the path actually written to."""
    day_dir = os.path.join(PHOTO_STORAGE_DIR, datetime.now(tz=timezone.utc).strftime("%Y-%m-%d"))
    os.makedirs(day_dir, exist_ok=True)
    filename = f"{uuid.uuid4().hex}{suffix}"
    path = os.path.join(day_dir, filename)
    with open(path, "wb") as f:
        f.write(data)
    return path


async def save_identify_photo(
    conn,
    photo_bytes: bytes,
    species: str | None = None,
    score: float | None = None,
) -> int | None:
    """Write the photo to disk and insert a row recording it. Returns the
    new row's id, or None if saving failed (never raises — photo capture is
    a nice-to-have and must not break the /identify flow)."""
    try:
        path = _write_file(photo_bytes)
    except OSError as exc:
        log.warning("event=identify_photo_save_failed error=%r", exc)
        return None

    try:
        async with conn.cursor() as cur:
            await cur.execute(
                """INSERT INTO identify_photos (file_path, species, score)
                   VALUES (%s, %s, %s) RETURNING id""",
                (path, species, score),
            )
            row = await cur.fetchone()
        await conn.commit()
        return row["id"] if row else None
    except Exception as exc:
        log.warning("event=identify_photo_insert_failed error=%r", exc)
        return None


async def link_identify_photos(
    conn,
    photo_ids: list[int],
    sensor_id: int,
    sensor_name: str | None,
    plant_name: str,
    plant_id: int | None = None,
) -> None:
    """Attach the resolved sensor + plant name to previously-saved photo
    rows once the /identify assign flow completes. Best-effort."""
    if not photo_ids:
        return
    try:
        async with conn.cursor() as cur:
            for photo_id in photo_ids:
                await cur.execute(
                    """UPDATE identify_photos
                       SET sensor_id = %s, sensor_name = %s, plant_name = %s, plant_id = %s
                       WHERE id = %s""",
                    (sensor_id, sensor_name, plant_name, plant_id, photo_id),
                )
        await conn.commit()
    except Exception as exc:
        log.warning("event=identify_photo_link_failed error=%r", exc)


async def delete_plant_photos(conn, plant_id: int) -> int:
    """Delete all identify photos for a plant: remove files from disk then
    delete rows. Best-effort file removal (ignores missing/inaccessible files).

    Does NOT commit — the caller owns the transaction so that photo deletion
    is atomic with the plant deletion.

    Returns the number of rows deleted.
    """
    # Fetch file paths first
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT file_path FROM identify_photos WHERE plant_id = %s",
            (plant_id,),
        )
        rows = await cur.fetchall()

    # Best-effort unlink
    for row in rows:
        try:
            os.unlink(row["file_path"])
        except (FileNotFoundError, OSError):
            pass

    # Delete DB rows
    async with conn.cursor() as cur:
        await cur.execute(
            "DELETE FROM identify_photos WHERE plant_id = %s",
            (plant_id,),
        )
    return len(rows)
