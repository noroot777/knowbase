"""假 yt-dlp 和「跑一整轮」那套夹具:**两份用例文件共用这一份,不许各抄一遍。**

拆成两份是 700 行上限逼出来的(`test_youtube_throttle` 验熔断与拒绝,
`test_youtube_diagnostics` 验诊断的运输与归并)。夹具抄两份之后,其中一份会跟着
代码慢慢走偏,而红只在另一份上出现 —— 搬出来这一份就是为了让两边验的是同一个工具。
"""
import json
import sys
from datetime import datetime
from unittest.mock import patch

from kb.collectors import FetchScope
from kb.feeds import registry as reg
from kb.youtube import collector, login, media, notes, toolchain
from tests.youtube_fixture import YouTubeFixture


#: 假 yt-dlp。`@PLAN@` 是 `(匹配串, 做法)` 的清单,**第一个在 argv 里出现的说了算**;
#: 每次调用先把 argv 记进 `@CALLS@` —— 用例断言的正是「哪几次调用真的发生了」。
FAKE_TOOL = '''
import json
import pathlib
import sys

ARGS = sys.argv[1:]
with pathlib.Path(r'@CALLS@').open('a', encoding='utf-8') as handle:
    handle.write(json.dumps(ARGS) + '\\n')
LIMIT = 'ERROR: HTTP Error 429: Too Many Requests'
FILLER = '\\n'.join('WARNING: [youtube] retrying player client fallback #%d' % n for n in range(40))
BOTLINE = 'ERROR: Sign in to confirm you are not a bot. Use --cookies-from-browser'
COOKIELINE = 'WARNING: cannot decrypt v10 cookies: no key found'
target = ARGS[-1]
behaviour = ''
for mark, action in json.loads(pathlib.Path(r'@PLAN@').read_text(encoding='utf-8')):
    if any(mark in arg for arg in ARGS):
        behaviour = action
        break


def identity():
    return target.split('/channel/')[-1].split('/')[0]


def video_id():
    if 'watch?v=' in target:
        return target.rsplit('=', 1)[-1]
    return '%011d' % int(identity()[2:] or 0)


def listed():
    body = {'id': video_id(), 'title': 'AI agent coding tutorial', 'duration': 600,
            'channel_id': identity(), 'description': 'Practical AI workflow for engineers'}
    if '--flat-playlist' not in ARGS:
        body.update(upload_date='@TODAY@', view_count=1000)
    return json.dumps(body)


if behaviour == 'limit':
    sys.stdout.write(LIMIT + '\\n')
    sys.exit(1)
if behaviour == 'long':
    sys.stdout.write('WARNING: Falling back on generic information extractor\\n')
    sys.stdout.write(FILLER + '\\n' + LIMIT + '\\n' + FILLER + '\\n' + BOTLINE + '\\n')
    sys.exit(1)
if behaviour == 'mixed':
    print(COOKIELINE)
    print(LIMIT)
    sys.exit(1)
if behaviour == 'bot':
    print(BOTLINE)
    sys.exit(1)
if behaviour == 'broken':
    sys.stdout.write('ERROR: Unable to download webpage: timed out\\n')
    sys.exit(1)
if behaviour == 'partial':
    sys.stdout.write(listed() + '\\n')
    sys.stdout.write(LIMIT + '\\n')
    sys.exit(1)
if behaviour == 'warnedonly':
    # 退 0、只有一条 429 警告、一条合法条目都没印 —— 解析那一步必然空手
    sys.stdout.write('WARNING: ' + LIMIT + '\\n')
    sys.exit(0)
if behaviour == 'warned':
    sys.stdout.write('WARNING: ' + LIMIT + '\\n')
if behaviour == 'walled':
    # 字幕那一次**真发出去并失败**:同一份输出里既有 cookie 警告又有机器人验证
    print(COOKIELINE)
    print(BOTLINE)
    sys.exit(1)
if behaviour == 'cookieonly':
    # 退 0、只有一条 cookie 警告、一条合法条目都没印 —— 解析那一步必然空手
    print(COOKIELINE)
    sys.exit(0)
if behaviour == 'cookie':
    print(COOKIELINE)
if behaviour == 'noise':
    print('WARNING: Falling back on generic information extractor')
if '--dump-single-json' in ARGS:
    subtitles = {'en': [{'url': 'https://example.invalid/s'}]}
    if behaviour == 'twolangs':
        subtitles['zh'] = [{'url': 'https://example.invalid/z'}]
    sys.stdout.write(json.dumps({'id': video_id(), 'duration': 600, 'language': 'en',
                                 'upload_date': '@TODAY@', 'view_count': 1000,
                                 'subtitles': subtitles}) + '\\n')
    sys.exit(0)
if '--write-subs' in ARGS or '--write-auto-subs' in ARGS:
    if behaviour in ('badsub', 'goodsub'):
        # 工具退 0、文件也写了,**内容坏在解析那一步** —— 真机形态:下到一半的 json3。
        written = ARGS[ARGS.index('-o') + 1].replace('%(ext)s', 'json3')
        pathlib.Path(written).write_text(
            '{"events": [' if behaviour == 'badsub' else json.dumps(
                {'events': [{'tStartMs': 0, 'dDurationMs': 600000,
                             'segs': [{'utf8': 'hello from this caption track'}]}]}),
            encoding='utf-8')
    sys.exit(0)
sys.stdout.write(listed() + '\\n')
sys.exit(0)
'''


