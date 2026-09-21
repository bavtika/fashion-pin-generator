"""
Fashion image generation via the reverse-engineered Gemini web API
(gemini_webapi by HanaokaYuzu) — no browser, no captcha wall.

Workflow:
  - reference/model/  — photos of the model (face source), one picked at random
  - reference/styles/ — outfit inspiration images, cycled in order
  Two-step prompt:
    1) attach style → ask Gemini for a faithful (color-inverted) outfit description
    2) attach model → "edit this image to wear that outfit", returns image bytes

Cookies:
  - default: pulled from local Chrome via browser-cookie3 (no manual setup;
    user just needs to be logged in to gemini.google.com in Chrome)
  - override: data/gemini_cookies.json
    {"__Secure-1PSID": "...", "__Secure-1PSIDTS": "..."}
  Refreshed cookies persist across restarts via GEMINI_COOKIE_PATH.
"""
import asyncio
import contextvars
import enum
import json
import logging
import os
import random
import shutil
import time
from pathlib import Path

from dotenv import load_dotenv
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from playwright.async_api import async_playwright

load_dotenv()

# gemini_webapi imports enum.StrEnum (added in Python 3.11). On 3.10 we polyfill
# from the `StrEnum` PyPI backport before any gemini_webapi import.
if not hasattr(enum, "StrEnum"):
    from strenum import StrEnum as _StrEnumBackport  # type: ignore
    enum.StrEnum = _StrEnumBackport  # type: ignore[attr-defined]

import gemini_webapi.exceptions as _gex
from gemini_webapi import ChatSession, GeminiClient
from gemini_webapi.constants import AccountStatus, Model
from gemini_webapi.exceptions import (
    APIError,
    AuthError,
    GeminiError,
)
from gemini_webapi.exceptions import (
    TimeoutError as GeminiTimeoutError,
)

# gemini_webapi 2.1+ renamed *Error suffixes; keep aliases for both 2.0 and 2.1+.
TemporarilyBlocked = getattr(_gex, "TemporarilyBlocked", None) or getattr(
    _gex, "TemporarilyBlockedError"
)
UsageLimitExceeded = getattr(_gex, "UsageLimitExceeded", None) or getattr(
    _gex, "UsageLimitExceededError"
)

from generators.prompts import (
    DESCRIBE_PROMPT,
    POSE_REGEN_PROMPT,
    generate_from_description_prompt,
)

logger = logging.getLogger(__name__)

OUTPUT_DIR    = Path("data/generated")
MODEL_DIR     = Path("reference/model")
STYLES_DIR    = Path("reference/styles")
COOKIES_FILE  = Path("data/gemini_cookies.json")
COOKIE_CACHE  = Path("data/gemini_webapi_cache")

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}

# Persist auto-refreshed cookies across bot restarts.
COOKIE_CACHE.mkdir(parents=True, exist_ok=True)
os.environ.setdefault("GEMINI_COOKIE_PATH", str(COOKIE_CACHE.resolve()))

# ~1 min per image attempt. Watchdog must match step timeout — otherwise the
# library kills silent image streams at 50s while we still wait to 65s.
GEMINI_IMAGE_STEP_TIMEOUT = float(os.getenv("GEMINI_IMAGE_STEP_TIMEOUT", "75"))
GEMINI_DESCRIBE_STEP_TIMEOUT = float(os.getenv("GEMINI_DESCRIBE_STEP_TIMEOUT", "90"))
GEMINI_REQUEST_TIMEOUT = float(os.getenv("GEMINI_REQUEST_TIMEOUT", "90"))
_STEP_TIMEOUT_CEILING = max(GEMINI_IMAGE_STEP_TIMEOUT, GEMINI_DESCRIBE_STEP_TIMEOUT)
GEMINI_WATCHDOG_TIMEOUT = float(
    os.getenv("GEMINI_WATCHDOG_TIMEOUT", str(_STEP_TIMEOUT_CEILING))
)
GEMINI_IMAGE_MAX_ATTEMPTS = int(os.getenv("GEMINI_IMAGE_MAX_ATTEMPTS", "2"))
GEMINI_RECOVERY_TIMEOUT = float(os.getenv("GEMINI_RECOVERY_TIMEOUT", "25"))
MAX_DESCRIPTION_CHARS = int(os.getenv("GEMINI_MAX_DESCRIPTION_CHARS", "1200"))
IMAGE_MODEL = Model.BASIC_FLASH
DESCRIBE_MODEL = Model.UNSPECIFIED  # vision+text; BASIC_FLASH is for image generation
STYLE_UPLOAD_MAX_PX = int(os.getenv("GEMINI_STYLE_MAX_PX", "1280"))
GEMINI_BROWSER_DESCRIBE_ENABLED = os.getenv("GEMINI_BROWSER_DESCRIBE_ENABLED", "1") == "1"
GEMINI_BROWSER_DESCRIBE_TIMEOUT = float(os.getenv("GEMINI_BROWSER_DESCRIBE_TIMEOUT", "75"))


