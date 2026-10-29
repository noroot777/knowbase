# -*- coding: utf-8 -*-
"""**yt-dlp 取到了元数据,不许在写盘那一刻被我们自己丢掉。**

事故形态(真实请求实测):yt-dlp 退 0、45 个格式 21 个带音频,吐出的那一行元数据 JSON
有 11.55 MiB —— 其中 11.42 MiB 是 `automatic_captions`(自动语音字幕 × 177 种翻译语言 ×
每种好几个格式,2.3 万条长链接)。`diagnosis.redact` 原先**先量单行上限(4 MiB)再解析**,
整行换成 `[过长的输出已遮蔽]`,`media._single` 找不到 JSON,报「YouTube 元数据无效」。
**大小因视频而异**:同一轮里另一条只有 0.67 MiB,所以同步里有的成、「检查登录态」却必挂。

雪上加霜的是那个替身以 `[` 开头,被 `diagnostics()` 当成 JSON 行滤掉了 —— 面板上
「yt-dlp 原文」只剩同一份输出里那几句 JS 挑战警告,分类器照它们下结论、让人去更新
yt-dlp,而用户早就是最新版。

所以这一份钉三件事:**剪掉的恰好是选轨永远不会选的**(集合级)、**剪完再量**(端到端,
真子进程)、**真超限时说的是本机那道上限**(不许被同一份输出里的别的警告冒名顶替)。
最后那件事**每个读日志的解析口都要成立**(单条元数据、两条列频道的路、总量上限、归并层、
检查结论旁边那条并列提醒)—— 只修一个口,另外几个照旧让人去更新 yt-dlp(审查实测)。
"""
import io
import json
import os
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from kb.youtube import collector, diagnosis, login, media, toolchain
from tests.youtube_fixture import YouTubeFixture

#: 用户贴出来的那几句原话。它们和元数据**同一份输出**,真超限时不许由它们来下结论。
CHALLENGE = ('WARNING: [youtube] [jsc] Remote components challenge solver script (deno) and NPM package (deno) '
             'were skipped. These may be required to solve JS challenges.\n'
             'WARNING: Only images are available for download. use --list-formats to see them\n'
             'WARNING: Requested format is not available\n')
VIDEO = 'C4Sq3Rot8FU'
FORMATS = ('json3', 'srv1', 'srv2', 'srv3', 'ttml', 'vtt')


def caption(language, translate_to=''):
    query = f'v={VIDEO}&lang={language}&kind=asr&signature=' + 'S' * 81 + '&sparams=' + 'p' * 39
    return [{'ext': ext, 'name': language,
             'url': f'https://www.youtube.com/api/timedtext?{query}&fmt={ext}'
                    + (f'&tlang={translate_to}' if translate_to else '')}
            for ext in FORMATS]


def metadata(language='en', languages=177, copies=22):
    """**按真实那一份的形状造**:原语言两条不带 `tlang`,其余全是现场翻译、量大到超过上限。

    `copies` 是同一种翻译语言被几个客户端各造一遍的份数(真实那一份平均每种语言 130 条)。
    混进一个**同一个键里既有原文也有译文**的键,那是最容易剪错的形状。
    """
    automatic = {language: caption(language), f'{language}-orig': caption(language)}
    for number in range(languages):
        code = f'x{number:03d}'
        automatic[code] = caption(language, code) * copies
    automatic['mixed'] = caption('mixed') + caption(language, 'mixed') * copies
    return {'id': VIDEO, 'title': '一条普通视频', 'duration': 1860, 'language': language,
            'formats': [{'format_id': '140', 'acodec': 'mp4a', 'vcodec': 'none', 'cookies': 'SID=secret'}],
            'subtitles': {'zh': caption('zh')}, 'automatic_captions': automatic}


