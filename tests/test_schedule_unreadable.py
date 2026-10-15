"""读不出定时配置 ≠ 用户关掉了开关。两条定时**各有一组**。

第 1 轮审查把两个配置文件 chmod 000 之后实测:两个端点都回 200,日报
`settings.enabled=false`、页头 `enabled=false`,把这两份真实回包喂给前端那句
说明,得到的是「日报定时未启用,这一类来源目前不会自动同步」—— 而恢复权限之后
两条定时原样开着。根因是两个加载函数都把 `OSError` / `JSONDecodeError` 吃成
空 dict,再派生出一个**假的确定值**。

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
                  patch.object(pipeline, '_SCHED_FILE', self.path),
                  patch.object(daily_run, '_log'), patch.object(pipeline, '_log')):
            p.start(); self.addCleanup(p.stop)
        self.catch_announcements(daily_run)
        self.catch_announcements(pipeline)

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

    def test_a_failed_head_save_keeps_the_previous_schedule(self):
        saved = pipeline.save_schedule(True, '07:30', ['rss'])
        before = self.path.read_bytes()
        with patch('kb.config.os.replace', Mock(side_effect=OSError('配额不够'))):
            with self.assertRaises(OSError):
                pipeline.save_schedule(True, '10:00', ['rss'])
        self.assertEqual(before, self.path.read_bytes())
        again = pipeline.load_schedule()
        self.assertTrue(again['enabled'])
        self.assertEqual(saved['time'], again['time'])
        self.assertEqual(['rss'], again['channels'])

    def test_a_failed_bookkeeping_write_keeps_the_previous_settings(self):
        """定时自己那两次回写(`_write_settings` / `_mark_fired`)同理 ——
        它们写的是 last_fired / next_run,截掉的却是整份配置。

        **这两处写失败不往上抛**(抛出去会把这一跳算成"出错没起跑",紧接着另一条
        定时来抢同一把管道锁),但要留下原因;判据落在"盘上那份一字未动"。
        """
        daily_run.save_settings(True, '09:00', ['rss'], True, 1)
        before = self.path.read_bytes()
        with patch('kb.config.os.replace', Mock(side_effect=OSError('配额不够'))):
            with daily_run._lock:      # 同上:回写只发生在临界区里
                daily_run._write_settings(next_run='2026-10-15T08:00')
        self.assertEqual(before, self.path.read_bytes())
        self.assertTrue(daily_run.load_settings()['enabled'])
        self.assertTrue(any('没能写进磁盘' in line for line in self.said), self.said)
        self.said.clear()
        pipeline.save_schedule(True, '07:30', ['rss'])
        before = self.path.read_bytes()
        with patch('kb.config.os.replace', Mock(side_effect=OSError('配额不够'))):
            pipeline._mark_fired('2026-10-15')
        self.assertEqual(before, self.path.read_bytes())
        self.assertTrue(pipeline.load_schedule()['enabled'])
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
            r = self.client.post('/api/pipeline/schedule',
                                 json={'enabled': True, 'time': '10:00', 'channels': ['rss']})
            self.assertEqual(503, r.status_code, r.text)
        self.assertTrue(daily_run.load_settings()['enabled'])
        self.assertEqual('09:00', daily_run.load_settings()['time'])


class HeadScheduleUnreadableTests(_Base):
    def setUp(self):
        super().setUp()
        for p in (patch.object(pipeline, '_SCHED_FILE', self.path),
                  patch.object(pipeline, '_log')):
            p.start(); self.addCleanup(p.stop)
        self.catch_announcements(pipeline)

    def test_an_unreadable_file_is_not_reported_as_disabled(self):
        for name, break_it in self.unreadable_forms():
            with self.subTest(name):
                break_it()
                with self.assertRaises(ScheduleUnreadable):
                    pipeline.load_schedule()
                r = self.client.get('/api/pipeline/schedule')
                self.assertEqual(503, r.status_code, r.text)
                self.assertNotIn('"enabled"', r.text)      # 见日报那组同一处
                self.assertEqual({'detail'}, set(r.json()))
                self.assertIn('页头的定时同步设置', r.json()['detail'])

    def test_a_missing_file_is_still_a_clean_disabled(self):
        r = self.client.get('/api/pipeline/schedule')
        self.assertEqual(200, r.status_code, r.text)
        self.assertFalse(r.json()['enabled'])
        self.assertFalse(self.path.exists())

    def test_an_unreadable_file_stops_the_timer_instead_of_firing(self):
        self.path.write_text('{"enabled": tru', encoding='utf-8')
        with patch.object(pipeline, 'start_pipeline', Mock(return_value=True)) as start:
            self.assertFalse(pipeline._fire_head_schedule())
            start.assert_not_called()
            # 整跳也不许因此抛出去:调度线程那层只有一个 `except Exception: pass`,
            # 抛到那里的后果是**日报那一档跟着被跳过**,而且一句话都不留。
            with patch.object(daily_run, 'tick', Mock(return_value=False)) as tick, \
                    patch.object(daily_run, 'is_running', Mock(return_value=False)):
                pipeline._scheduler_tick()
                tick.assert_called_once()
        self.assertTrue(any('页头的定时同步设置:' in line and '定时暂停' in line
                            for line in self.said), self.said)

    def test_an_unreadable_file_is_never_overwritten(self):
        broken = '{"enabled": true, "time": "07:30"'
        self.path.write_text(broken, encoding='utf-8')
        pipeline._mark_fired('2026-10-14')
        self.assertEqual(broken, self.path.read_text(encoding='utf-8'))

    def test_saving_refuses_instead_of_resetting_the_file(self):
        broken = '{"enabled": true, "time": "07:30"'
        self.path.write_text(broken, encoding='utf-8')
        r = self.client.post('/api/pipeline/schedule',
                             json={'enabled': True, 'time': '06:05', 'channels': ['rss']})
        self.assertEqual(503, r.status_code, r.text)
        self.assertEqual(broken, self.path.read_text(encoding='utf-8'))


class TaskIsolationTests(_Base):
    """**一条定时的故障只影响它自己。**

    外面那层 `_scheduler_loop` 只有一个 `except Exception: pass`:异常从
    `_scheduler_tick` 抛出去的后果是**后面那条健康的定时一跳都不跑**,而且一句话
    都不留。第 2 轮实测把页头那份配置写成非 UTF-8 字节,`UnicodeDecodeError`
    (是 `ValueError` 不是 `OSError`)直接穿过去,日报那条明明开着,从此不再触发。
    判据落在「另一条还跑不跑」和「有没有留下原因」,不是「线程死没死」。
    """

    def setUp(self):
        super().setUp()
        for p in (patch.object(pipeline, '_SCHED_FILE', self.path),
                  patch.object(pipeline, '_log')):
            p.start(); self.addCleanup(p.stop)
        self.catch_announcements(pipeline)

    def test_a_broken_head_config_does_not_starve_the_daily_timer(self):
        for name, write in (('不是 UTF-8', lambda: self.path.write_bytes(b'\xff\xfe{')),
                            ('内容坏了', lambda: self.path.write_text('{oops')),
                            ('空文件', lambda: self.path.write_text(''))):
            with self.subTest(name):
                self.said.clear()
                write()
                with patch.object(daily_run, 'tick', Mock(return_value=False)) as tick, \
                        patch.object(daily_run, 'is_running', Mock(return_value=False)):
                    pipeline._scheduler_tick()          # 不许抛
                    tick.assert_called_once()           # 日报那条照常轮到
                self.assertTrue(any('页头的定时同步设置' in line for line in self.said),
                                self.said)

    def test_an_unexpected_failure_in_one_task_is_named_and_contained(self):
        # 读不出配置之外的意外(这里让它直接抛)同样不许带走另一条
        with patch.object(pipeline, '_fire_head_schedule',
                          Mock(side_effect=RuntimeError('说不清的毛病'))), \
                patch.object(daily_run, 'tick', Mock(return_value=False)) as tick, \
                patch.object(daily_run, 'is_running', Mock(return_value=False)):
            pipeline._scheduler_tick()
            tick.assert_called_once()
        self.assertTrue(any('说不清的毛病' in line for line in self.said), self.said)
        # 反过来钉一边:日报那条抛了也不许把异常捅到调度线程那层去
        self.said.clear()
        with patch.object(pipeline, '_fire_head_schedule', Mock(return_value=False)), \
                patch.object(daily_run, 'tick', Mock(side_effect=RuntimeError('日报那边炸了'))), \
                patch.object(daily_run, 'is_running', Mock(return_value=False)):
            pipeline._scheduler_tick()
        self.assertTrue(any('日报那边炸了' in line for line in self.said), self.said)


if __name__ == '__main__':
    unittest.main()