class GeminiBlockedError(RuntimeError):
    """Raised when Gemini refuses to serve us (auth, IP block, quota)."""


# ──────────────────────────────────────────────
# Client singleton
# ──────────────────────────────────────────────

_client: GeminiClient | None = None
_client_lock = asyncio.Lock()


def _load_cookies_file() -> tuple[str, str] | None:
    """Return (PSID, PSIDTS) from data/gemini_cookies.json if both present, else None."""
    if not COOKIES_FILE.exists():
        return None
    try:
        data = json.loads(COOKIES_FILE.read_text(encoding="utf-8-sig"))
    except Exception as e:
        logger.warning(f"Could not parse {COOKIES_FILE}: {e}")
        return None
    psid = (data.get("__Secure-1PSID") or "").strip()
    psidts = (data.get("__Secure-1PSIDTS") or "").strip()
    if psid and psidts:
        return psid, psidts
    return None


async def _get_client() -> GeminiClient:
    global _client
    if _client is not None:
        return _client
    async with _client_lock:
        if _client is not None:
            return _client

        override = _load_cookies_file()
        if override:
            psid, psidts = override
            client = GeminiClient(psid, psidts, proxy=None)
            cookie_source = f"{COOKIES_FILE}"
        else:
            # No override — let the lib import cookies from local Chrome via
            # browser-cookie3, then auto-refresh via cached path.
            client = GeminiClient(proxy=None)
            cookie_source = "browser-cookie3 / cookie cache"

        try:
            await client.init(
                timeout=GEMINI_REQUEST_TIMEOUT,
                auto_close=False,
                auto_refresh=True,
                watchdog_timeout=GEMINI_WATCHDOG_TIMEOUT,
            )
        except AuthError as e:
            raise GeminiBlockedError(
                f"Gemini cookies invalid ({cookie_source}): {e}. "
                "Run: python generators/save_gemini_session.py"
            ) from e
        if getattr(client, "account_status", None) == AccountStatus.UNAUTHENTICATED:
            raise GeminiBlockedError(
                f"Gemini session not authenticated ({cookie_source}). "
                "Run: python generators/save_gemini_session.py"
            )
        _client = client
        logger.info(f"Gemini client initialized via {cookie_source} (auto-refresh on)")
        return client


async def shutdown_client() -> None:
    """Close the client cleanly on bot shutdown."""
    global _client
    if _client is None:
        return
    try:
        await _client.close()
    except Exception:
        pass
    _client = None


async def _invalidate_client() -> None:
    """Drop cached client/chat after a failed generation so the next attempt reconnects."""
    await shutdown_client()


# ──────────────────────────────────────────────
# Reference photo helpers
# ──────────────────────────────────────────────

def _list_images(directory: Path) -> list[Path]:
    return sorted(
        p for p in directory.iterdir()
        if p.suffix.lower() in IMAGE_EXTENSIONS
    )


