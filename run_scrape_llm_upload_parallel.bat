@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

set "PY=%~dp0venv\Scripts\python.exe"
if not exist "%PY%" (
  echo [parallel] venv not found: %PY%
  echo Run from repo root: python -m venv venv
  pause
  endlocal & exit /b 1
)

echo [parallel] 将打开三个独立窗口（并行运行）：
echo   1. 微猫抓取  scrape_wecatalog_store.bat
echo   2. LLM 处理  llm_enrich_sqlite.bat
echo   3. 真实上架  upload_seven17_real.bat
echo.
echo 三者共用 SQLite；若锁竞争请错开任务或先停其一。各窗口内 exit 75 仍会按配置自动重跑。
echo.

rem /d 设置子进程工作目录；首对引号内为窗口标题（不可省，否则首参被当成标题）
start "wecatalog-scrape" /d "%~dp0" cmd /k call "%~dp0scrape_wecatalog_store.bat"
start "listing-llm" /d "%~dp0" cmd /k call "%~dp0llm_enrich_sqlite.bat"
start "seven17-upload" /d "%~dp0" cmd /k call "%~dp0upload_seven17_real.bat"

echo 已启动。本窗口可关闭。
endlocal & exit /b 0
