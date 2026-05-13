@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

set "PY=%~dp0venv\Scripts\python.exe"
if not exist "%PY%" (
  echo [sp1] venv not found: %PY%
  echo Run from repo root: python -m venv venv
  echo Then: venv\Scripts\pip install -r requirements.txt
  pause
  endlocal & exit /b 1
)

set "STORE_URL=https://www.wecatalog.cn/weshop/store/_ddYqfVQW6mlRb5szWI5ni6txeRsQ5rZ3_QFVHeg"
set "LOG_FILE=data\wecatalog_scrape.log"
rem SQLite: PRODUCT_FEED_SQLITE in config\seven17.json (default data\product_feed.db)
rem exit 75 = auto re-run per WECATALOG_SCRAPE_RESTART_AFTER_ITEMS

:sp1_loop
"%PY%" -m product_feed_kr.wecatalog_scrape_store ^
  --store-url "%STORE_URL%" ^
  --log-file "%LOG_FILE%" ^
  --detail-delay 5 ^
  --checkpoint-every 1 %*

set "EC=%ERRORLEVEL%"
if "%EC%"=="75" (
  echo [sp1] threshold reached ^(exit 75^), re-running...
  goto sp1_loop
)

echo.
if not "%EC%"=="0" (
  echo [sp1] FAILED exit=%EC%. See output and log: %LOG_FILE%
  echo Hint: pip install -r requirements.txt ^(filelock^) and SQLite path in config\seven17.json
) else (
  echo [sp1] OK exit=0.
)
echo Press any key to close...
pause >nul
endlocal & exit /b %EC%
