# product_feed_kr 包结构

实现代码只在子包内；**包根目录仅保留** `__init__.py`、`__main__.py`、`_paths.py`。

## 子包

| 子包 | 职责 | 可 `python -m` 运行的模块 |
|------|------|---------------------------|
| `common/` | 配置、日志、时间、锁、Playwright、汇率 | — |
| `db/` | SQLite 商品库 | —（含 `pf_store_item`、`pf_scrape_skip` 失效跳过表） |
| `wecatalog/` | 微猫采集 | `wecatalog_scrape_store`、`wecatalog_fetch_tags` |
| `seven17/` | 登录、上架、LLM 入口 | `seven17_upload`、`seven17_llm`、`seven17_dump_itemform_categories` |
| `listing/` | LLM 补全、计费、对比 | `llm_providers_compare` |
| `wego/` | commodity 解析 | — |
| `tools/` | 迁移、韩元重算、属性修复 | `recalc_price_krw_db`、`migrate_product_feed_legacy_db`、`migrate_llm_spec_db`、`fix_attr_ko_shoe_sizes_mm` |
| `pf_browser/` | 商品库 Web UI（分类配对、无价白名单、诊断） | `pf_browser` |

## 分类映射数据（`data/`）

| 文件 | 说明 |
|------|------|
| `wecatalog_categories.json` | 微猫 commodity/tags（05 启动同步） |
| `seven17_categories.json` | 韩文分类列表（05 启动同步） |
| `wecatalog_category_pairs.json` | 微猫 ↔ 韩文配对（05 UI 维护） |
| `seven17_path_ca_map.json` | 韩文路径 → `ca_id`（上架用） |

## 路径

- `product_feed_kr/_paths.py`：`PACKAGE_ROOT`、`REPO_ROOT`（`config/`、`data/`、`sql/`）

## Windows bat

| bat | 模块 |
|-----|------|
| `01_采集微猫店铺.bat` | `product_feed_kr.wecatalog.wecatalog_scrape_store` |
| `02_LLM补全上架信息.bat` | `product_feed_kr.seven17.seven17_llm` |
| `03_上传韩国站正式.bat` | `product_feed_kr.seven17.seven17_upload` |
| `04_无价格白名单设置.bat` | 启动 `pf_browser`（无价白名单弹窗） |
| `05_查看商品库.bat` | `product_feed_kr.pf_browser` |

## import 示例

```python
from product_feed_kr.seven17.seven17_upload import ...
from product_feed_kr.db.store_sqlite import sqlite_db_path
```
