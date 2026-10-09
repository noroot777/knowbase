# -*- coding: utf-8 -*-
"""「重试失败项」那几组回归共用的摆盘:临时 feeds 库 + 临时管道状态。

单独成文件是因为两个测试文件(单来源那组 / 多来源与面板那组)都要用它 ——
各写一份的话两边的前提会各漂各的。背景见 `tests/test_retry_scope.py` 的注释。
"""
from __future__ import annotations

import tempfile
import time
import unittest
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from kb import maintenance, pipeline_retry, pipeline_stages, pipeline_state
from kb.feeds import registry as reg, sync
# 直接从它自己的模块导:`sync.fetching` 这个属性压根不存在(sync 是
# `from kb.feeds.fetching import ...`),写成那样抛的是 AttributeError ——
# 范围照样收窄得出来,可"抓取失败"这个前提是假的(第 1 轮审查发现)。
from kb.feeds.fetching import FeedError

# 样本日期跟着今天走:新订阅只收最近 `reg.HISTORY_DAYS` 天(见
# `sync._within_history`),写死一个日期的样本过几天就被那道闸挡住,而红的是
# 一堆跟重试范围毫无关系的用例。
_DAY = (datetime.now(timezone.utc) - timedelta(days=1)).astimezone(
    ZoneInfo('Asia/Shanghai')).date().isoformat()


def _entry(url: str) -> dict:
    return {'url': url, 'title': 'post', 'body': 'body text', 'author': '',
            'published': f'{_DAY} 08:00', 'kind': 'article'}


