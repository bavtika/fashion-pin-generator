"""Regression tests for review-queue and scraper throttle fixes."""
import asyncio
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


class TestDatabase(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        import db.database as db

        self.db = db
        self._orig_path = db.DB_PATH
        db.DB_PATH = Path(self._tmpdir.name) / "pins.db"
        db.init_db()

    def tearDown(self):
        self.db.DB_PATH = self._orig_path
        self._tmpdir.cleanup()

    def _insert(self, status: str) -> int:
        with __import__("sqlite3").connect(self.db.DB_PATH) as conn:
            cur = conn.execute(
                "INSERT INTO pins (image_path, prompt, status) VALUES (?, ?, ?)",
                ("data/x.png", "p", status),
            )
            return cur.lastrowid

    def test_count_review_queue_includes_regenerating(self):
        self._insert("pending_approval")
        self._insert("regenerating")
        self._insert("regenerating_pose")
        self._insert("regenerating_custom")
        self._insert("approved")
        self.assertEqual(self.db.count_review_queue(), 4)
        self.assertEqual(self.db.count_pending_approval(), 4)

    def test_update_pin_rejects_unknown_fields(self):
        pin_id = self._insert("pending_approval")
        with self.assertRaises(ValueError):
            self.db.update_pin(pin_id, evil_field="x")

    def test_update_pin_allows_known_fields(self):
        pin_id = self._insert("pending_approval")
        self.db.update_pin(pin_id, status="approved")
        pin = self.db.get_pin(pin_id)
        self.assertEqual(pin["status"], "approved")


class TestScraperCooldown(unittest.TestCase):
    def test_refresh_skips_scrape_when_pool_full_and_cooldown_active(self):
        import pinterest.scraper as scraper

        scraper._last_scrape_attempt = time.time()
        with patch.object(scraper, "_existing_count", return_value=scraper.MIN_STYLES):
            downloaded = asyncio.run(scraper.refresh_styles())
        self.assertEqual(downloaded, 0)

    def test_refresh_runs_when_pool_below_min(self):
        import pinterest.scraper as scraper

        scraper._last_scrape_attempt = time.time()
        with patch.object(scraper, "_existing_count", return_value=scraper.MIN_STYLES - 1):
            with patch.object(scraper, "_get_trends_bucket", return_value=[]):
                with patch.object(scraper, "_scrape_pinterest_images", return_value=[]):
                    downloaded = asyncio.run(scraper.refresh_styles())
        self.assertEqual(downloaded, 0)


class TestAsyncEntrypoints(unittest.TestCase):
    def test_generate_outfit_image_is_coroutine(self):
        import inspect

        from generators.image_gen import generate_outfit_image

        self.assertTrue(inspect.iscoroutinefunction(generate_outfit_image))


if __name__ == "__main__":
    unittest.main()
