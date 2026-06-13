# product-feed-kr（精简）

微猫 **wecatalog** 店铺采集 → **SQLite** → **seven17.kr** 后台上架。  
分类 `(分组, 标签)` → 韩文路径：在 **`05_查看商品库.bat`** 打开的 Web UI 中维护（`data/wecatalog_category_pairs.json` 等三个 JSON）。

## 环境

```bash
pip install -r requirements.txt
python -m playwright install chromium
```

可选：本地 Chromium → `PLAYWRIGHT_CHROMIUM_EXECUTABLE`，或项目下 `chrome-win/chrome.exe`（见 `playwright_path.py`）。

## 命令

```bash
# 爬取（默认入口等同下列模块）
python -m product_feed_kr
python -m product_feed_kr.wecatalog.wecatalog_scrape_store --store-url "..." --out data/wecatalog_store_products.json --existing-json data/wecatalog_store_products.json

# 商品库浏览（分类配对 / 无价白名单 / 不上架诊断）
python -m product_feed_kr.pf_browser
# 或双击 05_查看商品库.bat；无价白名单：04_无价格白名单设置.bat

# 上架（默认读取 data/wecatalog_store_products.json）
python -m product_feed_kr.seven17.seven17_upload --limit 5

# 爬一次后台「商品录入」页，导出 ca_id 下拉选项并写入 data/seven17_path_ca_map.json
python -m product_feed_kr.seven17.seven17_dump_itemform_categories --out data/seven17_ca_options.json
```

配置：复制 **`config/seven17.example.json`** → **`config/seven17.json`**（账号等）。环境变量优先于 JSON；可用 **`SEVEN17_CONFIG`** 指向其它配置文件路径。**勿将含密码的 `seven17.json` 提交到 Git。**

**seven17 商品分类 `ca_id`**：微猫 `(分组, 标签)` → 韩文路径（`data/wecatalog_category_pairs.json`，05 UI「分类配对」）→ `data/seven17_path_ca_map.json`（05 启动同步或 `seven17_dump_itemform_categories`）。未配对则采集/上架会跳过或报错。

### `seven17_upload`（上架）怎么用

**前置条件**

- 已完成 **`pip install -r requirements.txt`** 与 **`python -m playwright install chromium`**（或使用 `PLAYWRIGHT_CHROMIUM_EXECUTABLE` / 项目下 `chrome-win/chrome.exe`）。
- **`config/seven17.json`**（或环境变量）中必填：**`SEVEN17_MB_ID`**、**`SEVEN17_MB_PASSWORD`**。
- 上架数据文件默认 **`data/wecatalog_store_products.json`**（scrape_store 导出格式）；每条待上架记录需要 **`detail_response.result.commodity`**（脚本从中解析标题、价格、图片等）。
- 每条记录须有可下载的主图 URL；无图会跳过并报错。

**默认行为**

- 默认**跳过**已标记 **`uploaded_to_platform: true`** 的商品；若要仍处理它们，加 **`--include-uploaded`**。
- **`--store-json`**：指定 JSON 路径（默认 `data/wecatalog_store_products.json`）。
- **`--limit N`**：最多处理 N 条「待上架」记录（不含被跳过的已上传条）。

**日志**：每条商品在填表前会向 **stderr** 打一行 **`INFO [seven17_upload]`**，写明即将写入的后台字段名与取值（`it_explan` 只打长度与截断预览）。标准输出仍以 JSON 为主，便于重定向分离。

**推荐首次用法**

```bash
# 只登录并填表，不点最终提交（确认浏览器里表单是否正常）
python -m product_feed_kr.seven17.seven17_upload --limit 5 --dry-run

# 正式提交最多 5 条
python -m product_feed_kr.seven17.seven17_upload --limit 5

# 成功后把 uploaded_to_platform 写回 JSON，便于断点续传
python -m product_feed_kr.seven17.seven17_upload --limit 5 --write-back-store-json
```

**本地看浏览器（调试登录/校验错误）**

- PowerShell：`$env:SEVEN17_HEADLESS='0'`  
- 或在 **`seven17.json`** 里设 **`"SEVEN17_HEADLESS": false`**

**不登录预览单条会填什么字段**

```bash
python -m product_feed_kr.seven17.seven17_upload --test-store-json data/wecatalog_store_products.json --test-index 0
```

**常用可选配置（环境变量或 `seven17.json`）**

- **`SEVEN17_BASE_URL`**：站点根 URL，默认 `https://www.seven17.kr`
- **`SEVEN17_STOCK_QTY`**、**`SEVEN17_DEFAULT_PRICE`**、**`SEVEN17_SC_TYPE`**、**`SEVEN17_MAX_IMAGES`**
- **`WEGO_TITLE_PREFIX`**、**`WEGO_DESC_TEMPLATE`**（商品说明 HTML 模板）

**后台表单：脚本会填什么、不会填什么**

- **会写入**：기본분류（`ca_id`）、상품명（`it_name`）、판매가격（`it_price`，货源无价时用 **`SEVEN17_DEFAULT_PRICE`**，默认常为 `0`）、재고수량（`it_stock_qty`）、판매여부（`it_use`）、배송비유형相关（`it_sc_type`）、PC 侧 상품설명（`it_explan`，含 CKEditor 同步）、상품이미지（`it_img1`～）。
- **不会自动填**：기본설명、모바일 상품설명、상품요약정보/전자상거래 고시 각 항목、브랜드·원산지·옵션等 그누보드其余字段；这些需在后台模板或后续手工补。

若页面上「只剩分类像填对了」：先看 **가격是否为 0**（货源 `optimaPrice` / `priceArr` 是否为空）；再看 **상품설명** 是否在「웹에디터」里——脚本写的是 PC 설명栏，编辑器加载慢时已改为等待 CKEditor 实例后再 `setData`。

Windows 核心入口：**`01_采集微猫店铺.bat`**、**`02_LLM补全上架信息.bat`**、**`03_上传韩国站正式.bat`**、**`04_无价格白名单设置.bat`**、**`05_查看商品库.bat`**。并行三任务可用 **`test/run_scrape_llm_upload_parallel.bat`**。

## 包结构

代码按功能分子包（详见 **`product_feed_kr/STRUCTURE.md`**）。包根目录不再堆放业务 `.py`，仅入口与配置 JSON。

| 子包 | 说明 |
|------|------|
| `wecatalog/` | 爬取、弹窗、尺码、分类映射 |
| `seven17/` | 登录、上架、LLM 入口 |
| `listing/` | LLM 补全与计费 |
| `db/` | SQLite 商品库 |
| `common/` | 配置、日志、汇率、Playwright |
| `wego/` | commodity 解析 |
| `tools/` | 迁移、韩元重算等维护脚本 |
| `pf_browser/` | 商品库 Web UI（分类配对、白名单、诊断） |
