import contextlib
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from kb.config import DATA_DIR, VIDEOS_ROOT
from kb.youtube import login, throttle, toolchain, transcripts
from kb.youtube.diagnosis import (LOCAL, OVERSIZE, YOUTUBE, ToolFailure, diagnostics, excerpt, on_disk,
                                   pump_output, record, redact, scrub, serial, translated, unique, written)
from kb.youtube.channels import VIDEO_ID
from kb.youtube.settings import model_path, of, tools

#: 项目时区。口径同 `ranking.TZ`(CLAUDE.md:时间一律按项目时区,不按宿主机)。
TZ = ZoneInfo('Asia/Shanghai')


class Interrupted(RuntimeError):
    pass


class PartialOutput(RuntimeError):
    """命令没跑成,但**已经完整落盘的那几行仍然合法** —— 列表路专用(`run(partial=True)`)。

    **为什么要有这一档**:列频道是「一条视频一行 JSON」的流式输出。中间夹一条私有视频、
    或者预算在第 7 条上到点,前面已经整行印出来的那几条一点问题都没有。按整个进程的
    退出码一刀切等于**一条受限视频就让这个频道本轮颗粒无收**(审查用两个真实子进程
    复现:正常那条的输出被连带删掉,显式日期那一轮整个频道报失败、库里既有候选也不处理)。

    **单视频的三条路不许用它**(元数据、字幕、音频):那几条要的是这一条自己跑完,
    半截结果比没有更坏。所以这个契约只加在列表适配层,`run()` 的默认语义一个字不动。

    `path` 的删除责任在捕获方 —— 和 `run()` 成功返回时同一个约定。

    **把原来那条异常也带着**(`cause`):只留一句话的话,来源(`origin`)和完整诊断跟着
    就地丢了 —— 写盘失败、配额用尽这些**本机**错误先转成半截输出,列表里再没有合法条目,
    外层包一层 `ToolFailure` 之后由 `login.merge` 归并,最后按文本猜成「读不出 cookie」。
    """

    def __init__(self, path, reason, cause=None):
        super().__init__(reason)
        self.path = path
        self.reason = reason
        self.cause = cause


def cache_dir(video_id):
    if not VIDEO_ID.fullmatch(video_id):
        raise ValueError('视频 ID 不正确')
    path = DATA_DIR / 'youtube' / video_id
    with on_disk('建不了视频缓存目录'):
        path.mkdir(parents=True, exist_ok=True)
    return path


def save_json(path, payload):
    # 临时名也要带 pid 和序号:固定的 `.tmp` 在两份结果同时提交时会互相替换,
    # 还会被对方的 `unlink` 删掉 —— 原子替换保护的是读者,保护不了彼此。
    temporary = path.with_name(f'{path.name}.{os.getpid()}.{serial()}.tmp')
    try:
        with on_disk(f'写不了 {path.name}'):
            temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')
            os.replace(temporary, path)
    finally:
        heed(temporary)


def read_json(path):
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except (ValueError, OSError):
        return None


def terminate(process):
    if process.poll() is not None:
        return
    if os.name == 'nt':
        subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    process.wait()


