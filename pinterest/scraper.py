"""
Scrape trending outfit images from Pinterest search.
Uses saved Pinterest session or Dolphin Anty profile (set DOLPHIN_PROFILE_ID in .env).
Downloads high-res images to reference/styles/.
"""
import asyncio
import hashlib
import json
import logging
import os
import random
import time
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from dotenv import load_dotenv
from playwright.async_api import async_playwright

load_dotenv()

logger = logging.getLogger(__name__)

DOLPHIN_PROFILE_ID = os.getenv("DOLPHIN_PROFILE_ID", "").strip()
DOLPHIN_API = os.getenv("DOLPHIN_API", "http://localhost:3001/v1.0").strip()

# Only one Playwright/Dolphin session at a time (avoids 500 on parallel start).
_dolphin_lock = asyncio.Lock()


@asynccontextmanager
async def _dolphin_session():
    if DOLPHIN_PROFILE_ID:
        async with _dolphin_lock:
            yield
    else:
        yield

SESSION_FILE = Path("data/pinterest_session.json")
STYLES_DIR = Path("reference/styles")
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}

TRENDS_CACHE = Path("data/trends_cache.json")
TRENDS_CACHE_TTL = 24 * 3600  # refresh trends once per day
TRENDS_URL = "https://trends.pinterest.com/?country=US&age=18_24&gender=female"
TRENDS_MAX_KEYWORDS = 40

# Clothing-trend filter — Pinterest trends mixes recipes, decor, nails, makeup, hair etc.
# Keep keyword only if it (a) lacks any blocked term AND (b) contains at least one clothing
# or fashion-aesthetic term. Substring match, lowercased.
_CLOTHING_TERMS = (
    # Direct apparel
    "outfit", "ootd", "fit check", "lookbook", "fashion", "style", "wear",
    "dress", "jean", "skirt", "pant", "trouser", "blouse", "shirt", "tee",
    "jacket", "coat", "blazer", "sweater", "hoodie", "cardigan", "vest",
    "shoe", "boot", "sneaker", "heel", "sandal", "loafer",
    "bag", "purse", "tote", "handbag", "accessor", "ensemble",
    "swimsuit", "bikini", "swimwear", "lingerie", "bodysuit",
    # Aesthetic / "core" / "academia" suffix patterns common to fashion
    "core", "aesthetic", "academia",
    # Named fashion aesthetics
    "y2k", "coquette", "balletcore", "blokette", "downtown girl", "downtown nyc",
    "soft girl", "clean girl", "mob wife", "old money", "alt girl", "altgirl",
    "e-girl", "egirl", "indie sleaze", "gorpcore", "office siren",
    "model off duty", "sorority", "preppy", "streetwear", "street style",
    "tomboy", "grunge", "cottagecore", "fairycore", "americana", "western",
    "cowboy", "biker", "punk", "minimalist", "scandi", "y3k", "balletic",
)

_NON_CLOTHING_TERMS = (
    # Food / drinks
    "recipe", "food", "dinner", "cake", "cookie", "drink", "cocktail",
    "smoothie", "snack", "dessert", "meal", "breakfast", "lunch", "soup",
    "pasta", "salad", "pizza", "coffee", "latte",
    # Home / decor
    "decor", "bedroom", "kitchen", "living room", "garden", "plant",
    "candle", "wallpaper", "interior", "apartment tour", "room tour",
    # Beauty (not clothing)
    "nail", "manicure", "pedicure",
    "makeup", "lipstick", "mascara", "blush", "eyeshadow", "eyeliner",
    "foundation", "lashes", " brow ", "skincare", "perfume", "fragrance",
    # Hair has its own bucket — keep out of trends
    "hair", "hairstyle", "haircut", "highlights", "balayage", "blowout",
    "ponytail", "bun ",
    # Body mods
    "tattoo", "piercing",
    # Art / crafts
    "drawing", "painting", "sketch", "doodle", "knitting", "crochet",
    # Travel / lifestyle
    "vacation", "travel ", "road trip",
    # Kids
    "baby", "nursery", "toddler",
    # Wallpaper / phone bg
    "phone wallpaper", "lock screen", "background",
)


def _is_clothing_trend(keyword: str) -> bool:
    """Return True if keyword looks like a clothing/fashion trend (not food/decor/beauty/hair)."""
    kw = keyword.lower()
    if any(b in kw for b in _NON_CLOTHING_TERMS):
        return False
    return any(c in kw for c in _CLOTHING_TERMS)