def pick_model_photo() -> str:
    photos = _list_images(MODEL_DIR)
    if not photos:
        raise RuntimeError(
            f"No model photos found in {MODEL_DIR}/\n"
            "Add at least one .jpg/.png photo of the model there."
        )
    chosen = random.choice(photos)
    logger.info(f"Model photo: {chosen.name}")
    return str(chosen)


def pick_next_style() -> str:
    styles = _list_images(STYLES_DIR)
    if not styles:
        raise RuntimeError(
            f"No style images found in {STYLES_DIR}/\n"
            "Add outfit inspiration photos there."
        )
    chosen = random.choice(styles)
    logger.info(f"Style image: {chosen.name} ({len(styles)} remaining)")
    return str(chosen)


def archive_used_style(path: str) -> str | None:
    """Move a used reference out of styles/ into OUTPUT_DIR so it survives for
    Telegram preview but isn't picked again."""
    try:
        src = Path(path)
        if not src.exists():
            return None
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        dest = OUTPUT_DIR / f"ref_{int(time.time())}_{src.name}"
        shutil.move(str(src), str(dest))
        logger.info(f"Archived style: {src.name} -> {dest.name}")
        return str(dest)
    except Exception as e:
        logger.warning(f"Could not archive style {path}: {e}")
        return None


# ──────────────────────────────────────────────
# Gemini call wrappers
# ──────────────────────────────────────────────

def _trim_description(description: str) -> str:
    if len(description) <= MAX_DESCRIPTION_CHARS:
        return description
    trimmed = description[:MAX_DESCRIPTION_CHARS].rsplit(" ", 1)[0]
    logger.warning(
        f"Trimmed outfit description {len(description)} -> {len(trimmed)} chars"
    )
    return trimmed


GEMINI_POLL_INTERVAL = float(os.getenv("GEMINI_POLL_INTERVAL", "8"))
GEMINI_POST_DEADLINE_GRACE = float(os.getenv("GEMINI_POST_DEADLINE_GRACE", "8"))


def _map_api_error(exc: BaseException) -> BaseException:
    if isinstance(exc, APIError):
        err = str(exc)
        if "1097" in err:
            return GeminiBlockedError(f"Gemini overloaded (1097): {exc}")
        if "1100" in err or "unauthenticated" in err.lower():
            return GeminiBlockedError(f"Gemini auth/service error: {exc}")
    return exc


def _chat_cid(chat: ChatSession) -> str:
    cid = getattr(chat, "cid", None) or ""
    if cid:
        return cid
    last = getattr(chat, "last_output", None)
    if last and getattr(last, "metadata", None):
        return last.metadata[0] or ""
    return ""


def _prepare_style_upload(style_path: str) -> str:
    """Downscale large Pinterest refs so Gemini vision responds faster."""
    from PIL import Image

    src = Path(style_path)
    if not src.exists():
        return style_path
    try:
        with Image.open(src) as im:
            im = im.convert("RGB")
            w, h = im.size
            if max(w, h) <= STYLE_UPLOAD_MAX_PX:
                return style_path
            scale = STYLE_UPLOAD_MAX_PX / max(w, h)
            resized = im.resize((int(w * scale), int(h * scale)), Image.Resampling.LANCZOS)
            out = OUTPUT_DIR / f"upload_{src.stem}.jpg"
            OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
            resized.save(out, format="JPEG", quality=88, optimize=True)
            logger.info(f"Resized style for upload: {w}x{h} -> {resized.size[0]}x{resized.size[1]}")
            return str(out)
    except Exception as e:
        logger.warning(f"Could not resize {style_path}: {e}")
        return style_path


async def _drain_task_error(task: asyncio.Task) -> BaseException | None:
    if not task.done():
        return None
    exc = task.exception()
    if exc is None or isinstance(exc, asyncio.CancelledError):
        return None
    return _map_api_error(exc)


