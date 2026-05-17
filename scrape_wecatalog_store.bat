@echo off
cd /d "%~dp0"
venv\Scripts\python.exe -m product_feed_kr.wecatalog_scrape_store --store-url "https://www.wecatalog.cn/weshop/store/_ddYqfVQW6mlRb5szWI5ni6txeRsQ5rZ3_QFVHeg" --checkpoint-every 1 %*
