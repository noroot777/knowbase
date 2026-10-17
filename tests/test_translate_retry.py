"""译不动的那些要尽快重试,而不是排到三周后;导出阶段也要分清本轮和此前。

两件事是一个病的两面(2026-10-08 实测)。那天库里的形状是:翻译队列 896 条,
833 条从没试过、63 条试过一次失败了(全是和中转站之间的网络错误)。
`translate_pending` 原来按 `ORDER BY translate_tries` **升序**取数,于是失败
过的永远排在"从没试过"的后面 —— 一轮 40 条,要 21 轮同步、三周才轮得到这 63
条重试一次。而这期间每一轮导出都把它们原样数进「补译失败」并判红:一个三周
不变的红点,说的还不是这一轮发生的事,真出了故障反而淹在里面看不出来。

所以:取数改成**失败优先**,导出的失败文案和判红改成**只认本轮**。

脚手架借 `test_feed_translate.FeedCase`(临时 registry + 临时 vault + 固定
模型)。import 进来的 `FeedCase` 自己不带 test 方法,被 loader 收走也是空跑。
"""
import json
from functools import partial
import unittest
from copy import deepcopy
from unittest.mock import Mock, patch

from kb import llm, pipeline_state
from kb.collectors.feeds import FeedCollector
from kb.feeds import registry as reg, sync, translate
from tests.test_feed_translate import FeedCase

OK = '###TITLE###\n中文标题\n###BODY###\n中文正文。'


class RetryCase(FeedCase):
    """翻译队列 + 导出阶段状态的共同摆盘。"""

    def setUp(self):
        super().setUp()
        p = patch.dict(pipeline_state._state, {
            'stages': deepcopy(pipeline_state._state['stages']),
            'log': [], 'stop_requested': False})
        p.start(); self.addCleanup(p.stop)

    def stage(self):
        return pipeline_state._state['stages']['export']

    def key_of(self, title):
        with reg.db() as c:
            return c.execute('SELECT key FROM feed_items WHERE title=?',
                             (title,)).fetchone()['key']

    def mark_failed(self, title, tries=1, error='HTTP 503 translator'):
        """把一条标成"此前试过、失败了、还排在队列里"。

        直接写库而不是真跑一轮失败的同步:要造的形状是「队列里有 bad,而**这
        一轮没碰它**」—— 失败优先之后真跑一轮必然先把它取走,那就造不出来了。
        现实里这个形状每天都在发生:bad 有 63 条而一轮只译 40 条,剩下 23 条
        这一轮就是轮不到。
        """
        with reg.db() as c:
            c.execute('UPDATE feed_items SET translate_tries=?, translate_error=? '
                      'WHERE key=?', (tries, error, self.key_of(title)))


