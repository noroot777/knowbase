import itertools
import json
import os
import re
import signal
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo

from kb.config import DATA_DIR, VIDEOS_ROOT
from kb.youtube import login, toolchain, transcripts
from kb.youtube.diagnosis import ToolFailure, excerpt
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
    """

    def __init__(self, path, reason):
        super().__init__(reason)
        self.path = path
        self.reason = reason


#: 同进程内给输出文件编号。跨进程靠 pid,进程内靠这个 —— 两者合起来才唯一。
_SERIAL = itertools.count()

#: 落盘和报错之前要剔掉的键。yt-dlp 在媒体 URL 有匹配 cookie 时会把 cookie 串
#: 放进 `info_dict['cookies']`,`sanitize_info` 不移除它。
_SECRET = re.compile(r'cookie|authorization|password|token', re.I)


def unique(output):
    """这一次调用自己的输出文件。**几次调用不许共写一个路径。**

    面板上的「检查登录态」和同步是两个并发入口(`login.check` 也走 `probe` 和
    `channel_videos`),共写时后一次的 `open('wb')` 截断会把前一次读到一半的 JSON
    变成半截 —— 报出来是「YouTube 元数据无效」,而那条视频一点问题都没有,随后还要
    白等一次退避(审查用两个真实子进程复现)。按单个 HTTP 路由加锁挡不住这个:
    竞争发生在文件上,不在路由里。
    """
    output.parent.mkdir(parents=True, exist_ok=True)
    return output.with_name(f'{output.name}.{os.getpid()}.{next(_SERIAL)}')


def _written(path):
    """这份输出里到底有没有字节。**读不到就按没有算** —— 「有没有落下东西」这件事
    拿不准时宁可当没有,拿半截去冒充结果比重列一次贵得多。"""
    try:
        return path.stat().st_size > 0
    except OSError:
        return False


def diagnostics(output):
    """从工具输出里挑出能给人看、也能给 `login.classify` 看的那几行:**只要非 JSON 行**。

    两个理由钉在一起:①警告和错误都在非 JSON 行里 —— cookie 解密失败就是其中一条
    (`cannot decrypt v10 cookies: no key found`),`login` 要靠它把「cookie 读不出来」
    和「没登录」分开;②JSON 那一行可能带着 `cookies` 字段,它一个字都不该进异常消息
    或日志 —— 我们对外承诺的是不落盘、不记日志。

    **这里一个字都不裁。** 分类要看完整文本,裁字数只许发生在展示那一端
    (`diagnosis.excerpt`)—— 原先按尾部留 800 字,把开头那行 429 裁掉了,于是限流被判成
    bot 墙(审查实测)。文件本身已被 `_pump` 按 8 MiB 封顶,整份读进来不是新增的内存风险
    (原先也是整份 `read_text` 之后才裁)。
    """
    try:
        text = output.read_text(encoding='utf-8', errors='replace')
    except OSError:
        return ''
    lines = [line.strip() for line in text.splitlines()
             if line.strip() and not line.lstrip().startswith(('{', '['))]
    return ' | '.join(lines)


def scrub(value):
    """递归剔掉可能带着凭据的键,落盘和返回之前都过一次。"""
    if isinstance(value, dict):
        return {key: scrub(item) for key, item in value.items() if not _SECRET.search(str(key))}
    if isinstance(value, list):
        return [scrub(item) for item in value]
    return value


#: 非 JSON 行上的凭据:只遮**值**,不遮那句话。
#: **不许按「这行里出现了 cookie 字样」整行遮掉** —— yt-dlp 把「解不开 cookie」报成
#: `WARNING: cannot decrypt v10 cookies: no key found`,整行遮掉之后 `login.classify`
#: 就再也分不出「读不出 cookie」和「没登录」,面板上那行「yt-dlp 原文」也空了
#: (第一版就这么写的,实测把这条唯一线索擦掉了)。
_SECRET_VALUE = re.compile(r'((?:set-)?cookies?|authorization|password|token)'
                           r'(["\']?\s*[:=]\s*)(.+)$', re.I)


def _hide(match):
    """像凭据的值才遮,而且**从它开始遮到行尾** —— cookie 头是 `k=v; k=v; …`,
    只遮第一段等于把后面几段原样留在磁盘上。判据看第一段:长,或者是个 `k=v`。
    「no key found」这种人话两条都不沾,留着。"""
    head = match[3].split(';')[0].split(',')[0].strip()
    looks_secret = len(head) >= 24 or ('=' in head and len(head) >= 8)
    return match[1] + match[2] + ('[已遮蔽]' if looks_secret else match[3])


#: 单行上限。一份完整 info JSON 几十 KB 很正常,超过这个的不是元数据,整行遮掉。
_LINE_CAP = 4 * 1024 * 1024
#: 一次调用的输出总上限。
_OUTPUT_CAP = 8 * 1024 * 1024


def redact(line: bytes) -> bytes:
    """**一行输出在写进文件之前**就把凭据抹掉。

    JSON 行解析出来过一次 `scrub()` 再写回去;解析不了的按特征串整行遮掉。
    """
    if len(line) > _LINE_CAP:
        return b'[\xe8\xbf\x87\xe9\x95\xbf\xe7\x9a\x84\xe8\xbe\x93\xe5\x87\xba\xe5\xb7\xb2\xe9\x81\xae\xe8\x94\xbd]\n'
    text = line.decode('utf-8', errors='replace')
    stripped = text.strip()
    if stripped.startswith(('{', '[')):
        try:
            return (json.dumps(scrub(json.loads(stripped)), ensure_ascii=False) + '\n').encode('utf-8')
        except ValueError:
            pass
    return _SECRET_VALUE.sub(_hide, text).encode('utf-8')


def _pump(stream, path, state):
    """把子进程输出边读边脱敏地写进 `path`。

    **这是凭据进磁盘的唯一边界。** 原来是把子进程的 stdout 直接重定向到文件、等它跑完
    再解析和 `scrub()`:那份原始 JSON 在执行期间就躺在数据目录里(审查实测读到了,权限
    还是 0644),而**强杀服务或断电根本不会执行 Python 的 finally**,文件就一直留着。
    「最终缓存里没有 cookies」不等于「cookies 没落过盘」—— 所以脱敏必须发生在写之前。
    文件本身按 0600 创建:诊断日志和凭据同源,不该是全机可读。
    """
    try:
        with open(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), 'wb') as log:
            for raw in stream:
                data = redact(raw)
                state['bytes'] += len(data)
                if state['bytes'] > _OUTPUT_CAP:
                    state['oversize'] = True
                    return
                log.write(data)
                log.flush()
    except (OSError, ValueError) as error:
        state['failure'] = error
    finally:
        state['done'] = True
        try:
            stream.close()
        except OSError:
            pass


def cache_dir(video_id):
    if not VIDEO_ID.fullmatch(video_id):
        raise ValueError('视频 ID 不正确')
    path = DATA_DIR / 'youtube' / video_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def save_json(path, payload):
    # 临时名也要带 pid 和序号:固定的 `.tmp` 在两份结果同时提交时会互相替换,
    # 还会被对方的 `unlink` 删掉 —— 原子替换保护的是读者,保护不了彼此。
    temporary = path.with_name(f'{path.name}.{os.getpid()}.{next(_SERIAL)}.tmp')
    try:
        temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


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


def run(arguments, output, deadline, stopped, partial=False):
    """跑一条命令,输出落在**这一次调用自己的**文件上(见 `unique`),返回那个文件。

    **唯一化在这里做,不在各个调用方那里做** —— 口径只有一份:所有子进程输出都经过
    这一道,新加一条调用路径时不必记得自己去重命名。失败时把那份删掉,该带走的诊断
    已经进了异常消息。

    `partial=True` 只给**列表路**用:失败时不删输出,改抛 `PartialOutput` 把它交出去,
    由调用方按条目收(见那个类的说明)。默认语义不动 —— 单视频那三条路要的是全有全无。
    """
    if stopped() or time.monotonic() >= deadline:
        raise Interrupted('已停止或本轮视频处理时间预算用尽')
    scratch = unique(output)
    state = {'bytes': 0, 'oversize': False, 'done': False, 'failure': None}
    try:
        try:
            process = subprocess.Popen(arguments, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                       start_new_session=os.name != 'nt')
        except FileNotFoundError:
            raise RuntimeError(f'缺少命令 {arguments[0]}，请先安装对应工具') from None
        # **输出先过一道脱敏再落盘**(`_pump`):直接重定向到文件的话,原始 info JSON
        # 在执行期间就带着 cookie 躺在磁盘上,之后再 scrub 已经晚了。
        pump = threading.Thread(target=_pump, args=(process.stdout, scratch, state), daemon=True)
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
                raise RuntimeError(f'写不了工具输出：{state["failure"]}') from None
            if process.returncode:
                # **分档在这儿就算完**,算的是完整诊断;异常消息只是给人看的摘要。
                # 这是唯一一处能看见未裁切原文的地方 —— 往上走的每一层都在裁。
                noise = diagnostics(scratch)
                raise ToolFailure(f'{arguments[0]} 执行失败', noise, login.classify(noise))
        finally:
            terminate(process)
            pump.join(5)
    except Exception as error:
        # **列表路要的是「已经整行印出来的那几条」,不是进程的退出码**(见 `PartialOutput`)。
        # 按了停止那一下例外:那时不许拿半截结果去固化这一轮的末态。
        if partial and not stopped() and _written(scratch):
            raise PartialOutput(scratch, str(error)) from None
        scratch.unlink(missing_ok=True)
        raise
    except BaseException:
        scratch.unlink(missing_ok=True)
        raise
    return scratch


def ytdlp(arguments, output, deadline, stopped, spec='', playlist=False, partial=False):
    """跑一次 yt-dlp。

    **命令前缀走 `toolchain.command()`**(自备那份优先):用户在面板上更新过之后就该用上新版,
    而 PATH 里那份可能还是旧的。**登录态走 `--cookies-from-browser`**,口径见决策文档第一节。

    **`playlist=True` 才不带 `--no-playlist`**:列频道那条路要的恰恰是播放列表展开,
    而单视频的三条路(元数据、字幕、音频)都必须钉住只处理这一条,不能被频道页带偏。
    """
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
    return run([*prefix, *base, *(login.arguments(spec) if spec else []), *arguments],
               output, deadline, stopped, partial=partial)


def _channel_url(source):
    """频道的视频页。**两条 yt-dlp 发现路共用这一份** —— 它们的差别只在 flat 与否。"""
    return str(source['url']).rstrip('/') + '/videos'


def _cap(limit):
    """一次最多列几条。**封顶在这儿只有一份口径**,两条列表路都过它。"""
    return str(max(1, min(50, int(limit))))


def _collect_listing(arguments, path, spec, deadline, stopped, parse, url, notes, warnings=None):
    """列频道共用的「按条目收」适配层。**两条列表路都走这一份。**

    **契约和单视频那三条不一样**:yt-dlp 非零退出、或者预算在第 7 条上到点时,已经整行
    印出来的条目照旧收下(见 `PartialOutput`),失败原话另记进 `notes` —— 调用方把它转成
    面板上那句「这个频道没列完」。一条合法条目都没有才算这次真失败,那时把命令自己的
    原话放在最前面:它是人唯一的线索,也是 `login.classify` 分档用的那份原文。

    **`notes` 和 `warnings` 都是出参,不是开关**:不传就只是没人收,收不收都不改变
    「条目照旧收下」这件事 —— 否则哪天漏传一次,丢数据就又回来了。

    **跑成功了也要把警告交出去**(`warnings`)。cookie 解密失败在 yt-dlp 那边只是一条
    warning:进程照旧退 0、公开视频照旧列得出来,于是「读不出 cookie」这条铁证跟着这份
    临时输出一起被删掉,面板上只剩一句「取到了元数据」(审查实测)。这份输出是它唯一的
    落点,读它只有这一个时机。
    """
    reason = ''
    try:
        output = ytdlp(arguments, path, deadline, stopped, spec=spec, playlist=True, partial=True)
    except PartialOutput as broken:
        output, reason = broken.path, broken.reason
    noise = diagnostics(output)
    try:
        entries = parse(output, url)
    except Exception as error:
        raise ToolFailure('；'.join(part for part in (reason, str(error)) if part),
                          noise, login.coded(error)) from None
    finally:
        output.unlink(missing_ok=True)
    if reason and notes is not None:
        notes.append(f'只列到 {len(entries)} 条就中断了：{reason}')
    if noise and warnings is not None:
        warnings.append(noise)
    return entries


def channel_videos(source, settings, deadline, stopped, limit=20, notes=None, warnings=None):
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
                            of(settings)['cookies_browser'], deadline, stopped, _listed, url, notes,
                            warnings)


def detailed_videos(source, settings, deadline, stopped, limit=10, notes=None, warnings=None):
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

    `--dump-json` 每条视频印一行 JSON,正好落在 `_pump` 的 `redact()` 那道边界上
    (JSON 行会过 `scrub()`,凭据键当场摘掉)。**一条私有视频或者预算到点不许连坐**:
    已经整行印出来的照旧收下(契约在 `_collect_listing`)。
    """
    url = _channel_url(source)
    return _collect_listing(['--skip-download', '--dump-json', '--playlist-end', _cap(limit), url],
                            DATA_DIR / 'youtube' / 'channels' / f"detail-{int(source['id'])}.jsonl",
                            of(settings)['cookies_browser'], deadline, stopped, _detailed, url, notes,
                            warnings)


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
        raise ToolFailure(f'yt-dlp 没有从 {url} 解析出带发布时间的视频', noise, login.classify(noise))
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
        raise ToolFailure(f'yt-dlp 没有从 {url} 列出任何视频', noise, login.classify(noise))
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


