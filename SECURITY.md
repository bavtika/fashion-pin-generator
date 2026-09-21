# Security Policy

## Reporting a vulnerability

If you find a security issue in this repository, please open a **private**
GitHub security advisory (or email the maintainer) instead of filing a public
issue. Do not include live tokens, cookies, or personal photos in reports.

## Secrets that must never be committed

| Secret | Where it lives locally |
|--------|------------------------|
| `TELEGRAM_BOT_TOKEN` / chat ID | `.env` |
| Gemini session cookies | `data/gemini_cookies.json`, `data/gemini_webapi_cache/` |
| Pinterest session | `data/pinterest_session.json` |
| Dolphin Anty profile / API token | `.env` |
| Model face photo | `reference/model/` |

Use `.env.example` as the template. Runtime dirs (`data/`, personal media) are
gitignored.

## Operational hygiene

1. **Rotate** Telegram bot tokens (BotFather → Revoke) if they ever appeared in
   logs, zip archives, screenshots, or chat history.
2. Logs redact `api.telegram.org/bot…` URLs; still treat `data/bot.log` as
   sensitive and keep it out of git.
3. Prefer Docker Compose volume mounts for `data/` and `reference/` so secrets
   never bake into images.
4. Live Gemini / Pinterest sessions expire — refresh with the provided save
   scripts; do not paste cookies into issues or CI secrets unless you intend to
   run optional live jobs.

## Scope / ethics

This project is a **human-in-the-loop lab**: images are generated for operator
review in Telegram. Scraping and unofficial Gemini web clients may violate
third-party Terms of Service — use only accounts and data you control, and do
not present this as production SaaS automation against ToS.
