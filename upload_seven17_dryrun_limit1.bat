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
echo [upload-dry] loops after every run ^(any exit code^); close window or Ctrl+C to stop
echo.

:upload_dry_loop
"%PY%" -m product_feed_kr.seven17_upload --limit 1 --dry-run --keep-open %*

set "EC=!ERRORLEVEL!"
echo.
echo [upload-dry] last exit=!EC!, starting next run...
echo.
goto upload_dry_loop
