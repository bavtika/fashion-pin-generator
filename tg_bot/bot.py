"""
Telegram bot for human review of generated pins.
Buttons: Approve, Regen (same description), Regen pose, Custom prompt.
"""
import logging
from pathlib import Path

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
from db.database import get_pin, update_pin

logger = logging.getLogger(__name__)

_app: Application = None

# {'pin_id': int, 'message_id': int, 'chat_id': int}
_pending_custom: dict | None = None


def _is_authorized(update: Update) -> bool:
    chat = update.effective_chat
    user = update.effective_user
    if chat and chat.id == TELEGRAM_CHAT_ID:
        return True
    if user and user.id == TELEGRAM_CHAT_ID:
        return True
    return False


def _approval_keyboard(pin_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Одобрить", callback_data=f"approve:{pin_id}"),
            InlineKeyboardButton("🔄 Регенер", callback_data=f"regen:{pin_id}"),
        ],
        [
            InlineKeyboardButton("🕺 Регенер позы", callback_data=f"regenpose:{pin_id}"),
            InlineKeyboardButton("✏️ Свой промпт", callback_data=f"customprompt:{pin_id}"),
        ],
        [
            InlineKeyboardButton("🗑 Брак", callback_data=f"brak:{pin_id}"),
        ],
    ])


def _cancel_keyboard(pin_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("❌ Отмена", callback_data=f"cancelprompt:{pin_id}"),
    ]])


async def _handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global _pending_custom

    query = update.callback_query
    if not _is_authorized(update):
        await query.answer("Not authorized", show_alert=True)
        logger.warning(
            f"Rejected callback from unauthorized user_id={update.effective_user.id if update.effective_user else '?'}"
        )
        return

    await query.answer()

    try:
        action, pin_id_str = query.data.split(":")
        pin_id = int(pin_id_str)
    except (ValueError, AttributeError):
        logger.warning(f"Malformed callback data: {query.data!r}")
        return

    if action == "approve":
        update_pin(pin_id, status="approved")
        await query.edit_message_caption(
            caption=f"✅ Pin #{pin_id} одобрен. Следующий сгенерируется автоматически."
        )

    elif action == "regen":
        update_pin(pin_id, status="regenerating")
        await query.edit_message_caption(caption=f"🔄 Регенерирую pin #{pin_id}...")
        from pipeline import regenerate_pin
        context.application.create_task(regenerate_pin(pin_id))

    elif action == "regenpose":
        update_pin(pin_id, status="regenerating_pose")
        await query.edit_message_caption(caption=f"🕺 Регенерирую позу pin #{pin_id}...")
        from pipeline import regenerate_pin_pose
        context.application.create_task(regenerate_pin_pose(pin_id))

    elif action == "customprompt":
        _pending_custom = {
            "pin_id": pin_id,
            "message_id": query.message.message_id,
            "chat_id": query.message.chat_id,
        }
        await query.edit_message_caption(
            caption=f"✏️ Pin #{pin_id}: пришли свой промпт следующим сообщением",
            reply_markup=_cancel_keyboard(pin_id),
        )

    elif action == "brak":
        update_pin(pin_id, status="rejected")
        await query.edit_message_caption(
            caption=f"🗑 Pin #{pin_id} — брак. Следующий сгенерируется автоматически."
        )

    elif action == "cancelprompt":
        _pending_custom = None
        await query.edit_message_caption(
            caption=f"📸 Pin #{pin_id}",
            reply_markup=_approval_keyboard(pin_id),
        )


async def _consume_custom_prompt(text: str, application: Application):
    """Shared logic: take a text prompt and trigger regen for the pending pin."""
    global _pending_custom
    if _pending_custom is None:
        return False
    pending = _pending_custom
    _pending_custom = None
    pin_id = pending["pin_id"]

    try:
        await _app.bot.edit_message_caption(
            chat_id=pending["chat_id"],
            message_id=pending["message_id"],
            caption=f"🔄 Pin #{pin_id}: генерирую с твоим промптом...",
        )
    except Exception as e:
        logger.warning(f"Failed to edit caption on prompt receive: {e}")

    update_pin(pin_id, status="regenerating_custom")
    from pipeline import regenerate_pin_custom
    application.create_task(regenerate_pin_custom(pin_id, text))
    return True


