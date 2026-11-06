# -*- coding: utf-8 -*-
"""同步面板失败明细上的「从浏览器采集」按条目**此刻**的状态标(kb/pipeline_capture.py)。

事故形态:按钮原来是那一轮补抓失败时写进明细里的一个字段。升级之前跑的那一轮
(七条补抓失败)一颗按钮都没有,而人正对着那一屏找它 —— 明细是那一轮的事实,
「现在还能不能从浏览器补」是条目此刻的状态,冻进记录里两头都错。

判据落在**回包里那一条带不带 `capture`**,和「问题与待办」那颗按钮同一份判据
(`lanes.capturable`)。反过来钉住:补成之后、搁置之后、翻译失败、别的来源的身份、
没身份的那条,一律不带;冻在旧记录里的字段也要被摘掉。

第二件事(`HandFilledTests`):**补抓失败说的那一版正文、后来被人亲手补的正文取代了,那一条
从明细里拿掉。** 实测形态:采集抽屉补完 OpenAI 那篇,按钮消失了、整行还在,旁边只剩一颗
「不再抓取」—— 指着一篇刚补好正文的文章让人永久删掉。判据落在**回包里还有没有那一行、明细
总数、那一行还带不带 `drop`**。判据是版本不是时刻:同步还在跑时补好的、收尾同一秒补好的
一样拿掉。反过来钉住:导出 / 翻译那几类失败补正文不替它们办完,行和原因照旧在(只摘掉
「不再抓取」);自动补抓补上的不算人办完,行和按钮都不动。升级之前那几轮的旧记录(没有
`step` 也没有版本,实测那条 OpenAI 就是)分不出是哪一步,一律留着、只摘掉「不再抓取」——
「这篇补抓失败过」「补在收尾之后」都证明不了**这一条**是补抓失败。

脚手架复用 `tests/test_feed_fulltext.py` 的 `FullTextCase`(临时 registry)。
"""
import copy
import unittest
from unittest.mock import Mock, patch

from kb import browser_capture, pipeline_capture, pipeline_state
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


def titles(run):
    return ([f['title'] for f in run['stages']['export']['failures']],
            [f['title'] for f in run['results']['export']['rss']['failures']])


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


