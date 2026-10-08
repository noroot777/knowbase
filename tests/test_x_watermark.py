# -*- coding: utf-8 -*-
"""登录态通道的**存盘契约**:水位什么时候推进、区间补抓用谁的边界。

和 `test_x_channels.py` 按形状分开(那边是通道的选择、整批预取和它跟 HTTP 那
一侧的分派):这里每一条都要真跑一轮 `sync.collect` 把结果落进 feeds.db,看的是
"写盘失败那一轮水位动没动"这类末态 —— 挤进那个文件会越过仓库那条 700 行上限。

被打桩的只有"起一个浏览器抓一批"那一下(`x_browser.harvest`),存盘、事务、
水位判定一行不打桩。摆盘共用 `test_x_channels.ChannelBase`。

跑:  python -m unittest tests.test_x_watermark -v
"""
import sys
import threading
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from kb.collectors import FetchScope
from kb.feeds import registry as reg, sync, x_browser, x_channels
from tests.test_x_channels import FRESH, FRESH_LOCAL, ChannelBase, tweet


class BrowserRound(ChannelBase):
    """真跑一轮浏览器通道的摆盘:只替掉"起浏览器抓一批"那一下。"""

    def entry(self, url='https://x.com/Example/status/1'):
        return {'url': url, 'title': 'a thought', 'body': 'a thought',
                'author': 'Example Person', 'published': FRESH_LOCAL, 'content_kind': 'post'}

    def setUp(self):
        super().setUp()
        # 浏览器通道的"就绪"判据是 profile 里有过 Cookies;不摆上的话整轮会被
        # 记成"待配置"、一条订阅都不取,而这几条用例要看的正是存盘那一步。
        cookies = x_channels.BROWSER_PROFILE / 'Default' / 'Cookies'
        cookies.parent.mkdir(parents=True, exist_ok=True)
        cookies.write_bytes(b'x')
        self.settings(x_channels.BROWSER)

    def collect(self, scope=None, **by_handle):
        """真跑一轮 `sync.collect`,只把"起浏览器抓一批"那一下替掉。

        **不能用 `harvested()` 摆盘**:`collect` 自己会先调 `_prefetch_x`,那一步
        `reset()` + 真预取,摆好的缓存当场被清掉(而且会去起一个真 Chrome)。
        """
        with patch.object(x_browser, 'harvest', lambda jobs, **kw: dict(by_handle)):
            return sync.collect('social', scope or FetchScope(force=True))

    def repoint(self, source_id, handle='Other'):
        """采集途中用户把这条订阅改指到另一个账号 —— 走的是真的保存那条路
        (`reset`:清 config、清条目归属、重算历史下界)。"""
        return reg.save_subscription(
            'social', {'name': handle, 'url': f'https://x.com/{handle}'}, source_id)


class WatermarkCommitTests(BrowserRound):
    """**水位和条目一个事务**:存盘失败那一轮水位不许动。"""

    def test_a_failed_save_does_not_advance_the_watermark(self):
        """原来 `take()` 在工作线程里当场存一次水位,而条目要等主线程的
        `_save_result` 才写进库 —— 两个独立事务没有共同的成功/失败边界。

        审查实测:注入一次写盘失败之后 `stored_items=0`、而水位已经推进到那条
        没入库的推文,重试取回 0 条 —— 那不是提示不准,是丢数据。
        """
        s = self.sub()
        with patch.object(reg, 'put_items', side_effect=OSError('写盘失败')):
            summary = self.collect(example={'entries': [self.entry()], 'error': '',
                                            'watermark': '2026-10-07T00:00:00+00:00'})
        self.assertEqual(1, len(summary['errors']))
        self.assertEqual('', x_channels.watermark(reg.subscription('social', s['id']),
                                                  x_channels.BROWSER))
        self.assertEqual(0, reg.items('social', s['id'])['total'])

    def test_a_successful_save_advances_it_in_the_same_transaction(self):
        s = self.sub()
        summary = self.collect(example={'entries': [self.entry()], 'error': '',
                                        'watermark': '2026-10-07T00:00:00+00:00'})
        self.assertEqual(1, summary['added'])
        self.assertEqual('2026-10-07T00:00:00+00:00',
                         x_channels.watermark(reg.subscription('social', s['id']),
                                              x_channels.BROWSER))

    def test_saving_the_watermark_keeps_the_other_channels_value(self):
        s = self.sub()
        x_channels.save_watermark(s['id'], x_channels.FEEDGRAB, '2026-10-05')
        self.collect(example={'entries': [self.entry()], 'error': '',
                              'watermark': '2026-10-07T00:00:00+00:00'})
        s = reg.subscription('social', s['id'])
        self.assertEqual('2026-10-05', x_channels.watermark(s, x_channels.FEEDGRAB))
        self.assertEqual('2026-10-07T00:00:00+00:00',
                         x_channels.watermark(s, x_channels.BROWSER))

    def test_an_incomplete_round_is_recorded_without_calling_it_a_failure(self):
        s = self.sub()
        summary = self.collect(example={
            'entries': [self.entry()], 'error': '', 'watermark': '',
            'incomplete': '滚动次数用完,没有读到上次的位置'})
        self.assertEqual([], summary['errors'])
        self.assertEqual(1, len(summary['partial']))
        row = reg.subscription('social', s['id'])
        self.assertIn('滚动次数', row['last_note'])
        self.assertEqual('', row['last_error'])
        self.assertTrue(row['last_success'])
        self.assertEqual(0, row['failures'])          # 不进退避重试

    def test_a_later_complete_round_clears_the_note(self):
        s = self.sub()
        self.collect(example={'entries': [self.entry()], 'error': '', 'watermark': '',
                              'incomplete': '滚动次数用完'})
        self.collect(example={'entries': [self.entry('https://x.com/Example/status/2')],
                              'error': '', 'watermark': ''})
        self.assertEqual('', reg.subscription('social', s['id'])['last_note'])


