@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

set "PY=%~dp0venv\Scripts\python.exe"
if not exist "%PY%" (
  echo [parallel] venv not found: %PY%
  echo Run from repo root: python -m venv venv
  pause
  endlocal & exit /b 1
)

echo [parallel] Starting three separate windows in parallel:
echo   1. scrape_wecatalog_store.bat
echo   2. llm_enrich_sqlite.bat
echo   3. upload_seven17_real.bat
echo.
echo [parallel] All share SQLite; stop one if you hit lock contention.
echo [parallel] Each window loops until you close it or press Ctrl+C.
echo.

rem First quoted arg to start is window title; /d sets child working directory
start "wecatalog-scrape" /d "%~dp0" cmd /k call "%~dp0scrape_wecatalog_store.bat"
start "listing-llm" /d "%~dp0" cmd /k call "%~dp0llm_enrich_sqlite.bat"
start "seven17-upload" /d "%~dp0" cmd /k call "%~dp0upload_seven17_real.bat"

echo [parallel] Launched. You can close this window.
endlocal & exit /b 0
