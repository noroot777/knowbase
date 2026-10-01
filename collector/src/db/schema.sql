-- WeChatOA Aggregation — SQLite schema v1

PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS schema_migrations (
  version     INTEGER PRIMARY KEY,
  applied_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS accounts (
  name              TEXT PRIMARY KEY,
  fakeid            TEXT NOT NULL DEFAULT '',
  -- 公众号在微信上的当前昵称,采集正文时顺手更新。它跟 name 分开存是因为
  -- name 是主键、又被 articles.account_name 和已导出的 vault 目录引用着,
  -- 改不动;能改的只有"显示成什么"。为空表示还没核对过。
  display_name      TEXT NOT NULL DEFAULT '',
  -- 上次去正文页核对昵称的时间。**主干不读也不写它**:名字直接从本机微信的
  -- 会话列表上读,零请求、每轮都核对,没有节流可言。留着这一列是因为这个库
  -- 要能被 wechat-oa 分支同时打开,那条通道还在按它节流(见 migrations v17)。
  display_checked_at TEXT,
  latest_crawl_at   TEXT,
  in_crawl_config   INTEGER NOT NULL DEFAULT 1,
  -- 已移出订阅列表。不真删这一行:articles.account_name 是指向它的外键,
  -- 删掉父行、留下已采集的文章就是一堆孤儿(见 remove_account_from_list)
  removed           INTEGER NOT NULL DEFAULT 0,
  created_at        TEXT,
  updated_at        TEXT,
  CHECK (in_crawl_config IN (0, 1)),
  CHECK (removed IN (0, 1))
);

CREATE TABLE IF NOT EXISTS articles (
  id                TEXT PRIMARY KEY,
  account_name      TEXT NOT NULL REFERENCES accounts(name),
  title             TEXT NOT NULL DEFAULT '',
  digest            TEXT NOT NULL DEFAULT '',
  link              TEXT NOT NULL DEFAULT '',
  cover_url         TEXT NOT NULL DEFAULT '',
  -- 封面图在本地的相对路径。**主干不读也不写它**:封面早就不下载了,要图按
  -- cover_url 现取。留着这一列的理由同 display_checked_at(见 migrations v17)。
  cover_path        TEXT,
  create_time       TEXT NOT NULL,
  create_time_unix  INTEGER,
  is_wx_deleted     INTEGER NOT NULL DEFAULT 0,
  is_user_deleted   INTEGER NOT NULL DEFAULT 0,
  item_show_type    INTEGER NOT NULL DEFAULT 0,
  llm_summary       TEXT,
  word_count        INTEGER,
  ingested_at       TEXT,
  updated_at        TEXT,
  source            TEXT NOT NULL DEFAULT 'crawl',
  import_listed     INTEGER NOT NULL DEFAULT 0,
  import_order      INTEGER NOT NULL DEFAULT 0,
  CHECK (is_wx_deleted IN (0, 1)),
  CHECK (is_user_deleted IN (0, 1)),
  CHECK (source IN ('crawl', 'import')),
  CHECK (import_listed IN (0, 1))
);

CREATE INDEX IF NOT EXISTS idx_articles_import_listed_time
  ON articles (import_listed, create_time DESC, id DESC)
  WHERE import_listed = 1;

CREATE INDEX IF NOT EXISTS idx_articles_import_listed_order
  ON articles (import_listed, import_order DESC, id DESC)
  WHERE import_listed = 1;

CREATE INDEX IF NOT EXISTS idx_articles_account_time
  ON articles (account_name, create_time DESC, id DESC);

CREATE INDEX IF NOT EXISTS idx_articles_time
  ON articles (create_time DESC, id DESC);

CREATE TABLE IF NOT EXISTS article_bodies (
  article_id   TEXT PRIMARY KEY REFERENCES articles(id) ON DELETE CASCADE,
  body_text    TEXT NOT NULL,
  body_html    TEXT,
  source_kind  TEXT NOT NULL DEFAULT 'crawl',
  fetched_at   TEXT,
  updated_at   TEXT
);

-- 正文读取失败是有期限的重试状态，不是用户删除或微信删除。
CREATE TABLE IF NOT EXISTS article_fetch_state (
  article_id    TEXT PRIMARY KEY REFERENCES articles(id) ON DELETE CASCADE,
  link_hash     TEXT NOT NULL,
  failures      INTEGER NOT NULL,
  retry_at      REAL NOT NULL,
  reason        TEXT NOT NULL
);

-- 本机公众号发现索引，不是已采集文章；不存原始消息或联系人。
CREATE TABLE IF NOT EXISTS local_wechat_sources (
  source_key TEXT PRIMARY KEY,
  checkpoint TEXT NOT NULL
);

-- 内容异常单独记账，不阻塞增量游标；只存指纹/位置，不存原始推送。
CREATE TABLE IF NOT EXISTS local_wechat_message_issues (
  source_key TEXT NOT NULL,
  local_id INTEGER NOT NULL,
  content_hash TEXT NOT NULL,
  reason TEXT NOT NULL,
  PRIMARY KEY (source_key, local_id)
);

-- 即使整条图文不可解析，也可从完整原文 URL 保留公众号归属线索。
CREATE TABLE IF NOT EXISTS local_wechat_issue_biz (
  source_key TEXT NOT NULL,
  local_id INTEGER NOT NULL,
  biz TEXT NOT NULL,
  PRIMARY KEY (source_key, local_id),
  FOREIGN KEY (source_key, local_id)
    REFERENCES local_wechat_message_issues(source_key, local_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS local_wechat_articles (
  source_key TEXT NOT NULL,
  id TEXT NOT NULL,
  biz TEXT NOT NULL,
  create_time TEXT NOT NULL,
  message_time INTEGER NOT NULL,
  local_id INTEGER NOT NULL,
  article_json TEXT NOT NULL,
  PRIMARY KEY (source_key, id)
);

CREATE INDEX IF NOT EXISTS idx_local_wechat_articles_scope
  ON local_wechat_articles (biz, create_time);

CREATE TABLE IF NOT EXISTS article_llm_tags (
  article_id  TEXT NOT NULL REFERENCES articles(id) ON DELETE CASCADE,
  tag         TEXT NOT NULL,
  PRIMARY KEY (article_id, tag)
);

CREATE INDEX IF NOT EXISTS idx_llm_tags_tag ON article_llm_tags (tag);

CREATE TABLE IF NOT EXISTS deleted_articles (
  article_id  TEXT PRIMARY KEY,
  deleted_at  TEXT
);

CREATE TABLE IF NOT EXISTS article_annotations (
  article_id      TEXT PRIMARY KEY REFERENCES articles(id) ON DELETE CASCADE,
  is_read         INTEGER NOT NULL DEFAULT 0,
  read_at         TEXT,
  starred         INTEGER NOT NULL DEFAULT 0,
  starred_at      TEXT,
  note            TEXT,
  note_updated_at TEXT,
  updated_at      TEXT,
  CHECK (is_read IN (0, 1)),
  CHECK (starred IN (0, 1))
);

CREATE TABLE IF NOT EXISTS article_user_tags (
  article_id  TEXT NOT NULL REFERENCES articles(id) ON DELETE CASCADE,
  tag         TEXT NOT NULL,
  PRIMARY KEY (article_id, tag)
);

CREATE TABLE IF NOT EXISTS article_notes (
  article_id   TEXT PRIMARY KEY REFERENCES articles(id) ON DELETE CASCADE,
  content      TEXT NOT NULL DEFAULT '',
  created_at   TEXT NOT NULL,
  updated_at   TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_article_notes_updated_at
  ON article_notes (updated_at DESC);

-- 全文索引。**主干不读也不写它**:文章检索在知识库那一侧按 vault 的 md 做,
-- 采集器只管抓和导出,所以这里一行 fts 代码都没有、也不付那笔分词开销。
-- 留着这张表是因为这个库要能被 wechat-oa 分支同时打开,它采集时每篇都写它 ——
-- 而删掉会把**正在同步**的那个旧进程当场打断(它只在首次打开库时跑这份
-- schema,不会因为别的进程删表而再跑一遍)。详见 migrations v18。
-- 列名和 tokenize 跟那个分支一字不差,它的 INSERT 才对得上。
CREATE VIRTUAL TABLE IF NOT EXISTS article_fts USING fts5(
  article_id UNINDEXED,
  title_tok,
  digest_tok,
  body_tok,
  tokenize='unicode61 remove_diacritics 0'
);