def run(arguments, output, deadline, stopped, partial=False, origin=YOUTUBE):
    """跑一条命令,输出落在**这一次调用自己的**文件上(见 `unique`),返回那个文件。

    **唯一化在这里做,不在各个调用方那里做** —— 口径只有一份:所有子进程输出都经过
    这一道,新加一条调用路径时不必记得自己去重命名。失败时把那份删掉,该带走的诊断
    已经进了异常消息。

    `partial=True` 只给**列表路**用:失败时不删输出,改抛 `PartialOutput` 把它交出去,
    由调用方按条目收(见那个类的说明)。默认语义不动 —— 单视频那三条路要的是全有全无。

    `origin` 是**这条命令朝谁发的请求**:默认 YouTube,本机工具(ffmpeg、whisper-cli)
    必须显式传 `LOCAL` —— 它们的报错一个字都不该进 `login.classify`(理由见 `diagnosis`
    那几个常量,`throttle.witness` 也记着这条)。

    **「这次输出里有没有限流」在这儿记一次,进程一跑完就记,退 0 也记**(下面那个
    `finally`)。挂在各个调用方那里的话必然漏:字幕和音频拿到产物之后直接把日志删了 ——
    而 429 很多时候只是一条警告(进程退 0、字幕也是完整的),于是那一次挨的罚谁都不知道,
    紧接着又去探下一条视频(审查实测:第一次字幕 429 之后还发起了两次新的读取)。
    """
    if stopped() or time.monotonic() >= deadline:
        raise Interrupted('已停止或本轮视频处理时间预算用尽')
    try:
        scratch = unique(output)
    except OSError as error:
        # 建目录是**本机**的事。不标上游的话 `permission denied` 会被判成「读不出 cookie」,
        # 面板让人去钥匙串放行,而问题在磁盘上(口径见 `diagnosis` 那几个常量)。
        raise ToolFailure(f'建不了工具输出目录：{error}', '', '', LOCAL) from None
    state = {'bytes': 0, 'oversize': False, 'done': False, 'failure': None,
             'noise': '', 'code': ''}
    try:
        try:
            process = subprocess.Popen(arguments, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                       start_new_session=os.name != 'nt')
        except FileNotFoundError:
            raise RuntimeError(f'缺少命令 {arguments[0]}，请先安装对应工具') from None
        except OSError as error:
            # 工具在、但起不来(没有执行权限、被系统策略拦下)。**这是本机的事** ——
            # 不标来源的话 `login.coded` 按 YouTube 算,`permission denied` 落进
            # cookie 那一档,面板让人去钥匙串放行。口径同 `llm_request` 那一道。
            raise ToolFailure(f'起不了 {arguments[0]}：{error}', '', '', LOCAL) from None
        # **输出先过一道脱敏再落盘**(`pump_output`):直接重定向到文件的话,原始 info JSON
        # 在执行期间就带着 cookie 躺在磁盘上,之后再 scrub 已经晚了。
        pump = threading.Thread(target=pump_output, args=(process.stdout, scratch, state), daemon=True)
        pump.start()
        try:
            while process.poll() is None:
                if stopped() or time.monotonic() >= deadline:
                    raise Interrupted('已停止或本轮视频处理时间预算用尽')
                if state['oversize']:
                    raise RuntimeError('工具输出异常过大，已停止本次处理')
                time.sleep(.2)
            pump.join(30)
            if state['oversize']:
                raise RuntimeError('工具输出异常过大，已停止本次处理')
            if state['failure'] is not None:
                raise ToolFailure(f'写不了工具输出：{state["failure"]}', '', '', LOCAL) from None
        finally:
            terminate(process)
            pump.join(5)
            # **这一次的完整诊断在这儿读一次、记一次**:成功、半截、失败都过这一道
            # (`throttle.witness`)。这是整条链上唯一一处能看见未裁切原文的地方,
            # 也是所有 yt-dlp 请求**共用的完成边界**;往上走的每一层都在裁。
            # 原文**不分上游都要读**:ffmpeg 转码为什么失败,人得在面板上看得见。
            # 分档和「记这一笔罚」才按上游门控,那道门在 `throttle.witness` 里面。
            state['noise'] = diagnostics(scratch)
            state['code'] = throttle.witness(state['noise'], origin)
            # **这一次的原话也在这儿交账**(`diagnosis.record`,成功、半截、失败都交)。
            # 挂在各个调用方的成功分支上必然漏:解析抛了、写盘失败了、下一阶段抛了,
            # 已经读到的那句 cookie 警告就整段蒸发(审查三轮各实测到一种组合)。
            # **这里只记事实,要不要说、说给谁听由开账那一段决定**(`diagnosis.heard`)。
            record(state['noise'], origin)
        if process.returncode:
            # **分档在这儿就算完**,算的是完整诊断;异常消息只是给人看的摘要。
            raise ToolFailure(f'{arguments[0]} 执行失败', state['noise'], state['code'], origin)
    except Exception as error:
        # **列表路要的是「已经整行印出来的那几条」,不是进程的退出码**(见 `PartialOutput`)。
        # 按了停止那一下例外:那时不许拿半截结果去固化这一轮的末态。
        if partial and not stopped() and written(scratch):
            raise PartialOutput(scratch, str(error), error) from None
        heed(scratch)
        raise
    except BaseException:
        heed(scratch)
        raise
    return scratch