async def _await_with_chat_recovery(
    client: GeminiClient,
    chat: ChatSession,
    coro,
    deadline: float,
    *,
    require_image: bool = False,
) -> object:
    """Wait for send_message while polling chat history (image often lands before stream ends)."""
    task = asyncio.create_task(coro)
    end = time.monotonic() + deadline
    try:
        while not task.done():
            remaining = end - time.monotonic()
            if remaining <= 0:
                break
            chunk = min(GEMINI_POLL_INTERVAL, remaining)
            done, _ = await asyncio.wait({task}, timeout=chunk)
            if done:
                try:
                    return task.result()
                except Exception as exc:
                    raise _map_api_error(exc)
            try:
                recovered = await _recover_from_chat(
                    client, chat, require_image=require_image
                )
            except GeminiBlockedError:
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
                raise
            if recovered:
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
                return recovered
        if task.done():
            try:
                return task.result()
            except Exception as exc:
                raise _map_api_error(exc)
        try:
            recovered = await _recover_from_chat(
                client, chat, require_image=require_image
            )
        except GeminiBlockedError:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
            raise
        if recovered:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
            return recovered
        # Stream may still be finishing — brief grace, then surface API 1097 etc.
        grace = min(GEMINI_POST_DEADLINE_GRACE, 15.0)
        done, _ = await asyncio.wait({task}, timeout=grace)
        if done:
            try:
                return task.result()
            except Exception as exc:
                raise _map_api_error(exc)
        mapped = await _drain_task_error(task)
        if mapped:
            task.cancel()
            raise mapped
        raise asyncio.TimeoutError()
    except Exception:
        if not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        raise


_gemini_refusal_reason: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "gemini_refusal_reason", default=None
)


def _capture_gemini_refusal_log(message) -> None:
    """gemini_webapi (loguru) logs refusals but often omits them from ChatHistory."""
    text = message.record["message"]
    if "interrupted/stopped" in text and "Reason:" in text:
        _gemini_refusal_reason.set(text.split("Reason:", 1)[-1].strip())


_IMAGE_REFUSAL_MARKERS = (
    "a lot of images",
    "can't create that",
    "cannot create that",
    "ask again later",
    "usage limit",
)


def _image_refusal_message(history) -> str | None:
    if not history or not history.turns:
        return None
    for turn in history.turns:
        if turn.role != "model":
            continue
        out = turn.model_output
        text = (getattr(out, "text", None) or "").strip()
        if not text:
            continue
        lower = text.lower()
        if any(m in lower for m in _IMAGE_REFUSAL_MARKERS):
            return text[:300]
    return None


async def _recover_from_chat(
    client: GeminiClient,
    chat: ChatSession,
    *,
    require_image: bool = False,
) -> object | None:
    """If the stream died but Gemini saved the turn, poll chat history."""
    cid = _chat_cid(chat)
    if not cid:
        last = getattr(chat, "last_output", None)
        if last:
            text = (getattr(last, "text", None) or "").strip()
            if last.images or (not require_image and len(text) >= 20):
                return last
        return None
    kind = "image" if require_image else "response"
    logger.info(f"Polling Gemini chat {cid} for completed {kind}...")
    from gemini_webapi.utils import logger as gemini_loguru

    _gemini_refusal_reason.set(None)
    sink_id = gemini_loguru.add(
        _capture_gemini_refusal_log,
        level="WARNING",
        filter=lambda r: "read_chat" in r["message"],
    )
    try:
        history = await asyncio.wait_for(
            client.read_chat(cid),
            timeout=GEMINI_RECOVERY_TIMEOUT,
        )
    except UsageLimitExceeded:
        raise
    except Exception as e:
        logger.warning(f"Chat recovery failed: {e}")
        return None
    finally:
        gemini_loguru.remove(sink_id)

    logged_refusal = _gemini_refusal_reason.get()
    if require_image and logged_refusal and any(
        m in logged_refusal.lower() for m in _IMAGE_REFUSAL_MARKERS
    ):
        raise GeminiBlockedError(f"Gemini image quota/refusal: {logged_refusal[:300]}")

    if require_image:
        refusal = _image_refusal_message(history)
        if refusal:
            raise GeminiBlockedError(f"Gemini image quota/refusal: {refusal}")
    if not history or not history.turns:
        return None
    for turn in history.turns:
        if turn.role != "model":
            continue
        out = turn.model_output
        if not out:
            continue
        text = (out.text or "").strip()
        if out.images:
            logger.info("Recovered image from chat history")
            return out
        if not require_image and len(text) >= 20:
            logger.info(f"Recovered text from chat history ({len(text)} chars)")
            return out
    return None


