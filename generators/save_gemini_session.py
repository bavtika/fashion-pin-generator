"""
Save Gemini web session cookies for gemini_webapi.

Interactive (default): opens a browser — log in at gemini.google.com, then press Enter.
From Chrome: reads cookies from an already-logged-in Chrome profile (close Chrome first on Windows).

Usage:
  python generators/save_gemini_session.py
  python generators/save_gemini_session.py --from-chrome
  python generators/save_gemini_session.py --from-chrome --browser chrome
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

GEMINI_URL = "https://gemini.google.com/app"
REQUIRED_COOKIES = ("__Secure-1PSID", "__Secure-1PSIDTS")


def _pick_auth_cookies(cookies: list[dict]) -> dict[str, str] | None:
    """Extract PSID + PSIDTS from Playwright or browser-cookie3 cookie dicts."""
    wanted: dict[str, str] = {}
    for c in cookies:
        name = c.get("name") or ""
        if name not in REQUIRED_COOKIES:
            continue
        value = (c.get("value") or "").strip()
        if value:
            wanted[name] = value
    if wanted.get("__Secure-1PSID") and wanted.get("__Secure-1PSIDTS"):
        return wanted
    return None


def _save_cookie_file(payload: dict[str, str]) -> Path:
    from generators.image_gen import COOKIES_FILE

    COOKIES_FILE.parent.mkdir(parents=True, exist_ok=True)
    COOKIES_FILE.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    try:
        COOKIES_FILE.chmod(0o600)
    except OSError:
        pass
    return COOKIES_FILE


def _load_from_chrome(browser: str | None) -> dict[str, str]:
    try:
        import browser_cookie3 as bc3
    except ImportError as e:
        raise RuntimeError(
            "browser-cookie3 is not installed. Run: pip install browser-cookie3"
        ) from e

    loaders = {
        "chrome": bc3.chrome,
        "chromium": bc3.chromium,
        "brave": bc3.brave,
        "edge": bc3.edge,
        "opera": bc3.opera,
        "firefox": bc3.firefox,
    }
    if browser:
        if browser not in loaders:
            raise RuntimeError(f"Unknown browser {browser!r}. Choose: {', '.join(loaders)}")
        fns = [loaders[browser]]
    else:
        fns = [loaders["chrome"], loaders["chromium"], loaders["brave"], loaders["edge"]]

    last_err: Exception | None = None
    for fn in fns:
        try:
            jar = fn(domain_name="gemini.google.com")
        except Exception as e:
            last_err = e
            continue
        cookies = [
            {
                "name": c.name,
                "value": c.value,
                "domain": c.domain,
                "path": c.path,
            }
            for c in jar
            if not c.is_expired()
        ]
        picked = _pick_auth_cookies(cookies)
        if picked:
            print(f"Loaded cookies from {fn.__name__} (gemini.google.com)")
            return picked
    hint = (
        "Close Chrome completely, log in at gemini.google.com, then retry.\n"
        "Or run without --from-chrome to log in via the built-in browser."
    )
    raise RuntimeError(
        f"Could not find {REQUIRED_COOKIES} in browser cookies. {hint}"
    ) from last_err


async def _save_via_playwright() -> dict[str, str]:
    from playwright.async_api import async_playwright

    print("Opening Gemini in browser...")
    print("Log in at gemini.google.com, open the chat once, then press Enter here.")
    print()

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=False,
            args=["--disable-blink-features=AutomationControlled"],
        )
        context = await browser.new_context(
            viewport={"width": 1366, "height": 900},
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
        await page.goto(GEMINI_URL, wait_until="domcontentloaded")

        print(f"Browser opened: {GEMINI_URL}")
        print("After you see Gemini and can send a message — press Enter in this terminal.")
        await asyncio.to_thread(input)

        raw = await context.cookies()
        await browser.close()

    picked = _pick_auth_cookies(raw)
    if not picked:
        have = [c["name"] for c in raw if c.get("name", "").startswith("__Secure")]
        raise RuntimeError(
            f"Missing {REQUIRED_COOKIES} after login. Found secure cookies: {have or 'none'}.\n"
            "Try sending one message in Gemini, then run this script again."
        )
    return picked


async def _verify_saved_session() -> bool:
    # Force reload — drop cached client so we read the file we just wrote.
    import generators.image_gen as ig
    from generators.image_gen import GeminiBlockedError, shutdown_client

    await shutdown_client()

    try:
        client = await ig._get_client()
        status = getattr(client, "account_status", None)
        print(f"Verification OK — account status: {status}")
        return True
    except GeminiBlockedError as e:
        print(f"Verification FAILED — {e}")
        return False
    finally:
        await shutdown_client()


async def _run(from_chrome: bool, browser: str | None, skip_verify: bool) -> int:
    if from_chrome:
        print("Reading Gemini cookies from local browser profile...")
        payload = _load_from_chrome(browser)
    else:
        payload = await _save_via_playwright()

    path = _save_cookie_file(payload)
    print(f"Saved session to {path}")
    print(f"  __Secure-1PSID:    {payload['__Secure-1PSID'][:12]}…")
    print(f"  __Secure-1PSIDTS:  {payload['__Secure-1PSIDTS'][:12]}…")

    if skip_verify:
        print("Skipped API verification (--no-verify).")
        return 0

    print("\nVerifying with gemini_webapi...")
    ok = await _verify_saved_session()
    return 0 if ok else 1


def main() -> None:
    parser = argparse.ArgumentParser(description="Save Gemini session cookies for the bot.")
    parser.add_argument(
        "--from-chrome",
        action="store_true",
        help="Read cookies from Chrome/Edge/Brave (profile must already be logged in)",
    )
    parser.add_argument(
        "--browser",
        choices=("chrome", "chromium", "brave", "edge", "opera", "firefox"),
        help="Browser profile for --from-chrome (default: try chrome, then others)",
    )
    parser.add_argument(
        "--no-verify",
        action="store_true",
        help="Save cookies without calling Gemini API to verify",
    )
    args = parser.parse_args()

    try:
        code = asyncio.run(
            _run(
                from_chrome=args.from_chrome,
                browser=args.browser,
                skip_verify=args.no_verify,
            )
        )
    except KeyboardInterrupt:
        print("\nCancelled.")
        code = 130
    except Exception as e:
        print(f"Error: {e}")
        code = 1

    raise SystemExit(code)


if __name__ == "__main__":
    main()