def ytdlp(arguments, output, deadline, stopped, spec='', playlist=False, partial=False, downloads=False):
    """跑一次 yt-dlp。

    **命令前缀走 `toolchain.command()`**(自备那份优先):用户在面板上更新过之后就该用上新版,
    而 PATH 里那份可能还是旧的。**登录态走 `--cookies-from-browser`**,口径见决策文档第一节。

    **`playlist=True` 才不带 `--no-playlist`**:列频道那条路要的恰恰是播放列表展开,
    而单视频的三条路(元数据、字幕、音频)都必须钉住只处理这一条,不能被频道页带偏。
    """
    # **这一轮已经撞上 429 的话,到这儿就结束,一个进程都不起**(`throttle.guard`)。
    # 这道闸待在请求入口而不是某个 `except` 里:后路 flat、probe、字幕的下一种语言、
    # 音频下载都从这儿过,挂在循环上的话每加一条路径就得记得再挡一次(审查实测漏了三处)。
    throttle.guard()
    prefix = toolchain.command()
    if not prefix:
        raise RuntimeError('缺少命令 yt-dlp，请在「我的订阅 → YouTube」里更新一次，或自行安装后重启服务')
    # **不带 `--no-warnings`。** cookie 解密失败在 yt-dlp 那边只是一条警告
    # (`cannot decrypt v10 cookies: no key found`),压掉它之后「读不出 cookie」就只能
    # 表现成后面撞墙,于是全被归成「没登录」—— 而那两件事的下一步完全不同(一个去钥匙串
    # 放行,一个去浏览器登录)。消费方都按行解析,多几行警告不影响。
    base = ['--ignore-config', '--no-cache-dir', '--no-progress',
            '--socket-timeout', '15', '--retries', '1', '--extractor-retries', '1']
    if not playlist:
        base.append('--no-playlist')
    if not downloads:
        # **不下载的那几条路不许因为「没有可下载的格式」整条失败**(`--ignore-no-formats-error`)。
        # 真机形态(实测复现):YouTube 发了 JS 挑战,yt-dlp 没有本地解题脚本 → 只剩
        # 预览图那几个格式 → 默认的格式选择器一个都没匹配上 → `Requested format is not
        # available` 当场抛错。元数据、列频道、字幕这三条**压根不需要任何格式**,却跟着
        # 一起失败:连「检查登录态」都报错,而那一轮的 cookie 其实好着
        # (`Found YouTube account cookies`,1843 条)。带上这个开关之后同一次调用退 0、
        # 元数据照旧完整,那两句话降级成 warning 留在输出里 —— 分档照旧看得见
        # (`login.classify` 的 `js_challenge`),面板上并列说出来。
        # **下音频那一步不带它**:那里「没有可用格式」是真失败,不是可以往下走的警告。
        base.append('--ignore-no-formats-error')
    return run([*prefix, *base, *(login.arguments(spec) if spec else []), *arguments],
               output, deadline, stopped, partial=partial)


def _channel_url(source):
    """频道的视频页。**两条 yt-dlp 发现路共用这一份** —— 它们的差别只在 flat 与否。"""
    return str(source['url']).rstrip('/') + '/videos'


def _cap(limit):
    """一次最多列几条。**封顶在这儿只有一份口径**,两条列表路都过它。"""
    return str(max(1, min(50, int(limit))))


def _collect_listing(arguments, path, spec, deadline, stopped, parse, url, notes):
    """列频道共用的「按条目收」适配层。**两条列表路都走这一份。**

    **契约和单视频那三条不一样**:yt-dlp 非零退出、或者预算在第 7 条上到点时,已经整行
    印出来的条目照旧收下(见 `PartialOutput`),失败原话另记进 `notes` —— 调用方把它转成
    面板上那句「这个频道没列完」。一条合法条目都没有才算这次真失败,那时把命令自己的
    原话放在最前面:它是人唯一的线索,也是 `login.classify` 分档用的那份原文。

    **`notes` 是出参,不是开关**:不传就只是没人收,收不收都不改变「条目照旧收下」
    这件事 —— 否则哪天漏传一次,丢数据就又回来了。

    **工具这一次报了什么不在这儿交账**:那是 `run` 的完成边界记的(`diagnosis.record`)。
    原先在这儿、而且排在 `parse` 之后 —— 条目里有个非法日期把解析抛了,已经读到的那句
    cookie 警告就一个字都到不了结果。
    """
    reason, cause = '', None
    try:
        output = ytdlp(arguments, path, deadline, stopped, spec=spec, playlist=True, partial=True)
    except PartialOutput as broken:
        output, reason, cause = broken.path, broken.reason, broken.cause
    # 这一份原文还要再分一次档给下面那几抛用。**记那一笔罚不靠这儿** —— 正路是
    # `run` 的完成边界(成功、半截、失败都过),这儿只是冗余带:`trip` 第一笔说了算,
    # 多调一次不会算成两笔。
    noise = diagnostics(output)
    throttle.witness(noise)
    try:
        entries = parse(output, url)
    except Exception as error:
        # **半截输出底下那条真实失败说了算**(`PartialOutput.cause`):它带着上游和在完整
        # 诊断上算好的分档,而这儿看得见的只是「没有可解析的条目」。丢掉它的话,本机写盘
        # 失败经过这一抛就成了 YouTube 的失败,再过一次归并又按文本猜成「读不出 cookie」,
        # 面板让人去钥匙串放行(而该做的是腾磁盘)。没有半截输出的照旧按解析错误算。
        raise ToolFailure('；'.join(part for part in (reason, str(error)) if part), noise,
                          login.coded(cause if cause is not None else error),
                          getattr(cause, 'origin', YOUTUBE)) from None
    finally:
        heed(output)
    if reason and notes is not None:
        notes.append(f'只列到 {len(entries)} 条就中断了：{reason}')
    return entries


