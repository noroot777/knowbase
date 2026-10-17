# -*- coding: utf-8 -*-
"""手动收录的**非网页入口**:在终端(或任何能跑命令的地方)把链接收进知识库。

入口是 `./kb.sh grab`,skill `kb-grab` 也打这一条。

**这一层不做任何关于收录的判断。** 抓取、查重、重抓替换、起后半条链那一整套
口径仍然只有 `kb/web/grab_api.py` 那一份(硬约束在
`docs/decisions/link-grab.md` 第一节)。这里只做三件事:把参数变成那两个接口的
请求、**按真实事实收敛那句「写进知识库了没有」**、把结果说成终端上的一行话
(或一行 JSON)。

**为什么不进程内 import `kb.feeds.grab` 自己抓:**
① 查重那几档各自说什么、重抓怎么替换、起哪一轮,全在 grab_api 里 —— 进程内
   再写一遍就是「同一个问题两处各判一遍」(元规则「口径只有一份」),而两处里
   迟早有一处把「暂存下来了」说成「已经在知识库里了」。
② **管道跑在 kb-web 进程内**:进程外抓完只能把内容留在 feeds.db 里,用户检索
   不到,而他以为收进去了。所以这条路和面板上那颗按钮**打同一个接口**,服务
   没起就先起服务(`kb.sh` 的 `cmd_grab` 里那道 `web_start`,口径同
   `./kb.sh sync`)。

**「已存下」和「搜得到」是两件事,终端上也要分开说**(同 grab_api 的模块说明):
`/api/grab` 回的是「那一轮起跑了」,不是「这一篇进去了」。所以收尾去问
`/api/grab/state`(只读)、按**这一篇自己**的事实落终态;整轮 export/index 双 ok
只是旁证 —— vault 里那一篇被删掉而两个 hash 还相等时双 ok,而那一篇搜不到。
这条收敛和界面那一侧(`kb/static/js/grab.js` 的 `grabCheckItem`)是同一条口径。

**回包没送达 ≠ 后端没存下来。** 保存排在起管道之前,断线可能发生在它之后 ——
说「这条没存下来」是在断言一件我们不知道的事。所以那一档去问一次只读的
`/api/grab/state`,问不到就如实说「结果不确定,再投一次是安全的」(重投按 URL
去重,`put_items` 的 `DO NOTHING` 挡着,内容不会变成两份)。

纯标准库:这条路不该因为 venv 里缺什么而用不了(`kb.sh` 用 `$BOOT_PY` 跑它)。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

#: 默认服务地址。`kb.sh` 总会显式传 `--api`(它知道 `KB_WEB_PORT`);这个默认值
#: 是给「手里只有这个脚本」的调用方的。
API_DEFAULT = 'http://127.0.0.1:8787'
#: 一次收录请求等多久。服务端自己的墙钟上限是 40 秒(`grab.GRAB_SECONDS`),
#: 这里留出起管道和回包的余量 —— 比它短的话会把一次**正在成功**的抓取说成
#: 超时,而内容其实已经存下了。
GRAB_HTTP_SECONDS = 70
#: 只读那两个接口给得紧:它们一读内存/一读库就有答案。
READ_HTTP_SECONDS = 15
#: 等「写进知识库」那一步的默认上限。done 只要 export+index(不等 extract 调
#: 模型),单条一般几秒。
SETTLE_SECONDS = 120
POLL_SECONDS = 1.0
#: 最多替用户起几轮补链。**有上限**:某一条一直导不出去时,无限起管道只会把
#: 这件事变成一个不停烧钱的循环(那一轮带 extract)。
MAX_KICKS = 2

_URL_RE = re.compile(r'https?://[^\s<>"\'）)】」]+')
#: 终态词 → 终端上那个前缀。**每一档都有自己的话**,不许几档共用一句
#: (「已存下」和「已写入知识库」混成一句就是谎报,用户搜不到)。
_LABELS = {
    'saved': '已写入知识库',
    'duplicate': '早就在知识库里了',
    'pending': '已存下，还没写进知识库',
    'bad_content': '收下的那一份看不出是正文',
    'failed': '没收下',
    'unknown': '结果不确定',
    'absent': '还没收进来',
}


class ApiDown(RuntimeError):
    """**打不通 kb-web**(连不上、超时、或者它自己 5xx 了)。

    **和「这条没收下」严格分开**:它说的是通道,不是这条链接 —— 保存排在起管道
    之前,所以这一档里内容**可能已经存下了**。谎称「没收下」会让用户去别处找原因
    (口径同 `grab.js` 那段 catch:回包没送达 ≠ 后端没存下来)。
    """


class ApiRefused(RuntimeError):
    """服务**明确拒绝**了这一条(4xx)。消息就是「为什么没收下 + 下一步」。

    只有这一档说得出「没收下」:后端那六道准入是在写库之前过的,拒了就一个字节
    都没写。5xx 不算 —— 那时写没写进去我们不知道(见 `ApiDown`)。
    """


#: 本机请求**一律不走代理**。环境里设了 `*_proxy` 时(这台机器上常设),urllib 会
#: 把 `127.0.0.1:8787` 也交给代理,拿回一个跟知识库毫无关系的 502 —— 那会被说成
#: 「这条没收下」,而服务其实好好跑着(实测:`--api http://127.0.0.1:9` 回 HTTP 502)。
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _detail_of(raw: bytes) -> str:
    """错误回包里那句面向用户的话。

    **不许用 `json.loads(...)['detail']` 一步到底**:未捕获异常时 Starlette 回的
    是纯文本 500,那一步会抛,真因被吞成一句无信息的「失败」(口径同
    `frontend-feedback.md` 里 `apiFail` 那条)。
    """
    text = (raw or b'').decode('utf-8', 'replace').strip()
    try:
        data = json.loads(text)
    except ValueError:
        return text[:500]
    if isinstance(data, dict) and isinstance(data.get('detail'), str):
        return data['detail']
    return text[:500]


def _request(api: str, path: str, *, payload: dict | None = None,
             timeout: float = READ_HTTP_SECONDS) -> dict:
    """打一次接口。返回解析好的 JSON;400/409 那类带着服务端那句话抛 RuntimeError。"""
    req = urllib.request.Request(api.rstrip('/') + path, method='GET')
    if payload is not None:
        req = urllib.request.Request(
            api.rstrip('/') + path, method='POST',
            data=json.dumps(payload).encode(),
            headers={'Content-Type': 'application/json'})
    try:
        with _OPENER.open(req, timeout=timeout) as resp:
            body = resp.read()
    except urllib.error.HTTPError as e:
        why = _detail_of(e.read()) or f'HTTP {e.code}'
        # **5xx 不是「这条没收下」**:服务端在保存之后的任何一步抛出,这边看到的
        # 都是 500,而那一行可能已经在库里了。归到通道那一档,由调用方去问事实。
        raise (ApiDown if e.code >= 500 else ApiRefused)(why) from None
    except (urllib.error.URLError, OSError, TimeoutError) as e:
        raise ApiDown(str(getattr(e, 'reason', None) or e)) from None
    try:
        return json.loads(body.decode('utf-8', 'replace'))
    except ValueError:
        # 回的不是 JSON = 服务那边不正常(反代插了一页、进程半死),同 5xx 一档。
        raise ApiDown('回的不是 JSON:' + body[:200].decode('utf-8', 'replace')) from None


def post_grab(api: str, url: str, refetch: bool) -> dict:
    return _request(api, '/api/grab', payload={'url': url, 'refetch': bool(refetch)},
                    timeout=GRAB_HTTP_SECONDS)


def get_state(api: str, url: str) -> dict:
    return _request(api, '/api/grab/state?url=' + urllib.parse.quote(url, safe=''))


def pipeline_idle(api: str) -> bool | None:
    """管道此刻空着吗。`None` = 问不出来(那时**不许**当成空着去起一轮)。

    `source=false` 的理由同 `kb.sh` 的停机保护:来源探测冷缓存时几十秒,而
    「在不在跑」本来一读内存就有。
    """
    try:
        return not _request(api, '/api/pipeline/status?source=false').get('running')
    except RuntimeError:
        return None


def kick_inbox(api: str) -> bool:
    """替用户起一轮**只补后半条链**的同步(手动收录这一类)。

    批量投递时第二条起必然撞上第一条起的那一轮(后端如实回 `busy`),它们的内容
    已经在 feeds.db 里、却没人替它们跑 export —— 所以收尾时补一轮。
    **按 channel 筛**:起全量会把别的来源一起抓一遍,那不是用户要的。
    """
    try:
        _request(api, '/api/pipeline/run',
                 payload={'stages': ['export', 'index', 'extract'], 'channel': 'inbox'})
        return True
    except RuntimeError:
        return False      # 这期间别人占住了管道:不是失败,下一轮轮询再看


# ------------------------------------------------------------------ 这一条怎么说

def links_in(text: str) -> list[str]:
    """一段文字里的链接,按出现顺序去重。

    为了 `pbpaste | ./kb.sh grab -` 这种用法:剪贴板里常常是「看这篇 https://…
    挺好」。**提不出来就原样交给服务端** —— 地址合不合法只有
    `grab.normalize` 那一份口径,在这里再判一遍就是两处各判一遍。
    """
    out: list[str] = []
    for hit in _URL_RE.findall(text or ''):
        hit = hit.rstrip('.,;:!?、，。；：')
        if hit not in out:
            out.append(hit)
    return out


def verdict(res: dict, state: dict | None) -> tuple[str, str]:
    """这一条此刻的终态词 + 那句话。**纯函数**,所有输出都过它。

    四档和界面那一侧(`grab.js` 的 `grabCheckItem`)一一对应:
    - 内容本身不对(`content_ok` 为假 / 这次重抓没换成)排**最前面** ——
      后面几步全做完也只是把一份看不出是正文的东西写进知识库(同 `owing`);
    - `done` → 真写进去了(按**这一篇自己**的事实,不是按那一轮的阶段结果);
    - 库里有这一行、但还没走完 → 如实说欠哪一步、下一步点哪儿;
    - 库里压根没有这一行 → 这才说得出「没存下来」。
    """
    known = bool(state.get('known')) if state else None
    if res.get('replaced') is False:
        # 重抓没换成(撞上关卡 / 还是确认不了 / 这期间别人动过它):不是终态,
        # 再来一次照旧会重抓。
        return 'bad_content', res.get('detail') or '这次重抓没换成'
    if state and known and not state.get('content_ok'):
        return 'bad_content', '加 --refetch 会重抓一次，确认得了就把它换掉'
    if res.get('content_ok') is False and not state:
        return 'bad_content', res.get('detail') or '库里那一份看不出是正文'
    if state and state.get('done'):
        label = '已经按这次重抓到的内容换掉了；' if res.get('replaced') else ''
        return 'saved', label + '全文检索里搜得到了'
    if state is None:
        # 没能问到这一篇的事实(只读接口也不通)。**不许替它下结论**。
        return 'unknown', res.get('detail') or '没核到这一篇的结果'
    if not known:
        return 'failed', '库里没有这一行，这条没存下来；再投一次'
    owes = state.get('owing') or '还没写进知识库'
    return 'pending', owes + (state.get('resume') or '').replace('，', '；', 1)


def _settled(status: str) -> bool:
    """这一档还用等吗。等的只有「存下了、那一轮还没把它写进去」这一种。"""
    return status in ('saved', 'duplicate', 'bad_content', 'failed')


def settle(api: str, items: list[dict], *, deadline: float) -> None:
    """轮询到每条都有终态,或者等满。**原地改 items**。

    **管道空着而还有没写进去的 → 替用户起一轮**(`kick_inbox`):批量投递时第二条
    起必然撞上第一条那一轮,它们的内容在库里却没人跑 export。起跑有次数上限。

    超时不谎报:最后一次问到的事实就是那句话(`pending` 自带「欠哪一步 + 去哪儿
    点一次」)。
    """
    kicks = 0
    while True:
        waiting = [it for it in items if not _settled(it['status'])]
        if not waiting:
            return
        for it in waiting:
            try:
                it['state'] = get_state(api, it['url'])
            except RuntimeError:
                it['state'] = it.get('state')     # 留着上一次问到的事实
            it['status'], it['detail'] = verdict(it['res'], it.get('state'))
        waiting = [it for it in items if not _settled(it['status'])]
        if not waiting or time.monotonic() >= deadline:
            return
        if kicks < MAX_KICKS and pipeline_idle(api) is True:
            kicks += 1 if kick_inbox(api) else 0
        time.sleep(min(POLL_SECONDS, max(0.0, deadline - time.monotonic())))


def collect(api: str, url: str, *, refetch: bool) -> dict:
    """投一条。回包落地即为这一条的初始档位(还要不要等由 `settle` 决定)。"""
    it = {'url': url, 'res': {}, 'state': None, 'status': 'failed', 'detail': ''}
    try:
        it['res'] = post_grab(api, url, refetch)
    except ApiDown as down:
        # **回包没送达 ≠ 后端没存下来**(保存排在起管道之前,断线可能发生在它
        # 之后)。去问一次只读那一问;问得到就照事实说,问不到才把通道那句话抛
        # 上去 —— 说「这条没存下来」是在断言一件我们不知道的事。
        try:
            it['state'] = get_state(api, url)
        except RuntimeError:
            raise ApiDown(str(down)) from None
        it['status'], it['detail'] = verdict({}, it['state'])
        if it['status'] == 'pending':
            it['detail'] = '连接断在半路：' + it['detail']
        return it
    except ApiRefused as e:
        # 4xx 那一句就是「为什么没收下 + 下一步」,原样交给用户。
        it['detail'] = str(e)
        return it
    res = it['res']
    it['title'] = res.get('title') or ''
    # **顺序就是判据的优先级,不许按 `content_ok` 先判。** 重抓**换成了**那一档
    # (`_recover` 的成功分支)只更新 `replaced` / `done` / `title`,`content_ok`
    # 仍是**库里旧那一份**的判断 —— 先看它就会把一次成功的替换说成「看不出是
    # 正文」,而且不去等它写进知识库(用户那一次重抓于是永远停在「没换成」)。
    if res.get('replaced') is False:
        it['status'], it['detail'] = verdict(res, None)      # 明确没换成
    elif res.get('pipeline'):
        # started / busy:两档都要收敛(busy 那一档靠 `settle` 补一轮)。
        # 重抓换成了的那一档也在这里 —— 它起的是同一条后半链。
        it['status'], it['detail'] = 'writing', res.get('detail') or ''
    elif res.get('content_ok') is False:
        # 内容不对、而且没起任何一轮:这一档要人再动一次手。
        it['status'], it['detail'] = verdict(res, None)
    else:
        # 没有 pipeline 这个字段 = 它早就走完整条链了(grab_api 的幂等那一档)。
        it['status'], it['detail'] = 'duplicate', res.get('detail') or ''
    return it


def recheck(api: str, url: str) -> dict:
    """`--state`:**只读**问一句这一条此刻在知识库里是什么状态。

    不发抓取、不起管道、不写一个字节 —— 「这篇我存过吗」是随时问得出的事实。
    """
    it = {'url': url, 'res': {}, 'state': None, 'status': 'unknown', 'detail': ''}
    try:
        it['state'] = state = get_state(api, url)
    except ApiRefused as e:
        # 4xx:多半是「这一串不是一条链接」(`grab.normalize` 那句话)。原样转述,
        # 而不是抛一个 traceback 给人看 —— 一批地址里混进一行脏数据是常事。
        it['status'], it['detail'] = 'failed', str(e)
        return it
    it['url'] = state.get('url') or url
    it['title'] = state.get('title') or ''
    if not state.get('known'):
        it['status'], it['detail'] = 'absent', '还没收进来'
    elif not state.get('content_ok'):
        it['status'], it['detail'] = 'bad_content', '加 --refetch 会重抓一次'
    elif state.get('done'):
        it['status'], it['detail'] = 'saved', '全文检索里搜得到了'
    else:
        it['status'] = 'pending'
        it['detail'] = (state.get('owing') or '还没写进知识库') + (state.get('resume') or '')
    return it


# ---------------------------------------------------------------------- 说出来

def line_of(it: dict) -> str:
    """一条的人读形态:状态词 + 标题,下一行地址,再一行那句话。

    **状态词按 `_LABELS` 逐档取**,不许几档共用一句:「已存下」和「已写入知识
    库」混成一句话,用户就会去检索里找一篇还没写进去的文章。
    """
    label = _LABELS.get(it['status'], it['status'])
    head = f"{label}  {it.get('title') or ''}".rstrip()
    rows = [head, f"  {it['url']}"]
    if it.get('detail'):
        rows.append(f"  {it['detail']}")
    return '\n'.join(rows)


def record_of(it: dict) -> dict:
    """一条的机读形态(JSONL 一行)。skill `kb-grab` 读的就是这个。

    **`status` 是收敛之后的终态**,另把后端那几个事实原样透出 —— 调用方要能
    自己复核,而不是只拿到我们归纳的一个词。
    """
    res, state = it.get('res') or {}, it.get('state') or {}
    return {'url': it['url'], 'status': it['status'], 'title': it.get('title') or '',
            'detail': it.get('detail') or '', 'label': _LABELS.get(it['status'], it['status']),
            'added': res.get('added', 0), 'duplicate': bool(res.get('duplicate')),
            'replaced': res.get('replaced'), 'kind': state.get('kind') or res.get('kind') or '',
            'done': bool(state.get('done')),
            'content_ok': state.get('content_ok', res.get('content_ok')),
            'owing': state.get('owing', ''), 'exported': state.get('exported'),
            'stored': state.get('stored'), 'indexed': state.get('indexed')}


def _targets(args: argparse.Namespace) -> list[str]:
    """命令行上/标准输入里要收的那几条。`-` = 从标准输入读(一行一条或一段文字)。"""
    out: list[str] = []
    for raw in args.url:
        chunks = [sys.stdin.read()] if raw == '-' else [raw]
        for chunk in chunks:
            found = links_in(chunk)
            # 提不出链接就原样交给服务端:那句「这不是一条链接」只有
            # `grab.normalize` 说得准(元规则「口径只有一份」)。
            for one in (found or ([chunk.strip()] if chunk.strip() else [])):
                if one not in out:
                    out.append(one)
    return out


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog='kb.sh grab', description='手动收录:把链接抓进知识库(网页端那颗按钮的命令行版)')
    p.add_argument('url', nargs='+', metavar='链接',
                   help="要收录的链接,可给多条;`-` = 从标准输入读")
    p.add_argument('--api', default=API_DEFAULT, help=f'kb-web 地址(默认 {API_DEFAULT})')
    p.add_argument('--refetch', action='store_true',
                   help='库里已经有这一条时重抓一次,确认得了就把它换掉')
    p.add_argument('--state', action='store_true',
                   help='只查状态:不抓取、不起管道、不写一个字节')
    p.add_argument('--json', action='store_true', help='一行一条 JSON(给程序读)')
    p.add_argument('--no-wait', action='store_true',
                   help='不等「写进知识库」那一步,投完就返回')
    p.add_argument('--timeout', type=float, default=SETTLE_SECONDS,
                   help=f'等「写进知识库」最多多少秒(默认 {SETTLE_SECONDS:.0f})')
    args = p.parse_args(argv)

    urls = _targets(args)
    if not urls:
        print('没给出要收录的链接', file=sys.stderr)
        return 2
    items: list[dict] = []
    try:
        for url in urls:
            items.append(recheck(args.api, url) if args.state
                         else collect(args.api, url, refetch=args.refetch))
        if not args.state and not args.no_wait:
            settle(args.api, items, deadline=time.monotonic() + max(0.0, args.timeout))
    except ApiDown as e:
        # **通道不通和「这条没收下」是两件事。** 已经投出去的那几条照旧输出
        # (它们可能已经存下了),再把这一句说清楚。
        for it in items:
            if it['status'] == 'writing':
                it['status'], it['detail'] = 'unknown', '投出去了，但后来连不上服务'
        _emit(items, args.json)
        print(f'打不通 kb-web({args.api}):{e}\n'
              '服务没起就 ./kb.sh start;起着的话看一眼 ./kb.sh logs',
              file=sys.stderr)
        return 1
    # `--no-wait` 下 `writing` 就是终态:那一轮起跑了,但我们没等它。
    for it in items:
        if it['status'] == 'writing':
            it['status'] = 'pending'
            it['detail'] = it['detail'] or '正在写入知识库，稍后 --state 查一次'
    _emit(items, args.json)
    bad = [it for it in items if it['status'] in ('failed', 'unknown')]
    return 1 if bad else 0


def _emit(items: list[dict], as_json: bool) -> None:
    for it in items:
        print(json.dumps(record_of(it), ensure_ascii=False) if as_json else line_of(it))
    if not as_json and len(items) > 1:
        tally: dict[str, int] = {}
        for it in items:
            tally[it['status']] = tally.get(it['status'], 0) + 1
        parts = [f'{n} 条{_LABELS.get(s, s)}' for s, n in tally.items()]
        print(f"共 {len(items)} 条:" + '、'.join(parts))


if __name__ == '__main__':
    sys.exit(main())
