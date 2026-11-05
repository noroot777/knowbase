# -*- coding: utf-8 -*-
"""同步面板失败明细上的「从浏览器采集」按条目**此刻**的状态标(kb/pipeline_capture.py)。

事故形态:按钮原来是那一轮补抓失败时写进明细里的一个字段。升级之前跑的那一轮
(七条补抓失败)一颗按钮都没有,而人正对着那一屏找它 —— 明细是那一轮的事实,
「现在还能不能从浏览器补」是条目此刻的状态,冻进记录里两头都错。

判据落在**回包里那一条带不带 `capture`**,和「问题与待办」那颗按钮同一份判据
(`lanes.capturable`)。反过来钉住:补成之后、搁置之后、翻译失败、别的来源的身份、
没身份的那条,一律不带;冻在旧记录里的字段也要被摘掉。

脚手架复用 `tests/test_feed_fulltext.py` 的 `FullTextCase`(临时 registry)。
"""
import copy
import unittest
from unittest.mock import Mock, patch

from kb import pipeline_capture, pipeline_state
from kb.feeds import fulltext, registry as reg
from kb.feeds.exclusion import failure
from kb.feeds.fetching import FeedError
from kb.intake_diag import shelf
from tests.test_feed_fulltext import PAGE, FullTextCase, page


def run_of(failures):
    """一轮运行记录的形状:阶段卡片一份,结果账里逐来源一份(两处都要标)。"""
    return {'stages': {'export': {'status': 'error', 'failures': copy.deepcopy(failures)}},
            'results': {'export': {'rss': {'status': 'error',
                                           'failures': copy.deepcopy(failures)}}}}


def marks(run):
    return ([f.get('capture') for f in run['stages']['export']['failures']],
            [f.get('capture') for f in run['results']['export']['rss']['failures']])


class CaptureMarkTests(FullTextCase):

    def _fail_once(self, key, error=FeedError('这一页要求先完成验证或登录')):
        with patch.object(fulltext, 'request', Mock(side_effect=error)), \
                patch.dict(pipeline_state._state, stop_requested=False):
            summary = fulltext.fetch_pending('rss')
        self.assertEqual(1, summary['failed'])
        return summary['errors']

    def test_an_old_record_without_the_field_gets_the_button(self):
        """升级之前那一轮的明细里没有 `capture`,只要条目此刻还缺正文就照样认得出。"""
        key = self.put()
        errors = self._fail_once(key)
        self.assertNotIn('capture', errors[0])          # 记录里本来就没有这一项
        run = run_of(errors)
        pipeline_capture.mark_runs([run])
        self.assertEqual(([key], [key]), marks(run))

    def test_state_and_last_run_are_both_marked(self):
        key = self.put()
        errors = self._fail_once(key)
        state = {**run_of(errors), 'last_run': run_of(errors)}
        pipeline_capture.mark_state(state)
        self.assertEqual(([key], [key]), marks(state))
        self.assertEqual(([key], [key]), marks(state['last_run']))

    def test_the_button_goes_away_once_the_body_is_in(self):
        """反过来钉一边:正文补上了,同一份旧记录再问就不带,冻着的字段也摘掉。"""
        key = self.put()
        errors = self._fail_once(key)
        with patch.object(fulltext, 'request', Mock(return_value=page(PAGE))), \
                patch.dict(pipeline_state._state, stop_requested=False):
            self.assertEqual(1, fulltext.fetch_pending('rss')['done'])
        stale = [{**errors[0], 'capture': key}]
        run = run_of(stale)
        pipeline_capture.mark_runs([run])
        self.assertEqual(([None], [None]), marks(run))

    def test_a_shelved_item_does_not_offer_it(self):
        key = self.put()
        errors = self._fail_once(key)
        self.assertEqual([key], shelf.shelve([key], 'rss', shelf.SNOOZE)['keys'])
        run = run_of(errors)
        pipeline_capture.mark_runs([run])
        self.assertEqual(([None], [None]), marks(run))

    def test_a_translation_failure_on_a_full_body_does_not(self):
        """翻译失败缺的不是正文:单号源(没开补抓)上那条正文齐全的内容不带。"""
        reg.put_items(self.plain, [{'url': 'https://solo.example.org/a', 'title': 'Plain',
                                    'body': 'The runtime schedules tools. ' * 20,
                                    'published': '2026-10-08 09:00',
                                    'content_kind': 'summary'}])
        item = next(i for i in reg.items('rss')['items']
                    if i['url'] == 'https://solo.example.org/a')
        run = run_of([failure({'key': item['key'], 'url': item['url'], 'title': 'Plain'},
                              '上游 500', 'rss')])
        pipeline_capture.mark_runs([run])
        self.assertEqual(([None], [None]), marks(run))

    def test_identity_must_match_kind_and_be_present(self):
        """别的来源的档案 ID 撞上这条 key 不算;给不出身份的那一条也不长按钮。"""
        key = self.put()
        errors = self._fail_once(key)
        other = {**errors[0], 'drop': {**errors[0]['drop'], 'channel': 'wechat_local'}}
        bare = {'title': '没有身份的一条', 'reason': '缺少正文'}
        run = run_of([other, bare, errors[0]])
        pipeline_capture.mark_runs([run])
        self.assertEqual(([None, None, key], [None, None, key]), marks(run))


if __name__ == '__main__':
    unittest.main()
