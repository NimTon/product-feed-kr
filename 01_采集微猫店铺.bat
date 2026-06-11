@echo off
chcp 65001 >nul
cd /d "%~dp0"

set "LIST_FLAG="

if /i "%~1"=="--list-from-start" (
  set "LIST_FLAG=--list-from-start"
  shift
  goto run
)
if /i "%~1"=="--resume" (
  shift
  goto run
)

call :show_menu
set "LIST_MODE="
set /p "LIST_MODE=请选择 1 或 2 [1]: "
if "%LIST_MODE%"=="" set "LIST_MODE=1"
if "%LIST_MODE%"=="2" set "LIST_FLAG=--list-from-start"
goto run

:show_menu
echo.
echo [01] 微猫店铺采集 - 列表翻页模式
echo   1. 断点续抓 - 从 SQLite 翻页断点继续默认
echo   2. 从头开抓 - 忽略断点从第一页重新遍历
echo.
exit /b 0

:run
if defined LIST_FLAG goto mode_from_start
echo [01] 已选：断点续抓
goto mode_done
:mode_from_start
echo [01] 已选：从头开抓
:mode_done
echo.

venv\Scripts\python.exe -m product_feed_kr.wecatalog.wecatalog_scrape_store --store-url "https://www.wecatalog.cn/weshop/store/_ddYqfVQW6mlRb5szWI5ni6txeRsQ5rZ3_QFVHeg" --checkpoint-every 1 --skip-uncategorized %LIST_FLAG% %*
