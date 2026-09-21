"""One-shot: refresh styles → generate one outfit image → save. No Telegram, no posting."""
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


async def run_once():
    from config import validate_config
    from generators.image_gen import generate_outfit_image
    from pinterest.scraper import refresh_styles

    validate_config()

    logger.info("=== Refreshing style images ===")
    await refresh_styles()

    logger.info("=== Generating outfit image ===")
    image_path, prompt, description, reference_path = await generate_outfit_image()
    logger.info(f"Image: {image_path}")
    logger.info(f"Reference: {reference_path}")
    logger.info(f"Description: {description[:200]}")
    return True


if __name__ == "__main__":
    result = asyncio.run(run_once())
    sys.exit(0 if result else 1)
