@echo off
chcp 65001 >nul
setlocal EnableDelayedExpansion
cd /d "%~dp0"

set "PY=%~dp0venv\Scripts\python.exe"
if not exist "%PY%" (
  echo [upload-real] venv not found: %PY%
  echo Run from repo root: python -m venv venv
  echo Then: venv\Scripts\pip install -r requirements.txt
  pause
  endlocal & exit /b 1
)

echo [upload-real] PY=%PY%
echo [upload-real] run real seven17 upload with write-back
echo [upload-real] loops after every run ^(any exit code^); close window or Ctrl+C to stop
echo.

:upload_loop
"%PY%" -m product_feed_kr.seven17_upload --write-back %*

set "EC=!ERRORLEVEL!"
echo.
echo [upload-real] last exit=!EC!, starting next round...
echo.
goto upload_loop
