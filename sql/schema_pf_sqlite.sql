-- product-feed-kr：本地 SQLite（单文件，无需 MySQL 服务）
-- 由 store_sqlite.ensure_sqlite_schema 在首次连接时执行
-- 列顺序：id / 整型 / 时间 靠前，大 JSON 靠后（便于 DB 浏览器查看）

PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS pf_store_info (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  skip_detail INTEGER NOT NULL DEFAULT 0,
  detail_delay_sec REAL NOT NULL DEFAULT 5,
  last_saved_at TEXT,
  updated_at TEXT NOT NULL DEFAULT (datetime('now', '+8 hours')),
  album_id TEXT NOT NULL UNIQUE,
  store_url TEXT NOT NULL,
  trans_lang TEXT NOT NULL DEFAULT 'zh',
  stats_json TEXT
);

CREATE TABLE IF NOT EXISTS pf_store_item (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tag_id INTEGER NOT NULL DEFAULT 0,
  llm_attempt_count INTEGER NOT NULL DEFAULT 0,
  can_process INTEGER NOT NULL DEFAULT 1,
  can_upload INTEGER NOT NULL DEFAULT 0,
  rescrape_pending INTEGER NOT NULL DEFAULT 0,
  uploaded_to_platform INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL DEFAULT (datetime('now', '+8 hours')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now', '+8 hours')),
  llm_processed_at TEXT,
  seven17_uploaded_at TEXT,
  seven17_ca_id TEXT,
  album_id TEXT NOT NULL,
  goods_id TEXT NOT NULL,
  wecatalog_group TEXT NOT NULL DEFAULT '',
  wecatalog_tag TEXT NOT NULL DEFAULT '',
  shop_category_path_json TEXT,
  goods_url TEXT NOT NULL,
  commodity_title TEXT NOT NULL DEFAULT '',
  wecatalog_listed_at TEXT,
  price_cny TEXT,
  commodity_goods_num TEXT,
  commodity_image_urls_json TEXT,
  commodity_tag_names_json TEXT,
  commodity_sizes_json TEXT,
  commodity_colors_json TEXT,
  first_image_hash TEXT,
  price_krw TEXT,
  sizes_ko_json TEXT,
  colors_ko_json TEXT,
  llm_name_zh TEXT,
  llm_name_ko TEXT,
  llm_desc_zh TEXT,
  llm_desc_ko TEXT,
  llm_source TEXT,
  llm_reason TEXT,
  UNIQUE (album_id, goods_id, tag_id)
);

CREATE INDEX IF NOT EXISTS idx_pf_item_upload ON pf_store_item (album_id, uploaded_to_platform);
CREATE INDEX IF NOT EXISTS idx_pf_item_goods ON pf_store_item (album_id, goods_id);
CREATE INDEX IF NOT EXISTS idx_pf_item_album ON pf_store_item (album_id);

-- 抓取永久跳过：popUps 商品失效等，下次 run 不再请求 popUpsInfoV2
CREATE TABLE IF NOT EXISTS pf_scrape_skip (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  album_id TEXT NOT NULL,
  goods_id TEXT NOT NULL,
  reason TEXT NOT NULL DEFAULT 'popups_invalid',
  errcode INTEGER,
  errmsg TEXT,
  goods_url TEXT,
  first_seen_at TEXT NOT NULL DEFAULT (datetime('now', '+8 hours')),
  last_seen_at TEXT NOT NULL DEFAULT (datetime('now', '+8 hours')),
  hit_count INTEGER NOT NULL DEFAULT 1,
  UNIQUE (album_id, goods_id)
);

CREATE INDEX IF NOT EXISTS idx_pf_scrape_skip_album ON pf_scrape_skip (album_id);