def channel_videos(source, settings, deadline, stopped, limit=20, notes=None):
    """用 yt-dlp 列一个频道最近的视频。**只走 `--flat-playlist`。**

    实测:flat 列一个频道 1.4~30 秒(端侧抖动很大)。**它和 `detailed_videos` 的差别只有
    `--flat-playlist` 这一个参数**,而代价差一个数量级:flat 拿不到播放量、发布时间和简介
    (连 `youtubetab:approximate_date` 也不给),非 flat 每条约 4.3 秒(20 条 78 秒,早先记的
    「25 秒 0 条」是当时只给了 25 秒预算被超时掐掉,不是 bot 墙,已更正)。所以这条排在
    `detailed_videos` 之后、只在它也没取到时兜底 —— 估出来的日期要等 `media.probe` 才收口。

    **列不出东西一律抛**:真空频道和列举失败在这条路上分不开,而抛的代价只是按 URL 去重地
    重列一次,漏报则是这个频道从此「今天没有新内容」。**但「列了几条之后失败」不算列不出**
    —— 那几条照旧收下(契约在 `_collect_listing`)。
    """
    url = _channel_url(source)
    return _collect_listing(['--flat-playlist', '--playlist-end', _cap(limit), '--dump-json', url],
                            DATA_DIR / 'youtube' / 'channels' / f"{int(source['id'])}.jsonl",
                            of(settings)['cookies_browser'], deadline, stopped, _listed, url, notes)


def detailed_videos(source, settings, deadline, stopped, limit=10, notes=None):
    """列一个频道最近的视频,**带登录态逐条解析** —— 拿回真实发布时间和播放量。

    和 `channel_videos` 同一个地址、同一把 cookie,差别只有一个:**不带 `--flat-playlist`**。
    flat 快但只给 id、标题和时长;非 flat 每条都真解析一次,`upload_date`/`view_count`
    才有。实测同一频道带 cookie、`--playlist-end 3` 用 13 秒拿回 3 条(约 4.3 秒一条),
    20 条 78 秒 —— 所以它是**退路**,而且要靠 `limit` 封顶。

    **这条路不要走 `feeds/videos.xml`。** daily-report 的 `fetch_yt_channels.py` 是把那个
    订阅地址交给 yt-dlp 的,但 yt-dlp 不认领它(离线核过:匹配到 `generic`),generic 把那份
    Atom 当成**单个视频页**处理 —— 实测只抠出最新那 1 条,`--max-downloads 10` 根本没机会
    生效。拿它当退路等于一个频道只补回一条视频。

    **拿到的是真实日期,所以条目上不打 `approximate`**:指定日期补抓那一轮也能用它。

    `--dump-json` 每条视频印一行 JSON,正好落在 `pump_output` 的 `redact()` 那道边界上
    (JSON 行会过 `scrub()`,凭据键当场摘掉)。**一条私有视频或者预算到点不许连坐**:
    已经整行印出来的照旧收下(契约在 `_collect_listing`)。
    """
    url = _channel_url(source)
    return _collect_listing(['--skip-download', '--dump-json', '--playlist-end', _cap(limit), url],
                            DATA_DIR / 'youtube' / 'channels' / f"detail-{int(source['id'])}.jsonl",
                            of(settings)['cookies_browser'], deadline, stopped, _detailed, url, notes)


