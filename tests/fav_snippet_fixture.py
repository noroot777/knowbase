# -*- coding: utf-8 -*-
"""「收藏此段」那组用例共用的摆盘:一个空的 v4 库 + 一篇真摆在盘上的文档。

两份用例文件按**验的是什么**分,共用这一份摆盘:
- `tests/test_fav_snippet_store.py` —— 存取两头(迁移、四类来源、列表与计数、
  行动卡与分诊、保存契约的四种拒绝)。
- `tests/test_fav_snippet_lock.py` —— 绑文档那一类的**临界区**(核验到提交之间
  换文件的那条路进不来、等不到锁如实拒)。

搬出来而不是让后者 import 前者:两边都得有摆盘,而 import 一个 TestCase 模块会
把它那几十条用例在这边再跑一遍(unittest 按模块收集)。摆盘自己不含任何判据。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi import FastAPI                                    # noqa: E402
from fastapi.testclient import TestClient                      # noqa: E402

from kb import favbrain, maintenance, store                    # noqa: E402
from kb.scanner import body_version                            # noqa: E402
from kb.web import common as web_common                        # noqa: E402
from kb.web import favorites_api                               # noqa: E402


#: 真的那个 `connect`。夹具把 `store.connect` 换成"默认连到这次的临时库",于是
#: `favorites_api` / `favbrain` 里那些不带参数的 `store.connect()` 全落在沙箱上。
#: patch 走 `mock.patch.object`,退出时自动还原 —— 不碰 `KB_DATA_DIR`,那个变量是
#: 整组测试共用的,打桩泄漏出去会带红别人。
REAL_CONNECT = store.connect

DOC = "source/dr-gzh/live.md"
GONE = "source/dr-gzh/gone.md"

#: 盘上那一篇的样子。**正文和 frontmatter 分开要紧**:版本号只按正文算,提取回写
#: 只动 frontmatter(补 tags、写 extracted_at)时收藏不该被误拒(用例在
#: `test_fav_snippet_store.py`)。
DOC_MD = """---
title: 活着的那篇
account: 某号
tags:
  - topic/skill工具
---

文中一句在这儿,后面还有一段。

那一句要先把索引建好,照着跑一遍。
"""


def seed_vault(test: unittest.TestCase, root: Path, text: str = DOC_MD) -> str:
    """盘上真摆一篇 `DOC`,把 `favorites_api.VAULT_ROOT` 指过去;回它的正文版本号。

    绑文档的收藏落库前要按**盘上此刻**那一份算一次版本号(`_doc_version_now`),
    所以这些用例不能只有 documents 里那一行 —— 没有文件的话一律 503。
    """
    full = root / DOC
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text(text, encoding="utf-8")
    # 打的是 `kb.web.common` 那一份:读文件和"读不出来怎么报"的口径收在那儿
    # (`read_doc_text`),收藏那一头只是它的消费方。
    patch = mock.patch.object(web_common, "VAULT_ROOT", root)
    patch.start()
    test.addCleanup(patch.stop)
    return body_version(text)


def snip_client(test: unittest.TestCase, db: Path) -> TestClient:
    """把 `store.connect` 指到这个库上,返回一个只挂收藏路由的 client。

    行动卡默认拦掉 —— 那是后台线程里的模型调用,验存取的用例不该碰它;
    真要验行动卡的那几条自己放开。
    """
    patch = mock.patch.object(
        store, "connect", lambda db_path=db: REAL_CONNECT(db_path))
    patch.start()
    test.addCleanup(patch.stop)
    quiet = mock.patch.object(favbrain, "extract_action_async", lambda i: None)
    quiet.start()
    test.addCleanup(quiet.stop)
    app = FastAPI()
    app.include_router(favorites_api.router)
    return TestClient(app)


class ApiFixture(unittest.TestCase):
    """一个空的 v4 库 + 一篇被索引过的文档,外加一个只挂收藏路由的 app。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="kb-fav-api-")
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / "kb.db"
        self.conn = REAL_CONNECT(self.db)
        self.addCleanup(self.conn.close)
        self.conn.execute(
            "INSERT INTO documents (path, source, title, account, published, url,"
            " summary, content_hash) VALUES (?, 'dr-gzh', '活着的那篇', '某号',"
            " '2026-10-01', 'https://e.org/live', '一句摘要', 'h')", (DOC,))
        self.conn.commit()
        self.vault = Path(self.temp.name) / "vault"
        self.version = seed_vault(self, self.vault)
        # 绑文档的那一类落库要取**维护锁**(`favorites_api._bind_and_insert`),
        # 锁文件在 `DATA_DIR` 下。打到这次的临时目录上:用真的那个 `data/` 时,
        # 本机正跑着的一轮同步/索引攥着同一把锁,这几条用例就会等到超时、拿一片
        # 503 变成假红(而且那是**别人的**锁,不是用例自己的事)。**不碰
        # `KB_DATA_DIR`**,那个变量整组测试共用。
        lock = mock.patch.object(maintenance, "DATA_DIR", Path(self.temp.name))
        lock.start()
        self.addCleanup(lock.stop)
        self.client = snip_client(self, self.db)

    def seed_chat(self, title="一轮问答"):
        """真落一轮问答(`conversations` 一行 + `messages` 两行),回
        `(会话 id, 提问那条的 id, 回答那条的 id)`。

        问答片段落库要核验「这条消息在不在、属不属于这个对话」
        (`favorites_api._chat_message_id`)—— 光有会话号不算,那只说明那个对话
        存在,而一轮问答是答完才整轮写进 `messages` 的。所以这几条用例得有真的
        消息行,不能只给一个会话号。
        """
        now = "2026-10-15 10:00"
        cid = self.conn.execute(
            "INSERT INTO conversations (title, created_at, updated_at)"
            " VALUES (?, ?, ?)", (title, now, now)).lastrowid
        uid = self.conn.execute(
            "INSERT INTO messages (conversation_id, role, content, created_at)"
            " VALUES (?, 'user', '有哪些视频模型', ?)", (cid, now)).lastrowid
        aid = self.conn.execute(
            "INSERT INTO messages (conversation_id, role, content, model, created_at)"
            " VALUES (?, 'assistant', '回答一句在这儿', 'stub/none', ?)",
            (cid, now)).lastrowid
        self.conn.commit()
        return cid, uid, aid

    def snip(self, **body):
        """绑文档的那几条**默认带上盘上此刻那一版的版本号**(前端那一头同口径:
        核对读到哪一版就回传哪一版)。要验契约本身的用例自己显式传。"""
        if body.get("path") and "doc_version" not in body:
            body["doc_version"] = self.version
        return self.client.post("/api/favorites", json=body)

    def listed(self, status="inbox"):
        r = self.client.get("/api/favorites", params={"status": status})
        self.assertEqual(200, r.status_code, r.text)
        return r.json()
