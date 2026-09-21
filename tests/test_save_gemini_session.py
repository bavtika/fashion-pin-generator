"""Unit tests for save_gemini_session helpers (no browser)."""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from generators.save_gemini_session import _pick_auth_cookies, _save_cookie_file


class TestSaveGeminiSession(unittest.TestCase):
    def test_pick_auth_cookies(self):
        raw = [
            {"name": "__Secure-1PSID", "value": "abc"},
            {"name": "__Secure-1PSIDTS", "value": "def"},
            {"name": "other", "value": "x"},
        ]
        got = _pick_auth_cookies(raw)
        self.assertEqual(got["__Secure-1PSID"], "abc")
        self.assertEqual(got["__Secure-1PSIDTS"], "def")

    def test_pick_missing_psidts(self):
        self.assertIsNone(_pick_auth_cookies([{"name": "__Secure-1PSID", "value": "a"}]))

    def test_save_cookie_file_writes_json(self):
        import tempfile
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cookies.json"
            with patch("generators.image_gen.COOKIES_FILE", path):
                saved = _save_cookie_file(
                    {"__Secure-1PSID": "p", "__Secure-1PSIDTS": "t"}
                )
                self.assertEqual(saved, path)
                text = path.read_text(encoding="utf-8")
                self.assertIn("__Secure-1PSID", text)


if __name__ == "__main__":
    unittest.main()