def _detailed(output, url):
    """逐行取 JSON,**只收真的带发布时间那几条**。

    读不出时间的不拿「现在」顶替(那会让一条老视频以今天的身份进窗口),直接跳过;
    一条都没收到时照 `_listed` 的口径抛,并带上 yt-dlp 原话 —— 真空频道和取失败在这条路
    上同样分不开,而漏报是静默丢数据。
    """
    entries, seen = [], set()
    for line in output.read_text(encoding='utf-8', errors='replace').splitlines():
        if not line.startswith('{'):
            continue
        try:
            payload = json.loads(line)
        except ValueError:
            continue
        video_id = str(payload.get('id') or '')
        if not VIDEO_ID.fullmatch(video_id) or video_id in seen:
            continue
        stamp = payload.get('timestamp')
        day = str(payload.get('upload_date') or '')
        if isinstance(stamp, (int, float)):
            published = datetime.fromtimestamp(stamp, timezone.utc)
        elif len(day) == 8 and day.isdigit():
            # 只有日期那一档:按项目时区的当天零点算,不补一个假的钟点。
            published = datetime.strptime(day, '%Y%m%d').replace(tzinfo=TZ)
        else:
            continue
        seen.add(video_id)
        views = payload.get('view_count')
        duration = payload.get('duration')
        entries.append({'video_id': video_id, 'title': str(payload.get('title') or video_id),
                        'channel_id': str(payload.get('channel_id') or ''),
                        'published': published.astimezone(TZ).isoformat(),
                        'views': max(0, int(views)) if isinstance(views, (int, float)) else 0,
                        'description': str(payload.get('description') or '')[:4000],
                        'duration': duration if isinstance(duration, (int, float)) else None,
                        'url': 'https://www.youtube.com/watch?v=' + video_id})
    if not entries:
        noise = diagnostics(output)
        raise ToolFailure(f'yt-dlp 没有从 {url} 解析出带发布时间的视频', noise,
                          throttle.witness(noise))
    return entries


def _listed(output, url):
    entries = []
    seen = set()
    for line in output.read_text(encoding='utf-8', errors='replace').splitlines():
        # stdout 和 stderr 都重定向到了同一个文件,所以非 JSON 行照常出现,跳过即可。
        if not line.startswith('{'):
            continue
        try:
            payload = json.loads(line)
        except ValueError:
            continue
        video_id = str(payload.get('id') or '')
        if not VIDEO_ID.fullmatch(video_id) or video_id in seen:
            continue
        seen.add(video_id)
        duration = payload.get('duration')
        entries.append({'video_id': video_id, 'title': str(payload.get('title') or video_id),
                        'channel_id': str(payload.get('channel_id') or ''),
                        'duration': duration if isinstance(duration, (int, float)) else None,
                        'url': 'https://www.youtube.com/watch?v=' + video_id})
    if not entries:
        # **工具的原话要带上**:`login.classify` 靠它分「cookie 读不出来」和「没登录」,
        # 面板上的「yt-dlp 原文」那一行也是它。只报「没列出任何视频」等于把唯一的线索扔了。
        noise = diagnostics(output)
        raise ToolFailure(f'yt-dlp 没有从 {url} 列出任何视频', noise, throttle.witness(noise))
    return entries


def fresh_metadata(path, video_id):
    cached = read_json(path)
    if not isinstance(cached, dict) or cached.get('id') != video_id:
        return None
    lifetime = 900 if tracks(cached, False) or tracks(cached, True) else 120
    try:
        age = time.time() - path.stat().st_mtime
    except OSError:
        return None
    if age > lifetime:
        return None
    return cached


def probe(row, settings, deadline, stopped, force=False):
    """取一条视频的元数据。

    **工具这一次报了什么由 `run` 的完成边界交账**(`diagnosis.record`):原先在这儿交、
    而且排在写缓存之后 —— 写盘一失败,已经读到的那句 cookie 警告就到不了结果。
    命中缓存那一次压根没起过进程,所以那本账里一个字都没有 —— 凭空报一条警告等于拿旧
    证据冒充这一轮的事实,这件事现在由「没起过进程就没有记录」自然保证。
    """
    directory = cache_dir(row['video_id'])
    cached = None if force else fresh_metadata(directory / 'metadata.json', row['video_id'])
    if cached:
        return cached
    if not force and VIDEOS_ROOT.is_dir():
        for previous in sorted(VIDEOS_ROOT.glob('*' + row['video_id'] + '*')):
            cached = fresh_metadata(previous / 'metadata' / 'youtube_metadata.json', row['video_id'])
            if cached:
                return cached
    output = ytdlp(['--skip-download', '--dump-single-json', row['entry']['url']],
                   directory / 'probe.log', deadline, stopped,
                   spec=of(settings)['cookies_browser'])
    # 分档在这儿算出来给下面那几抛用。**「这一次撞上限流没有」已经由 `run` 的完成边界
    # 记过了**(取成功也可能带着 429 警告,而后面还有字幕和音频两个请求要发)。
    noise = diagnostics(output)
    code = throttle.witness(noise)
    try:
        payload = _single(output)
        if not isinstance(payload, dict) or payload.get('id') != row['video_id']:
            raise ToolFailure('元数据的视频 ID 与候选不一致', noise, code)
        # **凭据不许落盘。** yt-dlp 会把匹配上的 cookie 串放进 `info_dict['cookies']`,
        # 它的 `sanitize_info` 不移除那个键 —— 原样缓存等于把登录凭据写进数据目录。
        payload = scrub(payload)
        save_json(directory / 'metadata.json', payload)
        return payload
    finally:
        heed(output)


