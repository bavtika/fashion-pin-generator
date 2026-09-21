"""
Standalone preview bot: every hour generate one outfit and post to Telegram
with Approve / Regen buttons. Regen reuses the cached outfit description.

This is a slimmer alt entry point to main.py — it has no DB, no scheduler library,
just an asyncio loop.
"""
import asyncio
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
os.makedirs("data", exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("data/bot.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger(__name__)


from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CallbackQueryHandler, ContextTypes

from config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, validate_config

_generation_lock = asyncio.Lock()
_current_description: str | None = None  # last outfit description (reused on regen)


async def _generate_and_send(app: Application, reuse_description: bool = False):
    """Generate one outfit image and send to Telegram.

    reuse_description=True → skip describing, regenerate using cached description.
    reuse_description=False → fresh reference + describe + generate.
    """
    global _current_description

    from generators.image_gen import generate_outfit_image, regenerate_with_description
    from pinterest.scraper import refresh_styles

    async with _generation_lock:
        try:
            if reuse_description:
                if not _current_description:
                    logger.warning("No cached description — falling back to fresh generation")
                    reuse_description = False

            if reuse_description:
                logger.info("Regenerating with cached description")
                image_path, _, _, _ = await regenerate_with_description(_current_description)
            else:
                from pinterest.scraper import MIN_STYLES, count_styles
                if count_styles() < MIN_STYLES:
                    await refresh_styles()
                logger.info("Generating outfit (fresh reference)...")
                image_path, _, description, _ = await generate_outfit_image()
                _current_description = description

            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Одобрить", callback_data="approve"),
                InlineKeyboardButton("🔄 Регенер", callback_data="regen"),
            ]])

            with open(image_path, "rb") as photo:
                await app.bot.send_photo(
                    chat_id=TELEGRAM_CHAT_ID,
                    photo=photo,
                    caption="👗 Новый образ",
                    reply_markup=keyboard,
                )
            logger.info("Sent to Telegram")

        except Exception as e:
            logger.error(f"Generation failed: {e}", exc_info=True)
            try:
                await app.bot.send_message(
                    chat_id=TELEGRAM_CHAT_ID,
                    text=f"❌ Генерация не удалась: {e}",
                )
            except Exception:
                pass


async def _handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    chat = update.effective_chat
    if not chat or chat.id != TELEGRAM_CHAT_ID:
        await query.answer("Not authorized", show_alert=True)
        return

    await query.answer()

    if query.data == "approve":
        await query.edit_message_caption(caption="✅ Одобрено")
        return

    if query.data == "regen":
        await query.edit_message_caption(caption="🔄 Регенерирую с тем же описанием...")
        context.application.create_task(
            _generate_and_send(context.application, reuse_description=True)
        )


async def _hourly_generation(app: Application):
    """Generate a fresh image (new reference) every hour."""
    while True:
        await _generate_and_send(app, reuse_description=False)
        delay = 3600
        logger.info(f"Next generation in {delay // 60} min")
        await asyncio.sleep(delay)


async def main():
    validate_config()

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CallbackQueryHandler(_handle_callback))

    async with app:
        await app.start()
        await app.updater.start_polling(drop_pending_updates=True)
        logger.info("Preview bot running. Generating every hour. Press Ctrl+C to stop.")

        gen_task = asyncio.create_task(_hourly_generation(app))

        try:
            await asyncio.Event().wait()
        except (KeyboardInterrupt, SystemExit):
            pass
        finally:
            gen_task.cancel()
            await app.updater.stop()
            await app.stop()
            logger.info("Bot stopped.")


if __name__ == "__main__":
    asyncio.run(main())
