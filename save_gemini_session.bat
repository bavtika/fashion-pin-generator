@echo off
cd /d "%~dp0"
python generators\save_gemini_session.py %*
if errorlevel 1 pause