def _single(output):
    """从输出里取那一整行 JSON。**按行找,不整份 `json.loads`** —— 不带
    `--no-warnings` 之后 stdout 里会混进警告行(那正是 cookie 诊断的来源)。"""
    text = output.read_text(encoding='utf-8', errors='replace')
    for line in text.splitlines():
        if line.lstrip().startswith('{'):
            try:
                return json.loads(line)
            except ValueError:
                continue
    noise = diagnostics(output)
    if OVERSIZE in text:
        # **是本机那道上限丢的,不是 YouTube 没给。** 照 YouTube 报的话,同一份输出里只要
        # 还有 JS 挑战之类的警告,分类器就照它们下结论、让人去更新 yt-dlp(实测)。
        raise ToolFailure('yt-dlp 取到了元数据，但那一行超过了本机的单行上限、没有写盘', noise, '', LOCAL)
    raise ToolFailure('YouTube 元数据无效，未开始下载或转录', noise, throttle.witness(noise))


def tracks(metadata, automatic=False):
    available = metadata.get('automatic_captions' if automatic else 'subtitles') or {}
    if not isinstance(available, dict):
        return []
    original = str(metadata.get('language') or '').split('-')[0]
    keys = [key for key in available if isinstance(key, str) and key != 'live_chat'
            and isinstance(available[key], list) and available[key]]
    if automatic:
        # 判据和写盘前那一剪是同一份(`diagnosis.translated`):剪掉的恰好是这里永远不选的。
        keys = [key for key in keys if any(isinstance(item, dict) and item.get('url') and
            not translated(item) for item in available[key])]
        if original:
            keys = [key for key in keys if key.split('-')[0] == original]
    keys.sort(key=lambda key: (key.split('-')[0] != original if original else not key.endswith('-orig'),
                               not key.startswith('en'), key))
    return keys


def local_transcript(video_id, duration):
    if not VIDEOS_ROOT.is_dir():
        return None
    for directory in sorted(VIDEOS_ROOT.glob('*' + video_id + '*')):
        metadata = read_json(directory / 'metadata' / 'youtube_metadata.json')
        path = directory / 'subtitles' / 'source.srt'
        if not isinstance(metadata, dict) or metadata.get('id') != video_id or not path.is_file():
            continue
        try:
            segments = transcripts.validate(transcripts.parse(path.read_text(encoding='utf-8'), '.srt'), duration)
            return {'origin': 'local_cache', 'language': metadata.get('language') or '', 'segments': segments}
        except (ValueError, OSError):
            continue
    return None


def heed(log):
    """这一次的日志用完就删 —— **临时文件的清理只走这一个出口**。

    **证据不在这儿** —— 它在 `run` 的完成边界已经交过账了(`diagnosis.record`),所以删
    得掉也不怕;原先是在这儿读、在这儿交,而那只在拿到产物的那条路上会执行。

    删不掉是**本机**的毛病,所以标 `LOCAL`(`on_disk`):裸 `OSError` 的 `permission
    denied` 正在 `login._COOKIE` 的特征串里,不标来源就会被当成「读不出浏览器的
    cookie」,面板让人去钥匙串放行,而该做的是腾磁盘或改权限。

    **但已经有异常在飞时只闭嘴**(`sys.exc_info()`):清理挂在 `finally` 和 `except`
    上,那时正在抛的那条才是人要看的原因,拿「删不掉某个日志」顶掉它等于把线索换成
    一条噪声 —— 口径同 `obtain`「后面这一步只许追加、不许抹掉」。临时名带 pid 和序号
    (`unique`),留在盘上也不会和谁撞。
    """
    if sys.exc_info()[1] is not None:
        with contextlib.suppress(OSError):
            log.unlink(missing_ok=True)
        return
    with on_disk(f'删不掉 {log.name}'):
        log.unlink(missing_ok=True)


