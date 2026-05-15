# product-feed-kr（精简）

微猫 **wecatalog** 店铺采集 → **`wecatalog_store_products.json`** → **seven17.kr** 后台 **`itemform`** 表单上传。  
分类 `(分组, 标签)` → 韩文路径：维护 **`product_feed_kr/wecatalog_tag_category_map.json`**（可用 **`wecatalog_tag_category_map_builder`** 生成）。

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
python -m product_feed_kr.wecatalog_scrape_store --store-url "..." --out data/wecatalog_store_products.json --existing-json data/wecatalog_store_products.json

# 映射表生成（编辑 config/wecatalog_tag_category_map.txt 后）
python -m product_feed_kr.wecatalog_tag_category_map_builder
# 或双击 build_wecatalog_tag_category_map.bat

# 上架（默认读取 data/wecatalog_store_products.json）
python -m product_feed_kr.seven17_upload --limit 5

# 爬一次后台「商品录入」页，导出 ca_id / ca_id2 / ca_id3 下拉的 value + 文案（对照 map 里 seven17_ca_id）
python -m product_feed_kr.seven17_dump_itemform_categories --out data/seven17_ca_options.json

# 用上面的 dump 自动写入 wecatalog_tag_category_map.json 的 meta.seven17_ca_id（路径一致即匹配）
python -m product_feed_kr.wecatalog_tag_category_map_apply_seven17
```

配置：复制 **`config/seven17.example.json`** → **`config/seven17.json`**（账号等）。环境变量优先于 JSON；可用 **`SEVEN17_CONFIG`** 指向其它配置文件路径。**勿将含密码的 `seven17.json` 提交到 Git。**

**seven17 商品分类 `ca_id`**：仅在 **`product_feed_kr/wecatalog_tag_category_map.json`** 里对应 `(分组, 标签)` 的 **`meta.seven17_ca_id`**（与后台「기본분류」下拉的 option value 一致）；未配置则上架脚本会跳过该条并报错。抓取后台下拉对照填表：`seven17_dump_itemform_categories` → `wecatalog_tag_category_map_apply_seven17`。重新运行 **`wecatalog_tag_category_map_builder`** 时会保留已填的 `seven17_ca_id`。

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
python -m product_feed_kr.seven17_upload --limit 5 --dry-run

# 正式提交最多 5 条
python -m product_feed_kr.seven17_upload --limit 5

# 成功后把 uploaded_to_platform 写回 JSON，便于断点续传
python -m product_feed_kr.seven17_upload --limit 5 --write-back-store-json
```

**本地看浏览器（调试登录/校验错误）**

- PowerShell：`$env:SEVEN17_HEADLESS='0'`  
- 或在 **`seven17.json`** 里设 **`"SEVEN17_HEADLESS": false`**

**不登录预览单条会填什么字段**

```bash
python -m product_feed_kr.seven17_upload --test-store-json data/wecatalog_store_products.json --test-index 0
```

**常用可选配置（环境变量或 `seven17.json`）**

- **`SEVEN17_BASE_URL`**：站点根 URL，默认 `https://www.seven17.kr`
- **`SEVEN17_STOCK_QTY`**、**`SEVEN17_DEFAULT_PRICE`**、**`SEVEN17_SC_TYPE`**、**`SEVEN17_MAX_IMAGES`**
- **`WEGO_TITLE_PREFIX`**、**`WEGO_DESC_TEMPLATE`**（商品说明 HTML 模板）

**后台表单：脚本会填什么、不会填什么**

- **会写入**：기본분류（`ca_id`）、상품명（`it_name`）、판매가격（`it_price`，货源无价时用 **`SEVEN17_DEFAULT_PRICE`**，默认常为 `0`）、재고수량（`it_stock_qty`）、판매여부（`it_use`）、배송비유형相关（`it_sc_type`）、PC 侧 상품설명（`it_explan`，含 CKEditor 同步）、상품이미지（`it_img1`～）。
- **不会自动填**：기본설명、모바일 상품설명、상품요약정보/전자상거래 고시 각 항목、브랜드·원산지·옵션 등 그누보드其余字段；这些需在后台模板或后续手工补。

若页面上「只剩分类像填对了」：先看 **가격是否为 0**（货源 `optimaPrice` / `priceArr` 是否为空）；再看 **상품설명** 是否在「웹에디터」里——脚本写的是 PC 설명栏，编辑器加载慢时已改为等待 CKEditor 实例后再 `setData`。

Windows 一键爬取示例：**`scrape_wecatalog_store.bat`**（或并行三任务 **`run_scrape_llm_upload_parallel.bat`**）。

## 包结构（保留文件）

| 模块 | 说明 |
|------|------|
| `wecatalog_scrape_store.py` | 爬取 |
| `wecatalog_fetch_tags.py` | 分类树 / 浏览器 |
| `wecatalog_tag_mapping.py` + `wecatalog_tag_category_map.json` | 路径映射 |
| `wecatalog_tag_category_map_builder.py` | 生成 JSON |
| `seven17_upload.py` | 表单上传 |
| `seven17_dump_itemform_categories.py` | 登录后抓取 itemform 分类下拉的 value / 文案 |
| `wecatalog_tag_category_map_apply_seven17.py` | 用 dump 批量写入 map 的 seven17_ca_id |
| `seven17_adm.py` | 登录 |
| `seven17_config.py` | 配置 |
| `wego_commodity.py` | 详情 commodity → 标题/价/图 |
| `playwright_path.py` | Chromium 路径 |