class RetryOrderTests(RetryCase):
    """失败过的下一轮就该被取走。"""

    def test_a_failed_entry_is_picked_before_one_that_was_never_tried(self):
        self.put('Older failed post', 'Body of the one that failed before.')
        self.put('Newer fresh post', 'Body of the one never tried.')
        self.mark_failed('Older failed post')
        chat = Mock(return_value=OK)
        with patch.object(translate, 'translate_pending',
                          partial(translate.translate_pending, limit=1)), \
                patch.object(llm, 'chat', chat):
            translate.translate_pending('rss')
        # 升序取数时这里取的是"从没试过"的那条(而且它 fetched_at 更新,
        # 永远排在前面)—— 失败的那条要等队列前面全部清空才轮得到。
        self.assertIn('Body of the one that failed before.', chat.call_args[0][0])
        self.assertNotIn('Body of the one never tried.', chat.call_args[0][0])

    def test_the_retry_actually_clears_the_error(self):
        self.put('Failed once', 'Body that will translate on the retry.')
        self.mark_failed('Failed once')
        with patch.object(llm, 'chat', Mock(return_value=OK)):
            translate.translate_pending('rss')
        with reg.db() as c:
            row = c.execute('SELECT translate_error, translated_at, body_zh '
                            'FROM feed_items').fetchone()
        self.assertEqual('', row['translate_error'])
        self.assertTrue(row['translated_at'])
        self.assertEqual('中文正文。', row['body_zh'])

    def test_priority_does_not_let_a_hopeless_entry_camp_at_the_head(self):
        # 失败优先的代价要兜住:一条永远译不动的内容现在每轮都排第一。出口仍是
        # `translate_tries < MAX_TRIES` —— 试满就离开队列、按原文入库,不会把
        # 后面的内容堵死。
        self.put('Hopeless post', 'Body that never translates.')
        self.put('Fine post', 'Body that translates fine.')
        self.mark_failed('Hopeless post', tries=translate.MAX_TRIES - 1)
        with patch.object(translate, 'translate_pending',
                          partial(translate.translate_pending, limit=1)), \
                patch.object(llm, 'chat', Mock(side_effect=RuntimeError('503'))):
            translate.translate_pending('rss')          # 第 MAX_TRIES 次:出队
        chat = Mock(return_value=OK)
        with patch.object(translate, 'translate_pending',
                          partial(translate.translate_pending, limit=1)), \
                patch.object(llm, 'chat', chat):
            translate.translate_pending('rss')
        self.assertIn('Body that translates fine.', chat.call_args[0][0])


class NewVersusOldFailureTests(RetryCase):
    """导出阶段:本轮新失败判红,此前失败只是排队,如实写出来但不判红。"""

    def test_an_old_failure_waiting_its_turn_does_not_turn_the_stage_red(self):
        # 真实形状:bad 比一轮的额度多,这一轮取走一部分、剩下的轮不到。那不是
        # 这一轮的故障 —— 原来它让每一轮都红,红点三周不变(2026-10-08 实测)。
        self.put('Failed A', 'Body A in English.')
        self.put('Failed B', 'Body B in English.')
        self.mark_failed('Failed A')
        self.mark_failed('Failed B')
        with patch.object(translate, 'translate_pending',
                          partial(translate.translate_pending, limit=1)), \
                patch.object(llm, 'chat', Mock(return_value=OK)):
            FeedCollector('rss').materialize()
        stage = self.stage()
        self.assertEqual('ok', stage['status'])
        self.assertIn('1 条此前翻译失败（排队重试，暂未入库）', stage['detail'])
        self.assertNotIn('本轮', stage['detail'])

    def test_a_new_failure_is_labelled_as_this_round_and_turns_it_red(self):
        self.put('Fresh failure', 'Body that fails right now.')
        with patch.object(llm, 'chat', Mock(side_effect=RuntimeError('HTTP 503'))):
            FeedCollector('rss').materialize()
        stage = self.stage()
        self.assertEqual('error', stage['status'])
        self.assertIn('本轮 1 条翻译失败（暂未入库）', stage['detail'])
        self.assertIn('HTTP 503', json.dumps(stage.get('failures'), ensure_ascii=False))

    def test_both_kinds_are_counted_separately_in_one_line(self):
        self.put('Old failure', 'Body of the old failure.')
        self.put('New failure', 'Body of the new failure.')
        self.mark_failed('Old failure')
        # MAX_ITEMS=1 且失败优先 → 这一轮取的是 Old。让它再失败一次,New 轮不到。
        # 于是 Old 既是"此前失败"又是"本轮失败",口径必须认后者。
        with patch.object(translate, 'translate_pending',
                          partial(translate.translate_pending, limit=1)), \
                patch.object(llm, 'chat', Mock(side_effect=RuntimeError('HTTP 503'))):
            FeedCollector('rss').materialize()
        stage = self.stage()
        self.assertEqual('error', stage['status'])
        self.assertIn('本轮 1 条翻译失败（暂未入库）', stage['detail'])
        self.assertIn('1 条待翻译（译完再入库）', stage['detail'])
        self.assertNotIn('此前翻译失败', stage['detail'])

    def test_a_round_that_ran_no_translation_reports_everything_as_old(self):
        # `export` 被单独调用(没跑翻译)时一条"本轮新失败"都没有 —— 队列里那些
        # 错误全是此前留下的,把它们算成本轮就是凭空报一次故障。
        self.put('Failed earlier', 'Body that failed earlier.')
        self.mark_failed('Failed earlier')
        sync.export('rss', hold_untranslated=True)
        stage = self.stage()
        self.assertEqual('ok', stage['status'])
        self.assertIn('1 条此前翻译失败', stage['detail'])

    def test_giving_up_after_max_tries_is_still_not_a_new_failure(self):
        # 本轮确实失败了一次,可这一条随即试满 MAX_TRIES、离开队列、按原文入库
        # —— 那是设计好的出口,不判红。所以分档只能**回库里查还在队列里的**,
        # 不能直接用摘要里的 failed 计数(那个计数在这里是 1)。
        self.put('Stubborn post', 'Body that never gets translated.')
        self.mark_failed('Stubborn post', tries=translate.MAX_TRIES - 1)
        with patch.object(llm, 'chat', Mock(side_effect=RuntimeError('502'))):
            FeedCollector('rss').materialize()
        stage = self.stage()
        self.assertEqual('ok', stage['status'])
        self.assertIn('已保存 1 条', stage['detail'])
        self.assertNotIn('本轮', stage['detail'])


