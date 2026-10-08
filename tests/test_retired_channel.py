# -*- coding: utf-8 -*-
"""退役的公众号通道名:还得认得出来,而且要换成接替它的那条。

公众号曾经有两条通道二选一:`wechat_oa` 用逐号文章凭证(凭证靠 mitm 代理从绿泡泡
客户端流量里截),`wechat_local` 读本机已经同步下来的推送。两条抓的是同一批号、写
进同一套订阅和文章库,差别只在"从哪儿发现文章",所以主干只留了后一条,前一条整条
搬去 wechat-oa 分支。

删掉它不等于可以忘掉它的名字 —— 盘上那些"抓哪几个来源"的配置(定时范围、日报范围)
和浏览器 localStorage 里存的选择,写着的正是 `wechat_oa`。当成"不认识的通道"丢掉
的话,公众号从此不再定时同步、不再进日报,而面板上完全看不出少了什么。

钉住的几条:
1. 注册表里只剩一条公众号通道,它就是默认。
2. 旧名字映射到接替它的那条:取采集器、存量范围清单、定时范围、日报范围都要。
3. 旧名字仍然要能翻成人话(历史运行记录里写着的可能正是它)。
4. 别名表要供给浏览器那一侧(localStorage 里存着的选择后端 resolve 不到)。
5. 「添加一个公众号」走的是本机会话列表,不是搜公众平台。
6. 页面上同一件事由 tests/retired_channel_probe.mjs 真跑一遍。

跑:  python -m unittest tests.test_retired_channel -v
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from kb import collectors, daily_run, llm, pipeline  # noqa: E402
from tests.headless import probe_env, requires_headless  # noqa: E402
from kb.web import pipeline_api  # noqa: E402

RETIRED = "wechat_oa"
LIVE = "wechat_local"

# 页面那一侧的回归:probe 自带 mock 后端,CDP 直连 chrome-headless-shell
# (不装 playwright、不连网、始终无头、临时 profile)。怎么找那个二进制见
# tests/headless.py。
_PROBE = Path(__file__).resolve().parent / "retired_channel_probe.mjs"


class _Sandbox(unittest.TestCase):
    """两份范围配置都指到 tmp:不读也不改开发机上那份。"""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        original = llm._SECRETS_PATH
        llm._SECRETS_PATH = self.dir / "secrets.json"
        self.addCleanup(lambda: setattr(llm, "_SECRETS_PATH", original))


class RegistryTests(_Sandbox):

    def test_exactly_one_wechat_channel_and_it_is_the_default(self):
        offered = [c["id"] for c in collectors.collection_channels()]
        self.assertEqual([LIVE, "social", "rss"], offered)
        self.assertEqual(LIVE, collectors.active_collector().name)
        self.assertEqual(LIVE, collectors.wechat_channel())
        self.assertNotIn(RETIRED, collectors._REGISTRY)

    def test_the_retired_name_resolves_instead_of_raising(self):
        """显式点名旧通道的那些入口(老脚本、老页面、KB_COLLECTOR)不该当场炸。"""
        self.assertEqual(LIVE, collectors.resolve_channel(RETIRED))
        self.assertEqual(LIVE, collectors.get_collector(RETIRED).name)
        self.assertEqual(LIVE, collectors.active_collector(RETIRED).name)
        # 真·不认识的名字照旧要拒
        with self.assertRaises(ValueError):
            collectors.get_collector("没这个通道")

    def test_a_stored_scope_keeps_wechat_and_never_doubles_it(self):
        # 更早的版本两条都能勾,存量里可能同时写着它们 —— 映射到同一条之后要去重
        self.assertEqual([LIVE, "rss"],
                         collectors.known_channels([RETIRED, LIVE, "没这个通道", "rss"]))
        self.assertEqual([LIVE], collectors.known_channels([RETIRED]))

    def test_the_retired_name_still_has_a_human_label(self):
        """上次运行记录里写着的可能正是它,面板不该显示成一个裸 id。"""
        labels = collectors.channel_labels()
        self.assertIn(RETIRED, labels)
        self.assertEqual(labels[LIVE], labels[RETIRED])
        self.assertFalse(labels[RETIRED].startswith("wechat"))

    def test_the_alias_table_is_handed_to_the_browser(self):
        """localStorage 里存的选择后端 resolve 不到,前端也不该自己写死旧名字。"""
        self.assertEqual({RETIRED: LIVE}, collectors.retired_channels())
        data = pipeline_api.pipeline_channels()
        self.assertEqual({RETIRED: LIVE}, data["aliases"])
        # 拿到的是副本:外面改一笔不该动到模块里那张表
        collectors.retired_channels()["x"] = "y"
        self.assertEqual({RETIRED: LIVE}, collectors.retired_channels())

    def test_adding_an_account_reads_local_sessions_not_the_platform(self):
        """加号走 `accounts discover`(翻本机会话,不登录、不走网络)。

        写成 `search` 的话又回到要一份全局扫码凭证,而那正是这一轮去掉的依赖。
        """
        collector = collectors.get_collector(LIVE)
        with patch.object(type(collector), "_accounts",
                          return_value={"candidates": []}) as called:
            self.assertEqual([], collector.search_targets("关键词"))
        self.assertEqual(("discover", "关键词"), called.call_args.args)


class ScopeTests(_Sandbox):
    """存量的「抓哪几个来源」配置要跟着别名表走,不能被当成不认识的通道丢掉。"""

    def test_schedule_scope_keeps_wechat_under_its_new_name(self):
        path = self.dir / "schedule.json"
        path.write_text(json.dumps({"enabled": True, "time": "07:30",
                                    "channels": [RETIRED, "rss"]}), encoding="utf-8")
        with patch.object(pipeline, "_SCHED_FILE", path):
            loaded = pipeline.load_schedule()
            self.assertEqual([LIVE, "rss"], loaded["channels"])
            # 老的读取方只认单个 channel,它得是现在那条而不是盘上那个旧名字
            self.assertEqual(LIVE, loaded["channel"])
            # 存进去也一样:旧名字落盘成新的,否则定时那一轮会绕开它去跑一个不存在的通道
            pipeline.save_schedule(True, "07:30", [RETIRED, "rss"])
            self.assertEqual([LIVE, "rss"], json.loads(
                path.read_text(encoding="utf-8"))["channels"])

    def test_schedule_read_survives_a_channel_that_no_longer_exists(self):
        path = self.dir / "schedule.json"
        path.write_text(json.dumps({"enabled": True, "time": "07:30",
                                    "channels": ["没这个通道", "rss"]}), encoding="utf-8")
        with patch.object(pipeline, "_SCHED_FILE", path):
            self.assertEqual(["rss"], pipeline.load_schedule()["channels"])

    def test_daily_scope_keeps_wechat_under_its_new_name(self):
        path = self.dir / "daily.json"
        path.write_text(json.dumps({"enabled": True, "time": "08:00",
                                    "channels": [RETIRED, "rss"]}), encoding="utf-8")
        with patch.object(daily_run, "SETTINGS_FILE", path):
            self.assertEqual([LIVE, "rss"], daily_run.load_settings()["channels"])
            self.assertEqual(LIVE, daily_run.default_channels()[0])
            # 保存时送进来的旧名字同样落成新的
            self.assertEqual([LIVE], daily_run.save_settings(
                False, "08:00", [RETIRED])["channels"])


class ChannelApiTests(_Sandbox):

    def setUp(self):
        super().setUp()
        app = FastAPI()
        app.include_router(pipeline_api.router)
        self.client = TestClient(app)

    def test_channels_endpoint_offers_one_wechat_and_labels_the_retired_one(self):
        data = self.client.get("/api/pipeline/channels").json()
        self.assertEqual(LIVE, data["default"])
        self.assertEqual([LIVE, "social", "rss"], [c["id"] for c in data["channels"]])
        self.assertNotIn(RETIRED, [c["id"] for c in data["channels"]])
        self.assertIn(RETIRED, data["labels"])
        self.assertEqual({RETIRED: LIVE}, data["aliases"])
        # 每条候选都要带说明,不然同步范围那几行只有名字、看不出各自抓什么
        self.assertTrue(all(c["hint"] for c in data["channels"]))

    def test_the_channel_setting_endpoint_is_gone(self):
        """二选一没有了,那个设置端点也不该留着(留着就能存进一个不存在的通道)。"""
        r = self.client.post("/api/pipeline/channels/wechat", json={"channel": RETIRED})
        self.assertEqual(404, r.status_code)


@requires_headless
class BrowserTests(unittest.TestCase):
    """浏览器里存着的旧通道名真的会被带到现役那条上,公众号不会静默消失。"""

    def test_a_stored_retired_name_still_syncs_wechat(self):
        r = subprocess.run([shutil.which("node"), str(_PROBE)],
                           capture_output=True, text=True, timeout=300,
                           env=probe_env())
        self.assertEqual(0, r.returncode, r.stdout + r.stderr)


if __name__ == "__main__":
    unittest.main()
