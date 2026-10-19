"""历史/恢复/顺序那三族共用的摆盘 —— **判据一条都不在这里**。

分出来只为行数:那三个文件各自装自己的判据,而「怎么摆出一轮、怎么模拟重启、怎么让
某一份落盘写失败」是它们共用的一套。摆盘动了会同时改变三族的前提,所以改它之前先想
清楚三边。
"""
import json
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from kb import pipeline, pipeline_persist as persist, pipeline_state as state


class HistoryCase(TestCase):
    def setUp(self):
        self.saved = deepcopy(state._state)
        self.temp = TemporaryDirectory()
        self.dir = Path(self.temp.name)
        self.file_patch = patch.object(state, '_STATE_FILE', self.dir / 'state.json')
        self.file_patch.start()
        self.reset()

    def tearDown(self):
        self.file_patch.stop()
        self.temp.cleanup()
        state._state.clear()
        state._state.update(self.saved)
        # **进程内常驻那几格也要还回去**(口径同 `pipeline_round_fixture`
        # 的 `_PROCESS_GLOBALS`):`reset()` 只在 setUp 里清,留着的话这一族最后
        # 一条用例的欠账会漏进**别的文件** —— 那边每次重试都被「留档损坏,跑一次
        # 完整同步」顶回去,而它单跑永远是绿的。
        state._restored, state._restore_gap, state._seq_hwm = '', '', 0
        state._persist_error = ''

    def reset(self, run_id=''):
        # 「刚启动,还没从磁盘恢复过」。`running=True` 让恢复短路,所以这一句对自己
        # 摆 `_state` 的用例没有影响;要试重启那一档的用例把 running 改成 False。
        state._restored = ''
        state._restore_gap = ''
        # 完成序号的进程内高水位是模块级的,**跨用例会泄漏**:生产里它该跨轮累积,
        # 测试里不清的话「磁盘上发到第几号」这个前提当场不成立
        state._seq_hwm = 0
        # 出处跟着「开新一轮」一起署,和两个起跑入口一样(见 `pipeline_open.mark`)。
        state._state.update(
            running=True, stop_requested=False, stopped=False, run_kind='',
            seq_gen=state._SEQ_GEN,
            planned_stages=None, current_stage='', run_id=run_id,
            started_at='2026-10-14 09:00:00', finished_at='', crawl_filter={}, log=[],
            stages={name: {'status': 'pending', 'detail': ''} for name in state.STAGES})

    def restart(self):
        """模拟重启:内存清空、没跑过任何一轮,磁盘那份还在。"""
        self.reset()
        state._state.update(
            running=False, run_id='', started_at='', finished_at='', crawl_filter={},
            export_filter={}, crawl_retry={}, log=[],
            stages={name: {'status': '', 'detail': ''} for name in state.STAGES})

    def persisted_files(self):
        """一轮的终态会落到哪几份文件上。

        跟 `pipeline_state._SAVE_LABEL` 那几个留档出口一一对应(状态文件、当日账本、
        历史记录)。开工标记和失败名单各走自己的路,**不在这里** —— 把它们一起打掉
        会改掉这几局的前提(标记写不上就不是「跑过而没留下终态」了)。
        """
        from kb.pipeline_daybook import _book_file
        return (state._STATE_FILE, state._history_file(), _book_file())

    def only_state_write_fails(self):
        """状态文件写不上、另两份写得上 —— 磁盘上最新那一轮**只在历史里**。"""
        return self._write_fails_on(lambda path: path == state._STATE_FILE)

    def only_history_write_fails(self):
        """历史写不上、状态写得上 —— 那一轮**不在历史里**(位置证据就此缺失)。"""
        return self._write_fails_on(lambda path: path == state._history_file())

    def all_writes_fail(self, exc=None):
        """这一轮的终态**一份都没落下来**(开工标记照旧写得上,它是另一条路)。"""
        return self._write_fails_on(lambda path: path in self.persisted_files(), exc)

    def _write_fails_on(self, hit, exc=None):
        """**注入打在真实的那次 IO 上**(`Path.replace`,原子替换的最后一下)。

        打在某个模块的 `_write_atomic` 名字上是不行的:留档的三份各住在自己的模块
        里、各自 `from ... import _write_atomic`,那个名字**带的是绑定不是引用** ——
        历史那一份搬进 `kb/pipeline_history.py` 之后,`patch.object(state, ...)`
        当场拦不住它,而用例全绿(实测:8 条用例的前提一起失效,「那一轮没写进历史」
        变成了「写进去了」,红的地方却指向恢复逻辑)。
        """
        real = Path.replace
        exc = exc or PermissionError('read-only')

        def pick(tmp, target, *args, **kwargs):
            if hit(Path(target)):
                raise exc
            return real(tmp, target, *args, **kwargs)

        return patch.object(Path, 'replace', pick)

    def read_fails_on(self, hit):
        """**注入打在真实的那次 IO 上**(`Path.read_bytes`)。打在
        `_load_json_strict` 上的话「读不出来一律抛」那一道就被 patch 盖掉了。
        """
        real = Path.read_bytes

        def pick(path, *args, **kwargs):
            if hit(path):
                raise PermissionError('read-only')
            return real(path, *args, **kwargs)

        return patch.object(Path, 'read_bytes', pick)

    def run_round(self, run_id, export_status, detail, failures=None):
        """跑一轮 crawl + export,export 按给的档位收尾。

        **开工标记也落上** —— 那是两个起跑入口都做的第一件事(`mark_open`),而这里
        绕过起跑直接跑线程那一段。不落的话每个走到 `_end_run` 的用例都在一个「从来
        没有标记」的世界里,而生产里永远有。**传的是这一轮自己的身份**,和生产那两处
        一样(口径在 `pipeline_open.mark`:借上一轮的出处会让标记认不出身份)。
        """
        self.reset(run_id)
        state.mark_open(state._state)

        def crawl(_):
            state._stage('crawl', 'ok', '检查 51/51 个来源，新增 105 条')

        def export(_):
            state._stage('export', export_status, detail, failures=failures)

        with patch.object(pipeline, '_run_crawl', side_effect=crawl), \
             patch.object(pipeline, '_run_export', side_effect=export):
            pipeline._run_pipeline(['crawl', 'export'])
    def _legacy(self, rid, at, *, failed, started):
        """一份**旧格式**记录:没有世代标识,身份可有可无。

        这是被兼容的格式,不是坏文件 —— 字段、阶段、日志都齐,只是升级前的 writer
        没写身份/序号/世代。
        """
        run = {
            'finished_at': at, 'started_at': started,
            'run_kind': 'sync', 'stopped': False, 'planned_stages': list(state.STAGES),
            'stages': {name: {'status': 'error' if (failed and name == 'export') else 'ok',
                              'detail': '3 条翻译失败（服务返回 503）' if failed else '完成'}
                       for name in state.STAGES},
            'crawl_filter': {'channel': 'rss'},
            'log': [f'[{at[5:16]}] 管道结束'],
        }
        if rid:
            run['run_id'] = rid
        return run

    def _current(self, rid, seq, at, *, failed, started):
        """一份**当前格式**记录:身份和完成序号都带着出处(`seq_gen`)。

        跟 `_legacy` 同一套内容字段,差别只在那三个凭据 —— 两个辅助摆出来的两份
        可以内容逐字相同,好把判据逼到只能看凭据。
        """
        run = self._legacy(rid, at, failed=failed, started=started)
        run['run_seq'] = seq
        run['seq_gen'] = persist._SEQ_GEN
        return run

    def _put(self, saved, runs):
        state._STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        state._STATE_FILE.write_text(json.dumps(saved, ensure_ascii=False))
        state._history_file().write_text(json.dumps({'runs': runs}, ensure_ascii=False))
