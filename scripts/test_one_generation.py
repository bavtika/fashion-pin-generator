"""One-shot generation test (no Telegram). Exit 0 on success."""
import asyncio
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)


async def main() -> int:
    from generators.image_gen import generate_outfit_image, shutdown_client

    try:
        path, prompt, desc, ref = await generate_outfit_image()
        print(f"OK image={path}")
        print(f"ref={ref}")
        print(f"desc_len={len(desc)}")
        print(f"prompt_len={len(prompt)}")
        return 0
    except Exception as e:
        print(f"FAIL: {e}")
        return 1
    finally:
        await shutdown_client()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
