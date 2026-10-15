# -*- coding: utf-8 -*-
"""老库升上来一条不少:`favorites` 的重建与补列。

`favorites.path` 原来是 `NOT NULL REFERENCES documents(path)`,而「收藏此段」新接
的三个来源**在 documents 里都没有行**(日报写在 vault 的 `daily/` 下、那个目录不在
任何 `SOURCES.root` 底下;问答回答压根不是文件;订阅条目在导出之前只在 feeds.db
里)。改可空只能**重建整张表**(SQLite 没有 `ALTER COLUMN`),而重建是这条路上唯一
会丢数据的一步;锚点那几列(采集时唯一性、定不定得住、问答消息身份、正文版本)又
按「只增不减」走 ALTER 那条路,重建分支压根不经过它。所以这一份专钉升级:

1. **老库升上来一条不少**(连 v2 一起验:它的列集合比 v3 少一列)。
2. **孤儿行不许把整次迁移带崩** —— 外键是 ON 的,INSERT 回去逐行查 documents,
   一条 `FOREIGN KEY constraint failed` 就让 `store.connect()` 再也成功不了。
3. **补列一列都不许漏**:少一列后端那条 INSERT 当场 `no such column`,收藏按钮
   从此一按就失败;而老行读出来必须是 NULL(「没记下」比 0 弱,不许折成 0)。
4. **编号高水位要跟着搬**:复用一个已经发出去过的编号,会让旧页面上那颗按钮
   成功作用在另一条收藏上。

存取两头(四类来源、列表与计数、行动卡与分诊)在 `tests/test_fav_snippet_store.py`,
绑文档那一类的临界区在 `tests/test_fav_snippet_lock.py`,浏览器那一头在
`tests/test_fav_snippets.py`;硬约束和事故形态在 `docs/decisions/fav-snippets.md`。

跑:  PYTHONPATH=<worktree 根> python -m unittest tests.test_fav_snippet_migrate
"""

from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# 摆盘(临时库 + 盘上那一篇 + 只挂收藏路由的 client)在共用模块里,
# `test_fav_snippet_store.py` 用的是同一份。
from tests.fav_snippet_fixture import (                        # noqa: E402
    DOC, GONE, REAL_CONNECT, seed_vault, snip_client)


# v3 的建表语句(path NOT NULL、有 action_json、没有 origin 一族)。手抄一份而
# 不是从历史里取:这条回归要钉的正是"从这个形状升上来",形状得写死在用例里。
_V3_DDL = """
CREATE TABLE documents (
  path TEXT PRIMARY KEY, source TEXT NOT NULL DEFAULT '', title TEXT NOT NULL DEFAULT '',
  account TEXT NOT NULL DEFAULT '', published TEXT NOT NULL DEFAULT '',
  url TEXT NOT NULL DEFAULT '', summary TEXT NOT NULL DEFAULT '',
  content_hash TEXT NOT NULL DEFAULT '');
CREATE TABLE favorites (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  path TEXT NOT NULL REFERENCES documents(path) ON DELETE CASCADE,
  quote TEXT NOT NULL DEFAULT '', prefix TEXT NOT NULL DEFAULT '',
  suffix TEXT NOT NULL DEFAULT '', note TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'inbox', action_json TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE INDEX idx_favorites_path ON favorites(path);
"""

# v4 但**锚点那几列还没加**:今天盘上那一份就是这个形状(`path` 可空、origin 一族
# 都在)。那几列按「只增不减」走的是 ALTER 那条路,重建分支压根不经过它 —— 不单独
# 摆一份这样的老库,少 ALTER 一列在回归里一个字都看不出来。
_V4_NO_ANCHOR_DDL = _V3_DDL.split("CREATE TABLE favorites")[0] + """
CREATE TABLE favorites (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  path TEXT REFERENCES documents(path) ON DELETE CASCADE,
  quote TEXT NOT NULL DEFAULT '', prefix TEXT NOT NULL DEFAULT '',
  suffix TEXT NOT NULL DEFAULT '', note TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'inbox', action_json TEXT NOT NULL DEFAULT '',
  origin TEXT NOT NULL DEFAULT 'doc', origin_ref TEXT NOT NULL DEFAULT '',
  origin_title TEXT NOT NULL DEFAULT '', origin_url TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE INDEX idx_favorites_path ON favorites(path);
"""

# v2:同上但**没有 action_json**。重建时按老表实际有的那几列拼 INSERT,写死一份
# 列名会让它在 `no such column` 上翻车。
_V2_DDL = _V3_DDL.replace(" action_json TEXT NOT NULL DEFAULT '',", "")


