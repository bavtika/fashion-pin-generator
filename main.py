"""
Entry point: continuously generate pins back-to-back; Telegram handles approve/regen.
No scheduler — generation runs in an infinite loop.
"""
import asyncio
import logging
import os
import re
import sys

sys.path.insert(0, os.path.dirname(__file__))

os.makedirs("data", exist_ok=True)

from config import TELEGRAM_BOT_TOKEN, validate_config
from db.database import init_db


class _SecretRedactingFilter(logging.Filter):
    """Strip Telegram bot tokens from log records (httpx logs full request URLs)."""

    _TOKEN_IN_URL = re.compile(r"(api\.telegram\.org/bot)([^/\s]+)")

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        if "api.telegram.org/bot" in msg or (
            TELEGRAM_BOT_TOKEN and TELEGRAM_BOT_TOKEN in msg
        ):
            redacted = self._TOKEN_IN_URL.sub(r"\1***", msg)
            if TELEGRAM_BOT_TOKEN:
                redacted = redacted.replace(TELEGRAM_BOT_TOKEN, "***")
            record.msg = redacted
            record.args = ()
        return True


_log_level = getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO)
_handler_stream = logging.StreamHandler()
_handler_file = logging.FileHandler("data/bot.log", encoding="utf-8")
_redact = _SecretRedactingFilter()
for _h in (_handler_stream, _handler_file):
    _h.addFilter(_redact)

logging.basicConfig(
    level=_log_level,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[_handler_stream, _handler_file],
)
logger = logging.getLogger(__name__)


async def main():
    validate_config()
    init_db()
    logger.info("Database initialized")

    # Build Telegram app
    from tg_bot.bot import build_app
    app = build_app()

    logger.info("Starting Telegram bot polling...")
    from telegram import Update
    async with app:
        await app.start()
        # allowed_updates=ALL_TYPES → server returns every update kind, including
        # plain text messages in groups (needed even after privacy is disabled).
        await app.updater.start_polling(
            drop_pending_updates=True,
            allowed_updates=Update.ALL_TYPES,
        )
        logger.info("Bot is running. Press Ctrl+C to stop.")

        # Continuous generation loop, runs in background so Telegram buttons stay responsive.
        # Caps unreviewed pins at PENDING_LIMIT — waits for the user to act before refilling.
        from db.database import count_review_queue
        from generators.image_gen import GeminiBlockedError
        from pipeline import run_generation_step

        PENDING_LIMIT = 2

        async def _generation_loop():
            while True:
                try:
                    if count_review_queue() >= PENDING_LIMIT:
                        await asyncio.sleep(15)
                        continue
                    await run_generation_step()
                    await asyncio.sleep(2)
                except GeminiBlockedError as e:
                    logger.error(
                        "Gemini unavailable (%s); pausing 10 min before retry",
                        e,
                    )
                    await asyncio.sleep(600)
                except Exception:
                    logger.exception("Generation loop iteration failed; continuing")
                    await asyncio.sleep(5)

        asyncio.create_task(_generation_loop())

        try:
            await asyncio.Event().wait()
        except (KeyboardInterrupt, SystemExit):
            pass
        finally:
            await app.updater.stop()
            await app.stop()
            from generators.image_gen import shutdown_client
            await shutdown_client()
            logger.info("Bot stopped.")


if __name__ == "__main__":
    asyncio.run(main())
