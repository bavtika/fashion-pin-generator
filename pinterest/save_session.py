"""
Run this ONCE manually to save Pinterest session.
You log in yourself in the browser window — bot never sees your password.
Usage: python pinterest/save_session.py
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from pathlib import Path

from playwright.async_api import async_playwright

SESSION_FILE = Path("data/pinterest_session.json")


async def main():
    print("Opening Pinterest in browser...")
    print("LOG IN YOURSELF — then press Enter here.")
    print()

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=False,
            args=["--disable-blink-features=AutomationControlled"]
        )
        context = await browser.new_context(
            viewport={"width": 1366, "height": 768},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
        )
        await context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )

        page = await context.new_page()
        await page.goto("https://www.pinterest.com/login/")

        print("Browser opened. Log in to Pinterest manually.")
        print("After you see your feed — press Enter in this terminal.")
        input()

        # Save session regardless — user confirmed they're logged in
        SESSION_FILE.parent.mkdir(exist_ok=True)
        await context.storage_state(path=str(SESSION_FILE))
        print(f"Session saved to {SESSION_FILE}")
        print(f"Current URL: {page.url}")
        print("Bot will use this session for posting.")

        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
