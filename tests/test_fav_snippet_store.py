# -*- coding: utf-8 -*-
"""「收藏此段」不再要求那段话出自一篇被索引过的文档。

这件事的难点不在前端那颗按钮。`favorites.path` 原来是
`NOT NULL REFERENCES documents(path)`,而新加的三个来源**在 documents 里都没有行**:
日报写在 vault 的 `daily/` 下、那个目录不在任何 `SOURCES.root` 底下(`kb/config.py`);
问答回答压根不是文件;订阅条目在导出之前只在 feeds.db 里。空串占不了位 —— 空串
不是 NULL,外键照查,一条 `FOREIGN KEY constraint failed` 就是整条链的终点。

所以这一份钉的是存取两头:

1. **老库升上来一条不少。** `path` 从 NOT NULL 改可空只能重建整张表(SQLite 没有
   `ALTER COLUMN`),而重建是这条路上唯一会丢数据的一步。连 v2(还没有
   `action_json`)一起验,它的列集合比 v3 少一列。
2. **孤儿行不许把整次迁移带崩。** 外键是 ON 的,INSERT 回去逐行查 documents ——
   历史上留下的、文档已经不在索引里的收藏会让那一句整条失败,于是
   `store.connect()` 本身再也成功不了(页面 API、提取 worker、CLI 全挂)。
3. **四类来源都存得下,而「文档不在索引里」仍然是 404。** 后端不替调用方降级:
   把一条本该能定位回原文的收藏静默换成只有引文的那一类,是前端该自己做的决定
   (它知道自己手里那篇有没有入库,见 `kb/static/js/snip.js` 的 fallback)。
4. **列出来的条数和计数牌上那个数一致。** 内连接会让没有 path 的条目静默消失,
   而 `counts` 是全表数的 —— 标签上写着「待试 12」、列表里只有 9 条,剩下 3 条
   点不到也删不掉。
5. **行动卡和分诊也看得见它们。** 这两处原来也是内连接:漏掉的条目照旧堆在待试
   清单上、却永远分不到任何一组,而**指纹是按这批行算的** —— 漏掉等于新收一条
   日报片段之后缓存仍判「新鲜」。

浏览器那一头(四处正文上真选中、真收藏、真点回去)在 tests/test_fav_snippets.py。

跑:  PYTHONPATH=<worktree 根> python -m unittest tests.test_fav_snippet_store
"""

from __future__ import annotations

import json
import re
import sqlite3
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from kb import favbrain                                        # noqa: E402
from kb.scanner import body_version, file_hash                 # noqa: E402

