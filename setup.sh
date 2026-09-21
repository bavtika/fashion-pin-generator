#!/usr/bin/env bash
# Cross-platform setup helper (Linux / macOS / Git Bash).
set -euo pipefail
cd "$(dirname "$0")"

python3 -m pip install -r requirements.txt
python3 -m playwright install chromium
mkdir -p data/generated reference/model reference/styles

if [[ ! -f .env ]]; then
  cp .env.example .env
  echo "Created .env — fill TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID"
fi

cat <<'EOF'
Setup complete.
Next:
  1. Edit .env
  2. Add reference/model/model.jpg
  3. python generators/save_gemini_session.py
  4. python pinterest/save_session.py   # optional
  5. python main.py
     or: docker compose up -d --build
EOF
