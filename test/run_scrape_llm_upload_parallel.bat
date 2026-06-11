@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0\.."

set "PY=%~dp0..\venv\Scripts\python.exe"
if not exist "%PY%" (
  echo [parallel] venv not found: %PY%
  echo Run from repo root: python -m venv venv
  pause
  endlocal & exit /b 1
)

echo [parallel] Starting three separate windows in parallel:
echo   1. 01_采集微猫店铺.bat
echo   2. 02_LLM补全上架信息.bat
echo   3. 03_上传韩国站正式.bat
echo.
echo [parallel] All share SQLite; stop one if you hit lock contention.
echo [parallel] Each window loops until you close it or press Ctrl+C.
echo.

rem First quoted arg to start is window title; /d sets child working directory
start "wecatalog-scrape" /d "%~dp0\.." cmd /k call "%~dp0..\01_采集微猫店铺.bat" --resume
start "listing-llm" /d "%~dp0\.." cmd /k call "%~dp0..\02_LLM补全上架信息.bat"
start "seven17-upload" /d "%~dp0\.." cmd /k call "%~dp0..\03_上传韩国站正式.bat"

echo [parallel] Launched. You can close this window.
endlocal & exit /b 0
