@echo off
:: Rosetta Stone Bot - Windows launcher
:: Double-click this file to run the bot

cd /d "%~dp0"

echo ============================================
echo  Rosetta Stone Bot - Windows Setup
echo ============================================

:: Check if Python is installed
python --version >nul 2>&1
if errorlevel 1 (
    echo.
    echo  ERROR: Python is not installed or not on your PATH.
    echo.
    echo  Please install Python from: https://www.python.org/downloads/
    echo  Make sure to check "Add Python to PATH" during install.
    echo.
    pause
    exit /b 1
)

echo  Python found. Installing dependencies...
python -m pip install playwright pynput --quiet

echo  Setting up browser (first time only)...
python -m playwright install chromium

echo  Starting bot...
python main.py
pause
