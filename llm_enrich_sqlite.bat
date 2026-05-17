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
echo [llm] threads: OPENAI_API_KEY as JSON array in seven17.json ^(one key = one worker thread^)
echo [llm] only one bat window allowed ^(exit 11 if duplicate^)
echo [llm] loops after every run ^(any exit code^); close window or Ctrl+C to stop
echo.

:llm_loop
"%PY%" -m product_feed_kr.seven17_upload --llm-only --include-uploaded %*

set "EC=!ERRORLEVEL!"
if "!EC!"=="11" (
  echo [llm] STOP: another LLM enrich instance is running. Close the other window.
  pause
  endlocal & exit /b 11
)
echo.
echo [llm] last exit=!EC!, starting next run...
echo.
goto llm_loop
