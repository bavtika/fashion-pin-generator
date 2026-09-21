"""Pipeline generation tests with mocked Gemini (no network)."""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Minimal 1x1 PNG
_MIN_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01"
    b"\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)

_DESCRIBE_TEXT = (
    "A young woman in a mirror selfie, holding a phone at chest height. "
    "She wears a cropped white mesh top, high-waisted black denim jeans, "
    "and white leather sneakers with silver chain accessories. "
    "Soft indoor lighting, neutral bedroom background. "
    "shot on iPhone 13, mobile photography style, authentic lighting, standard, "
    "social media aesthetic, high-quality natural grain."
)


def _mock_gemini_responses():
    describe = MagicMock()
    describe.text = _DESCRIBE_TEXT
    describe.metadata = ["chat-describe"]
    describe.images = []

    gen = MagicMock()
    gen.text = ""
    gen.metadata = ["chat-generate"]

    async def _save(path, filename, verbose=False):
        dest = Path(path) / filename
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(_MIN_PNG)
        return str(dest)

    img = MagicMock()
    img.save = _save
    gen.images = [img]
    return describe, gen


class TestGenerationMocked(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._root = Path(self._tmpdir.name)
        self._model = self._root / "reference" / "model"
        self._styles = self._root / "reference" / "styles"
        self._out = self._root / "data" / "generated"
        self._model.mkdir(parents=True)
        self._styles.mkdir(parents=True)
        (self._model / "model.jpg").write_bytes(_MIN_PNG)
        (self._styles / "style.jpg").write_bytes(_MIN_PNG)

        import generators.image_gen as ig

        self.ig = ig
        self._orig_model = ig.MODEL_DIR
        self._orig_styles = ig.STYLES_DIR
        self._orig_out = ig.OUTPUT_DIR
        ig.MODEL_DIR = self._model
        ig.STYLES_DIR = self._styles
        ig.OUTPUT_DIR = self._out

        await ig.shutdown_client()

    async def asyncTearDown(self):
        await self.ig.shutdown_client()
        self.ig.MODEL_DIR = self._orig_model
        self.ig.STYLES_DIR = self._orig_styles
        self.ig.OUTPUT_DIR = self._orig_out
        self._tmpdir.cleanup()

    async def test_generate_outfit_image_mocked(self):
        describe, gen = _mock_gemini_responses()
        client = MagicMock()

        async def fake_send(_client, prompt, files, **kwargs):  # noqa: ARG001
            if "Professional AI Image Analyst" in prompt or "precise technical description" in prompt:
                return describe
            return gen

        with patch.object(self.ig, "_get_client", AsyncMock(return_value=client)):
            with patch.object(self.ig, "_send", side_effect=fake_send):
                image_path, prompt, description, archived = await self.ig.generate_outfit_image()

        self.assertTrue(Path(image_path).is_file())
        self.assertGreater(Path(image_path).stat().st_size, 50)
        self.assertIn("Scene:", prompt)
        self.assertGreaterEqual(len(description), 20)
        self.assertIsNotNone(archived)
        self.assertFalse((self._styles / "style.jpg").exists())
        self.assertTrue(Path(archived).exists())

    async def test_pipeline_run_generation_step_mocked(self):
        import db.database as db

        self._db_orig = db.DB_PATH
        db.DB_PATH = self._root / "data" / "pins.db"
        db.init_db()

        fake_result = (
            str(self._out / "outfit_mock.png"),
            "Scene: mock",
            _DESCRIBE_TEXT,
            None,
        )
        self._out.mkdir(parents=True, exist_ok=True)
        Path(fake_result[0]).write_bytes(_MIN_PNG)

        sent: list[int] = []

        async def fake_send(pin_id: int):
            sent.append(pin_id)

        with patch("pinterest.scraper.count_styles", return_value=99):
            with patch(
                "generators.image_gen.generate_outfit_image",
                AsyncMock(return_value=fake_result),
            ):
                with patch("tg_bot.bot.send_for_approval", side_effect=fake_send):
                    from pipeline import run_generation_step

                    await run_generation_step(max_retries=1)

        self.assertEqual(len(sent), 1)
        pin = db.get_pin(sent[0])
        self.assertEqual(pin["status"], "pending_approval")
        self.assertEqual(pin["clothing_description"], _DESCRIBE_TEXT)

        db.DB_PATH = self._db_orig


if __name__ == "__main__":
    unittest.main()
