@echo off
REM Launch JARVIS and keep a log of startup output in jarvis_run.log
cd /d "%~dp0"
set PYTHONUTF8=1
set PYTHONUNBUFFERED=1
python -u main.py > jarvis_run.log 2>&1
