# -*- coding: utf-8 -*-
"""**页面上勾的那几条、真跑的那几条、记成功的那几条,三个集合必须对得上。**

第 1 轮自由审查 R1 的形态:订阅里有一条名字是纯数字(比如名字叫「63」,它自己的
编号是 64),在同步中心只勾这一条,开始后编号 63 的另一条也被抓了 —— 面板上写着
「已选 1 / 3」。来历是范围目标原来是个**无类型的字符串空间**:同一个 `"63"` 既可能
是某条订阅的编号、也可能是另一条的名字,而匹配两种都认。反方向同形:点某一行那颗
「同步」,同名的另一条被一起抓(第 1 轮审查 R1/R2)。

判据落在**三个集合**上,而且是**真走产品那条路**攒出来的:请求过
`/api/pipeline/run` 自己那套解码(`_targets`),抓取过 `_run_crawl` → `sync.collect`
的选源,成功覆盖读**落盘的**当日账本。只验中间那个匹配函数的话,「匹配严了、而请求
入口照旧把名字和编号混在一个名单里发」这种实现一条都红不出来 —— 那正是上一轮的残留
形态(匹配那一侧已经分了编号,请求那一侧还在发无类型名单)。

**反过来钉三边**:①同名那几条照旧一起跑(名单上那一行代表的就是它们,收窄成一条就
是静默漏抓);②失败那一条照旧只记它自己的红(成功覆盖不许超出真实成功集合);
③缓存着的老页面发的那份无类型名单照旧两种都认 —— 判不准那一档宁可多抓一次(重新
加载页面就走新字段),静默一条都不抓才是救不回来的那一种。

匹配口径本身那一组在 `tests/test_feed_targets.py`,请求入口的形状在
`tests/test_sync_channels.py`,真实页面点哪个字段在 `tests/pipe_entry_probe.mjs`。

跑:  uv run --offline python -m unittest tests.test_target_selection -v
"""
from __future__ import annotations

import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from kb import pipeline
from kb.feeds import registry as reg
from kb.web import pipeline_api
from tests.daybook_case import DaybookCase