class RangedScopeTests(BrowserRound):
    """区间补抓:用用户点的那段日期当下界,而且不许动日常增量水位。"""

    def test_the_prefetch_gets_the_requested_date_not_the_daily_watermark(self):
        """审查实测:传给采集器的仍是日常水位 2026-10-07,于是用户点名要的
        10-05 一条也翻不到(`added=0`),而日常水位还被进一步推进了。"""
        s = self.sub()
        x_channels.save_watermark(s['id'], x_channels.BROWSER, '2026-10-07T00:00:00+00:00')
        seen, scope = {}, FetchScope(since='2026-10-05', until='2026-10-06')

        def fake(jobs, **kw):
            seen.update(dict(jobs))
            return {h: {'entries': [], 'error': '',
                        'watermark': '2026-10-08T00:00:00+00:00'} for h, _ in jobs}

        with patch.object(x_browser, 'harvest', fake):
            sync._prefetch_x('social', reg.subscriptions('social'),
                             self.settings(x_channels.BROWSER), scope)
        # 交下去的是**换算过**的下界(项目时区的零点、且把零点算在内),不是裸
        # 日期 —— 裸日期的含义由宿主时区决定(见 `x_channels.since_floor`)。
        self.assertEqual({'example': x_channels.since_floor('2026-10-05')}, seen)

    def test_a_ranged_round_never_moves_the_daily_watermark(self):
        s = self.sub()
        x_channels.save_watermark(s['id'], x_channels.BROWSER, '2026-10-07T00:00:00+00:00')
        scope = FetchScope(since='2026-10-05', until='2026-10-06')

        def fake(jobs, **kw):
            return {h: {'entries': [], 'error': '',
                        'watermark': '2026-10-08T00:00:00+00:00'} for h, _ in jobs}

        with patch.object(x_browser, 'harvest', fake):
            sync._prefetch_x('social', reg.subscriptions('social'),
                             self.settings(x_channels.BROWSER), scope)
            _, meta = x_channels.take(reg.subscription('social', s['id']),
                                      self.settings(x_channels.BROWSER))
        self.assertNotIn('config_patch', meta)
        self.assertEqual('2026-10-07T00:00:00+00:00',
                         x_channels.watermark(reg.subscription('social', s['id']),
                                              x_channels.BROWSER))

    def test_a_ranged_round_does_not_clear_the_incomplete_note(self):
        """它不改日常增量的任何状态,这条提示也算 —— 清掉等于替日常同步宣布
        "读完了",而那一边的进度一点没动。"""
        s = self.sub()
        with patch.object(x_browser, 'harvest', lambda jobs, **kw: {
                'example': {'entries': [], 'error': '', 'watermark': '',
                            'incomplete': '滚动次数用完'}}):
            sync.collect('social', FetchScope(force=True))
        self.assertIn('滚动次数', reg.subscription('social', s['id'])['last_note'])
        with patch.object(x_browser, 'harvest', lambda jobs, **kw: {
                'example': {'entries': [], 'error': ''}}):
            sync.collect('social', FetchScope(since='2026-10-05', until='2026-10-06'))
        self.assertIn('滚动次数', reg.subscription('social', s['id'])['last_note'])

    def test_a_daily_round_still_passes_each_accounts_own_watermark(self):
        a = self.sub('Alpha')
        x_channels.save_watermark(a['id'], x_channels.BROWSER, '2026-10-01T00:00:00+00:00')
        self.sub('Beta')
        seen = {}

        def fake(jobs, **kw):
            seen.update(dict(jobs))
            return {h: {'entries': [], 'error': ''} for h, _ in jobs}

        with patch.object(x_browser, 'harvest', fake):
            sync._prefetch_x('social', reg.subscriptions('social'),
                             self.settings(x_channels.BROWSER), FetchScope())
        self.assertEqual({'alpha': '2026-10-01T00:00:00+00:00', 'beta': ''}, seen)


