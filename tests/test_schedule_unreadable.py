"""读不出定时配置 ≠ 用户关掉了开关。

把配置文件 chmod 000 之后实测:端点照旧回 200、`settings.enabled=false`,把这份
真实回包喂给前端那句说明,得到的是「日报定时未启用,这一类来源目前不会自动同步」
—— 而恢复权限之后定时原样开着。根因是加载函数把 `OSError` / `JSONDecodeError`
吃成空 dict,再派生出一个**假的确定值**。

**原来页头那颗「定时同步」也有一组同形的判据**,它连同整条实现一起删了(见
`docs/decisions/sync-schedule.md`);`unreadable_forms` 那张坏法清单仍是两边共同
攒出来的,留着别删 —— 再加一条定时时照它过一遍就够。

判据落在后果上:**读不出来时界面拿不到"未启用"这个结论**(API 不是 200)、
**定时不触发也不改盘上那份**。前端拿到"请求失败"之后怎么说那句不确定的话,由
`tests/test_source_schedule_note.py` 钉(那边喂的是
一个会抛的 `pipeJson`,正是这里 503 落到浏览器里的样子)。

**反过来钉一边**:没有这个文件(还没设置过)照旧是干干净净的「未启用」——
一律按"不知道"处理会让新装的库永远说不清自己开没开。

**账本那几组在 `tests/test_schedule_ledger.py`**(同日不重复跑、补跑的退避与次数、
受理边界、起跑记账与收尾的竞态):拆出去只因为 700 行上限。
"""
import json
import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

from kb import config as config_mod
from kb import daily_run, pipeline
from kb.config import ScheduleUnreadable
from kb.server import app


