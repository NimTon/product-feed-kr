@echo off
cd /d "%~dp0"
venv\Scripts\python.exe -m product_feed_kr.seven17.seven17_upload --write-back %*
