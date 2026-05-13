@echo off
chcp 65001 >nul
rem 须延迟展开：set EC=%ERRORLEVEL% 会把 ERRORLEVEL 冲成 0，导致 exit 75 无法触发循环
setlocal EnableDelayedExpansion
cd /d "%~dp0"

set "PY=%~dp0venv\Scripts\python.exe"
if not exist "%PY%" (
  echo [llm] venv not found: %PY%
  echo Run from repo root: python -m venv venv
  echo Then: venv\Scripts\pip install -r requirements.txt
  pause
  endlocal & exit /b 1
)

echo [llm] PY=%PY%
echo [llm] run LLM enrich for SQLite records
echo [llm] exit 75 = restart per LISTING_LLM_RESTART_AFTER_ITEMS in config
echo.

:llm_loop
"%PY%" -m product_feed_kr.seven17_upload --llm-only --include-uploaded %*

set "EC=!ERRORLEVEL!"
if "!EC!"=="75" (
  echo [llm] threshold reached ^(exit 75^), re-running...
  goto llm_loop
)

echo.
if not "!EC!"=="0" (
  echo [llm] FAILED exit=!EC!
  echo Check OPENAI_API_KEY / OPENAI_BASE_URL / OPENAI_MODEL in config or env.
) else (
  echo [llm] OK exit=0
)
echo Press any key to close...
pause >nul
endlocal & exit /b %EC%
