"""Unit tests for Gemini timeout / retry configuration."""
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


class TestGeminiTimeouts(unittest.TestCase):
    def test_init_uses_long_request_timeout(self):
        import generators.image_gen as ig

        captured: dict = {}

        async def fake_init(**kwargs):
            captured.update(kwargs)

        client = MagicMock()
        client.init = fake_init
        client.account_status = None

        with patch.object(ig, "_load_cookies_file", return_value=("psid", "psidts")):
            with patch.object(ig, "GeminiClient", return_value=client):
                ig._client = None
                asyncio_run(ig._get_client())

        self.assertEqual(captured.get("timeout"), ig.GEMINI_REQUEST_TIMEOUT)
        self.assertEqual(captured.get("watchdog_timeout"), ig.GEMINI_WATCHDOG_TIMEOUT)
        self.assertLessEqual(captured.get("timeout", 999), 120)
        self.assertGreaterEqual(captured.get("watchdog_timeout", 0), 70)
        # Default watchdog covers the slower of describe vs image steps
        self.assertEqual(
            ig.GEMINI_WATCHDOG_TIMEOUT,
            max(ig.GEMINI_IMAGE_STEP_TIMEOUT, ig.GEMINI_DESCRIBE_STEP_TIMEOUT),
        )
        self.assertEqual(ig.GEMINI_IMAGE_STEP_TIMEOUT, 75.0)

    def test_trim_description(self):
        from generators.image_gen import MAX_DESCRIPTION_CHARS, _trim_description

        short = "x" * 100
        self.assertEqual(_trim_description(short), short)
        long = "word " * (MAX_DESCRIPTION_CHARS // 2 + 200)
        trimmed = _trim_description(long)
        self.assertLessEqual(len(trimmed), MAX_DESCRIPTION_CHARS)


def asyncio_run(coro):
    import asyncio

    return asyncio.run(coro)


if __name__ == "__main__":
    unittest.main()