async def _log_any_update(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Diagnostic: log every update the bot receives so we can see what's coming."""
    chat = update.effective_chat
    user = update.effective_user
    msg = update.message
    cb = update.callback_query
    chat_id = chat.id if chat else None
    chat_type = chat.type if chat else "?"
    user_id = user.id if user else None
    username = (user.username if user else None) or "?"
    text_preview = repr(msg.text[:50]) if (msg and msg.text) else None
    cb_data = cb.data if cb else None
    logger.info(
        f"UPDATE chat={chat_id}({chat_type}) user={user_id}({username}) "
        f"msg={text_preview} callback={cb_data}"
    )


async def _handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Plain text → if a pin is awaiting a prompt, use it as the scene description."""
    if not _is_authorized(update):
        return
    if _pending_custom is None:
        return
    text = (update.message.text or "").strip()
    if text:
        await _consume_custom_prompt(text, context.application)


async def _cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_authorized(update):
        return
    import sqlite3

    from db.database import DB_PATH
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(
            "SELECT status, COUNT(*) FROM pins GROUP BY status ORDER BY 2 DESC"
        ).fetchall()
    if not rows:
        await update.message.reply_text("No pins yet.")
        return
    lines = [f"`{s or 'None':<20}` {n}" for s, n in rows]
    await update.message.reply_text("*Pin status*\n" + "\n".join(lines), parse_mode="Markdown")


async def _cmd_generate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_authorized(update):
        return
    await update.message.reply_text("Generating new pin...")
    from pipeline import run_generation_step
    context.application.create_task(run_generation_step())


async def notify_operator(text: str) -> None:
    """Best-effort operator notification (generation failures, etc.)."""
    if _app is None:
        logger.warning("notify_operator skipped — bot not started yet: %s", text[:120])
        return
    try:
        await _app.bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=text)
    except Exception as e:
        logger.error(f"Failed to notify operator: {e}")


async def send_for_approval(pin_id: int) -> None:
    """Send the reference and the generated image to Telegram for approval."""
    pin = get_pin(pin_id)
    if not pin:
        logger.error(f"send_for_approval: pin #{pin_id} not found")
        return

    image_path = pin.get("image_path")
    reference_path = pin.get("reference_path")

    if not image_path or not Path(image_path).exists():
        logger.error(f"send_for_approval: pin #{pin_id} missing image at {image_path!r}")
        update_pin(pin_id, status="error")
        await notify_operator(f"❌ Pin #{pin_id}: файл изображения не найден ({image_path!r})")
        return

    if reference_path and Path(reference_path).exists():
        with open(reference_path, "rb") as ref:
            await _app.bot.send_photo(
                chat_id=TELEGRAM_CHAT_ID,
                photo=ref,
                caption=f"📌 Референс для Pin #{pin_id}",
            )

    with open(image_path, "rb") as photo:
        await _app.bot.send_photo(
            chat_id=TELEGRAM_CHAT_ID,
            photo=photo,
            caption=f"📸 Pin #{pin_id}",
            reply_markup=_approval_keyboard(pin_id),
        )
    logger.info(f"Sent pin #{pin_id} to Telegram for approval")


def build_app() -> Application:
    global _app
    _app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    # group=-1 runs first, just for logging — doesn't consume the update
    _app.add_handler(MessageHandler(filters.ALL, _log_any_update), group=-1)
    _app.add_handler(CallbackQueryHandler(_log_any_update), group=-1)
    _app.add_handler(CallbackQueryHandler(_handle_callback))
    _app.add_handler(CommandHandler("status", _cmd_status))
    _app.add_handler(CommandHandler("generate", _cmd_generate))
    _app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, _handle_text))
    return _app
