@echo off
chcp 65001 >nul
setlocal EnableDelayedExpansion
cd /d "%~dp0"

set "PY=%~dp0venv\Scripts\python.exe"
if not exist "%PY%" (
  echo [upload-real-limit1] venv not found: %PY%
  echo Run from repo root: python -m venv venv
  echo Then: venv\Scripts\pip install -r requirements.txt
  pause
  endlocal & exit /b 1
)

echo [upload-real-limit1] PY=%PY%
echo [upload-real-limit1] run real seven17 upload, limit=1, no write-back
echo [upload-real-limit1] exit 75 = restart per SEVEN17_UPLOAD_RESTART_AFTER_ITEMS
echo.

:upload_l1_loop
"%PY%" -m product_feed_kr.seven17_upload --limit 1 %*

set "EC=!ERRORLEVEL!"
if "!EC!"=="75" (
  echo [upload-real-limit1] threshold reached ^(exit 75^), re-running...
  goto upload_l1_loop
)

echo.
if not "!EC!"=="0" (
  echo [upload-real-limit1] FAILED exit=!EC!
) else (
  echo [upload-real-limit1] OK exit=0
)
echo Press any key to close...
pause >nul
endlocal & exit /b %EC%