class RepointedSubscriptionTests(BrowserRound):
    """采集期间这条订阅被改指到另一个账号:手里那份结果一个字都不许写。

    审查实测(真走 guard → `start_pipeline` → 采集 → 存盘,两个测试屏障只暂停
    函数入口):修改请求先在空闲状态通过 `intake_api.guard`、随后管道才起跑,
    于是保存落在采集中间 —— 库里躺着 `OldExample/status/900`,新账号那个水位键
    被写成旧账号最新推文的时间,`next_sync_selection=[]`(新账号比这个时间更早
    的推文从此任何一轮都选不到),而 crawl 报 ok、新增 1 条。
    """

    def test_the_old_accounts_tweets_are_not_credited_to_the_new_one(self):
        s = self.sub()

        def harvest(jobs, **kw):
            self.repoint(s['id'])
            return {'example': {'entries': [self.entry()], 'error': '',
                                'watermark': '2026-10-07T00:00:00+00:00'}}

        stages = []
        with patch.object(x_browser, 'harvest', harvest), \
                patch.object(sync, '_stage', lambda *a, **k: stages.append(a)):
            summary = sync.collect('social', FetchScope(force=True))
        row = reg.subscription('social', s['id'])
        self.assertIn('改了订阅链接', stages[-1][2])     # 面板上说得出这一轮为什么空
        self.assertEqual(0, reg.items('social', s['id'])['total'], '旧账号的推文记给了新账号')
        self.assertEqual('', x_channels.watermark(row, x_channels.BROWSER),
                         '旧账号的水位写进了新账号那个键')
        self.assertEqual('', row['last_success'], '替新账号宣布它同步成功了')
        self.assertEqual((0, 0, 1, []), (summary['added'], summary['checked'],
                                         summary['repointed'], summary['errors']))

    def test_the_new_account_can_still_be_read_from_its_own_beginning(self):
        """水位被旧账号污染的真实代价:新账号比那个时间更早的推文再也选不到。

        所以这一条不只看库里那个值,而是拿新账号一条**比旧账号那条更早**的推文
        过一次生产的 `_select`(审查实测 `next_sync_selection=[]`):水位该是空的,
        它就该被选中。污染之后它低于水位,从此任何一轮增量都挑不出来。
        """
        s = self.sub()

        def harvest(jobs, **kw):
            self.repoint(s['id'], 'NewExample')
            return {'example': {'entries': [self.entry()], 'error': '',
                                'watermark': FRESH.strftime('%Y-%m-%dT%H:%M:%SZ')}}

        with patch.object(x_browser, 'harvest', harvest):
            sync.collect('social', FetchScope(force=True))
        row = reg.subscription('social', s['id'])
        floor = x_channels.watermark(row, x_channels.BROWSER)
        earlier = tweet('901', handle='NewExample',
                        when=(FRESH - timedelta(hours=1)).strftime('%Y-%m-%dT%H:%M:%SZ'))
        picked = x_browser._select([earlier], x_browser._utc(floor) if floor else None,
                                   'NewExample')
        self.assertEqual(['https://x.com/NewExample/status/901'],
                         [e['url'] for e in picked], f'水位被污染成 {floor!r}')

    def test_a_failure_is_not_charged_to_the_subscription_that_replaced_it(self):
        """旧账号那次失败记在新账号头上 = 按别人的毛病把一条好订阅推进退避
        (最长 24 小时),面板上还显示"连接失败" —— 新账号一次都没被访问过。"""
        s = self.sub()

        def harvest(jobs, **kw):
            self.repoint(s['id'])
            return {'example': {'entries': [], 'error': '登录态已失效,请重新登录'}}

        with patch.object(x_browser, 'harvest', harvest):
            summary = sync.collect('social', FetchScope(force=True))
        row = reg.subscription('social', s['id'])
        self.assertEqual(('', 0, 0), (row['last_error'], row['failures'], row['retry_at']))
        self.assertEqual(([], 1), (summary['errors'], summary['repointed']))

    def test_a_subscription_deleted_mid_round_is_not_written_back(self):
        s = self.sub()

        def harvest(jobs, **kw):
            reg.delete_subscription('social', s['id'])
            return {'example': {'entries': [self.entry()], 'error': '',
                                'watermark': '2026-10-07T00:00:00+00:00'}}

        with patch.object(x_browser, 'harvest', harvest):
            summary = sync.collect('social', FetchScope(force=True))
        self.assertEqual((0, 1), (summary['added'], summary['repointed']))
        with reg.db() as c:
            self.assertEqual(0, c.execute('SELECT count(*) FROM subscription_items').fetchone()[0])

    def test_the_old_accounts_partial_note_does_not_stay_on_the_new_one(self):
        """换链接的那次 reset 清掉了 `last_success`,却留着上一轮那条「没读完」
        —— 而前端的状态链在 `last_success` 之前先看它,于是新账号那一行显示着旧
        账号的「部分同步」(审查实测,列为不拦验收项)。"""
        s = self.sub()
        self.collect(example={'entries': [], 'error': '', 'incomplete': '滚动次数用完'})
        self.assertIn('滚动次数', reg.subscription('social', s['id'])['last_note'])
        self.repoint(s['id'])
        row = reg.subscription('social', s['id'])
        self.assertEqual(('', ''), (row['last_note'], row['last_success']))

    def test_renaming_during_the_round_does_not_throw_the_result_away(self):
        """判据是**身份**(链接),不是"这一行有没有被动过"。改个名字、换个分组
        都不影响这一轮抓的是谁 —— 连这种也作废的话,正常同步会被一次改名白白
        丢掉一轮(而登录态通道一轮是十几分钟)。"""
        s = self.sub()

        def harvest(jobs, **kw):
            reg.save_subscription('social', {'name': '换个名字'}, s['id'])
            return {'example': {'entries': [self.entry()], 'error': '',
                                'watermark': '2026-10-07T00:00:00+00:00'}}

        with patch.object(x_browser, 'harvest', harvest):
            summary = sync.collect('social', FetchScope(force=True))
        self.assertEqual((1, 0), (summary['added'], summary['repointed']))
        self.assertEqual('2026-10-07T00:00:00+00:00',
                         x_channels.watermark(reg.subscription('social', s['id']),
                                              x_channels.BROWSER))


