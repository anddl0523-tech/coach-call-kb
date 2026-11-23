@echo off
chcp 65001 >nul
cd /d "%~dp0"
py -X utf8 coach_calls.py --check
pause
