@echo off
rem UTF-8 console for Python logs; keep this file ASCII-only for CMD on Chinese Windows
chcp 65001 >nul
rem Delayed expansion: capture !ERRORLEVEL! before echo overwrites ERRORLEVEL
setlocal EnableDelayedExpansion
cd /d "%~dp0"

set "PY=%~dp0venv\Scripts\python.exe"
if not exist "%PY%" (
  echo [scrape] venv not found: %PY%
  echo Run from repo root: python -m venv venv
  echo Then: venv\Scripts\pip install -r requirements.txt
  pause
  endlocal & exit /b 1
)

if not defined STORE_URL (
  set "STORE_URL=https://www.wecatalog.cn/weshop/store/_ddYqfVQW6mlRb5szWI5ni6txeRsQ5rZ3_QFVHeg"
)
set "LOG_FILE=data\wecatalog_scrape_store.log"
rem SQLite path: PRODUCT_FEED_SQLITE in config (default data\product_feed.db)
rem Do not pass --detail-delay here: module reads WECATALOG_DETAIL_DELAY from env or seven17.json (e.g. 3,8 for random range). Override: append args e.g. ... --detail-delay 3,8

echo [scrape] PY=%PY%
echo [scrape] STORE_URL=%STORE_URL%
echo [scrape] LOG_FILE=%LOG_FILE%
echo [scrape] loops after every run ^(any exit code^); close window or Ctrl+C to stop
echo.

:scrape_loop
"%PY%" -m product_feed_kr.wecatalog_scrape_store ^
  --store-url "%STORE_URL%" ^
  --log-file "%LOG_FILE%" ^
  --checkpoint-every 1 %*

set "EC=!ERRORLEVEL!"
if "!EC!"=="11" (
  echo [scrape] STOP: another scrape instance is running ^(lock held^). Close the other window.
  pause
  endlocal & exit /b 11
)
echo.
echo [scrape] last exit=!EC!, starting next round... ^(log: %LOG_FILE%^)
echo.
goto scrape_loop
