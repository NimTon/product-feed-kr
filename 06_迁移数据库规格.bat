@echo off
chcp 65001 >nul
cd /d "%~dp0"
if exist "venv\Scripts\python.exe" (
  set PY=venv\Scripts\python.exe
) else (
  set PY=python
)

echo.
echo ========================================
echo   一键迁移 product_feed.db 规格列结构
echo ========================================
echo.
echo 迁移前会自动备份为 data\product_feed.db.bak-时间戳
echo 请先关闭 01采集 / 02LLM / 03上传 / 商品库浏览，避免数据库写锁。
echo.
pause

%PY% -m product_feed_kr.migrate_llm_spec_db %*
set EXIT_CODE=%ERRORLEVEL%

echo.
if %EXIT_CODE%==0 (
  echo 完成。可打开 05_查看商品库.bat 核对列数据。
) else (
  echo 迁移异常，退出码 %EXIT_CODE%
)
pause
exit /b %EXIT_CODE%
