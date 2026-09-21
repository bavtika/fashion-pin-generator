"""Quick check: can we reach Gemini with current cookies? Exit 0 = OK, 1 = not auth."""
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


async def main() -> int:
    from generators.image_gen import GeminiBlockedError, _get_client, shutdown_client

    try:
        client = await _get_client()
        status = getattr(client, "account_status", None)
        print(f"OK — Gemini authenticated (status={status})")
        return 0
    except GeminiBlockedError as e:
        print(f"FAIL — {e}")
        print("Fix: python generators/save_gemini_session.py")
        return 1
    finally:
        await shutdown_client()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
