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
"""
import json
import sys
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


#: 假 yt-dlp:先印用户贴出来的那几句警告,再印一行元数据 JSON。`@PAYLOAD@` 是那一行的文件。
FAKE = '''
import pathlib, sys
sys.stdout.write(@CHALLENGE@)
sys.stdout.write(pathlib.Path(r'@PAYLOAD@').read_text(encoding='utf-8'))
'''


class TheRealCallSite(YouTubeFixture):
    """**真子进程 → 输出泵 → 写盘 → 解析**,走 `media.probe` 原样。打桩在它之上就绕过了出事的那一段。"""

    def setUp(self):
        super().setUp()
        self.payload = self.root / 'payload.json'
        script = self.root / 'fake-ytdlp.py'
        script.write_text(FAKE.replace('@CHALLENGE@', repr(CHALLENGE)).replace('@PAYLOAD@', str(self.payload)),
                          encoding='utf-8')
        tool = patch.object(toolchain, 'command', return_value=[sys.executable, str(script)])
        tool.start()
        self.addCleanup(tool.stop)
        self.row = {'video_id': VIDEO, 'entry': {'url': f'https://www.youtube.com/watch?v={VIDEO}'}}

    def probe(self, payload):
        self.payload.write_text(json.dumps(payload) + '\n', encoding='utf-8')
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
        bloated = {**metadata(languages=1, copies=1), 'description': 'x' * (diagnosis._LINE_CAP + 1)}
        with self.assertRaises(diagnosis.ToolFailure) as caught:
            self.probe(bloated)
        failure = caught.exception
        self.assertEqual(diagnosis.LOCAL, failure.origin)
        self.assertNotEqual('js_challenge', login.coded(failure), '又让人去更新 yt-dlp 了')
        self.assertIn(diagnosis.OVERSIZE, login.evidence(failure), '面板上的原文里看不见真正的原因')
        summary = {}
        collector.wall(summary, ['1'], failure)
        self.assertEqual({}, summary, '本机那道上限被记成了 YouTube 的墙')


if __name__ == '__main__':
    unittest.main()