def captions(row, metadata, settings, automatic, deadline, stopped):
    languages = tracks(metadata, automatic)
    if not languages:
        return None
    failures, broken = [], []
    for position, language in enumerate(languages):
        directory = cache_dir(row['video_id']) / (('automatic' if automatic else 'uploaded') + f'-{position}')
        with on_disk('清不出字幕缓存目录'):
            directory.mkdir(exist_ok=True)
            for stale in directory.glob('captions.*'):
                stale.unlink()
        try:
            heed(ytdlp(['--skip-download', '--write-auto-subs' if automatic else '--write-subs',
                        '--sub-langs', language, '--sub-format', 'json3/vtt',
                        '-o', str(directory / 'captions.%(ext)s'), row['entry']['url']],
                       directory / 'download.log', deadline, stopped,
                       spec=of(settings)['cookies_browser']))
            for path in sorted(directory.glob('captions.*')):
                if path.suffix not in ('.json3', '.vtt'):
                    continue
                segments = transcripts.validate(transcripts.parse(path.read_text(encoding='utf-8'), path.suffix),
                                               float(metadata['duration']))
                return {'origin': 'automatic_captions' if automatic else 'uploaded_captions',
                        'language': language.removesuffix('-orig'), 'segments': segments}
            failures.append('工具未生成预期的字幕文件')
        except Interrupted:
            raise
        except throttle.NotAttempted:
            # **被拒绝的那一次不是「这种语言没字幕」** —— 归并进去的话,它和真实失败
            # 混成一条普通 `RuntimeError` 交出去,上层再也认不出「压根没发请求」,
            # 于是这条视频被记失败、推退避(审查实测)。换别的语言也一样会被拒。
            # **但前面已经有真发出去的失败时,那条说了算**(见 `throttle.NotAttempted`):
            # 这条视频确实没拿到字幕,不许说成「没轮到」。
            if not broken:
                raise
            break
        except (ValueError, OSError, RuntimeError, TypeError, AttributeError) as error:
            # **一种语言坏了不许连坐其余几种。** 这一档收下失败、接着试下一种 ——
            # 删掉它的那一版让 en 的 JSON 一坏就整个循环退出,而 zh 的完整字幕就在那儿
            # (审查实测:只起了 en 那一次子进程,视频记成「没有可用字幕」并推了退避,
            # 独立对照读取证明 zh 是能解析出整段内容的)。**人工和自动两类共用这个循环**,
            # 所以漏的不是某一类。
            failures.append(str(error))
            broken.append(error)
    # **分档要跟着合并后那一条走**(`login.merge`,按证据强度挑、不按到达先后):
    # 只留文字的话,某种语言撞上的 429 到了外层就只是一句话 —— 上层既分不出档,
    # 也不知道这一轮该收手了。
    raise login.merge(RuntimeError('；'.join(failures)), broken)


def asr(row, metadata, settings, deadline, stopped):
    directory = cache_dir(row['video_id'])
    output = directory / 'whisper.json'
    if output.is_file():
        try:
            segments = transcripts.validate(transcripts.parse(output.read_text(encoding='utf-8'), '.json'),
                                           float(metadata['duration']))
            return {'origin': 'local_asr', 'language': metadata.get('language') or '', 'segments': segments}
        except (ValueError, OSError, TypeError):
            pass
    # **本轮已经撞上 429 的话,这里不许再往下走。** 往下第一句是「ASR 已关闭/缺工具」——
    # 那句话会把限流这件事盖掉(原始 429 再也传不到外层,而外层正要靠它决定收不收手)。
    # 上面那段本地已有的 whisper.json 照旧可以用:它一个请求都不发。
    throttle.guard()
    status = tools(settings)
    if not status['asr_enabled']:
        raise RuntimeError('没有可用字幕，且本地语音转录已关闭')
    if not all(status[key] for key in ('ffmpeg', 'whisper', 'model')):
        raise RuntimeError('没有可用字幕；本地转录需要 FFmpeg、whisper-cli 和 Whisper 模型')
    original_tracks = tracks(metadata, True)
    language = str(metadata.get('language') or (original_tracks[0] if original_tracks else 'auto'))
    language = language.removesuffix('-orig').split('-')[0]
    model = model_path(settings)
    if '.en.' in model.name and language != 'en':
        raise RuntimeError('当前 Whisper 模型仅支持英文；此视频需要多语言模型，未开始转录')
    audio_files = [path for path in directory.glob('audio.*')
                   if path.suffix in ('.m4a', '.webm', '.opus', '.mp3', '.ogg', '.wav', '.aac', '.flac')]
    if not audio_files:
        # **这一条是真下载,所以不带 `--ignore-no-formats-error`**(`downloads=True`):
        # 那里「没有可用格式」是真失败,带上之后 yt-dlp 会退 0 却一个文件都没下下来,
        # 我们只好报一句「未取得音频」,真正的原因就此丢掉(见 `ytdlp` 里那段)。
        heed(ytdlp(['-f', 'bestaudio[abr<=160]/bestaudio', '--max-filesize', '100M',
                    '-o', str(directory / 'audio.%(ext)s'), row['entry']['url']],
                   directory / 'audio-download.log', deadline, stopped,
                   spec=of(settings)['cookies_browser'], downloads=True))
        audio_files = [path for path in directory.glob('audio.*')
                       if path.suffix in ('.m4a', '.webm', '.opus', '.mp3', '.ogg', '.wav', '.aac', '.flac')]
    if not audio_files:
        raise RuntimeError('未取得音频，不能运行本地语音转录')
    wave = directory / 'speech.wav'
    # 这两条不读日志,只看产物;返回的那一份(带 pid 和序号)用完就删(`heed`)。
    heed(run(['ffmpeg', '-nostdin', '-y', '-i', str(audio_files[0]), '-vn', '-ar', '16000',
              '-ac', '1', '-acodec', 'pcm_s16le', str(wave)],
             directory / 'ffmpeg.log', deadline, stopped, origin=LOCAL))
    output = directory / 'whisper'
    heed(run(['whisper-cli', '-m', str(model), '-f', str(wave), '-l', language, '-oj', '-ojf',
              '-of', str(output), '-np', '-t', str(of(settings)['asr_threads'])],
             directory / 'asr.log', deadline, stopped, origin=LOCAL))
    segments = transcripts.validate(transcripts.parse(output.with_suffix('.json').read_text(encoding='utf-8'), '.json'),
                                   float(metadata['duration']))
    return {'origin': 'local_asr', 'language': language, 'segments': segments}