def probe(row, settings, deadline, stopped, force=False, warnings=None):
    """取一条视频的元数据。`warnings` 是出参,口径同 `_collect_listing`:**跑成功了也要
    把警告交出去**(cookie 解密失败只是一条 warning,进程照旧退 0)。命中缓存时不填 ——
    那一次压根没起过进程,凭空报一条警告等于拿旧证据冒充这一轮的事实。
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
    noise = diagnostics(output)
    try:
        payload = _single(output)
        if not isinstance(payload, dict) or payload.get('id') != row['video_id']:
            raise ToolFailure('元数据的视频 ID 与候选不一致', noise, login.classify(noise))
        # **凭据不许落盘。** yt-dlp 会把匹配上的 cookie 串放进 `info_dict['cookies']`,
        # 它的 `sanitize_info` 不移除那个键 —— 原样缓存等于把登录凭据写进数据目录。
        payload = scrub(payload)
        save_json(directory / 'metadata.json', payload)
        if noise and warnings is not None:
            warnings.append(noise)
        return payload
    finally:
        output.unlink(missing_ok=True)


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
    raise ToolFailure('YouTube 元数据无效，未开始下载或转录', noise, login.classify(noise))


def tracks(metadata, automatic=False):
    available = metadata.get('automatic_captions' if automatic else 'subtitles') or {}
    if not isinstance(available, dict):
        return []
    original = str(metadata.get('language') or '').split('-')[0]
    keys = [key for key in available if isinstance(key, str) and key != 'live_chat'
            and isinstance(available[key], list) and available[key]]
    if automatic:
        keys = [key for key in keys if any(isinstance(item, dict) and item.get('url') and
            not parse_qs(urlsplit(item['url']).query).get('tlang') for item in available[key])]
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


def captions(row, metadata, settings, automatic, deadline, stopped):
    languages = tracks(metadata, automatic)
    if not languages:
        return None
    failures = []
    for position, language in enumerate(languages):
        directory = cache_dir(row['video_id']) / (('automatic' if automatic else 'uploaded') + f'-{position}')
        directory.mkdir(exist_ok=True)
        for stale in directory.glob('captions.*'):
            stale.unlink()
        try:
            ytdlp(['--skip-download', '--write-auto-subs' if automatic else '--write-subs',
                   '--sub-langs', language, '--sub-format', 'json3/vtt',
                   '-o', str(directory / 'captions.%(ext)s'), row['entry']['url']],
                  directory / 'download.log', deadline, stopped,
                  spec=of(settings)['cookies_browser']).unlink(missing_ok=True)
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
        except (ValueError, OSError, RuntimeError, TypeError, AttributeError) as error:
            failures.append(str(error))
    raise RuntimeError('；'.join(failures))


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
        ytdlp(['-f', 'bestaudio[abr<=160]/bestaudio', '--max-filesize', '100M',
               '-o', str(directory / 'audio.%(ext)s'), row['entry']['url']],
              directory / 'audio-download.log', deadline, stopped,
              spec=of(settings)['cookies_browser']).unlink(missing_ok=True)
        audio_files = [path for path in directory.glob('audio.*')
                       if path.suffix in ('.m4a', '.webm', '.opus', '.mp3', '.ogg', '.wav', '.aac', '.flac')]
    if not audio_files:
        raise RuntimeError('未取得音频，不能运行本地语音转录')
    wave = directory / 'speech.wav'
    # 这两条不读日志,只看产物;返回的那一份(带 pid 和序号)用完就删。
    run(['ffmpeg', '-nostdin', '-y', '-i', str(audio_files[0]), '-vn', '-ar', '16000',
         '-ac', '1', '-acodec', 'pcm_s16le', str(wave)],
        directory / 'ffmpeg.log', deadline, stopped).unlink(missing_ok=True)
    output = directory / 'whisper'
    run(['whisper-cli', '-m', str(model), '-f', str(wave), '-l', language, '-oj', '-ojf',
         '-of', str(output), '-np', '-t', str(of(settings)['asr_threads'])],
        directory / 'asr.log', deadline, stopped).unlink(missing_ok=True)
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
    errors = []
    for automatic in (False, True):
        if payload:
            break
        try:
            payload = captions(row, metadata, settings, automatic, deadline, stopped)
        except Interrupted:
            raise
        except (ValueError, OSError, RuntimeError, TypeError, AttributeError) as error:
            errors.append(str(error)[:300])
    if errors or not payload:
        (directory / 'metadata.json').unlink(missing_ok=True)
    if not payload:
        payload = asr(row, metadata, settings, deadline, stopped)
    payload.update(video_id=row['video_id'], caption_warnings=errors)
    save_json(directory / 'transcript.json', payload)
    for path in [directory / 'speech.wav', *directory.glob('audio.*')]:
        if path.is_file():
            path.unlink(missing_ok=True)
    return payload