# Force US-locale signals on every Pinterest page open. Without a US IP this isn't
# bulletproof (Pinterest still geolocates), but combined with a US-region account in
# the saved session it strongly biases search/trends toward US results.
# Dolphin profiles ignore these — the profile itself must be configured with US proxy + locale.
US_LOCALE = "en-US"
US_TIMEZONE = "America/New_York"
US_HEADERS = {"Accept-Language": "en-US,en;q=0.9"}


def _us_context_kwargs() -> dict:
    """Standard new_context() kwargs that pin the browser to US-locale signals."""
    return {
        "storage_state": str(SESSION_FILE),
        "viewport": {"width": 1366, "height": 900},
        "user_agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "locale": US_LOCALE,
        "timezone_id": US_TIMEZONE,
        "extra_http_headers": dict(US_HEADERS),
    }


_DOLPHIN_LOCALE_WARNED = False


def _warn_dolphin_locale_once() -> None:
    """Dolphin profile config can't be overridden from Playwright — warn the user once."""
    global _DOLPHIN_LOCALE_WARNED
    if _DOLPHIN_LOCALE_WARNED:
        return
    _DOLPHIN_LOCALE_WARNED = True
    logger.info(
        "Dolphin profile in use — for US-only results set the profile's proxy to a US IP, "
        "language to en-US, and timezone to America/New_York."
    )

# Targeted at 18-24 audience: gen-z american girl core + a bit of alt + 2026 trends.
# Clothing only — no hair, no makeup, no nails. Trends bucket is filled live from
# trends.pinterest.com (filtered to clothing); the static lists below are fallback only.
QUERY_BUCKETS: dict[str, list[str]] = {
    "american_girl": [
        "clean girl aesthetic outfit",
        "coquette outfit aesthetic",
        "balletcore outfit inspo",
        "old money college outfit",
        "model off duty outfit gen z",
        "downtown girl outfit nyc",
        "soft girl outfit aesthetic",
        "blokette core outfit",
        "sporty chic outfit gen z",
        "quiet luxury outfit college",
        "sorority girl outfit inspo",
        "tiktok girl outfit 2026",
    ],
    "alt": [
        "alt girl outfit aesthetic",
        "grunge outfit inspo gen z",
        "indie sleaze outfit",
        "e-girl outfit aesthetic",
        "y2k cyber outfit",
        "dark academia outfit gen z",
    ],
    "trends": [
        "pinterest fashion trend 2026",
        "viral outfit tiktok 2026",
        "gen z outfit trend 2026",
        "gorpcore outfit aesthetic",
        "mob wife aesthetic outfit",
        "office siren outfit gen z",
    ],
}

# Per-refresh sampling weights — live Pinterest trends drive the mix; the rest are seasoning.
BUCKET_WEIGHTS: dict[str, float] = {
    "trends": 0.60,
    "american_girl": 0.30,
    "alt": 0.10,
}

MIN_STYLES = 20
MAX_STYLES = 50

# When the style pool is already full, skip Playwright scrapes for this long.
STYLES_SCRAPE_COOLDOWN = 3600
_last_scrape_attempt: float = 0.0


def count_styles() -> int:
    """Public wrapper — number of images in reference/styles/."""
    return _existing_count()


def _existing_count() -> int:
    if not STYLES_DIR.exists():
        return 0
    return len([p for p in STYLES_DIR.iterdir() if p.suffix.lower() in IMAGE_EXTENSIONS])


# Words that already signal "photo of a person" — if present, don't append the bias tail.
_PERSON_INDICATORS = (
    "girl", "model", "ootd", "fit check", "wearing", "on woman", "on her",
    "selfie", "mirror pic",
)
# Tail appended to neutral queries to push Pinterest toward person-shots, not flatlays.
_PERSON_BIAS_TAIL = "ootd full body"


def _bias_to_person(query: str) -> str:
    """Append a person-photo bias tail to queries that don't already imply a person."""
    q = query.lower()
    if any(ind in q for ind in _PERSON_INDICATORS):
        return query
    return f"{query} {_PERSON_BIAS_TAIL}"


