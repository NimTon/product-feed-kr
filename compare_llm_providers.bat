@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

set "PY=%~dp0venv\Scripts\python.exe"
if not exist "%PY%" (
  echo [llm-compare] venv not found: %PY%
  pause
  exit /b 1
)

rem Default: dry-run only (no API). Add --run after config is ready.
echo [llm-compare] dry-run by default; pass --run to call APIs
"%PY%" -m product_feed_kr.llm_providers_compare %*

endlocal
