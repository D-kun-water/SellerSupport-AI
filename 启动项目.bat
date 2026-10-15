@echo off
cd /d "%~dp0"
if exist "%~dp0.venv\Scripts\python.exe" (
    "%~dp0.venv\Scripts\python.exe" "%~dp0scripts\run.py"
) else (
    python "%~dp0scripts\run.py"
)
if errorlevel 1 pause
