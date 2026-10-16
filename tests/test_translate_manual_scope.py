# -*- coding: utf-8 -*-
"""「重新翻译」那一轮的**付费边界**:它动了谁、只动谁、标记活多久。

和 tests/test_translate_manual_retry.py 是同一颗按钮的两半(分家只因为 700 行
上限):那一份钉的是队列口径和面板计数 —— 试满额度的条目放回队列之后真的被译掉、
排在队首、面板上那几个数各说各的;**这一份钉的是「别多译」** —— 用户筛着一条订阅
点下去,库里动的只能是这条订阅的那几篇,而每多译一条就是一次他没点过的模型调用。

三条越界路径各有自己的用例:
  ① **射程**:点名标记写哪些行、取数那一侧按本次 `source_id` 再收一次窄;
  ② **标记的寿命**:它是一次性的,这一轮成也好败也好都要收掉 —— 留在库里的话
     排序键 `translate_manual DESC` 会让下一次常规同步一直只盯这批;
  ③ **补抓和翻译在同一轮里先后跑**(见 `kb/collectors/feeds.py` 的 `materialize`),
     所以"取数那一刻选不到"证明不了"这一轮不会译它" —— 补抓成功的那一下它就
     进了翻译队列,而确认框那个数字是补抓之前算的。

判据一律落在**后果**上:库里哪几行被改了、模型被调了几次、确认框说的数字和实际
动的那批对不对得上。断言 `translate_manual` 这一列等于几证明不了射程 —— 一个把
标记写对、取数却不收窄的实现照样能全绿。

跑:  python -m unittest tests.test_translate_manual_scope -v
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from kb import llm  # noqa: E402
from kb.feeds import registry as reg, translate  # noqa: E402
from tests.test_translate_manual_retry import ManualRetryCase  # noqa: E402


class ScopeTests(ManualRetryCase):
    """点名那一轮的射程、标记寿命,以及补抓和它同轮先后跑那道缝。"""

    def test_retrying_one_subscription_leaves_the_others_alone(self):
        # 那行提示跟着选中的订阅走,按钮的射程也得跟着收:否则用户看着「1 条」
        # 点下去,放回队列的是整个来源的全部条目,代价是他没点过的那一大笔
        # 模型调用。
        other = reg.save_subscription('rss', {'name': 'Other', 'url': 'https://o.org/f.xml'})
        self.exhaust('A new agent runtime')
        reg.put_items(other, [{'url': 'https://o.org/x', 'title': 'Other English post',
                               'body': 'Body here.', 'published': '2026-09-28 09:00'}])
        with reg.db() as c:
            c.execute("UPDATE feed_items SET translate_tries=? WHERE title='Other English post'",
                      (translate.MAX_TRIES,))
        self.assertEqual(1, translate.stuck('rss', self.source['id'])['failed'])
        self.assertEqual(1, translate.requeue_failed('rss', self.source['id']))
        with reg.db() as c:
            rows = {r['title']: r['translate_tries'] for r in
                    c.execute('SELECT title,translate_tries FROM feed_items')}
        self.assertEqual(0, rows['A new agent runtime'])
        self.assertEqual(translate.MAX_TRIES, rows['Other English post'])

    def test_the_round_only_translates_the_named_subscription(self):
        """**付费边界的端到端判据**:点名一条订阅,别人的内容一个都不许被译。

        `requeue_failed` 的射程收窄只管「放回队列时动了谁」,而紧跟着的
        `translate_pending` 原来按整个来源取数 —— 于是筛着订阅 A 点下去,B 的
        积压照样被译掉,用户付了他没点过的那笔钱(2026-10-13 第 1 轮审查实测:
        `requeued=1` 而 `pending=2`,两条都拿到了译文)。

        判据落在**后果**上:库里那两条各自有没有译文。断言"放回了几条"是不够的
        —— 那个数字在修之前就已经是对的。
        """
        other = reg.save_subscription('rss', {'name': 'Other', 'url': 'https://o.org/f.xml'})
        self.put('A new agent runtime', 'This runtime schedules tools.')
        reg.put_items(other, [{'url': 'https://o.org/x', 'title': 'Other English post',
                               'body': 'Body here.', 'published': '2026-09-28 09:00'}])
        self.assertEqual(1, translate.requeue_failed('rss', self.source['id']))
        with patch.object(llm, 'chat', Mock(return_value='###TITLE###\n全新的智能体运行时\n'
                                            '###BODY###\n这个运行时调度工具。')):
            summary = translate.translate_pending('rss', only_manual=True)
        self.assertEqual(1, summary['done'])
        with reg.db() as c:
            rows = {r['title']: r['title_zh'] for r in
                    c.execute('SELECT title,title_zh FROM feed_items')}
        self.assertEqual('全新的智能体运行时', rows['A new agent runtime'])
        self.assertEqual('', rows['Other English post'])   # 他没点的那条一个字没动

    def test_the_named_round_runs_through_the_export_stage(self):
        """端到端:端点只写意图,真正改库的那一下在 export 阶段,射程照旧跟着走。

        前一条用例直接调两个函数,证明不了「端点交出去的那份意图真的被接上了」
        —— 中间断一截(比如 `materialize` 读错了 `crawl_filter` 的键)的后果是
        按钮点下去整轮空转,而面板上一句错都没有。
        """
        from kb.collectors.feeds import FeedCollector
        from kb import pipeline_state
        other = reg.save_subscription('rss', {'name': 'Other', 'url': 'https://o.org/f.xml'})
        self.exhaust('A new agent runtime')
        reg.put_items(other, [{'url': 'https://o.org/x', 'title': 'Other English post',
                               'body': 'Body here.', 'published': '2026-09-28 09:00'}])
        with reg.db() as c:     # 另一条也试满了额度:不点名的话它也该被放回去
            c.execute("UPDATE feed_items SET translate_tries=?"
                      " WHERE title='Other English post'", (translate.MAX_TRIES,))
        # `materialize` 经 `pipeline_state.get_state()` 读它(那是 `_state` 的
        # 深拷贝),所以摆盘摆在 `_state` 上就够。
        state = {'crawl_filter': {'channel': 'rss',
                                  'translate_retry': {'channel': 'rss',
                                                      'source_id': self.source['id']}}}
        with patch.dict(pipeline_state._state, state, clear=False), \
             patch.object(llm, 'chat', Mock(return_value='###TITLE###\n全新的智能体运行时\n'
                                            '###BODY###\n这个运行时调度工具。')):
            FeedCollector('rss').materialize()
        with reg.db() as c:
            rows = {r['title']: dict(r) for r in c.execute('SELECT * FROM feed_items')}
        self.assertEqual('全新的智能体运行时', rows['A new agent runtime']['title_zh'])
        self.assertEqual('', rows['Other English post']['title_zh'])
        # 他没点的那条额度也不许被清零:清了的话它下一轮会插到队首,而用户从没
        # 要求过译它。
        self.assertEqual(translate.MAX_TRIES,
                         rows['Other English post']['translate_tries'])
        # **点名标记是一次性的**:留着的话下一次常规同步仍然只盯这批(排序上压过
        # 所有新内容),而筛着另一条订阅再点一次时,上一轮的残留会把别人的条目
        # 一起带进射程 —— 付费边界会跟着漂。
        self.assertEqual([0, 0], [r['translate_manual'] for r in rows.values()])

    def test_an_emptied_named_scope_does_not_fall_back_to_everything(self):
        """**点名范围被别人译空时,射程不许放开成整个来源。**

        端点放行之后、export 阶段开跑之前是一个真实的窗口(另一轮同步、另一个
        人点的重试、一次手工脚本都可能落在这里)。射程判据写成「点到了几条」
        (`only_manual=bool(picked)`)的话,`picked == 0` 当场把收窄撤掉 —— 用户
        筛着订阅 A 点下去,库里动的是 B 的积压,钱花在他没确认的范围上。

        判据只能是「这一轮是不是点名的」。点名范围空了就译 0 条,那是如实的。
        """
        from kb.collectors.feeds import FeedCollector
        from kb import pipeline_state
        other = reg.save_subscription('rss', {'name': 'Other', 'url': 'https://o.org/f.xml'})
        reg.put_items(other, [{'url': 'https://o.org/x', 'title': 'Other English post',
                               'body': 'Body here.', 'published': '2026-09-28 09:00'}])
        state = {'crawl_filter': {'channel': 'rss',
                                  'translate_retry': {'channel': 'rss',
                                                      'source_id': self.source['id']}}}
        chat = Mock(return_value='###TITLE###\n标题\n###BODY###\n正文。')
        # 点名那一下一条也没点到(本来有、这中间被别人译掉了)
        with patch.dict(pipeline_state._state, state, clear=False), \
             patch.object(translate, 'requeue_failed', lambda *a, **k: 0), \
             patch.object(llm, 'chat', chat):
            FeedCollector('rss').materialize()
        self.assertEqual(0, chat.call_count)       # 一次模型调用都没发生
        with reg.db() as c:
            row = c.execute("SELECT * FROM feed_items WHERE title='Other English post'").fetchone()
        self.assertEqual('', row['title_zh'])      # 他没点的那条一个字没动

    def test_the_flag_is_cleared_when_the_named_round_fails(self):
        """**一条都没译成时,点名标记同样要被收掉。**

        译成的那条 `_save` 自己会清,所以成功那条路对 `materialize` 里那句
        `finally: clear_manual` 是瞎的 —— 只有整轮失败才钉得住它(`_fail` 不清
        标记,那是对的:失败的条目还该排在队首等下一次)。

        留着的后果:排序键 `translate_manual DESC` 排在最前面,下一次常规同步会
        一直只盯这批,新抓回来的内容永远排在它们后面;而筛着另一条订阅再点一次
        重试时,上一轮的残留会把别人的条目一起带进射程 —— 付费边界跟着漂。
        """
        from kb.collectors.feeds import FeedCollector
        from kb import pipeline_state
        self.exhaust('A new agent runtime')
        state = {'crawl_filter': {'channel': 'rss',
                                  'translate_retry': {'channel': 'rss', 'source_id': None}}}
        with patch.dict(pipeline_state._state, state, clear=False), \
             patch.object(llm, 'chat', Mock(side_effect=RuntimeError('503 no available account'))):
            FeedCollector('rss').materialize()
        with reg.db() as c:
            row = c.execute('SELECT * FROM feed_items').fetchone()
        self.assertEqual('', row['title_zh'])                 # 确实没译成
        self.assertIn('503', row['translate_error'])          # 失败记上了
        self.assertEqual(0, row['translate_manual'])          # 而标记收掉了
        # 额度留在"清零 + 这一轮失败一次"= 1:它照旧排在队列里等下一轮,只是
        # 不再有"优先于一切"那个特权。
        self.assertEqual(1, row['translate_tries'])
        self.assertEqual(0, translate.pending('rss', only_manual=True))

    def test_an_item_waiting_for_its_body_is_not_translated_in_the_same_round(self):
        """**确认 1 条就只许译 1 条,补抓不许把规模悄悄撑大。**

        同一条订阅上:一条试满额度、正文已经到手(马上就能译),另一条还在等正文
        补抓。确认框和端点都按「可译队列 + 试满额度」算,**排除了等正文的那条**;
        而点名集合原来没有同样排除,于是同一轮里补抓先成功、它进了翻译队列、带着
        标记被取走 —— 确认 1 条、实际调了 2 次模型(2026-10-14 第 2 轮审查实测)。
        不需要并发,也不需要上一轮失败。

        少译一条是如实的(补抓成功后 `fulltext._save` 把译文和额度一起清掉,
        它是一条全新的待译条目,下一轮常规同步正常轮到);多译一条是花用户没
        确认过的钱。
        """
        from kb.collectors.feeds import FeedCollector
        from kb import pipeline_state
        from kb.feeds import fulltext
        self._with_fulltext(self.source)
        ready = self.exhaust('A new agent runtime')
        with reg.db() as c:     # 这一条的正文早就补抓到手了,它只是译不动
            c.execute("UPDATE feed_items SET fulltext_at='2026-10-01 09:00'"
                      ' WHERE key=?', (ready,))
        self.put('Waiting for its body', 'Shell only.')
        waiting = [r for r in reg.items('rss')['items']
                   if r['title'] == 'Waiting for its body'][0]['key']
        # 确认框承诺的就是这一条
        self.assertEqual(1, translate.stuck('rss')['exhausted'] + translate.pending('rss'))
        state = {'crawl_filter': {'channel': 'rss',
                                  'translate_retry': {'channel': 'rss', 'source_id': None}}}
        chat = Mock(return_value='###TITLE###\n标题\n###BODY###\n正文。')
        # **补抓回来的正文必须是英文。** 写成中文的话 `fulltext._save` 会把
        # `lang` 重判成 `zh`,那一条从此压根不在 `lang='en'` 的射程里 —— 这条
        # 用例就变成"用一个永远不会被译的条目去验不许译它",定点注入红 0 条
        # (第一版实测如此)。
        with patch.dict(pipeline_state._state, state, clear=False), \
             patch.object(fulltext, 'fetch_one',
                          lambda url: ('Full English body fetched from origin. ' * 20, '')), \
             patch.object(llm, 'chat', chat):
            FeedCollector('rss').materialize()
        self.assertEqual(1, chat.call_count)      # 确认 1 条、调用 1 次
        with reg.db() as c:
            rows = {r['key']: dict(r) for r in c.execute('SELECT * FROM feed_items')}
        self.assertTrue(rows[ready]['translated_at'])
        # 那一条的正文确实在同一轮里补回来了 —— 它只是不参加这一轮的翻译
        self.assertTrue(rows[waiting]['fulltext_at'])
        self.assertEqual('', rows[waiting]['translated_at'])
        self.assertEqual(0, rows[waiting]['translate_manual'])

    def test_an_exhausted_item_still_awaiting_its_body_is_not_promised(self):
        """**试满额度、可正文还没补抓回来 —— 这一条不算进确认数。**

        确认数的两段(`queued` + `exhausted`)和点名集合(`manual_sql`)必须逐字
        同一个射程,两个方向各错一头:
        - 算了而不标记 = 「说 2 条、只译 1 条」。如实,但用户是按那个数字决定
          要不要点的,而且他会以为按钮漏了东西。
        - 标记了而不算 = 多花一笔他没确认过的钱(修 2 那一档,见
          `test_an_item_waiting_for_its_body_is_not_translated_in_the_same_round`)。

        这一档于是变成**真实可达**的:面板上说「1 条翻译失败」、却一条都译不了。
        端点如实说出为什么(在等原文正文),而不是一句「没有等待翻译的内容」——
        后者和「压根没有外文内容」长得一模一样,下一步却完全不同。
        """
        from fastapi.testclient import TestClient
        from kb.server import app
        self._with_fulltext(self.source)
        self.exhaust('A new agent runtime')     # `fulltext_at` 是空的:还在补抓队列里
        s = translate.stuck('rss')
        self.assertEqual(1, s['failed'])        # 它确实失败过,那句话照说
        self.assertEqual(0, s['exhausted'])     # 但现在译不了,不许写进确认数
        self.assertEqual(0, s['queued'])
        self.assertEqual(0, translate.requeue_failed('rss'))   # 也不标记它
        with patch.object(translate, 'available', lambda: True):
            r = TestClient(app).post('/api/intake/rss/translate-retry', json={})
        self.assertEqual(400, r.status_code)
        self.assertIn('补抓', r.json()['detail'])

    def test_a_leftover_flag_from_a_crashed_round_is_not_this_round_s_permission(self):
        """**上一轮崩在补抓上留下的标记,不是这一次的授权。**

        `translate_manual` 是落在库里的持久标记,而清它那一步**不保证跑得到**:
        补抓阶段抛异常(磁盘写失败、原文站点掐连接)、进程被 kill、机器断电都会
        把它留在那儿。下一次筛着另一条订阅点重试,只按标记取数就会把残留的那些
        一起译掉 —— 用户确认 1 条、实际 2 条,多出来那条属于他压根没选的订阅
        (2026-10-14 第 2 轮审查实测:在正文保存处注入 `OSError`)。

        两道各管一段:①`finally` 从点名之后就开始,把补抓一起圈进来(治这一档);
        ②取数按**本次** `source_id` 收窄(治 `finally` 压根跑不到的那些)。
        这条用例**把两道一起钉住** —— 它先让清理真的跑不到,再验下一轮不越界。
        """
        from kb.collectors.feeds import FeedCollector
        from kb import pipeline_state
        from kb.feeds import fulltext
        other = reg.save_subscription('rss', {'name': 'Other', 'url': 'https://o.org/f.xml'})
        self._with_fulltext(other)
        self.exhaust('A new agent runtime')                  # 订阅 A:就绪、待译
        # **订阅 B 上要摆两条,因为这两件事现在互斥**:「排在补抓队列里」(才能让
        # 补抓阶段跑到它、在那儿抛异常)和「会被点名标记」(`manual_sql` 排除补抓
        # 队列)。只摆一条的话点名数是 0,「标记残留」这件事压根没发生,注入
        # 「`finally` 只圈翻译那一句」红 0 条(第一版实测如此)。
        reg.put_items(other, [{'url': 'https://o.org/shell', 'title': 'B shell awaiting body',
                               'body': 'Shell.', 'published': '2026-09-28 09:00'},
                              {'url': 'https://o.org/ready', 'title': 'B ready to translate',
                               'body': 'Plain English body already in hand.',
                               'published': '2026-09-28 09:00'}])
        by_title = {r['title']: r['key'] for r in reg.items('rss')['items']}
        with reg.db() as c:     # B 的第二条正文已到手,只是还没轮到翻译
            c.execute("UPDATE feed_items SET fulltext_at='2026-10-01 09:00' WHERE key=?",
                      (by_title['B ready to translate'],))

        # ① 为 B 发起一轮,让它崩在补抓的正文保存上 —— 清理那一步跑不到
        crash = {'crawl_filter': {'channel': 'rss',
                                  'translate_retry': {'channel': 'rss',
                                                      'source_id': other['id']}}}
        def boom(key, body, seen):
            raise OSError('simulated disk write failure')
        english = 'Full English body fetched from origin. ' * 20
        with patch.dict(pipeline_state._state, crash, clear=False), \
             patch.object(fulltext, 'fetch_one', lambda url: (english, '')), \
             patch.object(fulltext, '_save', boom), \
             patch.object(llm, 'chat', Mock(side_effect=AssertionError('不该走到翻译'))):
            with self.assertRaises(OSError):
                FeedCollector('rss').materialize()
        # `finally` 这一道生效了:B 那条就绪条目的标记没留在库里
        with reg.db() as c:
            self.assertEqual(0, c.execute('SELECT count(*) FROM feed_items'
                                          ' WHERE translate_manual=1').fetchone()[0])

        # ② 再把标记硬摆回去 —— 模拟连 `finally` 都跑不到的那一档(进程被 kill、
        #    断电)。这一半只有取数那道收窄挡得住。
        with reg.db() as c:
            c.execute('UPDATE feed_items SET translate_manual=1 WHERE key=?',
                      (by_title['B ready to translate'],))
        pick = {'crawl_filter': {'channel': 'rss',
                                 'translate_retry': {'channel': 'rss',
                                                     'source_id': self.source['id']}}}
        chat = Mock(return_value='###TITLE###\n标题\n###BODY###\n正文。')
        with patch.dict(pipeline_state._state, pick, clear=False), \
             patch.object(fulltext, 'fetch_one', lambda url: (english, '')), \
             patch.object(llm, 'chat', chat):
            FeedCollector('rss').materialize()
        self.assertEqual(1, chat.call_count)      # 只译了他点的那一条
        with reg.db() as c:
            rows = {r['title']: dict(r) for r in c.execute('SELECT * FROM feed_items')}
        self.assertTrue(rows['A new agent runtime']['translated_at'])
        self.assertEqual('', rows['B ready to translate']['translated_at'])

    def _crash_leaves_two_flags(self, titles=('B first post', 'B second post')):
        """造出「上一轮合法点名了两条、进程在译完之前就没了」那个末态。

        整条路径照旧真跑(管道意图 → `requeue_failed` → 补抓 → 取数 → 导出),
        只禁用收尾那次 `clear_manual` —— 审查用的 `os._exit(73)` 在进程内没法
        表达,而要钉的末态是同一个:库里留着两个本轮没人收掉的标记。

        这一步**必须在补抓关着的时候做**:那时两条都是可译的,于是 `manual_sql`
        把两条都标上 —— 残留是**真实产生**的,不是手摆进去的。
        """
        from kb.collectors.feeds import FeedCollector
        from kb import pipeline_state
        for title in titles:
            self.put(title, f'English body of {title}. ' * 20)
        state = {'crawl_filter': {'channel': 'rss', 'translate_retry': {
            'channel': 'rss', 'source_id': self.source['id']}}}
        with patch.dict(pipeline_state._state, state, clear=False), \
             patch.object(translate, 'clear_manual', lambda kind: 0), \
             patch.object(llm, 'chat', Mock(side_effect=RuntimeError('503 no available account'))):
            FeedCollector('rss').materialize()
        keys = {r['title']: r['key'] for r in reg.items('rss')['items']}
        with reg.db() as c:
            flags = sorted(r['translate_manual'] for r in
                           c.execute('SELECT translate_manual FROM feed_items'))
        self.assertEqual([1, 1], flags, '残留没造出来,后面那几句证明不了任何事')
        return keys

    def test_a_crash_leaves_no_permission_for_the_next_round_on_the_same_feed(self):
        """**同一条订阅里的旧标记,也不是这一次的授权。**

        上一条用例钉的是**跨订阅**的残留,挡住它的是取数那一侧按 `source_id`
        收窄;这一条钉的是**同一条订阅**里的 —— 它和本次写下的标记在库里长得
        一模一样,`source_id` 那道闸对它完全是瞎的,而 `manual_sql` 排除补抓队列
        只管「这一次标记谁」、管不了「上一次标记过谁」。

        2026-10-14 第 3 轮审查实测的那条路:两篇都还可译的那一轮被合法点名(确认
        2 条),进程在模型调用之前被 kill(注入 `os._exit(73)`,`finally` 跑不到);
        重开后给这条订阅打开「补抓原文正文」,其中一篇进了补抓队列,于是确认框按
        翻译队列算出「1 条」。而**补抓就排在翻译前面** —— 它补完正文离开补抓队列
        的那一下旧标记还在身上,两篇一起被取走。确认 1 条、实际 2 次模型调用。

        所以 `requeue_failed` 开工前先把上一轮的点名作废,和这一次的点名在同一个
        事务里。**这条用例同时钉住反边**:清场不许把本次该标的那条一起清掉 ——
        判据不是「一条都没译」,而是「恰好译了确认的那一条」。
        """
        from kb.collectors.feeds import FeedCollector
        from kb import pipeline_state
        from kb.feeds import fulltext
        keys = self._crash_leaves_two_flags()
        # 重开之后给这条订阅打开补抓:第一篇的正文早补齐过,第二篇还没
        self._with_fulltext(self.source)
        with reg.db() as c:
            c.execute("UPDATE feed_items SET fulltext_at='2026-10-01 09:00' WHERE key=?",
                      (keys['B first post'],))
        # 确认框按翻译队列算,它只看得见第一篇 —— 这就是用户点下去时看到的数字
        self.assertEqual(1, translate.pending('rss', self.source['id']))
        state = {'crawl_filter': {'channel': 'rss', 'translate_retry': {
            'channel': 'rss', 'source_id': self.source['id']}}}
        chat = Mock(return_value='###TITLE###\n标题\n###BODY###\n正文。')
        with patch.dict(pipeline_state._state, state, clear=False), \
             patch.object(fulltext, 'fetch_one',
                          lambda url: ('Full English body fetched from origin. ' * 20, '')), \
             patch.object(llm, 'chat', chat):
            FeedCollector('rss').materialize()
        self.assertEqual(1, chat.call_count, '确认 1 条,模型调用不能多也不能少')
        with reg.db() as c:
            rows = {r['key']: dict(r) for r in c.execute('SELECT * FROM feed_items')}
        first, second = rows[keys['B first post']], rows[keys['B second post']]
        self.assertTrue(first['translated_at'])          # 确认的那一条真的译了
        self.assertEqual('', second['translated_at'])    # 残留那一条没被捎带
        self.assertTrue(second['fulltext_at'])           # 它的正文确实补回来了
        self.assertEqual(0, second['translate_manual'])  # 旧标记被作废了

    def test_the_all_sources_entry_drops_the_old_flags_too(self):
        """不筛订阅那个入口走的是同一条清场。

        `_retry_where(None)` 只是不往 WHERE 上加那半截,清场本身跟它无关 ——
        这条用例钉的是「`source_id=None` 这条路也清」,而「清完之后补抓不会把它
        捞回来」由上一条用例钉住。
        """
        keys = self._crash_leaves_two_flags()
        self._with_fulltext(self.source)
        with reg.db() as c:
            c.execute("UPDATE feed_items SET fulltext_at='2026-10-01 09:00' WHERE key=?",
                      (keys['B first post'],))
        self.assertEqual(1, translate.requeue_failed('rss'))     # 只点名得了的那一条
        with reg.db() as c:
            rows = {r['key']: r['translate_manual'] for r in
                    c.execute('SELECT key,translate_manual FROM feed_items')}
        self.assertEqual(1, rows[keys['B first post']])
        self.assertEqual(0, rows[keys['B second post']])
        self.assertEqual(0, translate.pending('rss', only_manual=True)
                         - translate.pending('rss'))   # 点名集合不比队列大

    def test_clearing_the_old_flags_does_not_reach_another_source(self):
        """反过来钉一边:开工前那次清场只清本 kind。

        各通道的 `materialize` 各跑一轮自己的(`crawl_channels` 依次调),所以
        rss 这一轮作废的只能是 rss 的点名。清场写漏 `kind=?` 的话,rss 这边点
        一下重试就把 social 排在队首的那批打回队尾 —— 那一轮不会多译也不会
        少译,只是用户点过的优先级静默消失,而面板上看不出任何区别。
        """
        other = reg.save_subscription('social', {'name': 'A big V', 'url': 'https://x.com/someone'})
        reg.put_items(other, [{'url': 'https://x.com/someone/status/1',
                               'title': 'An English post', 'body': 'Body here.',
                               'published': '2026-09-28 09:00'}])
        with reg.db() as c:
            c.execute("UPDATE feed_items SET translate_manual=1 WHERE kind='social'")
        self.put('A new agent runtime', 'English body. ' * 20)
        self.assertEqual(1, translate.requeue_failed('rss'))
        with reg.db() as c:
            kept = c.execute('SELECT translate_manual FROM feed_items'
                             " WHERE kind='social'").fetchone()[0]
        self.assertEqual(1, kept, 'rss 那一轮的清场伸到了 social 的点名上')

    def test_turning_off_fulltext_while_confirming_does_not_widen_the_round(self):
        """**范围没换,范围里的那批内容变了 —— 这一轮一条都不许译。**

        前三轮修的都是「范围」(哪条订阅、哪个 kind、上一轮的残留),这一条是
        **范围里的集合**:`manual_sql` 排除「正文还在补抓队列里」的条目,而那一档
        取决于订阅自己的 `fulltext` 开关。确认框开着的时候把它关掉,等正文的那几条
        当场变成可译的 —— 订阅没换、请求里的 `source_id` 也没丢,漂的是集合本身
        (2026-10-14 第 5 轮审查实测:确认框说 1 条,另一个窗口
        `PATCH {"fulltext": false}`,点确认之后两条都译成了中文)。

        判据落在**模型调用次数**上(一次都没有),外加「一条都没被点名」—— 清场
        照旧生效,因为旧标记一个都不许留着(第 3 轮那条)。
        """
        from kb.collectors.feeds import FeedCollector
        from kb import pipeline_state
        from kb.feeds import fulltext
        # 这条订阅开着补抓:一条正文早补齐过、试满额度(确认框里那 1 条),
        # 另一条还在等正文(不算进确认数,见 `manual_sql`)
        self._with_fulltext(self.source)
        ready = self.exhaust('A new agent runtime')
        with reg.db() as c:
            c.execute("UPDATE feed_items SET fulltext_at='2026-10-01 09:00'"
                      ' WHERE key=?', (ready,))
        self.put('Waiting for its body', 'Shell only.')
        before = translate.stuck('rss', self.source['id'])
        self.assertEqual(1, before['exhausted'] + before['queued'])   # 确认框说 1 条
        sig = before['sig']
        # 确认框开着的时候,另一个窗口把这条订阅的「补抓原文正文」关掉
        with reg.db() as c:
            c.execute('UPDATE subscriptions SET fulltext=0 WHERE id=?',
                      (self.source['id'],))
        after = translate.stuck('rss', self.source['id'])
        self.assertEqual(2, after['exhausted'] + after['queued'])     # 现在是 2 条
        self.assertNotEqual(sig, after['sig'], '指纹没跟着集合变,它就什么也证明不了')
        state = {'crawl_filter': {'channel': 'rss', 'translate_retry': {
            'channel': 'rss', 'source_id': self.source['id'], 'scope_sig': sig}}}
        chat = Mock(return_value='###TITLE###\n标题\n###BODY###\n正文。')
        with patch.dict(pipeline_state._state, state, clear=False), \
             patch.object(fulltext, 'fetch_one',
                          lambda url: ('Full English body fetched. ' * 20, '')), \
             patch.object(llm, 'chat', chat):
            FeedCollector('rss').materialize()
        self.assertEqual(0, chat.call_count, '确认的那一批已经不是这一批了,一条都不许译')
        with reg.db() as c:
            rows = [dict(r) for r in c.execute('SELECT * FROM feed_items')]
        self.assertEqual([0, 0], [r['translate_manual'] for r in rows])
        self.assertEqual(['', ''], [r['translated_at'] for r in rows])

    def test_a_matching_confirmation_still_translates_its_round(self):
        """反过来钉一边:集合一个字没变的那一次照旧译。

        只钉「变了就不译」的话,一个**一律拒绝**的实现全绿,而那时这颗按钮
        什么也做不了 —— 整个任务就是为了让它能做事。
        """
        from kb.collectors.feeds import FeedCollector
        from kb import pipeline_state
        self.exhaust('A new agent runtime')
        sig = translate.stuck('rss', self.source['id'])['sig']
        state = {'crawl_filter': {'channel': 'rss', 'translate_retry': {
            'channel': 'rss', 'source_id': self.source['id'], 'scope_sig': sig}}}
        chat = Mock(return_value='###TITLE###\n标题\n###BODY###\n正文。')
        with patch.dict(pipeline_state._state, state, clear=False), \
             patch.object(llm, 'chat', chat):
            FeedCollector('rss').materialize()
        self.assertEqual(1, chat.call_count)

    def test_the_signature_follows_the_members_not_just_the_count(self):
        """指纹认的是**成员**,不只是条数。

        条数一样而动的是另一批内容时,用户确认的那句话("这个范围的 N 条")在
        字面上仍然成立,可译的已经不是他看到的那些了。两个方向的代价不对等:
        多拒一次他重点一下就是了,多译一条是花他没确认过的钱。
        """
        a = self.exhaust('A new agent runtime')
        first = translate.stuck('rss', self.source['id'])['sig']
        self.assertTrue(first.startswith('1:'), first)
        # 那一条译完了,同时另一条新抓回来 —— 条数还是 1
        with reg.db() as c:
            c.execute("UPDATE feed_items SET translated_at='2026-10-14 10:00',"
                      " title_zh='标题' WHERE key=?", (a,))
        self.put('Another English post', 'English body here. ' * 10)
        second = translate.stuck('rss', self.source['id'])['sig']
        self.assertTrue(second.startswith('1:'), second)      # 条数相同
        self.assertNotEqual(first, second)                    # 但不是同一批
        with self.assertRaises(translate.ScopeChanged):
            translate.requeue_failed('rss', self.source['id'], first)

    def test_the_count_and_the_signature_come_from_one_snapshot(self):
        """**同一份回包里,那个数字和那份指纹必须说同一件事。**

        前一轮加的指纹只管「拿到回包之后集合再变」。这一条是**回包生成过程中**
        就半新半旧:`sqlite3` 在 legacy `isolation_level` 下 SELECT 不开事务、
        不持任何锁(元规则第一条),于是 `stuck()` 里几次读之间能插进别人的提交。
        后果不是「数字晚一点更新」—— 确认框按前两个数字说「1 条」,而带回去的
        指纹是那**两条**的,于是两道指纹校验都正确匹配那两条、都把这个错误的
        确认当成有效授权:确认 1 条、译 2 条(2026-10-14 第 6 轮审查实测,
        回包是 `exhausted=1` + `sig='2:…'`)。

        **插桩只在原函数调用前改库,不替换任何计算结果** —— 打的是
        `translate_scope.scope_signature`(不是 `translate.` 那个再导出的名字:
        `stuck` 调的是本模块那一份)。这一刻正是「数量读完、指纹未读」。

        判据是**回包自己自洽**:指纹那个条数前缀 == 两个数字之和。写成「断言
        指纹等于某个值」就只是在重算一遍同一个函数。
        """
        from kb.feeds import translate_scope
        # 这条订阅开着补抓:一条正文早补齐过、试满额度;另一条还在等正文
        self._with_fulltext(self.source)
        ready = self.exhaust('A new agent runtime')
        with reg.db() as c:
            c.execute("UPDATE feed_items SET fulltext_at='2026-10-01 09:00'"
                      ' WHERE key=?', (ready,))
        self.put('Waiting for its body', 'Shell only.')
        real = translate_scope.scope_signature
        sid = self.source['id']

        def racing(*a, **kw):
            # 数量已经读完、指纹还没读 —— 这一刻另一个窗口把补抓关掉并提交
            with reg.db() as c2:
                c2.execute('UPDATE subscriptions SET fulltext=0 WHERE id=?', (sid,))
            return real(*a, **kw)

        with patch.object(translate_scope, 'scope_signature', racing):
            s = translate.stuck('rss', sid)
        total = s['queued'] + s['exhausted']
        self.assertEqual(1, total, '这一份回包里,数量该还是读事务那一刻的 1 条')
        self.assertTrue(s['sig'].startswith(f'{total}:'),
                        f"数字说 {total} 条、指纹说的是另一批:{s['sig']}")

    def test_a_refused_request_does_not_touch_the_database(self):
        """**忙碌拒绝时库里一个字节都不许动。**

        原来是先 `requeue_failed` 再 `start_pipeline`:409 返回之后额度已经清零、
        点名标记已经落盘,而面板再读一次时 `stuck.failed` 变成了 0 —— 失败说明
        和按钮一起消失,用户按拒绝文案说的「结束后重试」已经没有入口可点
        (2026-10-13 第 1 轮审查实测:3→0、0→1、1→0)。

        判据落在**库里那一行**上,不是「返回了 409」—— 后者在修之前也是对的。
        """
        from kb import pipeline
        from fastapi.testclient import TestClient
        from kb.server import app
        self.exhaust('A new agent runtime')
        before = translate.stuck('rss')
        with reg.db() as c:
            snapshot = [dict(r) for r in c.execute('SELECT * FROM feed_items ORDER BY key')]
        with patch.object(translate, 'available', lambda: True), \
             patch.object(pipeline, 'start_pipeline', lambda *a, **k: ''):
            r = TestClient(app).post('/api/intake/rss/translate-retry', json={})
        self.assertEqual(409, r.status_code)
        with reg.db() as c:
            after = [dict(r) for r in c.execute('SELECT * FROM feed_items ORDER BY key')]
        self.assertEqual(snapshot, after)
        # 面板那句话也还在,按钮跟着它走
        self.assertEqual(before, translate.stuck('rss'))

    def test_the_flag_is_cleared_even_when_the_round_fails(self):
        """反过来钉一边:这一轮译不动时标记**同样**要收掉。

        不收的话它在库里长期留着,而排序键 `translate_manual DESC` 排在最前面
        —— 下一次常规同步会一直只盯这批,新抓回来的内容永远排在它们后面。
        """
        self.exhaust('A new agent runtime')
        translate.requeue_failed('rss')
        with reg.db() as c:
            self.assertEqual(1, c.execute('SELECT count(*) FROM feed_items'
                                          ' WHERE translate_manual=1').fetchone()[0])
        self.assertEqual(1, translate.clear_manual('rss'))
        with reg.db() as c:
            self.assertEqual(0, c.execute('SELECT count(*) FROM feed_items'
                                          ' WHERE translate_manual=1').fetchone()[0])
        # 额度留在清零后的状态:下一轮常规同步照旧会取到它(它是真的待译),
        # 收掉的只是"优先于一切"这个特权。
        self.assertEqual(1, translate.pending('rss'))
        self.assertEqual(0, translate.pending('rss', only_manual=True))



if __name__ == "__main__":
    unittest.main()
