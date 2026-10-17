@echo off
title PropTrace AI — Standalone Property Owner & Personal Cell Skip-Tracer
cd /d "%~dp0"

echo ============================================================================
echo   PropTrace AI — Standalone Local Server (100%% Private to Your Machine)
echo ============================================================================
echo.

if not exist ".venv\Scripts\python.exe" (
    echo [First-Time Setup] Creating local Python virtual environment (.venv)...
    python -m venv .venv
    if errorlevel 1 (
        echo.
        echo [ERROR] Python 3.10+ was not found on your PATH.
        echo Please install Python from https://www.python.org/downloads/ and check "Add Python to PATH".
        pause
        exit /b 1
    )
    echo [First-Time Setup] Installing required packages from requirements.txt...
    ".venv\Scripts\python.exe" -m pip install --upgrade pip
    ".venv\Scripts\pip.exe" install -r requirements.txt
    echo.
    echo [First-Time Setup Complete!]
    echo.
)

echo Launching PropTrace AI in your browser at http://127.0.0.1:8765 ...
echo (On first launch, click "API Key & Settings" to paste your own free Gemini API Key)
echo.
start http://127.0.0.1:8765
".venv\Scripts\python.exe" app.py
pause