class TrimmingKeepsEveryTrackThatCouldBePicked(unittest.TestCase):
    """**写盘前那一剪,剪掉的恰好是 `media.tracks` 永远不会选的。**"""

    def setUp(self):
        self.original = metadata()
        self.line = (json.dumps(self.original) + '\n').encode('utf-8')
        self.assertGreater(len(self.line), diagnosis._LINE_CAP,
                           '原样那一行本来就要超过上限,否则这一份什么都没验')

    def written(self):
        return diagnosis.redact(self.line)

    def test_the_line_is_measured_after_it_is_trimmed(self):
        written = self.written()
        self.assertNotIn(diagnosis.OVERSIZE.encode('utf-8'), written, '剪完明明够小,却还是按原样判成了过长')
        self.assertLessEqual(len(written), diagnosis._LINE_CAP)
        self.assertEqual(VIDEO, json.loads(written)['id'])

    def test_every_original_language_picks_the_same_tracks(self):
        """**集合级那一条**:原始数据里出现过的**每一种**语言当原语言,剪前剪后选出来的一样。

        只验 `en` 一种的话,「剪的判据和选轨的判据其实不是同一份」这件事永远红不出来 ——
        剪错的那几种恰恰是没人当原语言用的。
        """
        trimmed = json.loads(self.written())
        languages = {key.split('-')[0] for key in self.original['automatic_captions']} | {''}
        self.assertGreater(len(languages), 100, '一种语言都没循环到,下面全是空转')
        for language in sorted(languages):
            with self.subTest(language=language):
                before = {**self.original, 'language': language}
                after = {**trimmed, 'language': language}
                for automatic in (True, False):
                    self.assertEqual(media.tracks(before, automatic), media.tracks(after, automatic))

    def test_an_original_item_beside_its_translations_is_kept(self):
        """反过来钉一边:**按条目剪,不按键剪。** 同一个键里原文和译文混着时,原文那几条照旧在。"""
        kept = json.loads(self.written())['automatic_captions']['mixed']
        self.assertEqual(caption('mixed'), kept)

    def test_nothing_else_is_touched_and_credentials_still_go(self):
        """再钉一边:人工字幕、格式表原样在;**凭据照旧不落盘**(剪和遮是同一道边界上的两件事)。"""
        written = self.written()
        payload = json.loads(written)
        self.assertEqual(self.original['subtitles'], payload['subtitles'])
        self.assertEqual('140', payload['formats'][0]['format_id'])
        self.assertNotIn(b'SID=secret', written)

    def test_a_line_that_is_not_json_is_still_capped(self):
        """非 JSON 的那一路不许因为「先解析」而放宽:超长照旧不写盘。"""
        written = diagnosis.redact(b'WARNING: ' + b'x' * (diagnosis._LINE_CAP + 1) + b'\n')
        self.assertIn(diagnosis.OVERSIZE.encode('utf-8'), written)

    def test_a_line_past_the_parse_ceiling_is_never_parsed(self):
        """**解析之前还有一道硬天花板**:剪完能很小的那一行,过了天花板也不许拿去解析
        (解析要占原文约 4 倍内存)。判据是「解析器有没有被叫到」,不是返回值像不像。"""
        with patch.object(diagnosis, '_PARSE_CAP', len(self.line) - 1), \
                patch.object(diagnosis.json, 'loads', side_effect=AssertionError('过了天花板还在解析')):
            written = diagnosis.redact(self.line)
        self.assertEqual(1, diagnosis.lost(written.decode()))

    def test_a_line_too_deep_to_parse_is_just_text(self):
        """嵌套过深(`RecursionError`)和语法不对是同一件事:按非 JSON 行写下去,不许抛出泵线程。"""
        deep = b'[' * 200000 + b'\n'
        self.assertEqual(deep, diagnosis.redact(deep))

    def test_a_pump_that_runs_out_of_memory_says_so(self):
        """内存不够也要落进 `failure`:漏出去的话泵线程静默死掉,这一轮按 yt-dlp 的退出码结算。"""
        state = {'bytes': 0, 'oversize': False, 'done': False, 'failure': None}
        with patch.object(diagnosis, 'redact', side_effect=MemoryError()):
            diagnosis.pump_output(io.BytesIO(b'{}\n'), self.scratch(), state)
        self.assertIsInstance(state['failure'], MemoryError)
        self.assertTrue(state['done'])

    def scratch(self):
        handle, name = tempfile.mkstemp()
        os.close(handle)
        self.addCleanup(os.unlink, name)
        return name

    def test_tlang_somewhere_else_in_the_link_is_not_a_translation(self):
        """反过来钉一边:`tlang` 这几个字出现在别的参数里不算现场翻译(子串只是快路)。"""
        for url in ('https://www.youtube.com/api/timedtext?v=x&lang=en&name=xtlang',
                    'https://www.youtube.com/api/timedtext?v=x&lang=en&xtlang=fr'):
            with self.subTest(url=url):
                self.assertFalse(diagnosis.translated({'url': url}))
        self.assertTrue(diagnosis.translated({'url': 'https://www.youtube.com/api/timedtext?v=x&tlang=fr'}))


