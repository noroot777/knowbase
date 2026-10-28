import json
import sys
import time
from unittest.mock import patch

from kb.youtube import diagnosis, media, toolchain
from tests.youtube_fixture import YouTubeFixture


class YouTubeMedia(YouTubeFixture):
    def setUp(self):
        super().setUp()
        self.row = {'video_id': '12345678901', 'entry': {'url': 'https://www.youtube.com/watch?v=12345678901'}}
        self.metadata = {'id': self.row['video_id'], 'duration': 60, 'language': 'en'}
        self.transcript = {'origin': 'uploaded_captions', 'language': 'en',
                           'segments': [{'start': 0, 'end': 59, 'text': 'An AI agent writes useful code.'}]}
        self.deadline = time.monotonic() + 30

    def obtain(self):
        return media.obtain(self.row, self.metadata, {'youtube': {}}, self.deadline, lambda: False)

    def touched(self, output):
        """打桩共用:在基名旁边造一份「这次调用自己的」文件并返回它。

        真实现返回的是 `media.unique(output)` 而不是 `output` 本身,消费方(读 JSON、
        `unlink`)认的都是**返回值**。打桩直接返回基名也能跑通,但那样就钉不住
        「消费方不许自己去拼基名」—— 这里刻意返回一个**不同于基名**的路径。
        """
        actual = media.unique(output)
        actual.write_bytes(b'')
        return actual

    def wrote(self, text):
        """打桩:把内容写进这次调用自己那份文件,并返回它。"""
        def pretend(arguments, output, *rest, **keywords):
            actual = media.unique(output)
            actual.write_text(text() if callable(text) else text, encoding='utf-8')
            return actual
        return pretend

    def test_cache_before_captions_or_audio(self):
        directory = media.cache_dir(self.row['video_id'])
        media.save_json(directory / 'transcript.json', {**self.transcript, 'video_id': self.row['video_id']})
        with patch.object(media, 'captions') as captions, patch.object(media, 'asr') as asr:
            self.assertEqual(self.transcript['segments'], self.obtain()['segments'])
        captions.assert_not_called()
        asr.assert_not_called()

    def test_bad_cache_recovers_and_uploaded_precedes_automatic(self):
        directory = media.cache_dir(self.row['video_id'])
        media.save_json(directory / 'transcript.json', {'video_id': self.row['video_id'],
                        'origin': 'uploaded_captions', 'language': 'en', 'segments': [{}]})
        with patch.object(media, 'captions', return_value=self.transcript) as captions, patch.object(media, 'asr') as asr:
            result = self.obtain()
        self.assertEqual('uploaded_captions', result['origin'])
        self.assertFalse(captions.call_args.args[3])
        self.assertEqual(1, captions.call_count)
        asr.assert_not_called()

    def test_caption_failure_falls_back_then_caches(self):
        with patch.object(media, 'captions', side_effect=[ValueError('坏字幕'), None]) as captions, \
                patch.object(media, 'asr', return_value={**self.transcript, 'origin': 'local_asr'}) as asr:
            result = self.obtain()
        self.assertEqual([False, True], [call.args[3] for call in captions.call_args_list])
        self.assertEqual('local_asr', result['origin'])
        self.assertEqual(['坏字幕'], result['caption_warnings'])
        asr.assert_called_once()
        self.assertEqual(result, media.read_json(media.cache_dir(self.row['video_id']) / 'transcript.json'))

    def test_original_tracks_never_use_auto_translation(self):
        metadata = {**self.metadata, 'automatic_captions': {
            'zh': [{'url': 'https://example.org/caption?tlang=zh'}],
            'en-orig': [{'url': 'https://example.org/caption?lang=en'}]}}
        self.assertEqual(['en-orig'], media.tracks(metadata, True))
        metadata['automatic_captions'].pop('en-orig')
        self.assertEqual([], media.tracks(metadata, True))

    def test_matching_local_artifact_and_corrupt_metadata(self):
        directory = media.VIDEOS_ROOT / ('review-' + self.row['video_id'])
        (directory / 'metadata').mkdir(parents=True)
        (directory / 'subtitles').mkdir()
        media.save_json(directory / 'metadata/youtube_metadata.json', self.metadata)
        (directory / 'subtitles/source.srt').write_text('1\n00:00:00,000 --> 00:00:59,000\nAn AI agent writes useful code.\n')
        with patch.object(media, 'ytdlp') as download, patch.object(media, 'captions') as captions:
            self.assertEqual(self.metadata, media.probe(self.row, {'youtube': {}}, self.deadline, lambda: False))
            self.assertEqual('local_cache', self.obtain()['origin'])
        download.assert_not_called()
        captions.assert_not_called()
        media.save_json(media.cache_dir(self.row['video_id']) / 'metadata.json', [])
        media.save_json(directory / 'metadata/youtube_metadata.json', [])
        # 打桩要像真实现那样**把路径返回去**:输出文件是每次调用自己的那一份
        # (`media.unique`),消费方读的是返回值而不是它给出去的基名。
        with patch.object(media, 'ytdlp', side_effect=self.wrote(lambda: json.dumps(self.metadata))):
            self.assertEqual(self.metadata, media.probe(self.row, {'youtube': {}}, self.deadline, lambda: False))

    def test_audio_only_asr_is_thread_capped_and_cleans_media(self):
        directory = media.cache_dir(self.row['video_id'])
        model = directory / 'ggml-base.en.bin'
        model.touch()
        commands = []

        def download(arguments, output, *rest, **kwargs):
            commands.append(arguments)
            (directory / 'audio.webm').write_bytes(b'fake audio')
            return self.touched(output)

        def run(arguments, output, *rest, **kwargs):
            commands.append(arguments)
            if arguments[0] == 'ffmpeg':
                (directory / 'speech.wav').touch()
            else:
                media.save_json(directory / 'whisper.json', {'segments': self.transcript['segments']})
            return self.touched(output)

        with patch.object(media, 'tools', return_value=dict(asr_enabled=True, ffmpeg=True, whisper=True, model=True)), \
                patch.object(media, 'model_path', return_value=model), patch.object(media, 'ytdlp', side_effect=download), \
                patch.object(media, 'run', side_effect=run):
            with self.assertRaisesRegex(RuntimeError, '多语言模型'):
                media.asr(self.row, {**self.metadata, 'language': 'ja'}, {'youtube': {}}, self.deadline, lambda: False)
            result = media.asr(self.row, self.metadata, {'youtube': {}}, self.deadline, lambda: False)
            self.assertEqual('local_asr', result['origin'])
            self.assertEqual('bestaudio[abr<=160]/bestaudio', commands[0][1])
            self.assertEqual(['-t', '2'], commands[-1][-2:])
        self.assertTrue((directory / 'audio.webm').exists())
        self.assertTrue((directory / 'speech.wav').exists())
        with patch.object(media, 'captions', return_value=None), patch.object(media, 'ytdlp') as redownload:
            self.obtain()
        redownload.assert_not_called()
        self.assertFalse((directory / 'audio.webm').exists())
        self.assertFalse((directory / 'speech.wav').exists())

    def test_the_log_on_disk_never_carries_the_credential(self):
        """**凭据进磁盘只有一条边界,就是 `media.run` 里那道脱敏。**

        yt-dlp 的 `_calc_headers` 会把整条 cookie 串塞进 info JSON 和 `http_headers`,
        而它的 debug 行和报错原文里也带。判据落在**磁盘上那份文件的内容**(不是「有没有
        调过 scrub」):直接重定向到文件的话,子进程还在跑的那几十秒里登录态就躺在磁盘上。
        顺带钉住文件权限 —— 这台机器上别的用户不该读得到它。
        """
        secret = 'SID=AbCdEfGhIjKlMnOpQrStUvWxYz0123456789; HSID=x9Q2; __Secure-1PSID=zzzz'
        script = ('import sys\n'
                  f'print("[debug] Cookie: {secret}")\n'
                  'print("WARNING: cannot decrypt v10 cookies: no key found")\n'
                  f'print(\'{{"id": "12345678901", "cookies": "{secret}", "title": "AI agent"}}\')\n')
        log = media.run([sys.executable, '-c', script], self.root / 'probe.log',
                        time.monotonic() + 20, lambda: False)
        text = log.read_text(encoding='utf-8')
        self.assertNotIn('AbCdEfGhIjKlMnOpQrStUvWxYz', text, '整条 cookie 串落到磁盘上了')
        self.assertNotIn('HSID=x9Q2', text, '只遮第一段等于把后面几段留在磁盘上')
        self.assertIn('[已遮蔽]', text)
        self.assertEqual(0o600, log.stat().st_mode & 0o777)
        # **反过来钉一边:那句警告必须原样留着。** 它是 `login.classify` 区分
        # 「读不出 cookie」和「没登录」的唯一依据,按「这行有 cookie 字样」整行丢掉
        # 之后四档分类退化成一档,而面板上一切正常。
        self.assertIn('cannot decrypt v10 cookies: no key found', text)
        self.assertIn('cookie', media.diagnostics(log))
        # JSON 行照旧解析得出来,带凭据的键被摘掉、正经字段一个不少。
        payload = [json.loads(line) for line in text.splitlines() if line.startswith('{')]
        self.assertEqual([{'id': '12345678901', 'title': 'AI agent'}], payload)

    def test_a_runaway_process_cannot_fill_the_disk(self):
        """输出总量有上限:一条疯掉的进度行不许把磁盘灌满,**而且这次调用算失败**
        (半份输出解析出来的元数据比没有更糟)。失败时那份文件也不留。"""
        script = ('import sys\n'
                  'line = "x" * 65536\n'
                  'while True:\n'
                  '    sys.stdout.write(line + "\\n")\n'
                  '    sys.stdout.flush()\n')
        # 封顶那个常量住在 `diagnosis`(写进磁盘的那一道边界归它),`media.run` 只是用它。
        # 打桩要打在**它住的地方**:`media` 里没有这个名字,`patch.object` 直接报
        # AttributeError —— 不是红在行为上,这一条压根没验过。
        with patch.object(diagnosis, '_OUTPUT_CAP', 256 * 1024):
            with self.assertRaisesRegex(RuntimeError, '过大'):
                media.run([sys.executable, '-c', script], self.root / 'flood.log',
                          time.monotonic() + 20, lambda: False)
        self.assertEqual([], list(self.root.glob('flood*.log')), '失败的那份输出不留在磁盘上')

    def test_redaction_keeps_the_words_and_drops_the_values(self):
        """脱敏按**值的形状**判。短的、像人话的值留着 —— 否则报错原文全是 `[已遮蔽]`,
        面板上那行「yt-dlp 原文」等于没有。"""
        self.assertEqual('Cookie: [已遮蔽]\n',
                         media.redact(b'Cookie: SID=AbCdEfGhIjKlMnOpQrStUvWxYz01; a=b\n').decode())
        self.assertEqual('authorization: [已遮蔽]\n',
                         media.redact(b'authorization: Bearer ya29.AAbbCCddEEffGGhh\n').decode())
        for keep in (b'WARNING: cannot decrypt v10 cookies: no key found\n',
                     b'ERROR: Sign in to confirm you are not a bot. Use --cookies-from-browser\n',
                     b'[info] Writing video subtitles to: en.vtt\n'):
            with self.subTest(line=keep[:40]):
                self.assertEqual(keep.decode(), media.redact(keep).decode())
        self.assertEqual('[过长的输出已遮蔽]\n', media.redact(b'z' * (4 * 1024 * 1024 + 1)).decode())

    def test_the_detailed_path_asks_for_real_dates_and_keeps_the_login_state(self):
        """**第 2 条发现路要的就是「真实日期」这一点**,不然它不值那份时间。

        命令上三件事都要在:带登录态、逐条解析(**不带 `--flat-playlist`** —— 带了就拿不到
        日期和播放量,那就退化成第 3 条估算路)、按 `--playlist-end` 封顶别把频道历史翻完。
        地址用的是**频道视频页**:实测把 `feeds/videos.xml` 交给 yt-dlp 会落到 generic,
        它把那份 Atom 当单个视频页处理、只抠出最新 1 条。
        """
        source = {'id': 7, 'url': 'https://www.youtube.com/channel/UC' + '0' * 22}
        with patch.object(media, 'run') as run, \
                patch.object(toolchain, 'command', return_value=['yt-dlp']), \
                patch.object(media, '_detailed', return_value=[]):
            media.detailed_videos(source, {'youtube': {'cookies_browser': 'firefox:Profile 1'}},
                                  self.deadline, lambda: False, limit=10)
            arguments = run.call_args.args[0]
        self.assertEqual(['--cookies-from-browser', 'firefox:Profile 1'],
                         arguments[arguments.index('--cookies-from-browser'):][:2])
        self.assertIn('--dump-json', arguments)
        self.assertEqual(['--playlist-end', '10'], arguments[arguments.index('--playlist-end'):][:2])
        self.assertNotIn('--flat-playlist', arguments, 'flat 拿不到日期和播放量,这条路就白走了')
        self.assertNotIn('--no-playlist', arguments, '频道页本身就是一串条目')
        self.assertEqual(media._channel_url(source), arguments[-1])
        self.assertNotIn('feeds/videos.xml', arguments[-1], 'feed 地址走 generic,只抠得出 1 条')

    def listing_script(self, payload, note='ERROR: [youtube] 12345678902: Private video. '
                                             'Sign in if you have been granted access'):
        """造一个「先整行印出一条,再报错并以 1 退出」的真子进程,当成 yt-dlp 用。"""
        return ('import sys\n'
                f'print({payload!r})\n'
                'sys.stdout.flush()\n'
                f'print({note!r}, file=sys.stderr)\n'
                'sys.exit(1)\n')

    def channel_source(self):
        return {'id': 7, 'name': '频道 7', 'url': 'https://www.youtube.com/channel/UC' + '0' * 22}

    def leftovers(self):
        return sorted(path.name for path in (media.DATA_DIR / 'youtube' / 'channels').glob('*'))

    def test_a_listing_keeps_the_entries_it_already_printed(self):
        """**一条视频受限、或者预算在第 7 条上到点,前面整行印出来的照旧要收下。**

        审查用两个真实子进程复现:第一条正常视频的 JSON 整行印了出来,第二条报私有并以 1
        退出 —— `run()` 按退出码抛异常并把整份输出删掉,`_detailed` 连看一眼的机会都没有。
        普通轮次于是丢掉事实元数据改走估算,显式日期那一轮整个频道报失败、连库里既有候选
        都不处理。**判据落在后果上**:拿回几条、是哪一条、「没列完」这句话有没有留下,
        以及那份带凭据的输出有没有被删掉。两条列表路都要钉 —— 它们共用同一个适配层,
        而「检查登录态」走的是 flat 那条。
        """
        detailed = json.dumps({'id': '12345678901', 'title': 'AI agent', 'timestamp': 1792000000,
                               'channel_id': 'UC' + '0' * 22, 'view_count': 5, 'duration': 600,
                               'description': 'An AI agent writes code.'})
        flat = json.dumps({'id': '12345678901', 'title': 'AI agent', 'duration': 600})
        for label, payload, call in (('逐条解析', detailed, media.detailed_videos),
                                     ('flat 列举', flat, media.channel_videos)):
            with self.subTest(path=label):
                notes = []
                with patch.object(toolchain, 'command',
                                  return_value=[sys.executable, '-c', self.listing_script(payload)]):
                    entries = call(self.channel_source(), {'youtube': {}}, self.deadline,
                                   lambda: False, notes=notes)
                self.assertEqual(['12345678901'], [item['video_id'] for item in entries])
                # 归属以**工具自己报的那一份**为准(flat 不报,就留空等调用方按本地那份补)。
                self.assertEqual('UC' + '0' * 22 if payload is detailed else '',
                                 entries[0]['channel_id'])
                self.assertEqual(1, len(notes), '「没列完」要留下一句,否则面板上看不出停在半路')
                self.assertIn('Private video', notes[0], '原话要带上,那是唯一的线索')
                self.assertIn('1 条', notes[0], '说清列到了几条')
                self.assertEqual([], self.leftovers(), '那份输出和凭据同源,不许留在磁盘上')

    def test_a_listing_that_printed_nothing_usable_still_fails(self):
        """反过来钉一边:一条合法条目都没有时照旧抛,**而且命令自己那句话要在最前面** ——
        否则 `login.classify` 分不出这是 cookie 读不出来,用户会被送去反复登录。"""
        script = self.listing_script('WARNING: cannot decrypt v10 cookies: no key found',
                                     note='ERROR: unable to open database file')
        with patch.object(toolchain, 'command', return_value=[sys.executable, '-c', script]):
            with self.assertRaises(RuntimeError) as caught:
                media.detailed_videos(self.channel_source(), {'youtube': {}}, self.deadline,
                                      lambda: False)
        self.assertIn('unable to open database file', str(caught.exception))
        self.assertIn('no key found', str(caught.exception))
        self.assertEqual([], self.leftovers())

    def test_one_video_at_a_time_stays_all_or_nothing(self):
        """**反过来钉一边:单视频那三条路不许沾部分成功。**

        半份元数据比没有更坏(时长、语言、字幕清单缺一样,后面每一步都在猜),所以
        `run()` 的默认语义一个字都不许动:失败就抛、那份输出当场删掉。
        """
        payload = json.dumps({'id': '12345678901', 'duration': 600, 'language': 'en'})
        directory = media.cache_dir(self.row['video_id'])
        with patch.object(toolchain, 'command',
                          return_value=[sys.executable, '-c', self.listing_script(payload)]):
            with self.assertRaises(RuntimeError):
                media.probe(self.row, {'youtube': {}}, self.deadline, lambda: False, force=True)
        self.assertEqual([], sorted(path.name for path in directory.glob('probe*')),
                         '失败的那份输出不留在磁盘上')
        self.assertFalse((directory / 'metadata.json').exists(), '半截元数据不许进缓存')

    def test_only_entries_with_a_real_date_come_back(self):
        """读不出发布时间的不拿「现在」顶替 —— 那会让一条老视频以今天的身份进窗口。

        反过来钉一边:`timestamp` 和只有 `upload_date` 这两档都算真实日期,都要收。
        """
        log = self.root / 'feed.jsonl'
        log.write_text('\n'.join([
            '[generic] Extracting URL: https://www.youtube.com/feeds/videos.xml?channel_id=UC',
            json.dumps({'id': '12345678901', 'title': '带时间戳的', 'timestamp': 1792000000,
                        'view_count': 4321, 'duration': 600, 'description': 'An AI agent.'}),
            json.dumps({'id': '12345678902', 'title': '只有日期的', 'upload_date': '20261020',
                        'view_count': 7, 'duration': 61}),
            json.dumps({'id': '12345678903', 'title': '读不出时间的', 'view_count': 99}),
            json.dumps({'id': 'not-a-video-id', 'title': '不是视频', 'timestamp': 1792000000}),
        ]) + '\n', encoding='utf-8')
        entries = media._detailed(log, 'https://example.invalid/feed')
        self.assertEqual(['12345678901', '12345678902'], [item['video_id'] for item in entries])
        self.assertEqual([4321, 7], [item['views'] for item in entries])
        self.assertTrue(all(item['published'].endswith('+08:00') for item in entries),
                        '时间按项目时区写,不按宿主机')
        self.assertEqual('2026-10-20', entries[1]['published'][:10], '只有日期那一档按当天算')
        self.assertEqual('An AI agent.', entries[0]['description'])
        # 一条都没解析出来是故障,不许安静返回空集,而且要带上工具原话。
        log.write_text('WARNING: cannot decrypt v10 cookies: no key found\n', encoding='utf-8')
        with self.assertRaises(RuntimeError) as caught:
            media._detailed(log, 'https://example.invalid/feed')
        self.assertIn('cannot decrypt', str(caught.exception))

    def test_public_tool_arguments_and_stop(self):
        """登录态只在**显式交了浏览器**时才上命令行,而且 `--no-playlist` 要跟着用途走。

        原来这里钉的是「任何情况下都不带 `--cookies-from-browser`」。那条约束已经由
        仓库主人改掉了(全程登录态,见 `docs/decisions/youtube-intake.md` 第一节),所以
        这里换成钉住新的两条,**并且反过来钉一边**:不交浏览器就依旧一个 cookie 参数都不带,
        免得哪天默认值漏进来、所有调用都悄悄开始读 cookie。
        """
        with patch.object(media, 'run') as run, \
                patch.object(toolchain, 'command', return_value=['yt-dlp']):
            media.ytdlp(['--skip-download', 'https://www.youtube.com/watch?v=12345678901'],
                         self.root / 'log', self.deadline, lambda: False)
            bare = run.call_args.args[0]
            media.ytdlp(['--skip-download'], self.root / 'log2', self.deadline, lambda: False,
                        spec='chrome:Profile 1')
            logged = run.call_args.args[0]
            media.ytdlp(['--flat-playlist'], self.root / 'log3', self.deadline, lambda: False,
                        spec='chrome', playlist=True)
            listing = run.call_args.args[0]
        self.assertIn('--ignore-config', bare)
        self.assertNotIn('--cookies-from-browser', bare)
        self.assertEqual(['--cookies-from-browser', 'chrome:Profile 1'],
                         logged[logged.index('--cookies-from-browser'):][:2])
        # 单视频的三条路必须钉住只处理这一条;列频道那条要的恰恰是展开播放列表。
        self.assertIn('--no-playlist', logged)
        self.assertNotIn('--no-playlist', listing)
        with patch.object(toolchain, 'command', return_value=[]):
            with self.assertRaisesRegex(RuntimeError, 'yt-dlp'):
                media.ytdlp(['--version'], self.root / 'log4', self.deadline, lambda: False)
        started = time.monotonic()
        with self.assertRaises(media.Interrupted):
            media.run([sys.executable, '-c', 'import time; time.sleep(20)'], self.root / 'child.log',
                      started + .3, lambda: False)
        self.assertLess(time.monotonic() - started, 3)