def _seed_old(db: Path, ddl: str) -> None:
    """摆一个老库:一条活着的片段收藏、一条整篇收藏、一条孤儿(文档已不在索引里)。"""
    conn = sqlite3.connect(db)
    # **起始态必须也是 WAL。** 真实的 v3 库是 `store.connect()` 建的,那一步每次
    # 都 `PRAGMA journal_mode = WAL`;照默认模式摆出来的话,下面那条并发用例测的
    # 其实是"12 个连接同时把库转成 WAL",而**转模式要独占锁、而且不走
    # busy_timeout** —— 整份跑 60 轮红 3 轮,报的是 `database is locked`,跟迁移
    # 一点关系没有(单独起一个 12 连接的最小复现:非 WAL 起始 30×12 里失败 2 次,
    # WAL 起始 0 次)。
    conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(ddl)
    conn.execute("INSERT INTO documents (path, title, account, url, summary)"
                 " VALUES (?, '活着的那篇', '某号', 'https://e.org/live', '摘要')", (DOC,))
    cols = {r[1] for r in conn.execute("PRAGMA table_info(favorites)")}
    extra = ", action_json" if "action_json" in cols else ""
    value = ", '{\"name\":\"老行动卡\"}'" if "action_json" in cols else ""
    conn.execute(
        f"INSERT INTO favorites (id, path, quote, prefix, suffix, note, status{extra},"
        f" created_at, updated_at) VALUES (7, ?, '文中一句', '前文', '后文', '随手记', 'done'"
        f"{value}, '2026-01-01', '2026-02-02')", (DOC,))
    conn.execute("INSERT INTO favorites (id, path, created_at, updated_at)"
                 " VALUES (8, ?, '2026-01-02', '2026-01-02')", (DOC,))
    # 孤儿:外键关着的那阵子(或老版本)留下的,原来在内连接下就看不见、却仍占计数
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute("INSERT INTO favorites (id, path, quote, created_at, updated_at)"
                 " VALUES (9, ?, '那篇已经没了', '2026-01-03', '2026-01-03')", (GONE,))
    conn.commit()
    conn.close()