# 摆盘(临时库 + 盘上那一篇 + 只挂收藏路由的 client)搬到了共用模块,
# 绑文档那一类的临界区用例在 `tests/test_fav_snippet_lock.py` 用同一份。
from tests.fav_snippet_fixture import (                        # noqa: E402
    DOC, GONE, REAL_CONNECT, ApiFixture, seed_vault, snip_client)


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
        `executescript(_FAVORITES_DDL)` 把它补回来(口径写在 `_FAVORITES_INDEX`
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


class SnippetWriteTests(ApiFixture):
    """四类来源都存得下,而该拒的照旧拒。"""

    def test_each_origin_lands(self):
        cid, _uid, aid = self.seed_chat()
        cases = [
            dict(path=DOC, quote="文中一句", prefix="前", suffix="后"),
            dict(quote="日报一句", origin="daily", origin_ref="2026-10-15",
                 origin_title="日报 · 2026-10-15"),
            dict(quote="回答一句", origin="chat", origin_ref=str(cid), origin_msg=aid,
                 origin_title="问答 · 有哪些视频模型"),
            dict(quote="订阅一句", origin="feed", origin_ref="rss:abc",
                 origin_title="Some English Post", origin_url="https://e.org/x"),
        ]
        for body in cases:
            with self.subTest(origin=body.get("origin", "doc")):
                self.assertEqual(200, self.snip(**body).status_code)
        got = {r["origin"]: r["path"] for r in self.conn.execute(
            "SELECT origin, path FROM favorites")}
        self.assertEqual({"doc": DOC, "daily": None, "chat": None, "feed": None}, got,
                         "没有文档的那三类必须落 NULL —— 空串会被外键当场拒掉")

    def test_missing_document_is_refused_not_downgraded(self):
        """`path` 给了但不在索引里 → 404。**不悄悄存成「没有 path」那一类** ——
        那等于把一条本该能定位回原文的收藏,静默换成一条只能看引文的。"""
        r = self.snip(path="source/dr-gzh/nope.md", quote="野路径")
        self.assertEqual(404, r.status_code, r.text)
        self.assertEqual(0, self.conn.execute(
            "SELECT COUNT(*) FROM favorites").fetchone()[0], "拒掉了却还是写了一行")

    def test_refusals(self):
        for name, body, code in [
            ("doc 来源没给 path", dict(quote="一句", origin="doc"), 400),
            ("没有文档又没有标题", dict(quote="一句", origin="daily"), 400),
            ("不认识的来源", dict(quote="一句", origin="nope", origin_title="t"), 400),
            ("空引文", dict(path=DOC, quote="   "), 400),
        ]:
            with self.subTest(name):
                self.assertEqual(code, self.snip(**body).status_code)
        self.assertEqual(0, self.conn.execute(
            "SELECT COUNT(*) FROM favorites").fetchone()[0])

    def test_a_chat_snippet_must_name_a_message_that_is_in_the_db(self):
        """问答片段要带**已经落库的那条消息**的 id,后端自己回库里核一次。

        会话号非空只说明「那个对话存在」—— 追问时它早就有了,而一轮问答是答完
        才整轮写进 `messages` 的:生成中、点了停止、出错、流断在最后一帧之前,
        库里都什么都没有。只信客户端那句「我已经保存了」的话,收下来的是一条
        按会话号点回去**打开的是对的那个对话、那段话却怎么也找不到**的收藏,
        而当场一切正常。客户端那一头另有一道(`chat.js` 的 `SNIP_CID`:没有
        `done` 帧不给身份),这一条钉的是服务端这一道 —— 两头各要有自己的红。
        """
        cid, _uid, aid = self.seed_chat()
        other, _ouid, _oaid = self.seed_chat("另一个对话")
        base = dict(quote="回答一句", origin="chat", origin_title="问答 · 某问")
        for name, body, code in [
            ("没带消息身份", dict(base, origin_ref=str(cid)), 400),
            ("消息 id 不存在", dict(base, origin_ref=str(cid), origin_msg=99999), 400),
            ("那条消息属于另一个对话",
             dict(base, origin_ref=str(other), origin_msg=aid), 400),
            ("会话号不是数字", dict(base, origin_ref="abc", origin_msg=aid), 400),
        ]:
            with self.subTest(name):
                self.assertEqual(code, self.snip(**body).status_code)
        self.assertEqual(0, self.conn.execute(
            "SELECT COUNT(*) FROM favorites").fetchone()[0], "拒掉了却还是写了一行")
        # 反过来钉一边:**对得上的那一条照旧存得下**,而且把身份记进了库
        self.assertEqual(200, self.snip(
            **dict(base, origin_ref=str(cid), origin_msg=aid)).status_code)
        self.assertEqual((str(cid), aid), self.conn.execute(
            "SELECT origin_ref, origin_msg FROM favorites").fetchone()[:2])

    def test_half_a_character_is_refused_not_a_500(self):
        """截断切开的「半个字」**如实拒成 400**,不是一句 500。

        JS 的字符串按 UTF-16 存,emoji 一类字占两个单元(代理对);前后文窗口
        按单元截就可能切在它中间。孤立的那一半 Python 的 str 装得下、UTF-8 编
        不出来 —— sqlite3 在 INSERT 绑参数那一句抛 UnicodeEncodeError,而那是
        `try` 里的最后一步,于是整条 POST 变成 500(审查实测:正文里 emoji 后面
        隔着 29 个字母,选中再后面那句普通中文,日报和独立阅读页都收藏失败)。

        采集那头已经不许切开字符(`kb/static/js/snip.js` 的 `snipWhole`),这一道
        是**存储层自己的口径**:不合法的输入该当场说清楚哪个字段坏了,不押在
        客户端上。判据落在后果上:**状态码是 400、库里一行没写**。

        反过来钉一边:**完整的 emoji 照旧存得下、而且原样存**。拒的是半个字,
        不是「非 BMP 的字一律不许收」—— 那会把正文里合法的字都挡在外面。
        """
        # **按浏览器的线上形态发**:半个字在 JSON 里是 `"\ud83d"` 这样的转义
        # (一串 ASCII 字节),服务端 `json.loads` 之后才成为一个孤立代理字符。
        # 走测试客户端的 `json=` 参数发不出去 —— httpx 自己就先 UTF-8 编码,当场
        # 抛在**客户端**那一头,于是这条用例永远到不了被测的那段代码。
        def raw(body):
            return self.client.post(
                "/api/favorites", content=json.dumps(body),
                headers={"Content-Type": "application/json"})

        for name, body in [
            ("前后文里有半个字", dict(path=DOC, quote="一句整话", prefix="\ud83d前")),
            ("引文里有半个字", dict(path=DOC, quote="半个字\ude00在这儿")),
            ("备注里有半个字", dict(path=DOC, quote="一句整话", note="记一笔\ud83d")),
            ("标题里有半个字", dict(quote="一句整话", origin="daily",
                                origin_title="日报 \ud83d")),
        ]:
            with self.subTest(name):
                r = raw(body)
                self.assertEqual(400, r.status_code, r.text)
                self.assertIn("半个字符", r.json()["detail"])
        # **带 `max_length` 的那几个快照字段尤其要钉**(上面那条标题):校验层
        # 自己也认不下半个字,而它回的 `RequestValidationError` 错误体里带着
        # `input` 原串 —— 序列化它又是一次 UTF-8 编码,于是"受控的 422"当场变成
        # 一个崩在错误处理里的 500。所以那一道判据必须排在长度之前
        # (`mode="before"`),回的那句话也不许带原串。
        self.assertEqual(0, self.conn.execute(
            "SELECT COUNT(*) FROM favorites").fetchone()[0], "拒掉了却还是写了一行")

        whole = "完整的 \U0001F600 照旧收得下"
        r = self.snip(path=DOC, quote=whole, prefix="\U0001F600 前文", suffix="后文")
        self.assertEqual(200, r.status_code, r.text)
        row = next(f for f in self.listed()["favorites"]
                   if f["id"] == r.json()["id"])
        self.assertEqual(whole, row["quote"], "合法的字被改掉了")
        self.assertEqual("\U0001F600 前文", row["prefix"])

    def test_the_stored_anchor_is_one_continuous_stretch(self):
        """引文首尾的空白挪进前后文,**不是只把引文 strip 掉**。

        判据落在后果上:库里那三段压平之后连起来,必须还是正文里连着的一段。
        少掉几个空白的话,回跳按 `prefix + quote + suffix` 整段找就必然失配、
        退回只找引文 —— 而正文里有两处一样的话时,那一步要么落在第一处(老口径,
        审查实测),要么如实判成找不到。两种都不是用户收藏的那一处。
        前后文的空白一律压成一个空格,也是为了跨渲染器对得上(段落之间在一头是
        两个换行、在另一头只有一个,口径在 `kb/static/js/snip.js`)。
        """
        r = self.snip(path=DOC, quote="\n  那一句要先把索引建好 \t ",
                      prefix="前文一段\n", suffix="\n后文一段")
        self.assertEqual(200, r.status_code, r.text)
        row = next(f for f in self.listed()["favorites"]
                   if f["id"] == r.json()["id"])
        self.assertEqual("那一句要先把索引建好", row["quote"])
        self.assertEqual("前文一段 那一句要先把索引建好 后文一段",
                         row["prefix"] + row["quote"] + row["suffix"],
                         "三段连不成原文里那一段")

    def test_the_quote_is_stored_as_it_was_written(self):
        """引文**按原文存**:换行和缩进一个都不许丢。

        压平空白是**匹配用的表示**(两头渲染出来的空白本来就不一样),可它不是
        收藏的内容。把它存进库等于把用户选中的四行 Python 改成接成一行的
        `if ready: run_task() else: stop_task()` —— 粘出来不是能跑的代码,而
        原始字符在库里压根不存在了,**事后补不回来**(审查实测,日报正文和独立
        阅读页两处都中)。

        反过来钉一边:**前后文仍然压平**。两边都按原文存的话,段落边界那一个
        换行在两头不一样,整段锚点又对不上了 —— 所以这条同时断言前后文里没有
        换行,以及三段压平之后仍然连成一段。
        """
        code = "if ready:\n    run_task()\nelse:\n    stop_task()"
        r = self.snip(path=DOC, quote=code,
                      prefix="先看这一段\n\n", suffix="\n\n照着跑一遍")
        self.assertEqual(200, r.status_code, r.text)
        row = next(f for f in self.listed()["favorites"]
                   if f["id"] == r.json()["id"])
        self.assertEqual(code, row["quote"], "存进去的不是那四行原文")
        self.assertNotIn("\n", row["prefix"] + row["suffix"],
                         "前后文该是压平过的匹配表示")
        flat = re.sub(r"\s+", " ", code)
        self.assertEqual("先看这一段 " + flat + " 照着跑一遍",
                         row["prefix"] + flat + row["suffix"],
                         "压平之后三段连不成原文里那一段")

    def test_a_doc_bound_snippet_must_name_the_version_it_verified(self):
        """绑了文档就必须说清绑的是**哪一版正文**,落库前再核一次。

        核对(调用方读 `/api/doc`)和写入是两个请求。没有共同的版本号的话,中间
        那一下重译写回、正文补抓把那一篇换掉,收藏照旧落成 `origin=doc` 返回
        200 —— 固化下来的是一条**点回去永远找不到那句话**的收藏,而当场一切正常
        (审查实测:核对读到旧译文,其间另一路真走 `_save → export → index` 换成
        新译文,回看 0 个高亮)。所以缺版本号就拒,版本号不是盘上这一版也拒。

        **三种拒绝各报各的**:调用方据此决定退回哪一档(前端那一头 404 退站外
        原文、409 说"刚换过一版"、503 说"这一刻读不出来" —— 见
        `kb/static/js/snip.js` 的 `RETRY_NOTE`)。合成一种的话,"暂时读不出来"
        就被说成了"这篇不在索引里",而后者是个已经证明了的结论。
        """
        bare = self.client.post("/api/favorites",
                                json={"path": DOC, "quote": "没说绑的是哪一版"})
        self.assertEqual(400, bare.status_code, bare.text)
        stale = self.client.post("/api/favorites",
                                 json={"path": DOC, "quote": "绑的是另一版",
                                       "doc_version": "0" * 16})
        self.assertEqual(409, stale.status_code, stale.text)
        (self.vault / DOC).unlink()
        gone = self.client.post("/api/favorites",
                                json={"path": DOC, "quote": "文件已经不在了",
                                      "doc_version": self.version})
        self.assertEqual(404, gone.status_code, gone.text)
        # **读不出来 ≠ 不存在**:换成一个目录,文件系统给的是 OSError 而不是
        # FileNotFoundError(权限、网盘没挂上同理)—— 这一种报 503
        (self.vault / DOC).mkdir()
        unreadable = self.client.post("/api/favorites",
                                      json={"path": DOC, "quote": "这一刻读不出来",
                                            "doc_version": self.version})
        self.assertEqual(503, unreadable.status_code, unreadable.text)
        self.assertEqual(0, self.conn.execute(
            "SELECT COUNT(*) FROM favorites").fetchone()[0], "拒掉了却还是写了一行")

    def test_a_frontmatter_writeback_does_not_invalidate_a_snippet(self):
        """反过来钉一边:**版本号只认正文**,frontmatter 改了不算换版。

        按整份文件算(`file_hash`,也就是 `documents.content_hash`)的话,提取回写
        那一刻的收藏会被误拒 —— 而那一步只往 frontmatter 里补 tags / 写
        extracted_at,正文一个字没变,收藏点回去照旧定位得到。误拒的代价是白退到
        站外原文,而用户看不出自己做错了什么。
        """
        before = (self.vault / DOC).read_text(encoding="utf-8")
        after = before.replace("  - topic/skill工具",
                               "  - topic/skill工具\n  - flag/已提取")
        (self.vault / DOC).write_text(after, encoding="utf-8")
        self.assertNotEqual(file_hash(before), file_hash(after),
                            "这一步得真改了文件,不然这条用例什么也没验")
        self.assertEqual(self.version, body_version(after),
                         "正文一个字没变,版本号不该动")
        r = self.client.post("/api/favorites",
                             json={"path": DOC, "quote": "回写之后照旧收得下",
                                   "doc_version": self.version})
        self.assertEqual(200, r.status_code, r.text)
        self.assertEqual([(DOC, "doc")], [tuple(x) for x in self.conn.execute(
            "SELECT path, origin FROM favorites")])

    def test_whole_article_star_is_untouched(self):
        """反向:整篇收藏那条老路一个字没变(它只对 doc 存在)。"""
        r = self.client.post("/api/favorites/toggle", json={"path": DOC})
        self.assertEqual(200, r.status_code, r.text)
        self.assertTrue(r.json()["favorited"])
        self.assertEqual([(DOC, "", "doc")], [tuple(x) for x in self.conn.execute(
            "SELECT path, quote, origin FROM favorites")])
        self.assertIn(DOC, self.listed()["doc_paths"])


class SnippetListTests(ApiFixture):
    """列出来的和计数牌上那个数必须是一回事。"""

    def seed_all(self):
        cid, _uid, aid = self.seed_chat()
        self.snip(path=DOC, quote="文中一句")
        self.snip(quote="日报一句", origin="daily", origin_ref="2026-10-15",
                  origin_title="日报 · 2026-10-15")
        self.snip(quote="回答一句", origin="chat", origin_ref=str(cid), origin_msg=aid,
                  origin_title="问答 · 有哪些视频模型")
        self.snip(quote="订阅一句", origin="feed", origin_ref="rss:abc",
                  origin_title="Some English Post", origin_url="https://e.org/x")

    def test_list_matches_counts(self):
        self.seed_all()
        d = self.listed()
        self.assertEqual(4, d["counts"]["inbox"])
        self.assertEqual(4, len(d["favorites"]),
                         "计数牌和列表对不上:没有 path 的条目被内连接吃掉了")

    def test_metadata_falls_back_to_the_snapshot(self):
        self.seed_all()
        by = {f["origin"]: f for f in self.listed()["favorites"]}
        self.assertEqual("日报 · 2026-10-15", by["daily"]["title"])
        self.assertEqual("2026-10-15", by["daily"]["origin_ref"])
        self.assertEqual("", by["daily"]["path"],
                         "path 为空要给空串:前端拿它拼 /doc/ 链接,null 会拼出 undefined")
        self.assertEqual("https://e.org/x", by["feed"]["url"], "站外链接该回落到快照")
        # 有文档的那一类照旧用索引里的最新元数据,不用快照
        self.assertEqual("活着的那篇", by["doc"]["title"])
        self.assertEqual("某号", by["doc"]["account"])

    def test_removing_the_document_only_takes_its_own_snippets(self):
        """反向:外键级联照旧管着有 path 的那些,没有 path 的一条都不许被带走。"""
        self.seed_all()
        self.conn.execute("DELETE FROM documents WHERE path=?", (DOC,))
        self.conn.commit()
        d = self.listed()
        self.assertEqual({"daily", "chat", "feed"},
                         {f["origin"] for f in d["favorites"]})
        self.assertEqual(3, d["counts"]["inbox"], "计数又和列表分叉了")

    def test_doc_paths_never_contains_null(self):
        """整篇收藏的 path 集合给前端当 Set 用。迁移置空过的孤儿行也满足
        `quote=''`,不排掉就会往这个数组里塞一个 null。"""
        self.conn.execute(
            "INSERT INTO favorites (path, quote, origin, origin_title,"
            " created_at, updated_at)"
            " VALUES (NULL, '', 'doc', '', '2026-01-01', '2026-01-01')")
        self.conn.commit()
        self.assertEqual([], self.listed()["doc_paths"])

    def test_status_flow_works_without_a_document(self):
        """反向:状态流转、改备注、删除这几条老路对没有 path 的条目一样管用。"""
        fid = self.snip(quote="日报一句", origin="daily", origin_ref="2026-10-15",
                        origin_title="日报").json()["id"]
        self.assertEqual(200, self.client.patch(
            f"/api/favorites/{fid}", json={"status": "done", "note": "试过了"}).status_code)
        done = self.listed("done")["favorites"]
        self.assertEqual(["试过了"], [f["note"] for f in done])
        self.assertEqual(204, self.client.delete(f"/api/favorites/{fid}").status_code)
        self.assertEqual(0, self.listed()["counts"]["done"])


class BrainTests(ApiFixture):
    """行动卡和分诊也得看见没有 path 的那些条目。"""

    def test_action_card_works_without_a_document(self):
        """内连接下这里一行都查不到 —— 行动卡静默不生成,面板上那一条永远只是
        一段引文。顺带钉住 prompt 里的「出处」不是一行光秃秃的标点。"""
        fid = self.snip(quote="按这个顺序装三个依赖就能跑起来", origin="daily",
                        origin_ref="2026-10-15", origin_title="日报 · 2026-10-15"
                        ).json()["id"]
        seen = {}

        def fake_chat(prompt, model, **kw):
            seen["prompt"] = prompt
            return '{"name":"本地索引","what":"不依赖外部服务","cmd":"","eta":"~10 分钟","kind":"try"}'

        with mock.patch.object(favbrain.llm, "chat", fake_chat), \
                mock.patch.object(favbrain.llm, "get_fav_model", lambda: "stub"):
            action = favbrain.extract_action(fid)
        self.assertIsNotNone(action, "没有 path 的收藏生成不出行动卡")
        self.assertEqual("本地索引", action["name"])
        self.assertEqual(action, json.loads(self.conn.execute(
            "SELECT action_json FROM favorites WHERE id=?", (fid,)).fetchone()[0]))
        self.assertIn("出处:每日日报", seen["prompt"],
                      "出处那一行空着,模型只能照着一行标点猜这段话是哪来的")
        self.assertIn("日报 · 2026-10-15", seen["prompt"])

    def test_action_card_still_reads_the_body_when_there_is_one(self):
        """反向:有文档的那一类照旧把正文喂进去(`_read_body` 的空 path 守卫不许
        把这条路一起短路)。"""
        # 夹具摆的那一篇就在 `self.vault`,这里只换掉它的正文(**换完版本号也
        # 变了** —— 落库那一道按盘上此刻那一份核,所以显式带新的那个)
        text = "---\ntitle: x\n---\n正文里有一句很特别的话。\n"
        (self.vault / DOC).write_text(text, encoding="utf-8")
        fid = self.snip(path=DOC, quote="文中一句",
                        doc_version=body_version(text)).json()["id"]
        seen = {}

        def fake_chat(prompt, model, **kw):
            seen["prompt"] = prompt
            return '{"name":"X"}'

        with mock.patch.object(favbrain, "VAULT_ROOT", self.vault), \
                mock.patch.object(favbrain.llm, "chat", fake_chat), \
                mock.patch.object(favbrain.llm, "get_fav_model", lambda: "stub"):
            favbrain.extract_action(fid)
        self.assertIn("正文里有一句很特别的话", seen["prompt"])
        self.assertIn("出处:某号", seen["prompt"], "有文档时出处该用索引里那个账号")

    def test_triage_sees_them_and_the_fingerprint_moves(self):
        """分诊的那批行就是指纹的来源:漏掉等于新收一条日报片段之后缓存仍判新鲜。"""
        self.snip(path=DOC, quote="文中一句")
        before = favbrain._fingerprint(favbrain._inbox_rows())
        self.snip(quote="日报一句", origin="daily", origin_ref="2026-10-15",
                  origin_title="日报 · 2026-10-15")
        rows = favbrain._inbox_rows()
        self.assertEqual(["活着的那篇", "日报 · 2026-10-15"], [r["title"] for r in rows],
                         "分诊看不见没有 path 的条目")
        self.assertNotEqual(before, favbrain._fingerprint(rows),
                            "收藏变了而指纹没动,分诊缓存会一直说自己新鲜")


if __name__ == "__main__":
    unittest.main()