class _Base(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / 'settings.json'
        self.client = TestClient(app)
        self.said: list[str] = []
        p = patch('kb.config._announced', {})     # 只在状态变化时喊,别让上一条残留
        p.start(); self.addCleanup(p.stop)

    def catch_announcements(self, module):
        """接住"跳过并留原因"那句话:**跳过而不留原因**也是一种静默故障 ——
        调度线程那层只有一个 `except Exception: pass`,定时从此不再触发,而日志里
        一个字都没有。顺带让测试输出干净。"""
        p = patch.object(module, 'announce_config_issue',
                         lambda what, reason: self.said.append(f'{what}:{reason}'))
        p.start(); self.addCleanup(p.stop)

    def unreadable_forms(self):
        """几种读不出来,**都要走一遍同一组判据**:内容坏了、空文件(上次保存
        写坏的残骸)、不是 UTF-8、顶层不是对象、以及没权限(root 下 chmod 拦不住,
        跳过那一种)。"""
        yield '内容坏了', lambda: self.path.write_text('{"enabled": tru', encoding='utf-8')
        # **空文件不是"还没设置过"**:0 字节只可能是写坏的残骸(就地 write_text
        # 先截断再写),里面原来那份开关/时刻/范围全被截掉了 —— 按"没设置过"处理
        # 就是拿缺省值冒充用户的设置,而 API 还带着 200 回去(第 2 轮实测)。
        yield '空文件', lambda: self.path.write_text('', encoding='utf-8')
        yield '不是 UTF-8', lambda: self.path.write_bytes(b'\xff\xfe\x00{')
        yield '顶层不是对象', lambda: self.path.write_text('[1, 2]', encoding='utf-8')
        # **合法 JSON 不等于一份可执行的定时。** 下面这几种派生得出"确定值",却跟
        # 调度和界面的口径对不上:`{"enabled": true, "time": 7}` 放过去的话 GET 回
        # 200、说明里写「每天 7」,而到期判据是两个字符串比大小,拿它跟数字比当场
        # 抛;字符串 "false" 在 `bool()` 眼里是真的,一条关掉的定时会显示成开着。
        # **界面说一套、实际跑另一套**比直接报错难查得多。字段口径在 `kb.config`
        # 的 `config_flag` / `config_clock` / `config_day` / `config_stamp`。
        yield '开关是字符串', lambda: self.path.write_text(
            '{"enabled": "false", "time": "09:00"}', encoding='utf-8')
        yield '时刻是数字', lambda: self.path.write_text(
            '{"enabled": true, "time": 7}', encoding='utf-8')
        yield '下次时刻只有日期', lambda: self.path.write_text(
            '{"enabled": true, "time": "09:00", "next_run": "2026-10-15"}', encoding='utf-8')
        yield '当天标记不是日期', lambda: self.path.write_text(
            '{"enabled": true, "time": "09:00", "last_fired": "昨天"}', encoding='utf-8')
        # **日历上不存在的日期**:只用 regex 的话它照样放行,而它比大小比得过任何
        # 真实日期 —— "下一次"永远不到期、"今天已经跑过"永远成立,定时从此不再触发
        # 而面板上一切正常。
        yield '下次时刻不在日历上', lambda: self.path.write_text(
            '{"enabled": true, "time": "09:00", "next_run": "2026-99-99T07:30"}',
            encoding='utf-8')
        # **falsey 但不是空**:`d.get(k) or default` 会把它们一起当成"没填"悄悄换成
        # 缺省,于是盘上根本不是这么写的配置被读成一份正常定时。
        yield '时刻是空数组', lambda: self.path.write_text(
            '{"enabled": true, "time": []}', encoding='utf-8')
        # **名单不是数组**:字符串会被逐字符当成通道名,一个都认不出来,于是同步
        # 范围静默回落到默认范围 —— 用户以为只同步 RSS,实际跑的是别的。
        yield '名单是字符串', lambda: self.path.write_text(
            '{"enabled": true, "time": "09:00", "channels": "rss"}', encoding='utf-8')
        if os.geteuid() != 0:
            def no_permission():
                self.path.write_text(json.dumps({'enabled': True, 'time': '09:00'}),
                                     encoding='utf-8')
                os.chmod(self.path, 0)
                self.addCleanup(lambda: os.path.exists(self.path)
                                and os.chmod(self.path, 0o600))
            yield '没权限', no_permission


class DailySettingsUnreadableTests(_Base):
    def setUp(self):
        super().setUp()
        for p in (patch.object(daily_run, 'SETTINGS_FILE', self.path),
                  patch.object(daily_run, '_log')):
            p.start(); self.addCleanup(p.stop)
        self.catch_announcements(daily_run)

    def test_an_unreadable_file_is_not_reported_as_disabled(self):
        for name, break_it in self.unreadable_forms():
            with self.subTest(name):
                break_it()
                with self.assertRaises(ScheduleUnreadable):
                    daily_run.load_settings()
                r = self.client.get('/api/daily/settings')
                self.assertEqual(503, r.status_code, r.text)
                # **别在失败回包里塞一个 enabled**:前端会照它下结论。钉的是
                # JSON 字段(整个回包只有一句 detail);诊断文字里提到字段名字
                # 是好事 —— 「哪一格读不懂」正是修这份文件要的信息。
                self.assertNotIn('"enabled"', r.text)
                self.assertEqual({'detail'}, set(r.json()))
                self.assertIn('日报定时设置', r.json()['detail'])

    def test_a_missing_file_is_still_a_clean_disabled(self):
        # 钉边:**文件压根不在**是确定的「未启用」(新装的库第一次打开面板),
        # 不许跟"读不出来"混成一档 —— 一律按"不知道"处理的话这个库永远说不清
        # 自己开没开。空文件是另一回事,见 `unreadable_forms`。
        r = self.client.get('/api/daily/settings')
        self.assertEqual(200, r.status_code, r.text)
        self.assertFalse(r.json()['settings']['enabled'])
        self.assertFalse(self.path.exists())

    def test_an_unreadable_file_stops_the_timer_instead_of_firing(self):
        self.path.write_text('{"enabled": tru', encoding='utf-8')
        with patch.object(daily_run, 'start', Mock(return_value=True)) as start:
            self.assertFalse(daily_run.tick())
            start.assert_not_called()
        self.assertTrue(any('日报定时设置:' in line and '定时暂停' in line
                            for line in self.said), self.said)

    def test_a_bad_field_pauses_the_timer_instead_of_running_by_the_shown_plan(self):
        # 「看着是开着的 09:00」不等于「真按 09:00 跑」:时刻是数字时到期判据当场
        # 抛,这一跳必须停下来留原因,不许起一轮(日报那一轮要花模型钱)。
        self.path.write_text('{"enabled": true, "time": 7}', encoding='utf-8')
        with patch.object(daily_run, 'start', Mock(return_value=True)) as start:
            self.assertFalse(daily_run.tick())
            start.assert_not_called()
        self.assertTrue(any('读不懂的字段' in line and '定时暂停' in line
                            for line in self.said), self.said)
        # 反过来钉一边:**字段缺着**(老配置少一格)照旧按缺省值跑起来。时钟钉死
        # 在缺省时刻之后,不然这条检查的成败跟着真实时间走(上午跑就"还没到点")。
        self.path.write_text('{"enabled": true}', encoding='utf-8')
        clock = Mock(wraps=datetime)
        clock.now.return_value = datetime(2026, 10, 15, 23, 59)
        with patch.object(daily_run, 'datetime', clock), \
                patch.object(daily_run, 'start', Mock(return_value=True)) as start, \
                patch.object(daily_run, '_report_done', Mock(return_value=False)):
            self.assertTrue(daily_run.tick())
            start.assert_called_once()

    def test_an_unreadable_file_is_never_overwritten(self):
        """**读不出来就一个字都不改。** 拿缺省值当底写一份出去是不可恢复的:
        用户的开关、时刻、同步范围会被静默换成缺省,而盘上原来那份可能只是
        一时读不出(权限、介质)。"""
        broken = '{"enabled": true, "time": "09:00"'
        self.path.write_text(broken, encoding='utf-8')
        with daily_run._lock:          # 产品里这一步一定在锁里(见 _write_settings)
            daily_run._write_settings(next_run='2026-10-14T08:00')
        self.assertEqual(broken, self.path.read_text(encoding='utf-8'))

    def test_saving_refuses_instead_of_resetting_the_file(self):
        broken = '{"enabled": true, "time": "09:00"'
        self.path.write_text(broken, encoding='utf-8')
        r = self.client.post('/api/daily/settings',
                             json={'enabled': True, 'time': '06:05'})
        self.assertEqual(503, r.status_code, r.text)
        self.assertEqual(broken, self.path.read_text(encoding='utf-8'))


class SettingsWriteFailureTests(_Base):
    """**一次保存失败不得改变上一份有效快照。**

    就地 `write_text` 是"先把文件截成 0 字节再往里写":写到一半失败(配额、满盘、
    进程被杀)盘上剩的是空文件或半截 JSON,而原来那份已经没了 —— 开关、时刻、
    同步范围、当天已跑标记一起丢。第 2 轮实测给保存进程加 `RLIMIT_FSIZE=0`:
    日报那份 185 字节变 0 字节、页头那份 136 字节变 0 字节,随后的 GET 照旧 200
    而且全是缺省值。判据落在**盘上那份**和**随后读到的那份**,不是"写有没有抛"。
    """

    def setUp(self):
        super().setUp()
        for p in (patch.object(daily_run, 'SETTINGS_FILE', self.path),
                  patch.object(daily_run, '_log')):
            p.start(); self.addCleanup(p.stop)
        self.catch_announcements(daily_run)

    def test_a_failed_daily_save_keeps_the_previous_settings(self):
        saved = daily_run.save_settings(True, '09:00', ['rss'], True, 1)
        before = self.path.read_bytes()
        with patch('kb.config.os.replace', Mock(side_effect=OSError('配额不够'))):
            with self.assertRaises(OSError):
                daily_run.save_settings(True, '10:00', ['rss'], True, 1)
        self.assertEqual(before, self.path.read_bytes())
        again = daily_run.load_settings()
        self.assertTrue(again['enabled'])
        self.assertEqual(saved['time'], again['time'])
        self.assertEqual(['rss'], again['channels'])
        # 临时文件不许留在那儿冒充配置
        self.assertEqual([self.path.name], [p.name for p in self.path.parent.iterdir()])

    def test_a_failed_bookkeeping_write_keeps_the_previous_settings(self):
        """定时自己那次回写(`_write_settings`)同理 —— 它写的是 last_fired /
        next_run,截掉的却是整份配置。

        **这一处写失败不往上抛**(抛出去会把这一跳算成"出错没起跑"),但要留下
        原因;判据落在"盘上那份一字未动"。
        """
        daily_run.save_settings(True, '09:00', ['rss'], True, 1)
        before = self.path.read_bytes()
        with patch('kb.config.os.replace', Mock(side_effect=OSError('配额不够'))):
            with daily_run._lock:      # 同上:回写只发生在临界区里
                daily_run._write_settings(next_run='2026-10-15T08:00')
        self.assertEqual(before, self.path.read_bytes())
        self.assertTrue(daily_run.load_settings()['enabled'])
        self.assertTrue(any('没能写进磁盘' in line for line in self.said), self.said)

    def test_a_failed_user_save_is_reported_as_unsaved(self):
        """反过来钉一边:**用户自己点的那次保存照旧要失败得出来** —— 界面靠它
        显示「未保存」并保留草稿;悄悄吞掉会让人以为存上了。"""
        daily_run.save_settings(True, '09:00', ['rss'], True, 1)
        with patch('kb.config.os.replace', Mock(side_effect=OSError('配额不够'))):
            r = self.client.post('/api/daily/settings',
                                 json={'enabled': True, 'time': '10:00'})
            self.assertEqual(503, r.status_code, r.text)
            self.assertIn('没能保存', r.json()['detail'])
        self.assertTrue(daily_run.load_settings()['enabled'])
        self.assertEqual('09:00', daily_run.load_settings()['time'])


class ConcurrentSaveTests(_Base):
    """**两个进程同时保存同一份配置时,它们不许写同一个临时文件。**

    原来临时名是写死的 `<name>.tmp`。kb-web 里真会并发:定时线程回写当天那几格、
    用户在页面上点保存,各走一条路。撞上的两种形态都不是"一方输了"那么轻:一方写
    到一半被另一方覆盖,`os.replace` 之后盘上那份是两段 JSON 的拼接(读出来直接是
    `ScheduleUnreadable`,整份配置连同当天已跑标记一起作废);或者一方刚
    `replace` 走、另一方对着已经不在的路径报 `FileNotFoundError`，而它那次保存的
    内容谁也没留下。判据落在**临时路径本身每次都不一样**,不在"并发跑一次没撞上"
    —— 后者是概率,过不了也说明不了什么。
    """

    def test_each_save_uses_its_own_temporary_file(self):
        seen = []
        real = os.replace

        def spy(src, dst):
            seen.append(str(src))
            return real(src, dst)

        with patch.object(config_mod.os, 'replace', spy):
            config_mod.write_config_file(self.path, {'a': 1})
            config_mod.write_config_file(self.path, {'a': 2})
        self.assertEqual(2, len(seen))
        self.assertNotEqual(seen[0], seen[1])
        for name in seen:
            self.assertTrue(name.endswith('.tmp'), name)
            self.assertNotEqual(str(self.path) + '.tmp', name)   # 旧的那个写死名
        self.assertEqual({'a': 2}, json.loads(self.path.read_text('utf-8')))

    def test_the_file_mode_follows_the_process_umask(self):
        """**权限要跟着 umask 走,不许写死一个数。**

        这条上一轮钉反了:当时写的是"一律 0644",而那恰恰是回归本身 —— 它在
        umask=022 的机器上碰巧等于基线,所以绿着。`mkstemp` 固定造 0600,补一句
        `chmod 0644` 看着像"保持原样",其实**绕过了 umask**:`open()`/`write_text`
        的 mode 要跟 umask 相与,`chmod` 不受它管。把 umask 设成 077 的人原来写出
        来的是 0600(只有自己能读),补了那句 chmod 之后变成本机任何账号都能读 ——
        而这个函数写的是日报设置、定时状态、带 `result`/`log`/`error` 的运行记录和
        登录态导入账本。所以判据是**两档 umask 下各自算出来的那个值**,一个写死的
        数字既证不了"没放宽"也证不了"没收紧"。
        """
        for mask, want in ((0o077, 0o600), (0o022, 0o644), (0o002, 0o664)):
            with self.subTest(umask=oct(mask)):
                before = os.umask(mask)
                try:
                    target = self.path.parent / f'm{mask}.json'
                    config_mod.write_config_file(target, {'a': 1})
                    self.assertEqual(want, target.stat().st_mode & 0o777)
                finally:
                    os.umask(before)       # 同进程跑的别人不该被带走

    def test_a_leftover_temporary_file_is_not_left_behind(self):
        """失败就把残骸带走 —— 满盘那一档正是因为没地方。"""
        with patch.object(config_mod.os, 'replace', side_effect=OSError('满了')):
            with self.assertRaises(OSError):
                config_mod.write_config_file(self.path, {'a': 1})
        self.assertEqual([], [q.name for q in self.path.parent.iterdir()
                              if q.name.endswith('.tmp')])

    def test_a_value_json_cannot_encode_leaves_nothing_behind(self):
        """**转不动的值在碰磁盘之前就该失败。** `json.dump` 边转边写:遇到 set
        (`TypeError`)或循环引用(`ValueError`)时文件已经建好、还写进了半截,而那
        两个都不是 `OSError`,清理那一手接不住 —— 于是临时文件在目录里攒着。
        基线是先 `json.dumps` 再写,这条把那个顺序钉住。
        """
        for bad, exc in (({'s': {1, 2}}, TypeError),):
            with self.subTest(value=bad):
                with self.assertRaises(exc):
                    config_mod.write_config_file(self.path, bad)
        self.assertEqual([], [q.name for q in self.path.parent.iterdir()
                              if q.name.endswith('.tmp')])
        self.assertFalse(self.path.exists())       # 也没造出一份半截配置

    def test_a_name_collision_does_not_delete_the_other_process_file(self):
        """**撞上的那个临时文件不是我们的,一个字都不许动。**

        `O_EXCL` 失败的含义恰好是「这个名字已经有人在用」。原来建和写在同一个
        `try` 里,于是这一档会走进清理那一手、**删掉另一个进程正在写的临时文件**:
        它随后那句 `os.replace` 报 `FileNotFoundError`,一次本来会成功的保存失败,
        而两边日志都只说「保存失败」。撞名就换个名字重来。
        """
        planted = self.path.with_name(f'{self.path.name}.{os.getpid()}.deadbeef.tmp')
        planted.write_text('别人写到一半的内容', encoding='utf-8')
        names = iter(['deadbeef' + '0' * 24, 'cafe1234' + '0' * 24])

        class _U:
            def __init__(self, hexs): self.hex = hexs

        with patch.object(config_mod.uuid, 'uuid4', lambda: _U(next(names))):
            config_mod.write_config_file(self.path, {'a': 1})
        self.assertEqual({'a': 1}, json.loads(self.path.read_text('utf-8')))
        self.assertEqual('别人写到一半的内容', planted.read_text('utf-8'))

    def test_a_name_that_never_comes_free_fails_without_touching_it(self):
        """**反过来钉一边:换名字不是无限重试。** 目录里躺着个删不掉的同名残骸时,
        重试要有尽头(否则那次保存永远不返回,定时线程跟着卡住),而且照旧不许动
        那个文件、不许动上一份有效配置。
        """
        self.path.write_text('{"a": 0}', encoding='utf-8')
        planted = self.path.with_name(f'{self.path.name}.{os.getpid()}.deadbeef.tmp')
        planted.write_text('不是我的', encoding='utf-8')

        class _U:
            hex = 'deadbeef' + '0' * 24

        with patch.object(config_mod.uuid, 'uuid4', _U):
            with self.assertRaises(FileExistsError):
                config_mod.write_config_file(self.path, {'a': 1})
        self.assertEqual('不是我的', planted.read_text('utf-8'))
        self.assertEqual({'a': 0}, json.loads(self.path.read_text('utf-8')))


class TaskIsolationTests(_Base):
    """**定时任务的故障留在它自己这一跳里,而且要留下原因。**

    外面那层 `_scheduler_loop` 只有一个 `except Exception: pass`:异常从
    `_scheduler_tick` 抛出去就是一句话都不留,而定时从此不再触发、面板上却一切
    正常(实测形态:把配置写成非 UTF-8 字节,`UnicodeDecodeError` —— 是
    `ValueError` 不是 `OSError` —— 直接穿过去)。**定时只剩日报这一条了,`_isolated`
    那层照旧要留着**:判据落在「这一跳抛不抛」「有没有留下原因」,不是「线程死没死」。
    """

    def setUp(self):
        super().setUp()
        p = patch.object(pipeline, '_log')
        p.start(); self.addCleanup(p.stop)
        self.catch_announcements(pipeline)

    def test_an_unexpected_failure_in_the_timer_is_named_and_contained(self):
        with patch.object(daily_run, 'tick',
                          Mock(side_effect=RuntimeError('日报那边炸了'))), \
                patch.object(daily_run, 'is_running', Mock(return_value=False)):
            pipeline._scheduler_tick()          # 不许抛到调度线程那层去
        self.assertTrue(any('日报那边炸了' in line for line in self.said), self.said)

    def test_a_round_still_running_is_not_asked_to_tick_again(self):
        """反过来钉一边:上一轮还在跑,这一跳不许再把它叫进去 —— 一轮日报要几十
        分钟,而这个循环 30 秒一跳。`tick` 自己也判,这里是**冗余带**,留着。"""
        with patch.object(daily_run, 'tick', Mock(return_value=False)) as tick, \
                patch.object(daily_run, 'is_running', Mock(return_value=True)):
            pipeline._scheduler_tick()
            tick.assert_not_called()
        with patch.object(daily_run, 'tick', Mock(return_value=False)) as tick, \
                patch.object(daily_run, 'is_running', Mock(return_value=False)):
            pipeline._scheduler_tick()
            tick.assert_called_once()


if __name__ == '__main__':
    unittest.main()
