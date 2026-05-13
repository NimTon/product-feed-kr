@echo off
chcp 65001 >nul
setlocal EnableDelayedExpansion
cd /d "%~dp0"

set "PY=%~dp0venv\Scripts\python.exe"
if not exist "%PY%" (
  echo [upload-dry] venv not found: %PY%
  echo Run from repo root: python -m venv venv
  echo Then: venv\Scripts\pip install -r requirements.txt
  pause
  endlocal & exit /b 1
)

echo [upload-dry] PY=%PY%
echo [upload-dry] run seven17 dry-run, limit=1, keep-open
echo [upload-dry] exit 75 = restart per SEVEN17_UPLOAD_RESTART_AFTER_ITEMS
echo.

:upload_dry_loop
"%PY%" -m product_feed_kr.seven17_upload --limit 1 --dry-run --keep-open %*

set "EC=!ERRORLEVEL!"
if "!EC!"=="75" (
  echo [upload-dry] threshold reached ^(exit 75^), re-running...
  goto upload_dry_loop
)

echo.
if not "!EC!"=="0" (
  echo [upload-dry] FAILED exit=!EC!
) else (
  echo [upload-dry] OK exit=0
)
echo Press any key to close...
pause >nul
endlocal & exit /b %EC%