class HandFilledTests(FullTextCase):
    """直接继承脚手架,不继承 `CaptureMarkTests` —— 那样会把它的用例在这里再收一遍。"""

    def setUp(self):
        super().setUp()
        self.key = self.put()
        self.other = self.put(url='https://e.org/other', title='Other')
        with patch.object(fulltext, 'request',
                          Mock(side_effect=FeedError('这一页要求先完成验证或登录'))), \
                patch.dict(pipeline_state._state, stop_requested=False):
            self.errors = fulltext.fetch_pending('rss')['errors']
        self.assertEqual(2, len(self.errors))
        self.mine = next(f for f in self.errors if f['drop']['id'] == self.key)

    def _fill_by_hand(self):
        browser_capture.save_pasted(self.key, '这是人工粘进来的一整段正文。' * 40)

    def _run(self, failures=None, finished='2026-11-05 10:23:14'):
        run = run_of(self.errors if failures is None else failures)
        if finished is not None:
            run['finished_at'] = finished
        return run

    def drops(self, run):
        return ([bool(f.get('drop')) for f in run['stages']['export']['failures']],
                [bool(f.get('drop')) for f in run['results']['export']['rss']['failures']])

    def test_the_failure_records_which_version_it_was_about(self):
        self.assertEqual(self.row(self.key)['content_rev'], self.mine['body_rev'])

    def test_filled_by_hand_drops_the_row(self):
        run = self._run()
        self._fill_by_hand()
        pipeline_capture.mark_runs([run])
        self.assertEqual((['Other'], ['Other']), titles(run))
        # 剩下那一条照旧还能从浏览器补、照旧能不再抓取 —— 拿掉的只有补好的那一条。
        self.assertEqual(([self.other], [self.other]), marks(run))
        self.assertEqual(([True], [True]), self.drops(run))

    def test_the_truncated_total_follows(self):
        run = self._run()
        run['stages']['export']['failures_total'] = 60
        self._fill_by_hand()
        pipeline_capture.mark_runs([run])
        self.assertEqual(59, run['stages']['export']['failures_total'])

    def test_state_and_last_run_both_lose_it(self):
        state = {**self._run(), 'last_run': self._run()}
        self._fill_by_hand()
        pipeline_capture.mark_state(state)
        self.assertEqual((['Other'], ['Other']), titles(state))
        self.assertEqual((['Other'], ['Other']), titles(state['last_run']))

    def test_filled_while_the_round_is_still_running(self):
        """同步还在跑后面几步时补好的:那一轮还没有收尾时刻,照样拿掉。"""
        run = self._run(finished=None)
        self._fill_by_hand()
        pipeline_capture.mark_runs([run])
        self.assertEqual((['Other'], ['Other']), titles(run))

    def test_the_round_finishing_after_the_fill_does_not_matter(self):
        """补在收尾之前(含同一秒):判据是版本,不是那一轮什么时候收的尾。"""
        self._fill_by_hand()
        run = self._run(finished='2999-01-01 08:00:00')
        pipeline_capture.mark_runs([run])
        self.assertEqual((['Other'], ['Other']), titles(run))

    def test_an_export_failure_is_not_solved_by_a_new_body(self):
        """导出写不进 vault 补正文办不完:行和原因照旧在,只是不给「不再抓取」。"""
        disk = failure({'key': self.key, 'url': self.mine['url'], 'title': 'Yes, and'},
                       '磁盘已满，Markdown 导出失败', 'rss', 'export')
        self.assertNotIn('body_rev', disk)
        run = self._run([disk, self.mine])
        self._fill_by_hand()
        pipeline_capture.mark_runs([run])
        self.assertEqual((['Yes, and'], ['Yes, and']), titles(run))
        reasons = [f['reason'] for f in run['stages']['export']['failures']]
        self.assertEqual(['磁盘已满，Markdown 导出失败'], reasons)
        self.assertEqual(([False], [False]), self.drops(run))

    def _legacy(self):
        """升级之前那几轮写的形状:没有 `step`,也没有 `body_rev`(实测那条 OpenAI 就是)。"""
        return {k: v for k, v in self.mine.items() if k not in ('body_rev', 'step')}

    def test_an_old_record_stays_without_drop(self):
        """旧记录分不出是哪一步:不管补在那一轮收尾之前、之后还是那一轮还在跑,行和
        原因都在,只是不给「不再抓取」。"""
        self._fill_by_hand()
        for finished in ('2000-01-01 08:00:00', '2999-01-01 08:00:00', None):
            with self.subTest(finished=finished):
                run = self._run([self._legacy()], finished=finished)
                pipeline_capture.mark_runs([run])
                self.assertEqual((['Yes, and'], ['Yes, and']), titles(run))
                self.assertEqual(([False], [False]), self.drops(run))

    def test_an_old_export_failure_on_a_fetch_failed_item_is_not_hidden(self):
        """审查复现的形状:这一篇补抓失败过、补在收尾之后,而那条旧记录其实是导出失败
        (还没写进知识库)。条目的事实证明不了**这一条**是补抓失败,原因要留着。"""
        disk = {k: v for k, v in failure(
            {'key': self.key, 'url': self.mine['url'], 'title': 'Yes, and'},
            '[Errno 21] Is a directory', 'rss', 'export').items() if k != 'step'}
        self.assertGreater(self.row(self.key)['fulltext_tries'], 0)
        run = self._run([disk], finished='2000-01-01 08:00:00')
        self._fill_by_hand()
        pipeline_capture.mark_runs([run])
        reasons = [f['reason'] for f in run['stages']['export']['failures']]
        self.assertEqual(['[Errno 21] Is a directory'], reasons)
        self.assertEqual(([False], [False]), self.drops(run))

    def test_new_records_say_which_step_they_came_from(self):
        self.assertEqual('fulltext', self.mine['step'])
        disk = failure({'key': self.key, 'url': self.mine['url'], 'title': 'Yes, and'},
                       '磁盘已满', 'rss', 'export')
        self.assertEqual('export', disk['step'])
        run = self._run([disk], finished='2000-01-01 08:00:00')
        self._fill_by_hand()
        pipeline_capture.mark_runs([run])
        # 新写的导出失败带着 `step`:补在收尾之后也不按旧口径拿掉。
        self.assertEqual((['Yes, and'], ['Yes, and']), titles(run))

    def test_a_failure_about_the_hand_filled_version_stays(self):
        """失败说的就是人补进来的那一版(版本不比它旧):不算被取代。"""
        self._fill_by_hand()
        later = {**self.mine, 'body_rev': self.row(self.key)['content_rev']}
        run = self._run([later])
        pipeline_capture.mark_runs([run])
        self.assertEqual((['Yes, and'], ['Yes, and']), titles(run))
        self.assertEqual(([False], [False]), self.drops(run))

    def test_an_automatic_fetch_is_not_by_hand(self):
        """自动补抓补上的不算「人办完了」:行和「不再抓取」照旧在(只是不再带按钮)。"""
        run = self._run()
        with patch.object(fulltext, 'request', Mock(return_value=page(PAGE))), \
                patch.dict(pipeline_state._state, stop_requested=False):
            fulltext.fetch_pending('rss')
        pipeline_capture.mark_runs([run])
        self.assertEqual((['Yes, and', 'Other'], ['Yes, and', 'Other']), titles(run))
        self.assertEqual(([None, None], [None, None]), marks(run))
        self.assertEqual(([True, True], [True, True]), self.drops(run))


if __name__ == '__main__':
    unittest.main()
