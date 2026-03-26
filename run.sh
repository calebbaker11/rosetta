#!/bin/bash
# Rosetta Stone Bot - Mac/Linux launcher
# Run with: bash run.sh

# Go to the folder this script is in
cd "$(dirname "$0")"

echo "============================================"
echo " Rosetta Stone Bot - Mac/Linux Setup"
echo "============================================"

# Find Python (try python3 first, then python)
PYTHON=""
if command -v python3 &>/dev/null; then
    PYTHON="python3"
elif command -v python &>/dev/null; then
    PYTHON="python"
else
    echo ""
    echo " ERROR: Python is not installed."
    echo " Mac:   brew install python   OR  download from python.org"
    echo " Linux: sudo apt install python3"
    echo ""
    exit 1
fi

echo " Using: $PYTHON"

# Install dependencies
$PYTHON -m pip install playwright pynput --quiet

# Install browser
echo " Setting up browser (first time only, may take a minute)..."
$PYTHON -m playwright install chromium

# Run the bot
echo " Starting bot..."
$PYTHON main.py
