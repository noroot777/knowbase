# -*- coding: utf-8 -*-
"""绿泡泡采集通道:两条路互斥,选哪条是设置,不是每次同步现勾的范围。

`wechat_oa`(逐号文章凭证)和 `wechat_local`(本机已同步的推送)抓的是同一批
公众号、写进同一套订阅和文章库,差别只在"从哪儿发现文章"。两条同时勾上只会
把同一批号抓两遍,所以它们从「同步范围」里撤出来,变成设置里的一个二选一。

钉住的几条:
1. **默认是本机数据库**,而且没被选中的那条压根不出现在可选来源里。
2. **没被选中的那条仍然要有人话名字**:历史运行记录里写着的可能正是它,
   面板不该把「上次来源」显示成一个裸 id。
3. **存量配置里的旧通道名跟着设置走**(定时范围、日报范围)。当成"不认识的
   通道"丢掉的话,公众号从此不再同步,而面板上完全看不出少了什么。
4. **显式点名另一条通道仍然有效**:设置管的是默认和面板,不该把
   `--channel wechat_oa` 这条排查路子堵死。
5. 设置真落盘、认不出的值被拒。

跑:  python -m unittest tests.test_wechat_channel -v
"""

from __future__ import annotations

import json
import os
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
from kb.web import pipeline_api  # noqa: E402

# 页面那一侧的回归:probe 自带 mock 后端,CDP 直连 playwright 缓存里的
# chrome-headless-shell(不装 playwright、不连网、始终无头、临时 profile)。
_PROBE = Path(__file__).resolve().parent / "wechat_channel_probe.mjs"
_SHELL = (Path.home() / "Library/Caches/ms-playwright/chromium_headless_shell-1243"
          / "chrome-headless-shell-mac-arm64" / "chrome-headless-shell")


class _Sandbox(unittest.TestCase):
    """secrets.json(设置)和两份范围配置都指到 tmp:不读也不改开发机上那份。"""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        original = llm._SECRETS_PATH
        llm._SECRETS_PATH = self.dir / "secrets.json"
        self.addCleanup(lambda: setattr(llm, "_SECRETS_PATH", original))

    def secrets(self) -> dict:
        return json.loads(llm._SECRETS_PATH.read_text(encoding="utf-8"))