class LateEditTests(BrowserRound):
    """**改指赶在身份检查之后**:那半个窗口只能靠写锁关掉,不是靠检查写在哪。

    上面那几条排的是"修改先提交、再做身份检查",它们在 `_repointed` 落地那一轮
    就绿了。可身份检查自己只是一次 SELECT —— python 的 `sqlite3` 在 legacy
    `isolation_level` 下**压根不开事务、不持任何锁**,于是"检查通过了、第一笔
    写入还没发生"这一瞬间照样插得进另一个连接的提交,后果和这道判据本来要挡的
    事故一模一样(2026-10-08 第 3 轮审查实测:检查那一刻 `in_transaction=false`,
    随后真 PATCH 以 200 提交,旧账号的 `/status/900` 照样记给了新账号、
    `next_sync_selection=[]`)。这里排的就是这后半个交错,**要真的两个连接**:
    同一个连接上压根排不出来。
    """

    def race(self, **by_handle):
        """真跑一轮,并让那次改指在**身份检查返回之后**才去提交。"""
        s = self.sub()
        done, seen, err = threading.Event(), {}, []

        def edit_once():
            try:
                self.repoint(s['id'], 'NewExample')
            except Exception as e:                    # noqa: BLE001
                err.append(e)
            finally:
                done.set()

        edit = threading.Thread(target=edit_once)
        real = sync._repointed

        def looked(c, source):
            # **屏障摆在身份那次 SELECT 刚读完的位置**,不是整个 `_claim_write`
            # 之后:那时候锁早拿到了,于是"先 SELECT 再取锁"这种写法也能混过去
            # —— 而它的窗口正好在这两句之间。
            verdict = real(c, source)
            seen['in_transaction'] = c.in_transaction
            edit.start()
            # 没拿到写锁的话这次保存当场就提交完了(实测毫秒级);拿到了的话它
            # 会一直等到我们提交 —— 这一秒只是给它机会抢在第一笔写入之前落下。
            seen['edit_committed_first'] = done.wait(1.0)
            return verdict

        with patch.object(sync, '_repointed', looked):
            summary = self.collect(**by_handle)
        edit.join(30)
        self.assertFalse(edit.is_alive(), '那次改指一直没能完成')
        self.assertEqual([], err, '那次改指报错了')
        self.assertTrue(seen, '这一轮压根没走到身份检查')
        return s, summary, seen

    def test_an_edit_after_the_identity_check_leaves_the_new_account_clean(self):
        s, summary, seen = self.race(example={
            'entries': [self.entry()], 'error': '',
            'watermark': FRESH.strftime('%Y-%m-%dT%H:%M:%SZ')})
        row = reg.subscription('social', s['id'])
        floor = x_channels.watermark(row, x_channels.BROWSER)
        earlier = tweet('901', handle='NewExample',
                        when=(FRESH - timedelta(hours=1)).strftime('%Y-%m-%dT%H:%M:%SZ'))
        picked = x_browser._select([earlier], x_browser._utc(floor) if floor else None,
                                   'NewExample')
        # **末态先断言**:注入旧版本时该红的是"后果",不是"锁取了没有" —— 只验
        # 后者的话,一个把检查挪到别处、什么都没防住的实现也能绿。
        self.assertEqual(0, reg.items('social', s['id'])['total'], '旧账号的推文记给了新账号')
        self.assertEqual('', floor, '旧账号的水位写进了新账号那个键')
        self.assertEqual(['https://x.com/NewExample/status/901'],
                         [e['url'] for e in picked], f'水位被污染成 {floor!r}')
        self.assertEqual('', row['last_success'], '替新账号宣布它同步成功了')
        self.assertEqual((True, False), (seen['in_transaction'], seen['edit_committed_first']),
                         '身份检查没有待在写事务里')

    def test_a_failure_after_the_identity_check_is_not_charged_either(self):
        """失败那一侧同一个缺陷:旧账号的错误和退避落在一条从没被访问过的订阅上。"""
        s, summary, seen = self.race(example={'entries': [],
                                              'error': '登录态已失效,请重新登录'})
        row = reg.subscription('social', s['id'])
        self.assertEqual(('', 0, 0), (row['last_error'], row['failures'], row['retry_at']),
                         '旧账号那次失败记在了新账号头上')
        # 这一轮确实没抓到旧账号那边的东西,阶段里照实说 —— 清掉的是**新订阅
        # 那一行**的状态(用户自己改指带来的),不是这一轮的成绩单。
        self.assertEqual(1, len(summary['errors']))
        self.assertEqual((True, False), (seen['in_transaction'], seen['edit_committed_first']))

    def test_the_write_lock_is_taken_before_the_identity_is_read(self):
        """直测一道:`BEGIN IMMEDIATE` 必须排在身份那次 SELECT **之前**。

        race 那两条看的是末态,这一条看的是机制本身 —— 持锁期间另一个连接的保存
        必须被挡在外面等。反过来写("先 SELECT 再升级成写事务")在 WAL 下报的是
        `SQLITE_BUSY_SNAPSHOT`,`busy_timeout` 管不着它。
        """
        s, seen = self.sub(), []
        real = sync._repointed
        other = threading.Thread(target=lambda: self.repoint(s['id'], 'NewExample'))

        def looked(c, source):
            seen.append(c.in_transaction)
            return real(c, source)

        try:
            with patch.object(sync, '_repointed', looked), reg.db() as c:
                self.assertTrue(sync._claim_write(c, s))
                other.start()
                other.join(1.0)
                self.assertTrue(other.is_alive(), '持锁期间另一个连接照样写得进去')
                self.assertEqual([True], seen, '读身份的时候写锁还没拿到手')
        finally:
            if other.ident is not None:                # 断言在 start 之前红掉时
                other.join(30)
        self.assertEqual('https://x.com/newexample',
                         reg.subscription('social', s['id'])['url'])


if __name__ == '__main__':
    unittest.main()