def obtain(row, metadata, settings, deadline, stopped):
    directory = cache_dir(row['video_id'])
    payload = read_json(directory / 'transcript.json')
    if (isinstance(payload, dict) and payload.get('video_id') == row['video_id'] and
            payload.get('origin') in ('local_cache', 'uploaded_captions', 'automatic_captions', 'local_asr') and
            isinstance(payload.get('language'), str)):
        try:
            transcripts.validate(payload['segments'], float(metadata['duration']))
            return payload
        except (KeyError, ValueError, TypeError):
            pass
    payload = local_transcript(row['video_id'], float(metadata['duration']))
    errors, broken = [], []
    for automatic in (False, True):
        if payload:
            break
        try:
            payload = captions(row, metadata, settings, automatic, deadline, stopped)
        except Interrupted:
            raise
        except throttle.NotAttempted:
            # 口径同 `captions` 里那一道:人工字幕被拒,自动字幕也一样会被拒,
            # 吞下来接着往下走等于把「没发请求」变成「这条视频失败了」;
            # 而前面已经有真实失败时,那条说了算。
            if not broken:
                raise
            break
        except (ValueError, OSError, RuntimeError, TypeError, AttributeError) as error:
            errors.append(str(error)[:300])
            broken.append(error)
    if errors or not payload:
        heed(directory / 'metadata.json')
    if not payload:
        try:
            payload = asr(row, metadata, settings, deadline, stopped)
        except Interrupted:
            raise
        except throttle.NotAttempted:
            # 本机转录这一步也被拒了。字幕那几次**真发出去并失败**的话,按它们交账:
            # 分档和完整诊断跟着走(`login.merge`),面板上说的还是那条真实原因。
            if not broken:
                raise
            raise login.merge(RuntimeError('；'.join(errors)), broken) from None
        except (ValueError, OSError, RuntimeError, TypeError, AttributeError) as error:
            # **后面这一步失败只许往后追加原因,不许把前面那几条抹掉。** 只抛自己那一条
            # 的话,字幕那几次真实失败(读不出 cookie、机器人验证)连同它们的分档和完整
            # 诊断一起被顶掉,面板上只剩一句「没有可用字幕,且本地语音转录已关闭」——
            # 人照着它去开转录,而根因是这一轮没带上登录态(审查实测)。
            # **这一档罩住本机转录的所有普通失败**:关着、缺工具、模型语言不对、
            # ffmpeg/whisper 失败、取不到音频,不是只有「关着」那一种。
            errors.append(str(error)[:300])
            broken.append(error)
            raise login.merge(RuntimeError('；'.join(errors)), broken) from None
    payload.update(video_id=row['video_id'], caption_warnings=errors)
    save_json(directory / 'transcript.json', payload)
    for path in [directory / 'speech.wav', *directory.glob('audio.*')]:
        if path.is_file():
            heed(path)
    return payload