async def _send(
    client: GeminiClient,
    prompt: str,
    files: list[str],
    *,
    chat: ChatSession | None = None,
    model: Model | str = Model.UNSPECIFIED,
    temporary: bool = False,
    cleanup_chat: bool = True,
    step_timeout: float | None = None,
    recovery_requires_image: bool = False,
) -> object:
    """Wrap generate_content with error mapping → GeminiBlockedError where applicable.

    Pass `chat` for image-edit (enables CID recovery). Image edits must use
    temporary=False — temporary chats trigger stricter real-person safety blocks.
    """
    deadline = step_timeout or GEMINI_REQUEST_TIMEOUT
    active_chat = chat

    try:
        if active_chat:
            # Model is set on the ChatSession; do not pass model= again.
            coro = active_chat.send_message(
                prompt,
                files=files,
                temporary=temporary,
            )
        else:
            coro = client.generate_content(
                prompt,
                files=files,
                temporary=temporary,
                model=model,
            )
        if active_chat:
            resp = await _await_with_chat_recovery(
                client,
                active_chat,
                coro,
                deadline,
                require_image=recovery_requires_image,
            )
        else:
            resp = await asyncio.wait_for(coro, timeout=deadline)
    except asyncio.TimeoutError:
        if active_chat:
            recovered = await _recover_from_chat(
                client, active_chat, require_image=recovery_requires_image
            )
            if recovered:
                return recovered
        raise TimeoutError(
            f"Gemini did not respond within {int(deadline)}s"
        )
    except AuthError as e:
        raise GeminiBlockedError(f"Cookies expired / unauthenticated: {e}") from e
    except TemporarilyBlocked as e:
        raise GeminiBlockedError(f"IP temporarily blocked (429): {e}") from e
    except UsageLimitExceeded as e:
        raise GeminiBlockedError(f"Gemini usage limit exceeded: {e}") from e
    except GeminiBlockedError:
        raise
    except GeminiTimeoutError as e:
        raise TimeoutError(str(e)) from e
    except APIError as e:
        err = str(e)
        if "1100" in err or "unauthenticated" in err.lower():
            raise GeminiBlockedError(f"Gemini API auth/service error: {e}") from e
        if "1097" in err or "429" in err:
            raise GeminiBlockedError(
                f"Gemini web rate-limited/overloaded (not image quota): {e}"
            ) from e
        if "silently aborted" in err.lower():
            if active_chat:
                recovered = await _recover_from_chat(
                    client, active_chat, require_image=recovery_requires_image
                )
                if recovered:
                    return recovered
            raise TimeoutError(
                "Gemini image stream ended without a result. Will retry."
            ) from e
        raise

    if cleanup_chat:
        cid = None
        if resp.metadata:
            cid = resp.metadata[0]
        if not cid and active_chat:
            cid = getattr(active_chat, "cid", None) or None
        if cid:
            try:
                await client.delete_chat(cid)
            except Exception as e:
                logger.debug(f"Could not delete chat {cid}: {e}")
    return resp


