@echo off
rem UTF-8 console for Python logs (file is ASCII-only so cmd parses correctly)
chcp 65001 >nul
rem 须延迟展开：set EC=%ERRORLEVEL% 会把 ERRORLEVEL 冲成 0，exit 75 无法循环
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

echo [scrape] PY=%PY%
echo [scrape] STORE_URL=%STORE_URL%
echo [scrape] LOG_FILE=%LOG_FILE%
echo [scrape] exit 75 = restart per WECATALOG_SCRAPE_RESTART_AFTER_ITEMS in config
echo.

:scrape_loop
"%PY%" -m product_feed_kr.wecatalog_scrape_store ^
  --store-url "%STORE_URL%" ^
  --log-file "%LOG_FILE%" ^
  --detail-delay 5 ^
  --checkpoint-every 1 %*

set "EC=!ERRORLEVEL!"
if "!EC!"=="75" (
  echo [scrape] threshold reached ^(exit 75^), re-running...
  goto scrape_loop
)

echo.
if not "!EC!"=="0" (
  echo [scrape] FAILED exit=!EC!. See output and log: %LOG_FILE%
  echo Hint: pip install -r requirements.txt ^(filelock^) and check SQLite path in config.
) else (
  echo [scrape] OK exit=0.
)
echo Press any key to close...
pause >nul
endlocal & exit /b %EC%
