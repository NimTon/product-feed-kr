@echo off
cd /d "%~dp0"

if not exist "venv\Scripts\pythonw.exe" if not exist "venv\Scripts\python.exe" (
  echo [04] venv not found. Run 00 first.
  pause
  exit /b 1
)

if exist "venv\Scripts\pythonw.exe" (
  start "" "venv\Scripts\pythonw.exe" -m product_feed_kr.seven17.seven17_no_price_whitelist_gui
  exit /b 0
)

"venv\Scripts\python.exe" -m product_feed_kr.seven17.seven17_no_price_whitelist_gui
if errorlevel 1 pause
exit /b %errorlevel%
