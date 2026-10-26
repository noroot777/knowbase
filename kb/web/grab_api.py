# -*- coding: utf-8 -*-
"""手动收录的端点:一条链接 → 抓取 → 入库 → 写进知识库。

抓取和入库的全部口径在 `kb.feeds.grab`(**硬约束在
`docs/decisions/link-grab.md` 第一节**),这一层只做三件事:把 `GrabError`
翻成 400、**按真实进度**回答这条链接走到哪一步了、以及**尽力**把后半条链
(导出 → 索引 → 提取)跑起来。

**「库里有这一行」和「知识库里搜得到」是两件事。** 重复投递时要分开答:
走完整条链的那一条就是幂等的一句「已经在知识库里了」;只暂存下来的那一条
(占不到管道、服务在保存之后重启、导出或索引自己失败)**要把后半条链再跑
一次**,并且如实说「还没写进知识库」—— 把它说成已完成,用户搜不到,而再投
一次只会再得到同一句话,后半条链永远没人补。

**占不到管道不是失败。** 内容已经在 feeds.db 里了,写进 vault 这一步由任何
一轮 `inbox` 的 export 兜住(面板上那颗按钮、下一次收录、定时同步都算),所以
那一档照旧回 200,只是如实说清「还没写进知识库」。反过来谎报成功的代价是用户
以为检索里搜得到,而 vault 里还没有这一篇。
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from kb import pipeline
from kb.feeds import grab
from kb.feeds.registry import KINDS
from kb.source_types import SOURCE_TYPES, permits_origin_body

router = APIRouter(prefix='/api/grab')


class RestoreRequest(BaseModel):
    """「只把欠的那一步补上」——`POST /api/grab/restore` 的请求体。

    **恢复故意是独立端点,不是 `/api/grab` 上的一个布尔字段。** 那种写法在
    **跨版本**那一刻不成立:Pydantic 默认忽略不认识的键,所以更新了文件而没有重启
    的那个 kb-web 收到 `restore_only=true` 会**静默当没看见**,继续走它那条
    「不在就抓一篇新的」—— 实测形态:等待期间历史清理把那一行删掉之后,旧端点实发
    1 次对外取页,把一条清理掉的 `rss` 文章**重建成 `inbox`**、标题换掉,而命令行
    照旧退出 0(第 3 轮审查,拿 `git show 5b70036:kb/web/grab_api.py` 的真模块当
    那个没重启的进程)。

    换成独立路径之后,**旧服务的回答是 404** —— 客户端据此知道「这台服务还不会做
    这件事」,停下来说一句,而不是被降级成一个会创建内容的动作。这就是「不认识这个
    操作的服务必须拒绝,不许默默当成别的操作」那条协议边界。

    **多余的键一律拒掉,不许被忽略**(`extra='forbid'`):默认那套「静默丢掉不认识
    的键」正是上面那个故障的机制 —— 哪天又有人想拿一个字段表达「只恢复」,他会当场
    被拒,而不是拿到一个会创建内容的 200。两个请求体都这么定(面板只传
    `url` / `refetch`,`kb/static/js/grab.js` 那一处)。

    **拒的那个码是 422,不是 400**:`extra='forbid'` 走的是 FastAPI 的请求体校验
    (`RequestValidationError`),和下面那几处手写的 `HTTPException(400, …)` 不是
    一条路。两边的消费方都按整个 4xx 分支处理(命令行 `ApiRefused`、面板
    `apiFail`),所以**这里不为了对齐注释去改校验的状态码** —— 那会把项目里
    「校验错 = 422」这条约定改掉。回归在 `tests/test_grab_endpoint.py`。
    """

    model_config = ConfigDict(extra='forbid')

    url: str = Field(min_length=1, max_length=2048)


class GrabRequest(RestoreRequest):
    #: **用户明确要求「重新抓一次」。** 它不看库里那一份过不过得了质量那一道:
    #: 那道判据只认得出**我们认得出的**坏内容(通篇复读那一类),而「五段各不相同
    #: 的会员墙文案」照样过关、从此 content_ok 为真 —— 要是恢复出口也架在同一道
    #: 判据上,这种错存就永远换不回来(第 3 轮审查实测:原站恢复可读之后重投仍回
    #: 「已经在知识库里了」,HTTP 发出 0 次)。所以人说「再抓一次」就重抓。
    refetch: bool = False


def _resume_hint(channel: str) -> str:
    """「稍后点一次」那半句话里的**按钮名,按归属来源给对的那一颗**。

    来源页工具栏那颗按钮在可轮询的来源里写「同步全部」,只有手动收录写「写入
    知识库」(判据就是 `pollable`,和 `sources.js` 的 `manual` 分支同一个)。
    一条推文被「大V观点」收过、这一轮又没占到管道时,照手动收录那套说就是指着
    一颗那一页上不存在的按钮。
    """
    spec = SOURCE_TYPES.get(channel)
    button = '写入知识库' if spec and not spec.pollable else '同步全部'
    return f'，稍后在「{KINDS.get(channel, channel)}」里点一次「{button}」'


def _write_through(out: dict, note: str, channel: str = grab.KIND) -> dict:
    """把后半条链跑起来,并把「写没写进知识库」如实写进这一份回答。

    **这一步只有这一份口径**:新收下的、以及「收过但没走完」的那两档都过它 ——
    两处各写一遍的话,其中一处迟早会变成「不启动管道、只说一句已完成」。

    **`channel` 要指向真正拥有那一行的来源,不许一律写 `inbox`。** export 阶段
    是按通道筛的(`pipeline_stages._per_channel`):一条推文已经被「大V观点」收过
    但还没导出时,起一轮 `inbox` 的 export 一个字都不会动它 —— 而回答里却写着
    「正在写入知识库」,那就是一句谎报。

    **起跑成功要把那一轮的身份原样交给前端**(`run`)。`start_pipeline` 返回 id
    而不是 True 正是为了让调用方**原子地**拿到「我起的是哪一轮」;前端拿它核对
    那一轮 export/index 的实际结果。没有它,面板只能按「run_id 变了」猜 ——
    别人的一轮同步插在中间、或者我们那一轮红了,都会被写成「已写入知识库」
    (2026-10-16 第二轮审查)。
    """
    why: list[str] = []
    started = pipeline.start_pipeline(['export', 'index', 'extract'], refused=why,
                                      crawl_filter={'channel': channel})
    out['pipeline'] = 'started' if started else 'busy'
    out['run'] = started
    # **恢复动作的身份要一路带到前端**,不只用在「占不到管道」那一句上:那一轮
    # 起跑之后 export/index 失败、或者这一篇最后仍没进知识库时,界面同样得说清
    # 「去哪一页点哪颗按钮」—— 写死成手动收录那一套,就是指着一颗那一页上不存在
    # 的按钮(一条推文的归属是「大V观点」)。
    out['resume'] = _resume_hint(channel)
    out['detail'] = (f'{note}，正在写入知识库' if started else
                     f'{note}；' + (why[0] if why else '其他同步正在运行') +
                     out['resume'])
    return out


def _recover(out: dict, url: str, seen: dict, label: str, asked: bool = False) -> dict:
    """受控重抓一次,确认得了才替换库里那一份。

    **两个入口,判据各自独立**:① 库里那一份看不出是正文(`content_ok` 为假);
    ② **用户明确要求重抓**(`asked`,`GrabRequest.refetch`)。②**不许**以①为前提
    —— ①那道判据只认得出我们认得出的坏内容,而「结构上和文章不可分的纯文字提示
    页」过得了关、从此 `content_ok` 为真,恢复出口要是也架在它上面,这种错存就
    **一辈子换不回来**(第 3 轮审查实测:原站恢复可读之后重投仍回「已经在知识库
    里了」、HTTP 发出 0 次;历史那些 `content_kind='post'` 的 X 残片同理)。
    **「已导出、已索引」不是内容质量证明**,所以两个入口都不看 `done`:走完整条
    链的坏内容更该换。

    **这是查重那道早检查里唯一会发 HTTP 的一档**,而且它必须发:`put_items` 是
    `DO NOTHING`,再投一次同一条链接一个字节都不会动那一行。

    三种结果各有一句话,而且都如实交出 `replaced`:重抓撞上关卡 / 还是确认不了
    (`grab.grab` 抛)→ 原样留着、不起管道;换成了 → 起后半条链(`content_hash`
    变了,导出闸门自然成立);`replace` 回 False → 这期间别人已经动过它,或者重抓
    到的就是同一份。**失败那两档不是终态**:界面据 `replaced` 为假说「没换成」,
    而不是说「已完成」—— 再来一次照旧会重抓。
    """
    if not seen.get('url_is_origin'):
        # **这一行的 `url` 指向的不是它自己**(`permits_origin_body`),所以这一档
        # **连 HTTP 都不发**:简报那条简讯的 `url` 指向的是它引用的那篇别人家的文章,
        # 换一次就把简报自己写的那两句话毁掉,而简报不进补抓队列、没有任何一轮同步
        # 会来补。`replace` 自己的 WHERE 也挡着(`origin_body_sql`),但只靠那一道的
        # 话人得先白等一次抓取、拿到的还是「和它一样,或者这期间有人换过」那句答不出
        # 原因的话。界面上那颗按钮据 `url_is_origin` 本就不露面,这一道是给直接打接口
        # 的那一侧。
        #
        # **问的是 `url` 的归属,不是「这一类进不进补抓队列」**(`permits_link_body`)
        # —— 一度用了后者,于是推文被连带拒掉:它的 `url` 就是它本人的永久链接、重抓
        # 走 X 的公开接口取回它自己的正文,而那是那些空正文/短残片唯一的人工出路。
        out['replaced'] = False
        out['detail'] = (f'这条之前被「{label}」收过了；它的正文是「{label}」自己写的'
                         '那段内容，而这个链接指向的是它引用的那一篇 —— '
                         '重抓一次换回来的就不是它了，要读那一篇请点原文链接')
        return out
    why = '你要求重新抓一次' if out.get('content_ok') else '库里那一份看不出是正文'
    try:
        fresh = grab.grab(url)
    except grab.GrabError as e:
        out['replaced'] = False
        out['detail'] = (f'这条之前被「{label}」收过了，{why}；'
                         f'这次重抓还是不行:{e}')
        return out
    if not grab.replace(seen, fresh):
        out['replaced'] = False
        out['detail'] = (f'这条之前被「{label}」收过了，{why}；'
                         '这次重抓到的内容和它一样，或者这期间已经有人换过了')
        return out
    out.update(replaced=True, done=False, title=fresh['title'])
    return _write_through(out, f'{why}，已经按这次重抓到的内容换掉', seen['kind'])


def _duplicate(url: str, seen: dict, refetch: bool = False,
               restore_only: bool = False) -> dict:
    """这条地址库里已经有了。**按它走到哪一步分开答。**

    `restore_only` 把这一档收窄成**纯恢复**:只把欠的那一步补上,**不走重抓**
    (那要发 HTTP、要替换正文,是人明确要求时才做的事),内容本身不对时也**不补**
    —— 后面几步全做完只是把一份看不出是正文的东西写进知识库。
    """
    label = KINDS.get(seen['kind'], seen['kind'])
    out = {'ok': True, 'added': 0, 'duplicate': True, 'url': url,
           'title': seen['title'], 'kind': seen['kind'], 'done': bool(seen['done']),
           'content_ok': bool(seen.get('content_ok')),
           # 界面据它决定「重新抓一次」露不露面:亮一颗必定被拒的按钮,和后端静默
           # 覆盖是同一处口径分家的两半(第 3 条审查形态)。
           'url_is_origin': bool(seen.get('url_is_origin'))}
    if restore_only and not out['content_ok']:
        # 恢复不替任何人做「换掉它」这个决定(出路是 `refetch`,见 `_recover`)。
        out['owing'] = grab.owing(seen)
        out['detail'] = f'这条之前被「{label}」收过了，{out["owing"]}'
        return out
    if (refetch or not out['content_ok']) and not restore_only:
        # **内容本身不对的那一档排在最前面**:后面几步全做完也只是把一份看不出
        # 是正文的东西写进知识库(`owing` 同一份口径里它也排第一)。
        # **用户明确要求重抓时同样走这里,不先问 `content_ok`**:那道判据漏放的
        # 错存正是最需要换掉的那几篇(见 `_recover`)。
        out['owing'] = grab.owing(seen)
        return _recover(out, url, seen, label, asked=refetch)
    if seen['done']:
        # 走完整条链了:幂等的一句话,不占管道(这是绝大多数重复投递的末态)。
        out['detail'] = f'这条已经在知识库里了（{label}）'
        return out
    # 暂存下来了、但还搜不到。**不覆盖库里那一份正文**(`DO NOTHING` 的语义),
    # 要补的只是后半条链 —— 而且要在**拥有这一行的那个来源**名下跑。
    #
    # **先把这一条欠的那一步落实成 export 认得的形状,再起那一轮。** 「补跑」不等
    # 于「它会被选中」:vault 里那一篇被删掉而两个 hash 还相等时,export 的待办
    # 判据(`exported_hash<>content_hash`)压根选不中它,两个阶段跑完都 ok,而那
    # 一篇仍然搜不到(2026-10-16 第 3 轮审查实测)。
    out['owing'] = grab.owing(seen)
    out['reclaimed'] = grab.reclaim_export(seen)
    return _write_through(
        out, f'这条之前被「{label}」收过了，{out["owing"] or "还没写进知识库"}',
        seen['kind'])


@router.get('/state')
def grab_state(url: str = Query(..., min_length=1, max_length=2048)):
    """这一条**此刻**在知识库里是什么状态。**只读**:不发 HTTP、不起管道、不写
    一个字节。

    界面那句「已写入知识库」只许由这一句话落地。**整轮 export/index 都 ok 只
    证明那一轮的阶段跑完了,证明不了这一篇的目标版本已经可检索** —— 实测形态:
    vault 里那一篇被删掉而两个 hash 还相等,export 一个字没写、index 正确地把
    旧索引收掉,双 ok,而那一篇搜不到(2026-10-16 第 3 轮审查)。所以收尾要按
    **条目身份**核对,拿阶段结果当旁证。
    """
    try:
        norm = grab.normalize(url)
    except grab.GrabError as e:
        raise HTTPException(400, str(e)) from None
    seen = grab.known(norm)
    out = {'ok': True, 'url': norm, 'known': bool(seen),
           'done': bool(seen and seen['done']), 'owing': grab.owing(seen or {}),
           'content_ok': bool(seen and seen['content_ok']),
           # 没收过的那一档按手动收录算(存进来就会是它),按钮本来也被 `known`
           # 那一道挡着。
           'url_is_origin': (bool(seen['url_is_origin']) if seen
                             else permits_origin_body(grab.KIND))}
    if seen:
        out.update(kind=seen['kind'], title=seen['title'],
                   exported=seen['exported'], stored=seen['stored'],
                   indexed=seen['indexed'], resume=_resume_hint(seen['kind']))
    return out


@router.post('/restore')
def grab_restore(body: RestoreRequest):
    """**只恢复**库里已经有的那一行:把欠的后半条链补上。

    不抓网页、不新建条目、不替换正文 —— 判断和 `/api/grab` 的重复那一档共用
    `_duplicate`(口径只有一份),收窄的部分由 `restore_only` 那个内部参数表达。

    **那一行不在了就 409 到此为止。** 调用方(命令行的自动恢复)手里只有一张旧
    快照,「刚才查到它还在」不是写入那一刻它还在的保证;历史清理随时会在这两步
    之间把它删掉。409 而不是 400:这不是「这条地址不对」,是「这一刻没有可恢复的
    条目」,调用方据此去问一次事实(只读那一问会答 `known=false`)。
    """
    try:
        url = grab.normalize(body.url)
    except grab.GrabError as e:
        raise HTTPException(400, str(e)) from None
    if not (seen := grab.known(url)):
        raise HTTPException(409, '这一条已经不在库里了，没有可恢复的条目')
    return _duplicate(url, seen, restore_only=True)


@router.post('')
def grab_link(body: GrabRequest):
    try:
        url = grab.normalize(body.url)
        # **查重排在抓取前面**:已经收过的那一条连 HTTP 都不必发。它只是一句
        # 提示 —— 真正防重复的是 `put_items` 的 `DO NOTHING`(见 `grab.known`)。
        if seen := grab.known(url):
            return _duplicate(url, seen, refetch=body.refetch)
        entry = grab.grab(url)
        saved = grab.save(entry)
    except grab.GrabError as e:
        raise HTTPException(400, str(e)) from None
    if not saved['added']:
        # 同一条链接在**这一类**里撞上了(两条投递同时落地):`DO NOTHING` 挡住
        # 了后一个,库里那一份一个字节都没动。这正是想要的末态,不是故障(元规则
        # 「0 行不是失败」)—— 但「它写进知识库没有」仍然要现查一次再答。
        # **这份兜底字典要按「存进来会是哪一类」把字段填齐**:缺一个键,
        # `_duplicate` 那边 `bool(seen.get(...))` 就填成 False,**正好抵消前端
        # 「字段读不到时按钮照旧亮」那道兜底** —— 收走的是唯一的恢复出路,给出的
        # 理由还是一句对手动收录并不成立的话。所以写的是判据函数,不是字面量。
        return _duplicate(url, grab.known(url) or
                          {'title': entry['title'], 'kind': grab.KIND,
                           'path': '', 'done': False,
                           'url_is_origin': permits_origin_body(grab.KIND)})
    # 已经入库的事实先定下来,再谈写不写得进知识库这一轮。
    # `content_ok` 恒真:它的含义就是「这份内容过完了准入那八道」,而新收的这一条
    # 刚过完(`grab_page.verdict` 的 ⑧)。两端字段对齐,界面不必按「有没有这个键」猜。
    out = {'ok': True, 'added': saved['added'], 'duplicate': False, 'done': False,
           'content_ok': True,
           'url': url, 'title': entry['title'], 'kind': grab.KIND,
           'source_id': saved['source']['id'], 'source_name': saved['source']['name'],
           'published': entry['published'], 'chars': len(entry['body'])}
    # **这一轮没有 crawl**:手动收录没有可轮询的源,内容刚才已经抓好入库了
    # (`inbox` 的 `pollable=False`,见 `sync.collect` 开头那一道)。要跑的是
    # 后半条链:export 把它写成 vault 里那一篇、index 让检索找得到、extract
    # 买一次打标(收件桶默认开着提取,见 `grab.bucket`)。
    return _write_through(out, '已收下')
