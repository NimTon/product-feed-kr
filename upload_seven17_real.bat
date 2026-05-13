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
echo.

"%PY%" -m product_feed_kr.seven17_upload --write-back %*

set "EC=%ERRORLEVEL%"
echo.
if not "%EC%"=="0" (
  echo [upload-real] FAILED exit=%EC%
) else (
  echo [upload-real] OK exit=0
)
echo Press any key to close...
pause >nul
endlocal & exit /b %EC%
