"""
Tests for app.photo_store: /identify photo persistence for the future
training/dataset feature.
"""
from __future__ import annotations

import os

import pytest

from app.photo_store import delete_plant_photos, link_identify_photos, save_identify_photo
from tests.conftest import FakeAsyncConnection


@pytest.fixture
def db():
    return FakeAsyncConnection()


class TestSaveIdentifyPhoto:
    @pytest.mark.asyncio
    async def test_writes_file_and_row(self, db, tmp_path, monkeypatch):
        monkeypatch.setattr("app.photo_store.PHOTO_STORAGE_DIR", str(tmp_path))

        photo_id = await save_identify_photo(db, b"fake-jpeg-bytes", species="Monstera deliciosa", score=0.95)

        assert photo_id is not None
        rows = db.get_identify_photos()
        assert len(rows) == 1
        row = rows[0]
        assert row["species"] == "Monstera deliciosa"
        assert row["score"] == 0.95
        assert row["sensor_id"] is None
        assert row["plant_name"] is None
        assert row["plant_id"] is None
        assert os.path.exists(row["file_path"])
        with open(row["file_path"], "rb") as f:
            assert f.read() == b"fake-jpeg-bytes"

    @pytest.mark.asyncio
    async def test_write_failure_returns_none(self, db, tmp_path, monkeypatch):
        # Point the storage dir at a path that is actually a *file*, so
        # os.makedirs() on a day-subdir underneath it fails with an OSError.
        blocker = tmp_path / "blocker"
        blocker.write_text("not a directory")
        monkeypatch.setattr("app.photo_store.PHOTO_STORAGE_DIR", str(blocker))

        photo_id = await save_identify_photo(db, b"data")

        assert photo_id is None
        assert db.get_identify_photos() == []


class TestLinkIdentifyPhotos:
    @pytest.mark.asyncio
    async def test_links_sensor_and_plant_name(self, db):
        db.seed_sensor(1, "balcony-1")
        db.seed_identify_photo(10, species="Monstera deliciosa")
        db.seed_identify_photo(11, species="Monstera deliciosa")

        await link_identify_photos(db, [10, 11], sensor_id=1, sensor_name="balcony-1", plant_name="Swiss Cheese")

        rows = {r["id"]: r for r in db.get_identify_photos()}
        assert rows[10]["sensor_id"] == 1
        assert rows[10]["sensor_name"] == "balcony-1"
        assert rows[10]["plant_name"] == "Swiss Cheese"
        assert rows[11]["plant_name"] == "Swiss Cheese"

    @pytest.mark.asyncio
    async def test_empty_ids_is_noop(self, db):
        await link_identify_photos(db, [], sensor_id=1, sensor_name="x", plant_name="y")
        assert db.get_identify_photos() == []

    @pytest.mark.asyncio
    async def test_writes_plant_id(self, db):
        """link_identify_photos stores the plant_id column."""
        db.seed_sensor(1, "balcony-1")
        db.seed_plant(5, "Rose")
        db.seed_identify_photo(20, species="Rosa")

        await link_identify_photos(
            db, [20], sensor_id=1, sensor_name="balcony-1",
            plant_name="Rose", plant_id=5,
        )

        rows = db.get_identify_photos()
        assert rows[0]["plant_id"] == 5


class TestDeletePlantPhotos:
    @pytest.mark.asyncio
    async def test_removes_files_and_rows_returns_count(self, db, tmp_path, monkeypatch):
        """delete_plant_photos removes files from disk, deletes DB rows, returns count."""
        monkeypatch.setattr("app.photo_store.PHOTO_STORAGE_DIR", str(tmp_path))
        db.seed_plant(1, "Rose")

        # Create two photo files
        f1 = tmp_path / "2026-07-20" / "a.jpg"
        f2 = tmp_path / "2026-07-20" / "b.jpg"
        f1.parent.mkdir(parents=True, exist_ok=True)
        f1.write_bytes(b"photo1")
        f2.write_bytes(b"photo2")

        db.seed_identify_photo(1, file_path=str(f1), plant_id=1)
        db.seed_identify_photo(2, file_path=str(f2), plant_id=1)

        count = await delete_plant_photos(db, plant_id=1)

        assert count == 2
        assert not f1.exists()
        assert not f2.exists()
        assert db.get_identify_photos() == []

    @pytest.mark.asyncio
    async def test_tolerates_missing_file(self, db, tmp_path, monkeypatch):
        """If a file is already gone, delete_plant_photos does not crash."""
        monkeypatch.setattr("app.photo_store.PHOTO_STORAGE_DIR", str(tmp_path))
        db.seed_plant(1, "Rose")

        missing_path = str(tmp_path / "gone" / "missing.jpg")
        db.seed_identify_photo(1, file_path=missing_path, plant_id=1)

        count = await delete_plant_photos(db, plant_id=1)

        assert count == 1
        assert db.get_identify_photos() == []

    @pytest.mark.asyncio
    async def test_returns_zero_when_no_photos(self, db):
        """No photos for this plant → returns 0, no crash."""
        db.seed_plant(1, "Rose")

        count = await delete_plant_photos(db, plant_id=1)

        assert count == 0
