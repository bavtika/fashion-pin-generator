"""
Live integration test: Gemini describe + image edit pipeline.

Requires:
  - reference/model/*.jpg
  - reference/styles/* (at least one)
  - data/gemini_cookies.json OR Chrome logged into gemini.google.com

Run: python -m unittest tests.test_generation_integration -v
Skip live API: set SKIP_LIVE_GENERATION=1
"""
import asyncio
import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

RUN_LIVE = os.getenv("RUN_LIVE_GENERATION", "").strip() in ("1", "true", "yes")
SKIP_LIVE = os.getenv("SKIP_LIVE_GENERATION", "").strip() in ("1", "true", "yes")
LIVE_TIMEOUT = int(os.getenv("GENERATION_TEST_TIMEOUT", "300"))


def _has_prerequisites() -> tuple[bool, str]:
    model_dir = ROOT / "reference" / "model"
    styles_dir = ROOT / "reference" / "styles"
    exts = {".jpg", ".jpeg", ".png", ".webp"}

    models = [p for p in model_dir.iterdir() if p.suffix.lower() in exts]
    styles = [p for p in styles_dir.iterdir() if p.suffix.lower() in exts]
    if not models:
        return False, f"No model photos in {model_dir}"
    if not styles:
        return False, f"No style images in {styles_dir}"
    cookies = ROOT / "data" / "gemini_cookies.json"
    cache = ROOT / "data" / "gemini_webapi_cache"
    has_cache = cache.is_dir() and any(cache.glob("*.json"))
    if not cookies.exists() and not has_cache:
        return (
            False,
            f"Missing {cookies} and no {cache} — need Gemini cookies or Chrome login",
        )
    return True, ""


@unittest.skipUnless(RUN_LIVE and not SKIP_LIVE, "Set RUN_LIVE_GENERATION=1 for live Gemini tests")
class TestLiveGeneration(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        ok, reason = _has_prerequisites()
        if not ok:
            self.skipTest(reason)
        from generators.image_gen import shutdown_client

        await shutdown_client()

    async def test_generate_outfit_image_end_to_end(self):
        from generators.image_gen import (
            GeminiBlockedError,
            generate_outfit_image,
            shutdown_client,
        )

        try:
            image_path, prompt, description, archived_ref = await asyncio.wait_for(
                generate_outfit_image(),
                timeout=LIVE_TIMEOUT,
            )
        except GeminiBlockedError as e:
            self.skipTest(f"Gemini not authenticated — refresh cookies: {e}")
        finally:
            await shutdown_client()

        self.assertIsInstance(image_path, str)
        self.assertTrue(Path(image_path).is_file(), f"Missing output: {image_path}")
        self.assertGreater(Path(image_path).stat().st_size, 10_000)

        self.assertIsInstance(prompt, str)
        self.assertGreater(len(prompt), 50)

        self.assertIsInstance(description, str)
        self.assertGreaterEqual(len(description), 20)

        if archived_ref is not None:
            self.assertTrue(Path(archived_ref).is_file())

    async def test_regenerate_with_description(self):
        from generators.image_gen import (
            GeminiBlockedError,
            generate_outfit_image,
            regenerate_with_description,
            shutdown_client,
        )

        try:
            _, _, description, _ = await asyncio.wait_for(
                generate_outfit_image(),
                timeout=LIVE_TIMEOUT,
            )
            image_path, prompt, _, _ = await asyncio.wait_for(
                regenerate_with_description(description),
                timeout=LIVE_TIMEOUT,
            )
        except GeminiBlockedError as e:
            self.skipTest(f"Gemini not authenticated — refresh cookies: {e}")
        finally:
            await shutdown_client()

        self.assertTrue(Path(image_path).is_file())
        self.assertGreater(len(prompt), 50)


if __name__ == "__main__":
    unittest.main()
