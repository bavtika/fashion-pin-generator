@echo off
echo Installing dependencies...
pip install -r requirements.txt

echo Installing Playwright browsers...
playwright install chromium

echo Creating directories...
mkdir data\generated 2>nul
mkdir reference\model 2>nul
mkdir reference\styles 2>nul

echo.
if not exist .env (
    copy .env.example .env
    echo Created .env from template — fill in TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID!
) else (
    echo .env already exists
)

echo.
echo ============================================
echo Setup complete!
echo ============================================
echo.
echo Next steps:
echo 1. Fill in .env with your Telegram bot token and chat ID
echo 2. Put your face photo at: reference\model\model.jpg
echo 3. Run: python generators\save_gemini_session.py   (login to Gemini)
echo    Or:  save_gemini_session.bat
echo 4. Run: python pinterest\save_session.py   (login to Pinterest)
echo 5. Run: python main.py
echo    Or:  docker compose up -d --build
echo.
echo Bot logs: data\bot.log
echo.
pause
