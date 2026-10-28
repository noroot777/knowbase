"""工具诊断证据的运输口径:**分类在完整文本上做,裁字数只许发生在展示那一端。**

这一层单独拿出来,是因为同一条链上**两次**丢掉了决定性的那一行(都实测复现过):

1. `diagnostics()` 原先只留尾部 800 字。429 那一行在最前面,后面跟着一长串重试诊断和
   一句机器人验证 —— 按尾部一裁,429 不见了,同一次失败被判成 bot 墙,照它的提示去
   重新登录完全没用(用户本来就带着 cookie)。
2. 面板上的「yt-dlp 原文」取前 400 字。真正的最终错误在尾部,从另一头裁又把它丢了。

所以口径钉成两件事:**完整的脱敏诊断先过分类**(结论随异常一起往上走,见 `ToolFailure`),
**给人看的那一份两头都留**(`excerpt`)。消费方不许再拿 `str(error)` 重新分类。

同一个理由把**凭据脱敏**也放在这儿:那道边界(`redact` / `pump_output`)和「挑哪几行给
分类看」(`diagnostics`)是同一份输出上的同一件事 —— 遮多了分类就没线索了(见 `_SECRET_VALUE`
上面那段),遮少了凭据就落了盘。两头的口径必须摆在一起读。
"""
import contextlib
import contextvars
import itertools
import json
import os
import re


def excerpt(text, limit=800):
    """给人看的那一份:超了就**两头都留**,中间省略。

    单端裁是上面两处丢证据的直接原因 —— 开头那行和结尾那行恰恰是最有信息的两行。
    """
    text = str(text or '').strip()
    if len(text) <= limit:
        return text
    half = max(1, (limit - 3) // 2)
    return f'{text[:half]} … {text[-half:]}'


class ToolFailure(RuntimeError):
    """一次外部工具失败。**分档在抛出的那一刻就算完了**,算的是未裁切的完整诊断。

    `code` 给消费方直接用(`login.coded`),`diagnostic` 是完整脱敏诊断、给展示端自己裁。
    异常消息本身只是摘要,**不许再拿它去分类** —— 它为了给人看已经裁过。

    `origin` 是**哪个上游报的**(见下面那几个常量):`code` 只在 `YOUTUBE` 那一档算数。
    默认值写成字面量而不是 `YOUTUBE`,只因为常量定义在这个类下面。
    """

    def __init__(self, summary, diagnostic='', code='', origin='youtube'):
        self.diagnostic = str(diagnostic or '')
        self.code = code
        self.origin = origin
        super().__init__(f'{summary}；{excerpt(self.diagnostic)}'.strip('；'))


#: 这条失败是**哪个上游**报的。
#: **同一个状态码来自不同上游,下一步完全不同**:YouTube 的 429 要等一段时间或换出口,
#: 模型服务的配额换出口一点用都没有,本机工具(ffmpeg、whisper-cli)输出里的
#: `permission denied` 压根不该去碰 cookie 那一档(`login._COOKIE` 里有这个串)。
#: 审查实测:只有模型服务说「请求过多」的那一轮,面板说的是「YouTube 对本机出口返回了
#: 429、网页根本没下下来、换出口也有用」,还把一个无辜的频道记进了登录态提醒。
#:
#: **没标的按 `YOUTUBE` 算。** 漏标一条真限流会让这一轮接着朝同一个出口撞下去
#: (静默加深惩罚、几十条不是自己毛病的失败),多标一条顶多让这一轮早收手、下一轮重来。
YOUTUBE = 'youtube'
MODEL = 'model'
LOCAL = 'local'


#: 归并多条失败时按**证据强度**挑结论,那份口径在 `login.merge` —— 分档的语义归它管,
#: 这一层只管运输(谁报的、完整原文、给人看那一份怎么裁)。
#: 同进程内给输出文件编号。跨进程靠 pid,进程内靠这个 —— 两者合起来才唯一。
_SERIAL = itertools.count()


def serial():
    """进程内的下一个序号。**计数器只有一份**,`media` 那边给临时文件编号也走它。"""
    return next(_SERIAL)

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


def written(path):
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
    bot 墙(审查实测)。文件本身已被 `pump_output` 按 8 MiB 封顶,整份读进来不是新增的内存风险
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


def pump_output(stream, path, state):
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


#: 这一段里每一次子进程调用的诊断。`None` = 现在没人收(轮外、单元测试直接调 `run`)。
_HEARD = contextvars.ContextVar('youtube_heard', default=None)


@contextlib.contextmanager
def heard():
    """开一本诊断账:这一段里**每一次**子进程调用的原话都落进去 —— 成功、半截、失败都落。

    **「这条诊断有没有发生」不许由「后面那一步成不成」推断。** 原先每个调用方各自在自己
    的成功分支上把这句话交出去(列表 parse 之后、probe 写完缓存之后、字幕拿到产物之后),
    于是同一个漏法换个组合就又回来了:解析抛了、写盘失败了、下一阶段抛了,已经读到的那句
    `cannot decrypt v10 cookies` 整段蒸发 —— 面板上只剩「没有可用字幕」或者「一个没能归类
    的错误」,而根因是这一轮压根没带上登录态(审查三轮各实测到一种组合)。
    **所以记在 `media.run` 的完成边界上,只记一处**(`record`),调用方只负责开账和读账。

    账是**按段**开的(`collector.heeded` 一步一段,`login.check` 整次检查一段),套着开的话
    内层那段会把外层挡住 —— 本意如此:谁开的账谁收。
    """
    ledger = []
    token = _HEARD.set(ledger)
    try:
        yield ledger
    finally:
        _HEARD.reset(token)


def record(noise, origin=YOUTUBE):
    """在共同完成边界记一笔(只许 `media.run` 的 `finally` 调)。**上游要跟着记** ——
    本机工具那句 `permission denied` 进了 YouTube 那套判据就成了「读不出 cookie」。"""
    ledger = _HEARD.get()
    if ledger is not None and noise:
        ledger.append({'origin': origin, 'noise': noise})


def witnessed(ledger, origin=YOUTUBE):
    """这本账里**某一个上游**说过的那几句原话,按发生顺序。"""
    return [item['noise'] for item in (ledger or []) if item['origin'] == origin]


@contextlib.contextmanager
def on_disk(what):
    """本机文件操作:失败一律标 `LOCAL`。

    裸着抛的话 `login.coded` 按 YouTube 算,而 `permission denied`、
    `no such file or directory` 两个串都在 `login._COOKIE` 里 —— 一次建目录失败就成了
    「读不出浏览器的 cookie」,面板让人去钥匙串放行,而问题在磁盘上。
    """
    try:
        yield
    except OSError as error:
        raise ToolFailure(f'{what}：{error}', '', '', LOCAL) from None
