@echo off
chcp 65001 >nul
setlocal
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
echo [upload-real] exit 75 = restart per SEVEN17_UPLOAD_RESTART_AFTER_ITEMS in config
echo.

:upload_loop
"%PY%" -m product_feed_kr.seven17_upload --write-back %*

set "EC=%ERRORLEVEL%"
if "%EC%"=="75" (
  echo [upload-real] threshold reached ^(exit 75^), re-running...
  goto upload_loop
)

echo.
if not "%EC%"=="0" (
  echo [upload-real] FAILED exit=%EC%
) else (
  echo [upload-real] OK exit=0
)
echo Press any key to close...
pause >nul
endlocal & exit /b %EC%