class ShippedBacklogTests(RetryCase):
    """已经按原文入库的那一档(补译)同样分本轮和此前。"""

    def shipped(self, title, body):
        self.put(title, body)
        sync.export('rss')                  # 没配 key 那阵子按英文入过库

    def test_an_old_backfill_failure_is_not_this_round_s_fault(self):
        self.shipped('Legacy A', 'Legacy body A in English.')
        self.shipped('Legacy B', 'Legacy body B in English.')
        self.mark_failed('Legacy A')
        self.mark_failed('Legacy B')
        with patch.object(translate, 'translate_pending',
                          partial(translate.translate_pending, limit=1)), \
                patch.object(llm, 'chat', Mock(return_value=OK)):
            FeedCollector('rss').materialize()
        stage = self.stage()
        self.assertEqual('ok', stage['status'])
        self.assertIn('1 条此前补译失败（排队重试，库里仍是原文）', stage['detail'])

    def test_a_new_backfill_failure_still_turns_it_red(self):
        self.shipped('Legacy one', 'Legacy body in English.')
        with patch.object(llm, 'chat', Mock(side_effect=RuntimeError('HTTP 503'))):
            FeedCollector('rss').materialize()
        stage = self.stage()
        self.assertEqual('error', stage['status'])
        self.assertIn('本轮 1 条补译失败（库里仍是原文）', stage['detail'])

    def test_the_two_shelves_do_not_bleed_into_each_other(self):
        # 一条未入库的本轮失败 + 一条已入库的此前失败:四个数各归各位。
        self.shipped('Shipped old', 'Shipped body in English.')
        self.put('Fresh new', 'Fresh body in English.')
        self.mark_failed('Shipped old')
        with patch.object(translate, 'translate_pending',
                          partial(translate.translate_pending, limit=1)), \
                patch.object(llm, 'chat', Mock(side_effect=RuntimeError('HTTP 503'))):
            # 失败优先 → 取 Shipped old(已入库那条),它本轮再败一次
            FeedCollector('rss').materialize()
        stage = self.stage()
        self.assertEqual('error', stage['status'])
        self.assertIn('本轮 1 条补译失败（库里仍是原文）', stage['detail'])
        self.assertIn('1 条待翻译（译完再入库）', stage['detail'])
        self.assertNotIn('此前', stage['detail'])


if __name__ == '__main__':
    unittest.main()
