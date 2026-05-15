@echo off
chcp 65001 >nul
rem Delayed expansion: capture !ERRORLEVEL! before echo overwrites ERRORLEVEL
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
echo [llm] loops after every run ^(any exit code^); close window or Ctrl+C to stop
echo.

:llm_loop
"%PY%" -m product_feed_kr.seven17_upload --llm-only --include-uploaded %*

set "EC=!ERRORLEVEL!"
echo.
echo [llm] last exit=!EC!, starting next round...
echo.
goto llm_loop
