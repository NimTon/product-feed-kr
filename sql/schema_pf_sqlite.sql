-- product-feed-kr：本地 SQLite（单文件，无需 MySQL 服务）
-- 由 store_sqlite.ensure_sqlite_schema 在首次连接时执行
-- 无兼容迁移：当前模型为两张表
--   1) pf_store_info：店铺信息（每店一行）
--   2) pf_store_item：抓取商品明细（每商品一行）

PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS pf_store_info (
  album_id TEXT NOT NULL PRIMARY KEY,
  store_url TEXT NOT NULL,
  trans_lang TEXT NOT NULL DEFAULT 'zh',
  detail_delay_sec REAL NOT NULL DEFAULT 5,
  skip_detail INTEGER NOT NULL DEFAULT 0,
  last_saved_at TEXT,
  stats_json TEXT,
  updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS pf_store_item (
  album_id TEXT NOT NULL,
  goods_id TEXT NOT NULL,
  tag_id INTEGER NOT NULL DEFAULT 0,
  wecatalog_group TEXT NOT NULL DEFAULT '',
  wecatalog_tag TEXT NOT NULL DEFAULT '',
  shop_category_path_json TEXT,
  goods_url TEXT NOT NULL,
  uploaded_to_platform INTEGER NOT NULL DEFAULT 0,
  seven17_uploaded_at TEXT,
  commodity_title TEXT NOT NULL DEFAULT '',
  commodity_price_raw TEXT,
  commodity_goods_num TEXT,
  commodity_image_urls_json TEXT,
  commodity_tag_names_json TEXT,
  fx_krw_per_cny REAL,
  price_krw TEXT,
  attr_map_json TEXT,
  attr_map_ko_json TEXT,
  llm_name_zh TEXT,
  llm_name_ko TEXT,
  llm_desc_zh TEXT,
  llm_desc_ko TEXT,
  llm_processed_at TEXT,
  product_desc_html TEXT,
  detail_response_json TEXT,
  listing_llm_json TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now')),
  PRIMARY KEY (album_id, goods_id, tag_id)
);

CREATE INDEX IF NOT EXISTS idx_pf_item_upload ON pf_store_item (album_id, uploaded_to_platform);
CREATE INDEX IF NOT EXISTS idx_pf_item_goods ON pf_store_item (album_id, goods_id);
