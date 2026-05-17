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
echo [upload-real-limit1] loops after every run ^(any exit code^); close window or Ctrl+C to stop
echo.

:upload_l1_loop
"%PY%" -m product_feed_kr.seven17_upload --limit 1 %*

set "EC=!ERRORLEVEL!"
echo.
echo [upload-real-limit1] last exit=!EC!, starting next run...
echo.
goto upload_l1_loop
