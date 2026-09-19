@echo off
setlocal
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" deploy.py start %*
) else (
    python deploy.py start %*
)
exit /b %errorlevel%
