"""
Core pipeline.
Generates an outfit image and ships it to Telegram for review.
No auto-posting, no Amazon, no SEO — just generate → send → optional regen.
"""
import asyncio
import logging

from db.database import create_pin, get_pin, update_pin

logger = logging.getLogger(__name__)

_generation_lock = asyncio.Lock()


async def run_generation_step(max_retries: int = 5):
    """Refresh references, generate one image, send to Telegram for approval.
    Retries up to max_retries times if Gemini declines.
    """
    from pinterest.scraper import MIN_STYLES, count_styles, refresh_styles

    # Don't block generation while topping up from 17→20 styles.
    MIN_STYLES_TO_RUN = min(12, MIN_STYLES)
    from generators.image_gen import GeminiBlockedError, generate_outfit_image
    from tg_bot.bot import notify_operator, send_for_approval

    if _generation_lock.locked():
        logger.info("Generation already in progress — skipping duplicate run")
        return

    async with _generation_lock:
        logger.info("Starting generation step...")
        refresh_after = False
        n_styles = count_styles()
        if n_styles < MIN_STYLES_TO_RUN:
            await refresh_styles()
        elif n_styles < MIN_STYLES:
            logger.info(
                f"Have {n_styles} styles (target {MIN_STYLES}) — generating first, "
                "will refresh styles after this pin"
            )
            refresh_after = True
        else:
            refresh_after = False

        last_error: Exception | None = None
        for attempt in range(1, max_retries + 1):
            try:
                image_path, prompt, description, reference_path = await generate_outfit_image()
                pin_id = create_pin(image_path, prompt)
                update_pin(
                    pin_id,
                    clothing_description=description,
                    reference_path=reference_path,
                )
                logger.info(f"Created pin #{pin_id}: {image_path}")
                await send_for_approval(pin_id)
                if refresh_after:
                    await refresh_styles()
                return
            except GeminiBlockedError as e:
                last_error = e
                logger.warning(f"Generation attempt {attempt}/{max_retries} blocked: {e}")
                from generators.image_gen import shutdown_client

                await shutdown_client()
                await asyncio.sleep(min(120, 30 * attempt))
                if attempt == max_retries:
                    await notify_operator(
                        f"❌ Gemini недоступен после {max_retries} попыток.\n{str(e)[:400]}"
                    )
                    raise
                continue
            except Exception as e:
                last_error = e
                logger.warning(f"Generation attempt {attempt}/{max_retries} failed: {e}")
                from generators.image_gen import shutdown_client

                await shutdown_client()
                # Back off when Gemini is slow/overloaded (avoid 5×55s hammering).
                await asyncio.sleep(min(60, 10 * attempt))
                if attempt == max_retries:
                    logger.error("All generation attempts failed", exc_info=True)
                    detail = str(last_error)[:400] if last_error else "unknown error"
                    await notify_operator(
                        f"❌ Генерация не удалась после {max_retries} попыток.\n{detail}"
                    )


async def regenerate_pin(pin_id: int):
    """Regenerate an image for a pin reusing the cached outfit description."""
    from generators.image_gen import regenerate_with_description
    from tg_bot.bot import send_for_approval

    pin = get_pin(pin_id)
    if not pin:
        return
    description = pin.get("clothing_description")
    if not description:
        logger.error(f"Pin #{pin_id}: no stored description, can't regenerate")
        update_pin(pin_id, status="error")
        return

    try:
        image_path, prompt, _, _ = await regenerate_with_description(description)
        update_pin(pin_id, image_path=image_path, prompt=prompt, status="pending_approval")
        await send_for_approval(pin_id)
    except Exception as e:
        logger.error(f"Pin #{pin_id}: Regeneration failed: {e}", exc_info=True)
        update_pin(pin_id, status="error")


async def regenerate_pin_custom(pin_id: int, custom_prompt: str):
    """Regenerate using a user-provided scene description (replaces Gemini's)."""
    from generators.image_gen import regenerate_with_description
    from tg_bot.bot import send_for_approval

    pin = get_pin(pin_id)
    if not pin:
        return

    try:
        image_path, prompt, _, _ = await regenerate_with_description(custom_prompt)
        update_pin(
            pin_id,
            image_path=image_path,
            prompt=prompt,
            clothing_description=custom_prompt,
            status="pending_approval",
        )
        await send_for_approval(pin_id)
    except Exception as e:
        logger.error(f"Pin #{pin_id}: Custom-prompt regen failed: {e}", exc_info=True)
        update_pin(pin_id, status="error")


async def regenerate_pin_pose(pin_id: int):
    """Pose regen: feed the pin's current image back to Gemini for a new pose."""
    from pathlib import Path

    from generators.image_gen import regenerate_pose
    from tg_bot.bot import send_for_approval

    pin = get_pin(pin_id)
    if not pin:
        return
    src_path = pin.get("image_path")
    if not src_path or not Path(src_path).exists():
        logger.error(f"Pin #{pin_id}: source image missing, can't pose-regen")
        update_pin(pin_id, status="error")
        return

    try:
        image_path, prompt, _, _ = await regenerate_pose(src_path)
        update_pin(pin_id, image_path=image_path, prompt=prompt, status="pending_approval")
        await send_for_approval(pin_id)
    except Exception as e:
        logger.error(f"Pin #{pin_id}: Pose regen failed: {e}", exc_info=True)
        update_pin(pin_id, status="error")
