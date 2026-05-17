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
echo [upload-real] threads: SEVEN17_UPLOAD_THREADS in seven17.json ^(default 1^)
echo [upload-real] loops after every run ^(any exit code^); close window or Ctrl+C to stop
echo.

:upload_loop
"%PY%" -m product_feed_kr.seven17_upload --write-back %*

set "EC=!ERRORLEVEL!"
if "!EC!"=="11" (
  echo [upload-real] STOP: another upload instance is running. Close the other window.
  pause
  endlocal & exit /b 11
)
echo.
echo [upload-real] last exit=!EC!, starting next run...
echo.
goto upload_loop