class MigrationTests(unittest.TestCase):
    """v2/v3 → v4:`path` 可空 + origin 一族。重建那一步不许丢行、不许炸。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="kb-fav-mig-")
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / "kb.db"

    def rows(self, conn) -> dict:
        return {r["id"]: dict(r) for r in conn.execute("SELECT * FROM favorites")}

    def test_v3_rows_survive_the_rebuild(self):
        _seed_old(self.db, _V3_DDL)
        conn = REAL_CONNECT(self.db)
        self.addCleanup(conn.close)
        rows = self.rows(conn)
        self.assertEqual({7, 8, 9}, set(rows), "重建丢了行")
        seven = rows[7]
        self.assertEqual(
            ("文中一句", "前文", "后文", "随手记", "done", '{"name":"老行动卡"}',
             "2026-01-01", "2026-02-02"),
            (seven["quote"], seven["prefix"], seven["suffix"], seven["note"],
             seven["status"], seven["action_json"],
             seven["created_at"], seven["updated_at"]),
            "老收藏的内容/状态/行动卡没原样搬过来")
        self.assertEqual(DOC, seven["path"], "活着的那篇的 path 不该动")
        self.assertEqual("doc", seven["origin"], "老收藏默认就是 doc 来源")

    def test_orphan_row_keeps_connect_working(self):
        """文档已不在索引里的那条:path 落 NULL,而不是让整次迁移(和 connect)炸。"""
        _seed_old(self.db, _V3_DDL)
        conn = REAL_CONNECT(self.db)
        self.addCleanup(conn.close)
        self.assertIsNone(self.rows(conn)[9]["path"])
        self.assertEqual("那篇已经没了", self.rows(conn)[9]["quote"], "孤儿行的引文丢了")

    def test_connect_hands_out_an_indexed_table_and_no_leftovers(self):
        """交出去的连接上:索引在、重建用的临时表不在。

        索引那一半是**冗余带,单点注入打不红它** —— 索引跟着 `RENAME TO` 挂到临时
        表上、临时表一删就跟着没,而 `connect()` 在迁移之后无条件跑一次
        `executescript(FAVORITES_DDL)` 把它补回来(口径写在 `FAVORITES_INDEX`
        头上)。实测:在重建里加 DROP INDEX / CREATE INDEX,去掉任何一句都红 0 条,
        所以那两句已经删掉了。留这半条断言是为了钉住**后果**:不管迁移怎么改,
        交出去的连接上这条索引必须在。
        临时表那一半是真判据(去掉 DROP TABLE → 红 1 条)。
        """
        _seed_old(self.db, _V3_DDL)
        conn = REAL_CONNECT(self.db)
        self.addCleanup(conn.close)
        names = {r["name"] for r in conn.execute("PRAGMA index_list(favorites)")}
        self.assertIn("idx_favorites_path", names)
        left = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE name LIKE 'favorites_v%'")]
        self.assertEqual([], left, "重建用的临时表留在库里了")

    def test_a_v4_db_without_the_anchor_columns_gets_them_added(self):
        """锚点那几列是后来加的,老库靠 ALTER 补 —— 补不上整条收藏链路当场断。

        判据落在**连接交出去之后那张表上**:列都在、老行读出来是 NULL(不是 0
        也不是空串 —— 那几个分别是「当时不唯一」「当时就定不住」「版本号读不
        出来」,都比「没记下」更强的断言),而且新行真写得进去。少 ALTER 任何
        一列,后端那条 INSERT 当场 `no such column`,收藏按钮从此一按就失败。
        """
        _seed_old(self.db, _V4_NO_ANCHOR_DDL)
        conn = REAL_CONNECT(self.db)
        self.addCleanup(conn.close)
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(favorites)")}
        self.assertLessEqual(
            {"quote_unique", "anchor_exact", "origin_msg", "doc_version"}, cols,
            "锚点那几列没补上")
        seven = self.rows(conn)[7]
        self.assertEqual(
            (None, None, None, None),
            (seven["quote_unique"], seven["anchor_exact"], seven["origin_msg"],
             seven["doc_version"]),
            "老行这几列该读出 NULL")
        self.assertEqual("文中一句", seven["quote"], "补列把内容弄丢了")
        conn.execute(
            "INSERT INTO favorites (path, quote, quote_unique, anchor_exact,"
            " origin_msg, doc_version, created_at, updated_at)"
            " VALUES (NULL, '补完列之后新收的一段', 1, 1, 0, 'v-新', ?, ?)",
            ("2026-03-01", "2026-03-01"))
        self.assertEqual(
            ("v-新", 1, 1), tuple(conn.execute(
                "SELECT doc_version, quote_unique, anchor_exact FROM favorites"
                " WHERE quote = '补完列之后新收的一段'").fetchone()),
            "新行写进去又读不出来")

    def test_v2_without_action_json_also_upgrades(self):
        _seed_old(self.db, _V2_DDL)
        conn = REAL_CONNECT(self.db)
        self.addCleanup(conn.close)
        rows = self.rows(conn)
        self.assertEqual({7, 8, 9}, set(rows))
        self.assertEqual("", rows[7]["action_json"], "v2 没有这一列,补出来该是空的")
        self.assertEqual("文中一句", rows[7]["quote"])

    def test_second_connect_changes_nothing(self):
        _seed_old(self.db, _V3_DDL)
        first = REAL_CONNECT(self.db)
        before = self.rows(first)
        first.close()
        second = REAL_CONNECT(self.db)
        self.addCleanup(second.close)
        self.assertEqual(before, self.rows(second), "再连一次又重建了一遍")

    def test_concurrent_first_use_does_not_blow_up(self):
        """升级后第一次并发使用。判据和列集合分在两个不持锁的语句里的话,两个
        连接各重建一次,后一个撞 `table favorites_v3 already exists` —— 那一个
        请求当场 500,而 connect() 是页面 API、worker、CLI 共用的入口。"""
        _seed_old(self.db, _V3_DDL)

        def one(_):
            try:
                conn = REAL_CONNECT(self.db)
                conn.close()
                return "ok"
            except Exception as e:                      # noqa: BLE001 - 原样报回来
                return f"{type(e).__name__}: {e}"

        with ThreadPoolExecutor(max_workers=12) as ex:
            out = list(ex.map(one, range(12)))
        self.assertEqual(["ok"] * 12, out)
        conn = REAL_CONNECT(self.db)
        self.addCleanup(conn.close)
        self.assertEqual({7, 8, 9}, set(self.rows(conn)), "并发重建把行弄丢了")


class UpgradeIdentityTests(unittest.TestCase):
    """升级不许把一个已经发出去过的收藏编号再发一遍。

    判据落在**后果**上:旧那一页上那颗按钮手里只有 id。正常不复用编号时它最多
    得到一句「这条不在了」(404);复用之后,同一个请求会**成功作用在另一条收藏
    上** —— 删除、状态流转、行动卡写回三处都只按 id 找行。
    """

    def test_a_stale_page_cannot_delete_the_favorite_that_took_its_number(self):
        with tempfile.TemporaryDirectory(prefix="kb-fav-seq-") as root:
            db = Path(root) / "kb.db"
            _seed_old(db, _V3_DDL)
            raw = sqlite3.connect(db)
            # **编号最大的那条先删掉** —— 这正是高水位唯一会丢的那一刻:现存行里
            # 再也没有它,重建按"插进去的最大 id"重算就会退回去。
            stale = raw.execute(
                "INSERT INTO favorites (path, quote, created_at, updated_at)"
                " VALUES (?, '旧页面上那条', 'x', 'x')", (DOC,)).lastrowid
            raw.execute("DELETE FROM favorites WHERE id = ?", (stale,))
            raw.commit()
            raw.close()
            REAL_CONNECT(db).close()          # 这一句就是升级
            client = snip_client(self, db)
            version = seed_vault(self, Path(root) / "vault")
            new = client.post("/api/favorites",
                              json={"path": DOC, "quote": "升级之后新存的一段",
                                    "doc_version": version})
            self.assertEqual(200, new.status_code, new.text)
            fresh = new.json()["id"]
            self.assertGreater(fresh, stale, "新收藏复用了已经发出去过的编号")
            self.assertEqual(404, client.delete(f"/api/favorites/{stale}").status_code,
                             "旧页面删掉了别人")
            live = [r["id"] for r in client.get("/api/favorites").json()["favorites"]]
            self.assertIn(fresh, live, "新收藏被一次陈旧的删除带走了")


if __name__ == "__main__":
    unittest.main()
