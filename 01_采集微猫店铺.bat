@echo off
chcp 65001 >nul
cd /d "%~dp0"

set "TAG_FLAG="

if /i "%~1"=="--tags-from-start" (
  set "TAG_FLAG=--tags-from-start"
  shift
  goto run
)

call :show_menu
set "TAG_MODE="
set /p "TAG_MODE=请选择 1 或 2 [1]: "
if "%TAG_MODE%"=="" set "TAG_MODE=1"
if "%TAG_MODE%"=="2" set "TAG_FLAG=--tags-from-start"
goto run

:show_menu
echo.
echo [01] 微猫店铺采集 - 按已配对标签
echo   1. 断点续抓 - 从各标签翻页断点继续（默认）
echo   2. 从头开抓 - 忽略标签断点，从第一个已配对标签重跑
echo.
exit /b 0

:run
if defined TAG_FLAG goto mode_from_start
echo [01] 已选：断点续抓（按标签）
goto mode_done
:mode_from_start
echo [01] 已选：从头开抓（全部标签）
:mode_done
echo.

venv\Scripts\python.exe -m product_feed_kr.wecatalog.wecatalog_scrape_store --store-url "https://www.wecatalog.cn/weshop/store/_ddYqfVQW6mlRb5szWI5ni6txeRsQ5rZ3_QFVHeg" --checkpoint-every 1 %TAG_FLAG% %*
