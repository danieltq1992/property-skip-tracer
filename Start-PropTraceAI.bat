@echo off
title PropTrace AI - Property Owner & Phone Skip-Tracing Agent
cd /d "%~dp0"
echo Starting PropTrace AI GUI Server on http://127.0.0.1:8765 ...
start "" "http://127.0.0.1:8765"
".venv\Scripts\python.exe" app.py
pause