class FakeToolCase(YouTubeFixture):
    """从真子进程跑起的那一半:**断言的是「这次调用压根没发生」。**

    打桩在 `discovery.fetch` / `media.probe` 之上验不了这几条 —— 后路、部分成功、
    发现到处理的阶段切换全在被绕过的那一段里。**这个类自己不带用例**,只带工具和夹具。
    """

    def setUp(self):
        super().setUp()
        self.isolate()
        self.calls = self.root / 'calls.jsonl'
        self.plan = self.root / 'plan.json'
        self.settings = reg.get_settings('youtube')
        for override in (patch.object(login, 'installed', return_value=True),
                         patch.object(collector.time, 'sleep')):
            override.start()
            self.addCleanup(override.stop)

    def tool(self, rules=()):
        self.plan.write_text(json.dumps([list(rule) for rule in rules]), encoding='utf-8')
        script = self.root / 'faketool.py'
        # **发布日期按当天算,不写死。** 写死的那一版在 7 天窗口外就什么都排不进来,
        # 而红出来的形态是「一条候选都没有」—— 看着像排名坏了。
        script.write_text(FAKE_TOOL.replace('@CALLS@', str(self.calls)).replace('@PLAN@', str(self.plan))
                          .replace('@TODAY@', datetime.now(media.TZ).strftime('%Y%m%d')),
                          encoding='utf-8')
        return patch.object(toolchain, 'command', return_value=[sys.executable, str(script)])

    def ran(self, mark):
        """argv 里带 `mark` 的那几次调用。**没有这个文件就是一次都没跑过。**"""
        if not self.calls.exists():
            return []
        return [args for args in (json.loads(line) for line in
                                  self.calls.read_text(encoding='utf-8').splitlines())
                if any(mark in arg for arg in args)]

    def sync(self, rules, channels=2, transcript=True, model=True):
        """跑一整轮。`transcript=False` 时**取字幕那一段也走真的** —— 字幕和音频那两条路
        要靠它才进得去(打桩在 `media.obtain` 之上就把它们一起绕过去了)。

        `model=False` 是「翻译和笔记模型还没配」那一轮:候选照旧排得出来,但一条都不处理。
        """
        for number in range(1, channels + 1):
            self.channel(number)
        stubs = [patch.object(notes, 'available', return_value=(True, '') if model else (False, '没配模型')),
                 patch.object(notes, 'build', side_effect=self.note)]
        if transcript:
            stubs.append(patch.object(media, 'obtain', return_value=self.complete_transcript()))
        with self.tool(rules):
            for stub in stubs:
                stub.start()
                self.addCleanup(stub.stop)
            return collector.collect(FetchScope())