async def _describe_via_headless_browser(upload_path: str) -> str | None:
    """Describe style image via hidden Gemini browser session (Playwright)."""
    if not GEMINI_BROWSER_DESCRIBE_ENABLED:
        return None

    logger.info("Step 1: Describing via headless browser session...")
    cookies: list[dict] = []

    # Primary path: reuse explicit Gemini session cookies (already used by gemini_webapi).
    # This avoids OS keyring decryption issues from browser_cookie3 on some Windows setups.
    override = _load_cookies_file()
    if override:
        psid, psidts = override
        cookies.extend(
            [
                {
                    "name": "__Secure-1PSID",
                    "value": psid,
                    "domain": ".google.com",
                    "path": "/",
                    "expires": -1,
                    "secure": True,
                    "httpOnly": True,
                    "sameSite": "None",
                },
                {
                    "name": "__Secure-1PSIDTS",
                    "value": psidts,
                    "domain": ".google.com",
                    "path": "/",
                    "expires": -1,
                    "secure": True,
                    "httpOnly": True,
                    "sameSite": "None",
                },
            ]
        )

    # Secondary path: load full Google cookie jar from Chrome profile when available.
    try:
        import browser_cookie3 as bc3
    except Exception:
        bc3 = None  # type: ignore[assignment]

    if bc3 is not None:
        try:
            jar = bc3.chrome(domain_name=".google.com")
            for c in jar:
                if c.is_expired():
                    continue
                cookies.append(
                    {
                        "name": c.name,
                        "value": c.value,
                        "domain": c.domain or ".google.com",
                        "path": c.path or "/",
                        "expires": float(c.expires) if c.expires else -1,
                        "secure": bool(c.secure),
                        "httpOnly": False,
                        "sameSite": "None",
                    }
                )
        except Exception as e:
            logger.warning(f"Could not read Chrome cookie jar for browser describe: {e}")

    if not cookies:
        logger.warning("No cookies available for headless Gemini session")
        return None

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
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
            await context.add_cookies(cookies)
            page = await context.new_page()
            await page.goto("https://gemini.google.com/app", wait_until="domcontentloaded")

            file_input = page.locator("input[type='file']").first
            await file_input.set_input_files(upload_path)
            await page.wait_for_timeout(700)

            editor = page.locator("div[contenteditable='true']").first
            await editor.click()
            await editor.fill(DESCRIBE_PROMPT)
            await page.keyboard.press("Enter")

            output = page.locator("main")
            await output.wait_for(timeout=int(GEMINI_BROWSER_DESCRIBE_TIMEOUT * 1000))
            await page.wait_for_timeout(3500)
            text = (await output.inner_text()).strip()
            await browser.close()
    except PlaywrightTimeoutError:
        return None
    except Exception as e:
        logger.warning(f"Headless browser describe failed: {e}")
        return None

    if len(text) < 20:
        return None
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    desc = max(lines, key=len) if lines else text
    if len(desc) < 20:
        return None
    logger.info(f"Outfit description via browser ({len(desc)} chars): {desc[:120]}...")
    return desc


async def _describe_reference(client: GeminiClient, style_path: str) -> str:
    upload_path = _prepare_style_upload(style_path)

    browser_desc = await _describe_via_headless_browser(upload_path)
    if browser_desc:
        return browser_desc

    logger.info(
        f"Step 1 fallback: gemini_webapi session (timeout {int(GEMINI_DESCRIBE_STEP_TIMEOUT)}s)..."
    )
    last_err: Exception | None = None
    for attempt in range(1, 3):
        chat = client.start_chat(model=DESCRIBE_MODEL)
        try:
            resp = await _send(
                client,
                DESCRIBE_PROMPT,
                [upload_path],
                chat=chat,
                temporary=False,
                cleanup_chat=True,
                step_timeout=GEMINI_DESCRIBE_STEP_TIMEOUT,
            )
            desc = (resp.text or "").strip()
            if len(desc) >= 20:
                logger.info(f"Outfit description ({len(desc)} chars): {desc[:120]}...")
                return desc
            last_err = RuntimeError(f"Description too short ({len(desc)} chars)")
        except GeminiBlockedError:
            raise
        except (TimeoutError, APIError, GeminiError) as e:
            last_err = e
            logger.warning(f"Web describe attempt {attempt}/2 failed: {e}")
            await _invalidate_client()
            client = await _get_client()
    raise RuntimeError(
        f"Could not describe outfit (browser + web session): {last_err}"
    ) from last_err