class SettingTests(_Sandbox):

    def test_default_is_the_local_database_and_survives_a_junk_value(self):
        self.assertEqual("wechat_local", collectors.wechat_channel())
        self.assertEqual("wechat_local", collectors.active_collector().name)
        for junk in ({}, {"collectors": {}}, {"collectors": {"wechat": "nope"}},
                     {"collectors": "不是个对象"}):
            with self.subTest(junk=junk):
                llm._SECRETS_PATH.write_text(json.dumps(junk), encoding="utf-8")
                self.assertEqual("wechat_local", collectors.wechat_channel())

    def test_saving_a_channel_persists_it_and_leaves_the_rest_of_the_file_alone(self):
        llm._SECRETS_PATH.write_text(json.dumps({"openai": {"key": "sk-o"}}),
                                     encoding="utf-8")
        collectors.set_wechat_channel("wechat_oa")
        self.assertEqual("wechat_oa", collectors.wechat_channel())
        self.assertEqual({"wechat": "wechat_oa"}, self.secrets()["collectors"])
        # 设置落在 secrets.json 里(那儿早就不只装 key),别把同住的东西写没了
        self.assertEqual({"key": "sk-o"}, self.secrets()["openai"])
        collectors.set_wechat_channel("wechat_local")
        self.assertEqual("wechat_local", collectors.wechat_channel())

    def test_unknown_channel_is_refused_instead_of_being_stored(self):
        for bad in ("", "rss", "wechat", "wechat_oa2"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                collectors.set_wechat_channel(bad)
        self.assertEqual("wechat_local", collectors.wechat_channel())

    def test_only_the_selected_channel_is_offered_but_both_keep_a_label(self):
        for chosen, hidden in (("wechat_local", "wechat_oa"),
                               ("wechat_oa", "wechat_local")):
            with self.subTest(chosen=chosen):
                collectors.set_wechat_channel(chosen)
                offered = [c["id"] for c in collectors.collection_channels()]
                self.assertIn(chosen, offered)
                self.assertNotIn(hidden, offered)
                # 订阅类来源一个都不能被这次过滤带走
                self.assertEqual(["social", "rss"], offered[1:])
                # 没被选中的那条仍然要能翻成人话:上次运行记录里写的可能正是它
                labels = collectors.channel_labels()
                self.assertTrue(labels[hidden] and not labels[hidden].startswith("wechat"))
                self.assertEqual(2, len(collectors.wechat_channel_options()))

    def test_explicitly_naming_the_other_channel_still_works(self):
        """设置只决定默认和面板上列哪条,不该把一次性排查也堵死。"""
        collectors.set_wechat_channel("wechat_local")
        self.assertEqual("wechat_oa", collectors.get_collector("wechat_oa").name)
        self.assertEqual("wechat_oa", collectors.active_collector("wechat_oa").name)

    def test_stored_names_follow_the_setting_and_unknown_ones_are_dropped(self):
        collectors.set_wechat_channel("wechat_local")
        self.assertEqual("wechat_local", collectors.resolve_channel("wechat_oa"))
        self.assertEqual("rss", collectors.resolve_channel("rss"))
        # 两条都写在存量里(旧版本能勾出这种组合)时不能变成抓两遍
        self.assertEqual(["wechat_local", "rss"],
                         collectors.known_channels(["wechat_oa", "wechat_local",
                                                    "没这个通道", "rss"]))
        collectors.set_wechat_channel("wechat_oa")
        self.assertEqual(["wechat_oa"], collectors.known_channels(["wechat_local"]))

    def test_each_channel_finds_candidates_its_own_way(self):
        """「添加一个公众号」两条通道走的不是同一条路,这件事只由一个类属性决定。

        本地通道从本机会话列表里找(`accounts discover`,不需要扫码凭证、不走
        网络);原通道搜公众平台(`accounts search`,要一份全局扫码凭证)。两边
        共用 `search_targets` 的解析,所以这里钉的是"它到底跑了哪个子命令"——
        写死成 search 的话本地通道加号又得去登录,而那正是要去掉的那条依赖。
        """
        for channel, action in (("wechat_local", "discover"), ("wechat_oa", "search")):
            collector = collectors.get_collector(channel)
            with patch.object(type(collector), "_accounts",
                              return_value={"candidates": []}) as called:
                self.assertEqual([], collector.search_targets("关键词"))
            self.assertEqual((action, "关键词"), called.call_args.args)


class ScopeTests(_Sandbox):
    """存量的「抓哪几个来源」配置要跟着设置走,不能被当成不认识的通道丢掉。"""

    def test_schedule_scope_switches_with_the_setting_instead_of_losing_wechat(self):
        path = self.dir / "schedule.json"
        with patch.object(pipeline, "_SCHED_FILE", path):
            collectors.set_wechat_channel("wechat_oa")
            pipeline.save_schedule(True, "07:30", ["wechat_oa", "rss"])
            self.assertEqual(["wechat_oa", "rss"], pipeline.load_schedule()["channels"])
            collectors.set_wechat_channel("wechat_local")
            loaded = pipeline.load_schedule()
            self.assertEqual(["wechat_local", "rss"], loaded["channels"])
            # 老的读取方只认单个 channel,它得是新通道而不是盘上那个旧名字
            self.assertEqual("wechat_local", loaded["channel"])

    def test_schedule_read_survives_a_channel_that_no_longer_exists(self):
        path = self.dir / "schedule.json"
        path.write_text(json.dumps({"enabled": True, "time": "07:30",
                                    "channels": ["没这个通道", "rss"]}), encoding="utf-8")
        with patch.object(pipeline, "_SCHED_FILE", path):
            self.assertEqual(["rss"], pipeline.load_schedule()["channels"])

    def test_daily_scope_switches_with_the_setting(self):
        path = self.dir / "daily.json"
        path.write_text(json.dumps({"enabled": True, "time": "08:00",
                                    "channels": ["wechat_oa", "rss"]}), encoding="utf-8")
        with patch.object(daily_run, "SETTINGS_FILE", path):
            collectors.set_wechat_channel("wechat_local")
            self.assertEqual(["wechat_local", "rss"],
                             daily_run.load_settings()["channels"])
            # 默认范围里的绿泡泡那条也是设置里选中的
            self.assertEqual("wechat_local", daily_run.default_channels()[0])
            collectors.set_wechat_channel("wechat_oa")
            self.assertEqual(["wechat_oa", "rss"],
                             daily_run.load_settings()["channels"])
            self.assertEqual("wechat_oa", daily_run.default_channels()[0])

    def test_daily_save_accepts_the_other_channel_by_storing_the_selected_one(self):
        path = self.dir / "daily.json"
        with patch.object(daily_run, "SETTINGS_FILE", path):
            collectors.set_wechat_channel("wechat_local")
            saved = daily_run.save_settings(False, "08:00", ["wechat_oa"])
            self.assertEqual(["wechat_local"], saved["channels"])


class ChannelApiTests(_Sandbox):

    def setUp(self):
        super().setUp()
        app = FastAPI()
        app.include_router(pipeline_api.router)
        self.client = TestClient(app)

    def test_channels_endpoint_reports_the_choice_and_every_label(self):
        data = self.client.get("/api/pipeline/channels").json()
        self.assertEqual("wechat_local", data["default"])
        self.assertEqual("wechat_local", data["wechat"]["selected"])
        self.assertEqual(["wechat_local", "wechat_oa"],
                         [c["id"] for c in data["wechat"]["options"]])
        self.assertNotIn("wechat_oa", [c["id"] for c in data["channels"]])
        self.assertIn("wechat_oa", data["labels"])
        # 候选上要带着各自的说明,不然面板上那两行只有名字、看不出差别
        self.assertTrue(all(c["hint"] for c in data["wechat"]["options"]))

    def test_posting_a_channel_switches_the_offered_sources_in_one_round_trip(self):
        data = self.client.post("/api/pipeline/channels/wechat",
                                json={"channel": "wechat_oa"}).json()
        self.assertEqual("wechat_oa", data["wechat"]["selected"])
        self.assertEqual("wechat_oa", data["default"])
        self.assertEqual("wechat_oa", [c["id"] for c in data["channels"]][0])
        self.assertNotIn("wechat_local", [c["id"] for c in data["channels"]])
        self.assertEqual("wechat_oa", collectors.wechat_channel())

    def test_posting_an_unknown_channel_is_rejected_and_changes_nothing(self):
        for bad in ("rss", "", "wechat"):
            with self.subTest(bad=bad):
                r = self.client.post("/api/pipeline/channels/wechat", json={"channel": bad})
                self.assertEqual(400, r.status_code)
        self.assertEqual("wechat_local", collectors.wechat_channel())


@unittest.skipUnless(_SHELL.exists() and shutil.which("node"),
                     "需要 node + playwright 缓存里的 chrome-headless-shell")
class BrowserTests(unittest.TestCase):
    """页面上那几处「同步范围」真的只剩一条绿泡泡通道,设置里换完各处当场跟着换。"""

    def test_panels_offer_exactly_one_wechat_channel(self):
        r = subprocess.run([shutil.which("node"), str(_PROBE)],
                           capture_output=True, text=True, timeout=300,
                           env={**os.environ, "HEADLESS_SHELL": str(_SHELL)})
        self.assertEqual(0, r.returncode, r.stdout + r.stderr)


if __name__ == "__main__":
    unittest.main()
