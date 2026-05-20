@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0\.."

set "PY=%~dp0..\venv\Scripts\python.exe"
if not exist "%PY%" (
  echo [mapbuild] venv not found: %PY%
  echo Run test\setup_venv.bat first.
  pause
  endlocal & exit /b 1
)

echo [mapbuild] config\wecatalog_tag_category_map.txt -^> product_feed_kr\wecatalog_tag_category_map.json
"%PY%" -m product_feed_kr.wecatalog_tag_category_map_builder %*
set "EC=%ERRORLEVEL%"
if not "%EC%"=="0" (
  echo [mapbuild] FAILED exit=%EC%
  pause
  endlocal & exit /b %EC%
)
echo [mapbuild] OK
endlocal & exit /b 0