class RoundBase(unittest.TestCase):
    """摆盘:临时 feeds 库 + 临时管道状态,真跑真落盘。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        # 维护锁的 lockfile 和管道状态文件都在 DATA_DIR 下:不指到 tmp 的话这些
        # 用例会去 flock 生产那一个(被 kb-web 正在跑的一轮索引挂住),而
        # `_end_run` 还会把 last_run 写进生产的 pipeline_state.json。
        for p in [patch.object(reg, 'DB_PATH', self.root / 'feeds.db'),
                  patch.object(maintenance, 'DATA_DIR', self.root),
                  patch.object(pipeline_state, '_STATE_FILE',
                               self.root / 'pipeline_state.json'),
                  patch.object(sync, 'VAULT_ROOT', self.root / 'vault')]:
            p.start(); self.addCleanup(p.stop)
        self.saved = deepcopy(pipeline_state._state)
        pipeline_state._state.update(running=False, stop_requested=False,
                                     crawl_retry={})
        self.addCleanup(lambda: pipeline_state._state.update(self.saved))

    def _begin(self, crawl_filter, kind='manual', stages=('crawl',)):
        """照 `pipeline.start_pipeline` 的样子摆一轮运行的骨架。"""
        pipeline_state._state.update({
            'running': True, 'stop_requested': False, 'stopped': False,
            'run_id': pipeline_state.new_run_id(), 'log': [],
            'started_at': pipeline_state._now(), 'finished_at': '',
            'crawl_filter': deepcopy(crawl_filter), 'run_kind': kind,
            'current_stage': '', 'planned_stages': list(stages),
            'stages': {s: {'status': 'pending' if s in stages else 'skipped',
                           'detail': '' if s in stages else '本次未选择'}
                       for s in pipeline_state.STAGES},
        })

    def _wait_idle(self, why='那一轮没有收尾'):
        deadline = time.monotonic() + 30
        while pipeline_state._state['running'] and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertFalse(pipeline_state._state['running'], why)

    def stage(self, name='crawl'):
        return deepcopy(pipeline_state._state['stages'][name])


class RetryScopeBase(RoundBase):
    """三条 rss 订阅(A/B/C),默认订阅全删掉,好让"全部"数得清。"""

    def setUp(self):
        super().setUp()
        for s in reg.subscriptions('rss'):
            reg.delete_subscription('rss', s['id'])
        self.a = reg.save_subscription('rss', {'name': 'A', 'url': 'https://a.org/feed'})
        self.b = reg.save_subscription('rss', {'name': 'B', 'url': 'https://b.org/feed'})
        self.c = reg.save_subscription('rss', {'name': 'C', 'url': 'https://c.org/feed'})

    # ── 跑一轮采集,返回这一轮哪些订阅真的被抓了 ──────────────────────────

    def crawl(self, crawl_filter, broken=()):
        """真跑一轮 crawl,`broken` 里那几条订阅让 fetch 抛错。

        返回 (被抓到的订阅 id 集合, crawl 阶段的终态)。打桩的只有最外面那次
        网络请求 —— `collect` 的选源、失败名单、退避判定一行都不打桩。

        **收尾那一步(`_end_run`)不能省**:`start_retry_failed` 读的是**落盘的**
        last_run(失败名单也在里面),不是进程内那份状态。少了它,下一步点重试只会
        收到"上次运行没有失败项",这一局的前提压根没建立起来。
        """
        got: set[int] = set()
        self._begin(crawl_filter)
        with patch.object(sync, 'fetch_source',
                          side_effect=self._fetcher(got, broken, 'p1')):
            pipeline_stages._run_crawl(crawl_filter)
        pipeline_state._end_run()
        return got, self.stage()

    def _fetcher(self, got, broken, tag):
        bad = {s['id'] for s in broken}

        def fetch(source, *a, **kw):
            got.add(source['id'])
            if source['id'] in bad:
                raise FeedError('地址错了')
            return ([_entry(source['url'] + '/' + tag)],
                    {'etag': tag, 'modified': '', 'resolved_url': source['url']})
        return fetch

    def retry(self, broken=()):
        """点一次「重试失败项」,真走 `start_retry_failed` 造范围那条路。

        非 crawl 的几个阶段打桩掉(它们各自几分钟、和范围无关);**crawl 这条链
        一行不打桩** —— 打桩成成功正好绕开唯一要验的那件事。
        """
        got: set[int] = set()
        with patch.object(sync, 'fetch_source',
                          side_effect=self._fetcher(got, broken, 'p2')), \
             patch.object(pipeline_retry, '_run_export'), \
             patch.object(pipeline_retry, '_run_index'), \
             patch.object(pipeline_retry, '_run_extract'), \
             patch.object(pipeline_retry, '_run_projects'), \
             patch.object(pipeline_retry, '_run_wiki'):
            ok, msg = pipeline_retry.start_retry_failed()
            self.assertTrue(ok, msg)
            self._wait_idle('重试那一轮没有收尾')
        return got, self.stage()

    def refuse(self):
        """点「重试失败项」而它拒绝起跑:返回那句话,并确认一个请求都没发。"""
        with patch.object(sync, 'fetch_source',
                          side_effect=AssertionError('不该发出任何请求')):
            ok, msg = pipeline_retry.start_retry_failed()
        self.assertFalse(ok, f'这一局本该拒绝起跑,实际起跑了:{msg}')
        return msg

    def last_error(self, source):
        return reg.subscription('rss', source['id'])['last_error']

    def succeeded_later(self, source):
        """模拟"记下名单之后,它在别处成功同步过一次"(单条同步、命令行…)。

        时刻要比名单那一刻**严格新**,而名单是这一轮收尾时记的 —— 所以这里写一个
        一分钟之后的时间戳,而不是 `reg.now()`(同一秒分不出先后,按"还要重试"算)。
        """
        later = (datetime.now(timezone.utc) + timedelta(minutes=1)
                 ).isoformat(timespec='seconds')
        with reg.db() as c:
            c.execute('UPDATE subscriptions SET last_success=? WHERE id=?',
                      (later, source['id']))

    def urls(self, source):
        """这条订阅库里现有的条目 url(用来确认"真的按新地址抓过")。"""
        with reg.db() as c:
            return [r[0] for r in c.execute(
                '''SELECT i.url FROM feed_items i JOIN subscription_items si
                   ON si.item_key=i.key WHERE si.subscription_id=?''',
                (source['id'],)).fetchall()]