async def _generate_image(
    client: GeminiClient,
    prompt: str,
    ref_path: str,
    suffix: str,
) -> str:
    """Generate one image, save to OUTPUT_DIR, return absolute path."""
    last_error: Exception | None = None

    for attempt in range(1, GEMINI_IMAGE_MAX_ATTEMPTS + 1):
        chat = client.start_chat(model=IMAGE_MODEL)
        try:
            resp = await _send(
                client,
                prompt,
                [ref_path],
                chat=chat,
                model=IMAGE_MODEL,
                temporary=False,
                cleanup_chat=True,
                step_timeout=GEMINI_IMAGE_STEP_TIMEOUT,
                recovery_requires_image=True,
            )
            if not resp.images:
                snippet = (resp.text or "")[:300]
                raise RuntimeError(
                    f"Gemini returned no image. Response text: {snippet!r}"
                )

            OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
            filename = f"outfit_{int(time.time())}{suffix}.png"
            saved = await resp.images[0].save(
                path=str(OUTPUT_DIR), filename=filename, verbose=False
            )
            image_path = saved if isinstance(saved, str) else str(OUTPUT_DIR / filename)
            if not Path(image_path).exists():
                raise RuntimeError(f"image.save() returned but file missing: {image_path}")
            size = Path(image_path).stat().st_size
            logger.info(f"Saved: {image_path} ({size} bytes)")
            return image_path
        except GeminiBlockedError:
            raise
        except (TimeoutError, APIError, GeminiError, RuntimeError) as e:
            last_error = e
            logger.warning(
                f"Image generation attempt {attempt}/{GEMINI_IMAGE_MAX_ATTEMPTS} failed: {e}"
            )
            try:
                recovered = await _recover_from_chat(
                    client, chat, require_image=True
                )
            except GeminiBlockedError:
                raise
            if recovered and recovered.images:
                resp = recovered
                OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
                filename = f"outfit_{int(time.time())}{suffix}.png"
                saved = await resp.images[0].save(
                    path=str(OUTPUT_DIR), filename=filename, verbose=False
                )
                image_path = saved if isinstance(saved, str) else str(OUTPUT_DIR / filename)
                if Path(image_path).exists():
                    logger.info(f"Saved (recovered): {image_path}")
                    return image_path
            await _invalidate_client()
            if attempt < GEMINI_IMAGE_MAX_ATTEMPTS:
                client = await _get_client()

    raise RuntimeError(
        f"Image generation failed after {GEMINI_IMAGE_MAX_ATTEMPTS} attempts"
    ) from last_error


# ──────────────────────────────────────────────
# Public API (async — call directly from event loop)
# ──────────────────────────────────────────────

async def generate_outfit_image() -> tuple[str, str, str, str | None]:
    """Pick references, describe and generate.
    Returns (image_path, gen_prompt, description, archived_reference_path)."""
    client = await _get_client()
    model_path = pick_model_photo()
    style_path = pick_next_style()

    description = await _describe_reference(client, style_path)
    description = _trim_description(description)
    gen_prompt = generate_from_description_prompt(description)
    logger.info(
        f"Step 2: Generating outfit image (≤{int(GEMINI_IMAGE_STEP_TIMEOUT)}s per attempt, "
        f"{GEMINI_IMAGE_MAX_ATTEMPTS} attempts)..."
    )
    image_path = await _generate_image(client, gen_prompt, model_path, "")
    archived_ref = archive_used_style(style_path)
    return image_path, gen_prompt, description, archived_ref


async def regenerate_with_description(description: str) -> tuple[str, str, None, None]:
    """Regenerate using a cached outfit description (skip describe step)."""
    client = await _get_client()
    model_path = pick_model_photo()
    gen_prompt = generate_from_description_prompt(_trim_description(description))
    image_path = await _generate_image(client, gen_prompt, model_path, "_v2")
    return image_path, gen_prompt, None, None


async def regenerate_pose(source_image_path: str) -> tuple[str, str, None, None]:
    """Pose regen: feed the existing image back to Gemini with the pose prompt."""
    client = await _get_client()
    image_path = await _generate_image(client, POSE_REGEN_PROMPT, source_image_path, "_pose")
    return image_path, POSE_REGEN_PROMPT, None, None
