@echo off
cd /d "%~dp0"
echo Installing HackerMode dependencies...
pip install PyQt6 pywin32 requests psutil pydivert
pause