@echo off
echo ============================================
echo   HackerMode Setup
echo ============================================
echo.

:: Check Python
python --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python not found. Install from https://python.org
    pause
    exit /b 1
)

:: Install dependencies
echo [1/2] Installing dependencies...
pip install PyQt6 pywin32 requests

echo.
echo [2/2] Launching HackerMode...
echo.
python HackerMode.py

pause