def _sample_balanced_queries(needed: int, trends_override: list[str] | None = None) -> list[str]:
    """Pick queries across buckets per BUCKET_WEIGHTS so alt/trends never get skipped.

    Each bucket contributes ceil(weight * pool_size) unique queries; pool sized to
    cover ~needed/2 downloads (each query yields ~2 keepers after filters).
    `trends_override` replaces the static trends bucket with live trending keywords.
    Every selected query gets a person-photo bias tail unless it already implies one.
    """
    pool_size = max(4, needed // 2 + 2)
    picked: list[str] = []
    for bucket, weight in BUCKET_WEIGHTS.items():
        per_bucket = max(1, round(weight * pool_size))
        if bucket == "trends" and trends_override:
            choices = trends_override
        else:
            choices = QUERY_BUCKETS.get(bucket, [])
        if not choices:
            continue
        picked.extend(random.sample(choices, min(per_bucket, len(choices))))
    random.shuffle(picked)
    return [_bias_to_person(q) for q in picked]


def _load_trends_cache() -> list[str] | None:
    if not TRENDS_CACHE.exists():
        return None
    try:
        data = json.loads(TRENDS_CACHE.read_text(encoding="utf-8"))
        if time.time() - float(data.get("ts", 0)) > TRENDS_CACHE_TTL:
            return None
        kws = data.get("keywords") or []
        return list(kws) if kws else None
    except Exception as e:
        logger.debug(f"Trends cache read failed: {e}")
        return None


def _save_trends_cache(keywords: list[str]) -> None:
    TRENDS_CACHE.parent.mkdir(parents=True, exist_ok=True)
    TRENDS_CACHE.write_text(
        json.dumps({"ts": time.time(), "keywords": keywords}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


async def _fetch_pinterest_trends() -> list[str]:
    """Pull live trending keywords from Pinterest's trends tab, filtered to 18-24 women.

    Uses saved Pinterest session (or Dolphin profile). Returns [] on any failure;
    caller falls back to static QUERY_BUCKETS['trends'].
    """
    keywords: list[str] = []

    async with _dolphin_session():
        async with async_playwright() as p:
            if DOLPHIN_PROFILE_ID:
                logger.info(f"Trends: using Dolphin profile {DOLPHIN_PROFILE_ID}")
                _warn_dolphin_locale_once()
                port = await _dolphin_start(DOLPHIN_PROFILE_ID)
                browser = await p.chromium.connect_over_cdp(f"http://localhost:{port}")
                context = browser.contexts[0] if browser.contexts else await browser.new_context()
            else:
                if not SESSION_FILE.exists():
                    logger.warning("Trends: no Pinterest session — skipping live fetch")
                    return []
                browser = await p.chromium.launch(
                    headless=True,
                    args=["--disable-blink-features=AutomationControlled"],
                )
                context = await browser.new_context(**_us_context_kwargs())
                await context.add_init_script(
                    "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
                )

            page = await context.new_page()
            try:
                await page.goto(TRENDS_URL, wait_until="domcontentloaded")
                await asyncio.sleep(random.uniform(3, 5))

                for _ in range(3):
                    await page.evaluate("window.scrollBy(0, window.innerHeight)")
                    await asyncio.sleep(random.uniform(1.0, 1.8))

                raw = await page.evaluate("""() => {
                const out = new Set();
                // Trend cards link to /search/pins/?q=<keyword> or /trends/<id>
                document.querySelectorAll('a[href*="/search/pins/"], a[href*="/trends/"]').forEach(a => {
                    const txt = (a.textContent || '').trim();
                    if (txt && txt.length >= 3 && txt.length <= 80 && !txt.includes('\\n')) {
                        out.add(txt);
                    }
                });
                // Card title nodes — defensive selectors in case Pinterest tweaks DTIs
                document.querySelectorAll(
                    '[data-test-id*="rend"] h3, [data-test-id*="rend"] h2, [data-test-id*="rend"] p, [data-test-id*="eyword"] h3'
                ).forEach(el => {
                    const txt = (el.textContent || '').trim();
                    if (txt && txt.length >= 3 && txt.length <= 80) out.add(txt);
                });
                return Array.from(out);
            }""")

                blocked = {
                    "see all", "trending", "top", "growing", "monthly", "weekly",
                    "filter", "category", "all categories", "explore", "trends",
                    "next", "prev", "load more", "popular", "view all",
                }
                seen: set[str] = set()
                dropped_non_clothing = 0
                for kw in raw:
                    norm = kw.strip().lower()
                    if not norm or norm in blocked:
                        continue
                    if norm.startswith(("#", "@")) or norm.endswith((":",)):
                        continue
                    if norm in seen:
                        continue
                    seen.add(norm)
                    if not _is_clothing_trend(norm):
                        dropped_non_clothing += 1
                        continue
                    keywords.append(kw.strip())

                keywords = keywords[:TRENDS_MAX_KEYWORDS]
                logger.info(
                    f"Trends: pulled {len(keywords)} clothing keywords "
                    f"({dropped_non_clothing} non-clothing dropped)"
                )

            except Exception as e:
                logger.warning(f"Trends fetch failed: {e}")
            finally:
                await page.close()
                if DOLPHIN_PROFILE_ID:
                    await _dolphin_stop(DOLPHIN_PROFILE_ID)
                else:
                    await browser.close()

    return keywords


async def _get_trends_bucket() -> list[str]:
    """Live trends with cache + static fallback."""
    cached = _load_trends_cache()
    if cached:
        logger.info(f"Trends: using cached list ({len(cached)} keywords)")
        return cached
    fresh = await _fetch_pinterest_trends()
    if fresh:
        _save_trends_cache(fresh)
        return fresh
    fallback = QUERY_BUCKETS.get("trends", [])
    logger.warning(f"Trends: live fetch empty — falling back to static ({len(fallback)})")
    return fallback


async def _dolphin_start(profile_id: str) -> int:
    """Start Dolphin Anty profile, return CDP port.

    Retries on 5xx because Dolphin briefly returns 500 if a previous stop hasn't
    fully released the profile yet (happens when we run trends fetch then image
    scrape back-to-back).
    """
    delays = [2.0, 4.0, 6.0]
    last_err: Exception | None = None
    async with httpx.AsyncClient(timeout=30) as client:
        for attempt in range(len(delays) + 1):
            try:
                resp = await client.get(
                    f"{DOLPHIN_API}/browser_profiles/{profile_id}/start?automation=1"
                )
                resp.raise_for_status()
                return resp.json()["automation"]["port"]
            except httpx.HTTPStatusError as e:
                last_err = e
                if e.response.status_code < 500 or attempt >= len(delays):
                    raise
                wait = delays[attempt]
                logger.warning(
                    f"Dolphin start returned {e.response.status_code}, retrying in {wait}s "
                    f"(attempt {attempt + 1}/{len(delays)})"
                )
                await asyncio.sleep(wait)
    # Should be unreachable — last iteration either returns or raises
    raise last_err or RuntimeError("Dolphin start failed without error")


async def _dolphin_stop(profile_id: str) -> None:
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            await client.get(f"{DOLPHIN_API}/browser_profiles/{profile_id}/stop")
        # Give Dolphin time to fully release the profile before next start.
        await asyncio.sleep(1.5)
    except Exception as e:
        logger.warning(f"Dolphin stop failed: {e}")


async def _scrape_pinterest_images(query: str, count: int = 10) -> list[str]:
    """Search Pinterest for a query and return image URLs from pin results."""
    urls = []

    async with _dolphin_session():
        async with async_playwright() as p:
            if DOLPHIN_PROFILE_ID:
                logger.info(f"Using Dolphin Anty profile: {DOLPHIN_PROFILE_ID}")
                _warn_dolphin_locale_once()
                port = await _dolphin_start(DOLPHIN_PROFILE_ID)
                browser = await p.chromium.connect_over_cdp(f"http://localhost:{port}")
                context = browser.contexts[0] if browser.contexts else await browser.new_context()
            else:
                if not SESSION_FILE.exists():
                    logger.error("No Pinterest session — run pinterest/save_session.py first")
                    return []
                browser = await p.chromium.launch(
                    headless=True,
                    args=["--disable-blink-features=AutomationControlled"],
                )
                context = await browser.new_context(**_us_context_kwargs())
                await context.add_init_script(
                    "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
                )
            page = await context.new_page()

            try:
                search_url = (
                    f"https://www.pinterest.com/search/pins/"
                    f"?q={query.replace(' ', '%20')}&rs=typed&country=US"
                )
                await page.goto(search_url, wait_until="domcontentloaded")
                await asyncio.sleep(random.uniform(3, 5))

                if "login" in page.url:
                    logger.error("Pinterest session expired")
                    return []

                for _ in range(3):
                    await page.evaluate("window.scrollBy(0, window.innerHeight * 2)")
                    await asyncio.sleep(random.uniform(1.5, 2.5))

                img_urls = await page.evaluate(r"""() => {
                const urls = new Set();
                // Pinterest serves images in srcset with multiple resolutions
                document.querySelectorAll('img[srcset], img[src*="pinimg.com"]').forEach(img => {
                    // Get highest resolution from srcset
                    const srcset = img.getAttribute('srcset') || '';
                    const sources = srcset.split(',').map(s => s.trim().split(' '));
                    let best = '';
                    let bestW = 0;
                    for (const [url, w] of sources) {
                        const width = parseInt(w) || 0;
                        if (width > bestW && url.includes('pinimg.com')) {
                            bestW = width;
                            best = url;
                        }
                    }
                    if (best && bestW >= 400) {
                        urls.add(best);
                    } else {
                        // Fallback: upgrade src URL to 736px version
                        const src = img.src || '';
                        if (src.includes('pinimg.com') && img.naturalWidth >= 200) {
                            const upgraded = src.replace(/\/\d+x\//, '/736x/');
                            urls.add(upgraded);
                        }
                    }
                });
                return Array.from(urls);
            }""")

                urls = img_urls[:count]
                logger.info(f"Found {len(urls)} images for '{query}'")

            except Exception as e:
                logger.error(f"Scrape failed for '{query}': {e}")
            finally:
                await page.close()
                if DOLPHIN_PROFILE_ID:
                    await _dolphin_stop(DOLPHIN_PROFILE_ID)
                else:
                    await browser.close()

    return urls


# --- Outfit filter: exactly one person via mediapipe pose ---------------------
# A pose is detected only when actual body keypoints (head/shoulders/hips/knees) are
# visible — flatlays, product shots, sandal/feet close-ups all get 0 poses, groups
# get 2+, single outfit photos get 1. This replaced the old Haar/HOG cascade stack
# which couldn't tell jeans+jacket flatlays from a real person silhouette.

POSE_MODEL_PATH = Path("data/models/pose_landmarker_lite.task")
POSE_MODEL_URL = (
    "https://storage.googleapis.com/mediapipe-models/pose_landmarker/"
    "pose_landmarker_lite/float16/latest/pose_landmarker_lite.task"
)

_PERSON_DETECTOR_READY: bool | None = None
_POSE_LANDMARKER = None


def _person_detector_ready() -> bool:
    """One-time import probe; warns once if mediapipe is missing."""
    global _PERSON_DETECTOR_READY
    if _PERSON_DETECTOR_READY is None:
        try:
            import cv2  # noqa: F401
            import mediapipe  # noqa: F401
            import numpy  # noqa: F401
            _PERSON_DETECTOR_READY = True
        except ImportError as e:
            logger.warning(
                f"mediapipe/cv2/numpy not installed — outfit filter disabled. "
                f"Run: pip install -r requirements.txt. ({e})"
            )
            _PERSON_DETECTOR_READY = False
    return _PERSON_DETECTOR_READY


def _ensure_pose_model() -> None:
    """Download mediapipe pose model (~6MB) if missing."""
    if POSE_MODEL_PATH.exists():
        return
    POSE_MODEL_PATH.parent.mkdir(parents=True, exist_ok=True)
    logger.info(f"Downloading pose model from {POSE_MODEL_URL}")
    with httpx.Client(timeout=60, follow_redirects=True) as client:
        resp = client.get(POSE_MODEL_URL)
        resp.raise_for_status()
        POSE_MODEL_PATH.write_bytes(resp.content)
    logger.info(f"Pose model saved to {POSE_MODEL_PATH} ({len(resp.content)} bytes)")


def _get_pose_landmarker():
    global _POSE_LANDMARKER
    if _POSE_LANDMARKER is None:
        _ensure_pose_model()
        from mediapipe.tasks import python as mp_python
        from mediapipe.tasks.python import vision as mp_vision
        opts = mp_vision.PoseLandmarkerOptions(
            base_options=mp_python.BaseOptions(model_asset_path=str(POSE_MODEL_PATH)),
            running_mode=mp_vision.RunningMode.IMAGE,
            num_poses=3,  # detect up to 3 — anything >=2 is rejected as a group
            min_pose_detection_confidence=0.5,
        )
        _POSE_LANDMARKER = mp_vision.PoseLandmarker.create_from_options(opts)
    return _POSE_LANDMARKER


def _passes_outfit_filter(img_bytes: bytes) -> bool:
    """True if image shows exactly one person AND enough of their body to read an outfit.

    Two-stage gate via mediapipe pose:
      1) exactly one pose detected — kills flatlays, product shots, group photos
      2) shoulders + hips + (knees or ankles) visible — kills face/torso close-ups
         (mukbang, beach selfie cropped at waist, etc.) where no real outfit shows
    Fail-open on any detector error.
    """
    if not _person_detector_ready():
        return True
    try:
        import cv2
        import mediapipe as mp
        import numpy as np

        arr = np.frombuffer(img_bytes, dtype=np.uint8)
        img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if img is None:
            return True  # decode failed elsewhere — don't double-reject

        # Don't resize — mediapipe normalizes internally; our resize introduces
        # artifacts that produce false-positive pose detections in reflections.
        rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
        mp_image = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb)
        result = _get_pose_landmarker().detect(mp_image)
        n_poses = len(result.pose_landmarks) if result.pose_landmarks else 0
        if n_poses != 1:
            return False

        # BlazePose indices: 11/12 shoulders, 23/24 hips, 25/26 knees, 27/28 ankles
        lm = result.pose_landmarks[0]
        VIS = 0.5

        def vis(idx: int) -> float:
            try:
                return lm[idx].visibility
            except Exception:
                return 0.0

        shoulders_ok = vis(11) > VIS and vis(12) > VIS
        hips_ok = vis(23) > VIS and vis(24) > VIS
        legs_ok = (vis(25) > VIS and vis(26) > VIS) or (vis(27) > VIS and vis(28) > VIS)
        return shoulders_ok and hips_ok and legs_ok
    except Exception as e:
        logger.debug(f"Pose filter error (failing open): {e}")
        return True


async def _download_image(url: str, session_path: str = None) -> Path | None:
    """Download a single image URL to reference/styles/."""
    import httpx

    STYLES_DIR.mkdir(parents=True, exist_ok=True)

    url_hash = hashlib.md5(url.encode()).hexdigest()
    dest = STYLES_DIR / f"{url_hash}.jpg"

    if dest.exists():
        return None  # already have it

    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            resp = await client.get(url, headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/124.0.0.0",
                "Referer": "https://www.pinterest.com/",
            })
            if resp.status_code != 200:
                return None

            data = resp.content
            if len(data) < 10_000:  # too small, probably placeholder
                return None

            # Validate it's an actual image
            import io

            from PIL import Image
            img = Image.open(io.BytesIO(data))
            w, h = img.size

            # Filter: need portrait or square, decent resolution
            if w < 300 or h < 400:
                return None
            if h < w * 0.8:  # too landscape
                return None

            # Reject flatlays / product shots / group photos — must show one real person
            if not _passes_outfit_filter(data):
                logger.debug(f"Rejected (not single-person outfit): {url[:80]}")
                return None

            img.save(dest, "JPEG", quality=90)
            logger.info(f"Downloaded: {dest.name} ({w}x{h})")
            return dest

    except Exception as e:
        logger.debug(f"Download failed {url[:60]}: {e}")
        return None


async def refresh_styles(target_count: int = MIN_STYLES) -> int:
    """Scrape Pinterest for outfit images until we have enough styles.

    Returns number of new images downloaded.
    """
    global _last_scrape_attempt

    current = _existing_count()
    if current >= target_count:
        logger.info(f"Already have {current} style images (target: {target_count})")
        return 0

    now = time.time()
    if current >= MIN_STYLES and now - _last_scrape_attempt < STYLES_SCRAPE_COOLDOWN:
        logger.info(
            f"Style pool has {current} images (>= {MIN_STYLES}); "
            f"skipping scrape (cooldown {STYLES_SCRAPE_COOLDOWN}s)"
        )
        return 0

    _last_scrape_attempt = now
    needed = target_count - current
    logger.info(f"Need {needed} more style images (have {current}, target {target_count})")

    downloaded = 0
    trends = await _get_trends_bucket()
    queries = _sample_balanced_queries(needed, trends_override=trends)

    for query in queries:
        if downloaded >= needed:
            break

        # Person-detection filter rejects ~30-50% — over-fetch so we still hit `needed`.
        per_query = min((needed - downloaded) * 2 + 5, 25)
        urls = await _scrape_pinterest_images(query, count=per_query)

        for url in urls:
            if downloaded >= needed:
                break
            result = await _download_image(url)
            if result:
                downloaded += 1

        # Polite delay between searches
        await asyncio.sleep(random.uniform(2, 4))

    total = _existing_count()
    logger.info(f"Style refresh done: downloaded {downloaded} new images (total: {total})")
    return downloaded


def refresh_styles_sync(target_count: int = MIN_STYLES) -> int:
    return asyncio.run(refresh_styles(target_count))


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    asyncio.run(refresh_styles(MIN_STYLES))