#: 假 yt-dlp:先印用户贴出来的那几句警告,再印输出。flat 列频道印 `@LISTING@`,
#: 别的(逐条列频道、取单条元数据)印 `@PAYLOAD@`。
FAKE = '''
import pathlib, sys
sys.stdout.write(@CHALLENGE@)
answer = r'@LISTING@' if '--flat-playlist' in sys.argv else r'@PAYLOAD@'
sys.stdout.write(pathlib.Path(answer).read_text(encoding='utf-8'))
'''
#: 剪完仍超限的那一种(长简介之类):剪不掉,只能说是本机丢的。
def bloated():
    return {**metadata(languages=1, copies=1), 'upload_date': '20261027',
            'description': 'x' * (diagnosis._LINE_CAP + 1)}


class TheRealCallSite(YouTubeFixture):
    """**真子进程 → 输出泵 → 写盘 → 解析**,走 `media.probe` 原样。打桩在它之上就绕过了出事的那一段。"""

    def setUp(self):
        super().setUp()
        self.payload = self.root / 'payload.json'
        self.listing = self.root / 'listing.json'
        self.listing.write_text(json.dumps({'id': VIDEO, 'title': 't', 'duration': 600}) + '\n', encoding='utf-8')
        script = self.root / 'fake-ytdlp.py'
        script.write_text(FAKE.replace('@CHALLENGE@', repr(CHALLENGE)).replace('@PAYLOAD@', str(self.payload))
                          .replace('@LISTING@', str(self.listing)), encoding='utf-8')
        tool = patch.object(toolchain, 'command', return_value=[sys.executable, str(script)])
        tool.start()
        self.addCleanup(tool.stop)
        self.row = {'video_id': VIDEO, 'entry': {'url': f'https://www.youtube.com/watch?v={VIDEO}'}}

    def answer(self, *lines):
        self.payload.write_text(''.join(json.dumps(line) + '\n' for line in lines), encoding='utf-8')

    def ours(self, failure):
        """**是本机那道上限丢的**:三处后果一起看 —— 档、原文、有没有被记成 YouTube 的墙。"""
        self.assertEqual(diagnosis.LOCAL, failure.origin)
        self.assertEqual(diagnosis.CAPPED, login.coded(failure), '又落回了别的档(上一次是让人去更新 yt-dlp)')
        summary = {}
        collector.wall(summary, ['1'], failure)
        self.assertEqual({}, summary, '本机那道上限被记成了 YouTube 的墙')

    def probe(self, payload):
        self.answer(payload)
        return media.probe(self.row, {'youtube': {}}, time.monotonic() + 30, lambda: False, force=True)

    def test_the_eleven_megabyte_answer_is_read(self):
        """**那一幕原样**:11 MiB 量级、绝大部分是现场翻译 —— 现在要读得出来。"""
        found = self.probe(metadata())
        self.assertEqual(VIDEO, found['id'])
        self.assertEqual(['en', 'en-orig'], media.tracks(found, True))

    def test_a_real_overflow_says_it_was_us(self):
        """**真超限时**(剪不掉的那种大):说的是本机那道上限,不许被 JS 挑战警告冒名顶替。

        判据落在后果上:分档不是 `js_challenge`(那会让人去更新 yt-dlp)、给人看的原文里有
        那句「超过了单行上限」、失败归并不把它记成 YouTube 的墙。
        """
        with self.assertRaises(diagnosis.ToolFailure) as caught:
            self.probe(bloated())
        self.ours(caught.exception)
        self.assertIn(diagnosis.OVERSIZE, login.evidence(caught.exception), '面板上的原文里看不见真正的原因')

    def test_both_listing_paths_say_it_was_us(self):
        """**列频道那两条路同一个判据**:几十个频道一起报「去更新 yt-dlp」的那一幕。"""
        source = self.channel()
        self.answer(bloated())
        self.listing.write_text(self.payload.read_text(encoding='utf-8'), encoding='utf-8')
        for name in ('detailed_videos', 'channel_videos'):
            with self.subTest(path=name), self.assertRaises(diagnosis.ToolFailure) as caught:
                getattr(media, name)(source, {'youtube': {}}, time.monotonic() + 30, lambda: False)
            self.ours(caught.exception)

    def test_a_listing_with_one_lost_line_keeps_the_rest_and_says_so(self):
        """反过来钉一边:列到了别的几条就照旧收下,**丢掉的那条如实说一句**(不许静默缺一条)。"""
        small = {'id': 'abcdefghijk', 'title': 't', 'duration': 600, 'upload_date': '20261027'}
        self.answer(small, bloated())
        notes = []
        found = media.detailed_videos(self.channel(), {'youtube': {}}, time.monotonic() + 30, lambda: False,
                                      notes=notes)
        self.assertEqual(['abcdefghijk'], [item['video_id'] for item in found])
        self.assertEqual(1, len(notes), notes)
        self.assertIn('本机的单行上限', notes[0])

    def test_the_total_cap_is_ours_too(self):
        """一次调用的输出**总量**撞上限也是本机的事。裸 `RuntimeError` 的那一版,检查那一侧
        说成「连不上 YouTube,先看网络」。"""
        self.answer(*[{'id': VIDEO, 'filler': 'y' * 4096}] * 64)
        with patch.object(diagnosis, '_OUTPUT_CAP', 64 * 1024), self.assertRaises(diagnosis.ToolFailure) as caught:
            media.probe(self.row, {'youtube': {}}, time.monotonic() + 30, lambda: False, force=True)
        self.ours(caught.exception)

    def test_merging_keeps_it(self):
        """**归并层不许把它丢回「判不出来」**;反过来,真有一道 YouTube 的墙时照旧是那道墙。"""
        own = diagnosis.ToolFailure('丢了一行', 'x', diagnosis.CAPPED, diagnosis.LOCAL)
        vague = diagnosis.ToolFailure('超时', 'y', '', diagnosis.YOUTUBE)
        walled = diagnosis.ToolFailure('墙', 'z', 'bot_wall', diagnosis.YOUTUBE)
        self.assertEqual(diagnosis.CAPPED, login.coded(login.merge(diagnosis.ToolFailure('合并'), [vague, own])))
        self.assertEqual('bot_wall', login.coded(login.merge(diagnosis.ToolFailure('合并'), [own, walled])))

    def test_the_check_speaks_for_itself_and_does_not_echo_the_warnings(self):
        """**检查结论旁边那条并列提醒**不许再拿同一份输出重算 —— 那几句正是当初冒名顶替的东西。"""
        self.channel()
        self.settings(cookies_browser='chrome')
        self.answer(bloated())
        result = login.check(self.settings())
        self.assertEqual(diagnosis.CAPPED, result['code'], result)
        self.assertEqual(login.describe(diagnosis.CAPPED, 'chrome'), result['message'])
        self.assertNotIn('warning_code', result, '又并列让人去更新 yt-dlp 了')
        self.assertIn(diagnosis.OVERSIZE, result.get('detail', ''))

    def test_a_listing_that_overflows_is_not_called_unreachable(self):
        """列频道那一步撞上它时,检查不许说成「连不上 YouTube」(会让人去查网络)。"""
        self.channel()
        self.settings(cookies_browser='chrome')
        self.answer(bloated())
        self.listing.write_text(self.payload.read_text(encoding='utf-8'), encoding='utf-8')
        self.assertEqual(diagnosis.CAPPED, login.check(self.settings())['code'])

    def test_a_check_that_got_through_still_mentions_the_challenge(self):
        """反过来钉一边:**取到了**的那一轮,JS 挑战那句并列提醒照旧在 —— 它是对的
        (下音频那一步确实过不去),压掉它的只该是「失败已经判成本机上限」那一种。"""
        self.channel()
        self.settings(cookies_browser='chrome')
        self.answer(metadata())
        result = login.check(self.settings())
        self.assertEqual('metadata_ok', result['code'], result)
        self.assertEqual('js_challenge', result.get('warning_code'))


if __name__ == '__main__':
    unittest.main()