class WhatThePageTickedIsWhatRunsTests(DaybookCase):
    def setUp(self):
        super().setUp()
        app = FastAPI()
        app.include_router(pipeline_api.router)
        self.client = TestClient(app)

    # ── 摆盘 ──────────────────────────────────────────────────────────────────

    def rename(self, source, name):
        """给这条订阅改个名(地址不动,免得触发「换了链接 = 从头开始」那一路)。"""
        return reg.save_subscription('rss', {'name': name, 'url': source['url']},
                                     source['id'])

    def requested(self, **fields):
        """真发一次 `/api/pipeline/run`,返回它整理出来的那份抓取范围。

        起跑那一步打桩掉:它另起一个线程把点名的几个阶段跑一遍,而这一组问的是
        「页面发的那一份怎么被解成身份」和「照它跑一轮会抓谁」。中间那段编排不是
        这一组的被测对象,所以把请求那一头和真跑那一头**直接接起来** —— 请求整理
        出来的范围原样喂给 `self.crawl`,中间不许有人替它改写。
        """
        with patch.object(pipeline, 'start_pipeline', return_value=True) as start:
            r = self.client.post('/api/pipeline/run',
                                 json={'stages': ['crawl'],
                                       'channels': [{'channel': 'rss', **fields}]})
        self.assertEqual(200, r.status_code, r.text)
        return start.call_args.kwargs['crawl_filter']

    def ran(self, broken=(), **fields):
        """页面照 `fields` 发一次请求,真跑一轮,返回(真抓了谁, 账本那几格)。"""
        got, _ = self.crawl(self.requested(**fields), broken=broken)
        return got, self.book_cells('crawl', 'rss')

    # ── 1. 名单上勾的那一行 ────────────────────────────────────────────────────

    def test_ticking_the_row_whose_name_is_another_subscriptions_number(self):
        """名字叫「A 的编号」那一行:只跑它,**编号 A 那一条一个请求都不发**。"""
        self.b = self.rename(self.b, str(self.a['id']))
        got, cells = self.ran(names=[self.b['name']])
        self.assertEqual({self.b['id']}, got,
                         '页面只勾了一条,真跑的却不是这一条(或者多跑了)')
        self.assertEqual({str(self.b['id']): 'ok'}, cells,
                         '成功覆盖超出了真实成功的那一条')

    def test_the_name_group_still_runs_every_subscription_sharing_that_name(self):
        """**反过来钉一边**:同名那两条照旧一起跑。

        名单是按名字去重过的(`/api/pipeline/accounts` 只给名字),那一行代表的就是
        它们 —— 收窄成「只跑其中某一条」的话,勾「同名博客」会漏掉另一条,而页面上
        看不出少跑了谁。
        """
        self.b = self.rename(self.b, self.a['name'])
        got, cells = self.ran(names=[self.a['name']])
        self.assertEqual({self.a['id'], self.b['id']}, got, '同名那一组没跑全')
        self.assertEqual({str(self.a['id']): 'ok', str(self.b['id']): 'ok'}, cells)

    def test_a_failure_in_the_group_is_booked_on_that_one_only(self):
        """**成功覆盖不许超出真实成功集合**:同名两条里坏的那条只记它自己的红。

        漏掉这一道的后果在下一轮:那条订阅的欠账被另一条的成功清掉,它从此不进
        「重试失败项」—— 而页面上这一步显示已完成(静默丢数据)。
        """
        self.b = self.rename(self.b, self.a['name'])
        got, cells = self.ran(names=[self.a['name']], broken=(self.b,))
        self.assertEqual({self.a['id'], self.b['id']}, got)
        self.assertEqual({str(self.a['id']): 'ok', str(self.b['id']): 'error'}, cells)

    # ── 2. 入口点名的那一条 ────────────────────────────────────────────────────

    def test_an_entry_that_points_at_one_number_runs_only_that_number(self):
        """按编号点名:只跑那个编号,**名字正好长得像那个目标的另一条也不算**。

        这就是上一轮 R1 残留的那一半(实测请求 `accounts=["id:63"]`,真跑了 63 和
        64 两条)。要点名「名字叫 id:63」的那一条得走名单那一侧,两种写法各有入口。
        """
        self.b = self.rename(self.b, reg.id_target(self.a['id']))
        got, cells = self.ran(ids=[str(self.a['id'])])
        self.assertEqual({self.a['id']}, got, '按编号点名却跑了别的订阅')
        self.assertEqual({str(self.a['id']): 'ok'}, cells)

    def test_that_oddly_named_row_is_still_reachable_from_the_list(self):
        """**反过来钉一边**:名字叫 `id:…` 的那一行,在名单里勾它就跑它。

        严格化不许把它变成「点不到的订阅」—— 那是另一种静默漏抓:页面上勾着一行,
        跑完一条都没抓,而面板上一个字都不说。
        """
        self.b = self.rename(self.b, reg.id_target(self.a['id']))
        got, cells = self.ran(names=[self.b['name']])
        self.assertEqual({self.b['id']}, got)
        self.assertEqual({str(self.b['id']): 'ok'}, cells)

    # ── 3. 缓存着的老页面 ──────────────────────────────────────────────────────

    def test_an_old_tab_sends_one_untyped_list_and_still_matches_both_ways(self):
        """老页面发的是**一个无类型名单**(`accounts`),照旧两种都认。

        它分不清名字和编号,所以后端也分不清 —— 这一档是真的**判不准**,而判不准
        宁可多抓一次(重新加载页面就走新字段),静默一条都不抓才是救不回来的那种。
        新页面一个字都不发这个字段,所以这条兼容碰不到上面那几局。
        """
        self.b = self.rename(self.b, str(self.a['id']))
        got, _ = self.ran(accounts=[str(self.a['id'])])
        self.assertEqual({self.a['id'], self.b['id']}, got,
                         '老页面那份名单的兼容被改掉了')


if __name__ == '__main__':
    unittest.main()
