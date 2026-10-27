# -*- coding: utf-8 -*-
"""**处理出口:每一颗按钮背后真正会发生什么。**

**硬约束在 `docs/decisions/intake-diagnostics.md` 第一节 —— 改这块之前先读那一份。**

**「立即执行」的射程必须说实话,而且那句话由服务端给。** 把文案留在前端的话,改了
阶段清单没人会想起去改那句话 —— 这里让 `scope_of()` 同时给出「跑哪几段」和「确认框
里那段话」,前端只负责显示。三条真相:
- `pipeline.start_pipeline(stages, crawl_filter=...)` **只有 `export` 这一段认
  `crawl_filter`**(`pipeline_stages._per_channel`);
- `index` 是整个 vault 的增量比对,要拿维护锁,**和来源无关**;
- `extract` 走 `kb.extract.pending_docs(conn)`,**压根没有来源参数**,按全库取数、
  花模型的钱。

所以「只执行这一条」是做不到的事,按钮上也不许这么写。四条路各自的真实射程:

| 这一档 | 真的会跑什么 | 谁来跑 |
| --- | --- | --- |
| 正文抓取(待补 / 耗尽 / 挂起) | `RUN_STAGES` + 这一类来源 —— 你选的那几条排在补抓队首,本轮按队列最多 `fulltext.MAX_ITEMS` 条(**不只你选的那几条**),可能联网并调用翻译模型;索引检查全库 | 本模块 |
| 翻译 | 既有端点的本体 `intake_api.start_translate_retry`,**阶段清单换成 `RUN_STAGES`**:它自己带额度清零、点名、射程指纹 | 既有本体 |
| 来源连接 / 覆盖未完成 | 既有端点的本体 `intake_api.start_sync`,**阶段清单换成 `SOURCE_STAGES`**(点名这一条订阅,跳过来源退避) | 既有本体 |
| 入库处理 | `RUN_STAGES` + 这一类来源,同第一行 | 本模块 |

**翻译和来源那两档的起跑逻辑刻意不在这里重写** —— 它们已经各有一条跑过审查的路
(含付费边界 `translate_manual`、含 `scope_sig` 那道「确认期间范围变没变」),再写
一遍就是第二份射程口径。**但阶段清单是这个面板自己的**:那两个端点原来的清单里带
着 `extract`,而它按全库取数、没有来源参数 —— 一颗开在某几条内容上的按钮不该顺手
买下全库那一轮模型调用(审查第 1 轮 F04)。所以这里传自己的清单进去,端点原来的
行为一个字不变(`intake_api.TRANSLATE_RETRY_STAGES` / `SYNC_STAGES`)。

**三条路的阶段清单里都不许出现 `extract`**,结构红钉在
`tests/test_intake_diag_scope.py`。
"""
from __future__ import annotations

import hashlib
import json

from kb import pipeline
from kb.feeds import fulltext, registry as reg
from kb.intake_diag import lanes, warrant

#: 这个面板起跑时只跑这两段。**和 `kb.web.browser_capture_api.CONTINUE_STAGES`
#: 同一份理由**(那边的注释是原文):`extract` 不在里面 —— 它的队列压根没有来源
#: 参数、按全库取数,一颗开在某一条内容上的按钮不该顺手买下全库那一轮模型调用。
#: `export` 这一段认 `crawl_filter`(只有这一类来源),`index` 是整个 vault 的增量
#: 比对、不调模型。
#:
#: **两处必须相等**,结构红钉在回归里(`tests/test_intake_diag_scope.py`):各自
#: 演进的话,同一句「条目化提取不在这一步」会在一个页面上成真、在另一个页面上变谎。
RUN_STAGES = ('export', 'index')

#: 来源连接那一档要多跑一段 `crawl`(它要的就是「再去这个来源读一次」),其余和
#: `RUN_STAGES` 相同 —— **同样不含 `extract`**。
SOURCE_STAGES = ('crawl', *RUN_STAGES)

#: 本面板起跑的三条路,一条都不许带 `extract`。这个不变量由 `scope_of` /
#: `kind_scope` 返回的 `stages` 承担,结构红钉在 `tests/test_intake_diag_scope.py`。
FORBIDDEN_STAGES = ('extract',)


def _ticket(route: str, kind: str, stages, *, source_id=None, scope_sig: str = '',
            picked: int = 0, members=()) -> str:
    """**这一份执行计划的凭证。** 确认框弹出时签一张,提交时按现态再签一张比一比。

    不绑凭证的后果实测过(审查第 1 轮 F03):`/scope` 和 `/run` 各按「此刻这几条
    在哪一档」重新选路,于是确认框里写着「条目化提取不在这一步」、而等待期间那一条
    补抓完成、变成待翻译,提交时就改走了另一条含 `extract` 的路 —— 用户同意的是
    一份计划,跑掉的是另一份,**而那一份要花模型的钱**。只给「正文→翻译」这一种
    转换加一个 if 治不了它:反向转换、混选变化、来源在确认期间改指都是同一类。

    **凭证绑的是整份计划**:走哪条路、跑哪几段、动哪个来源、付费射程指纹,以及
    选集里**这几世的这几条**(`life` + `content_rev`,见 `lanes.identities`)。
    **不绑展示数字** —— `detail` 里「此刻队列里 N 条」每一轮同步都在变,把它算进
    指纹等于让人每次都要重看一遍一句没变过的话。

    路由名写在最前面,所以两条路的凭证**不可能互换**:同一个选集、同一份阶段清单,
    走正文那条和走翻译那条签出来的也是两张不同的票。
    """
    payload = json.dumps([route, kind, list(stages), source_id, scope_sig, picked,
                          sorted(members)], ensure_ascii=False)
    return hashlib.sha1(payload.encode()).hexdigest()[:24]


def _members(kind: str, buckets: dict) -> list:
    """选集里**这几世的这几条**,连它们此刻所在的那一档。

    带上档位是因为「换了一档」本身就是计划变了(正文档和翻译档的射程、花的钱都
    不一样);带上 `life` 是因为 `item_key` 按网址算 —— 确认期间被清理删掉又从同一
    个链接收回来的那一条,key 一个字都没变而内容是另一份(见 `lanes.identities`)。
    """
    rows = lanes.identities([key for keys in buckets.values() for key in keys])
    return [(key, lane_id, *rows.get(key, ('', -1)))
            for lane_id, keys in buckets.items() for key in keys]


def _body_scope(kind: str, picked: int) -> dict:
    label = reg.KINDS.get(kind, kind)
    queued = fulltext.pending(kind)
    # `picked == 0` 是页头那颗「立即执行本类」:它压根没有「你点的那几条」,
    # 所以那半句不能照念 —— 念了就是在对一个空选集说「不只是你点的 0 条」。
    reach = (f'范围是整类「{label}」来源' if not picked else
             f'范围是整类「{label}」来源，不只是你点的 {picked} 条')
    return {
        'stages': list(RUN_STAGES),
        'kind': kind,
        'title': '现在执行这一批的后续处理',
        'confirm': '开始执行',
        'picked': picked,
        # **这段话是这颗按钮的射程声明,不许收窄着写。**
        'detail': (
            f'这一轮跑的是「导出 + 索引」两段，{reach}：\n'
            + (f'· 你选的 {picked} 条会先解除挂起 / 把重试额度还回去，并排在'
               '补抓队列的最前面；\n' if picked else '')
            + f'· 正文补抓按这一类来源自己的队列取，本轮最多 {fulltext.MAX_ITEMS} 条'
            f'（此刻队列里 {queued} 条），会联网访问原文站点；\n'
            '· 取到正文的英文内容会调用翻译模型，这一步要花钱；\n'
            '· 索引会检查整个知识库，不只这几条；\n'
            '· 条目化提取不在这一步——要提取请到同步中心的起跑弹窗里勾。\n'
            '结果以同步中心为准。'),
    }


def _translate_scope(kind: str, source_id: int | None, stuck: dict) -> dict:
    """翻译那一轮的射程。**数字和指纹原样取既有端点用的那一份**。

    `total = queued + exhausted` 和那个端点逐字相同(它的注释是原文:还排在队列
    里的 + 已经试满额度离开队列的,加 `exhausted` 而不是 `shipped`)。指纹也是
    `stuck()` 那一个读事务里给出来的那一份 —— 另开一次读就能出现「确认 1 条、
    带着那两条的指纹」。

    **它会拒的,这里就先拒**:确认框不许承诺一轮必然被 400 退回的执行,否则人
    照着那段话点下去,拿到的是一句跟确认框矛盾的拒绝。两句拒绝文案逐字照搬。
    """
    label = reg.KINDS.get(kind, kind)
    total = stuck['queued'] + stuck['exhausted']
    if not total:
        raise ValueError('这些内容此刻都在等正文补抓，补抓完会自动翻译。'
                         if stuck['failed'] else '这个范围里没有等待翻译的内容了。')
    where = '这条订阅' if source_id else f'整类「{label}」来源'
    return {
        'endpoint': f'/api/intake/{kind}/translate-retry',
        # **阶段清单是本面板自己的那一份**,不是那个端点原来的 —— 它原来带着
        # `extract`,而那一段按全库取数、没有来源参数(审查第 1 轮 F04)。
        'stages': list(RUN_STAGES),
        'kind': kind, 'source_id': source_id, 'scope_sig': stuck['sig'],
        'title': '现在重试翻译',
        'confirm': '开始翻译',
        'picked': total,
        'detail': (
            f'这一轮会把{where}里等着译的 {total} 条一起放回队列并开始翻译，'
            '不只是你点的那一条：\n'
            '· 翻译要调用大模型，这一步要花钱；\n'
            '· 译文写进知识库之后会重新导出并重建索引；\n'
            '· 条目化提取不在这一轮——译完之后那几篇的旧标签会被标成待重做，'
            '等下一次常规同步（或者到同步中心勾上「条目化提取」）再跑。\n'
            '确认之后范围里的条数若发生变化，这一轮会整批拒绝并让你重看一次。'),
    }


def _source_fingerprint(problem: dict) -> str:
    """来源那一档的**执行描述指纹**。口径只有 `warrant.source_sig` 一份。

    这里只取 `sources.problems` 在**同一次读里**算出来的那一份,不在这边按
    `problem` 上那几个展示字段重拼一次 —— 重拼就是第二份口径,而执行器那一侧
    必须能用同一份口径**自己重算**出逐字相同的值(它手上只有订阅行和设置,没有
    这份 problem)。

    **取不到就一律当「没有描述」**:空串由 `_ticket` 原样签进票里,而起跑那条路
    (`intake_diag_api.run`)对空描述的来源一律拒绝 —— 漏报是静默丢数据。
    """
    return str(problem.get('sig') or '')


def _source_scope(problem: dict) -> dict:
    cooling = problem['problem'] == 'x_boot'
    coverage = problem['problem'] in ('coverage', 'partial')
    detail = [f'这一轮只点名「{problem["name"]}」这一条订阅，跳过它的退避等待；'
              '随后按常规把这一类来源导出、重建索引（索引检查整个知识库，不只这'
              '一条来源）。条目化提取不在这一轮——这一次新抓到的内容要等下一次'
              '常规同步才生成标签。']
    if cooling:
        detail.append('X 的会话冷却由后端统一等待，不是前端倒计时，也不能靠反复点击绕开：'
                      '45 秒起，反复撞上限流会翻倍到最多 180 秒。等待结束不保证这一次'
                      '就读得到——不需要重新登录。')
    if coverage:
        detail.append(f'保留的同步下界是 {problem["since"] or "（这条订阅还没有水位）"}：'
                      '它会重新打开来源页从头读那之后的范围，按原文链接去重，'
                      '**不是**从上次停下的位置接着滚。同样的预算或加载障碍下，'
                      '这一次仍然可能读不完。')
    return {
        'endpoint': f'/api/intake/{problem["kind"]}/sync',
        # 同 `_translate_scope`:阶段清单是本面板自己那一份,不含 `extract`。
        'stages': list(SOURCE_STAGES),
        'kind': problem['kind'], 'source_id': problem['source_id'],
        'problem_id': problem['id'],
        # 执行描述的指纹进票(`_signed` 按 `scope_sig` 这一键签),理由见
        # `_source_fingerprint`。`rev` 另外原样带出来给屏幕上那句话用。
        'scope_sig': _source_fingerprint(problem),
        'rev': int(problem.get('rev') or 0),
        # **起跑那一下交下去的就是这一份**(不是编号):`intake_api.start_sync`
        # 按它再核一次,并把它随 `crawl_filter` 交到执行器手里 —— 执行器用同一份
        # 口径重算,对不上就作废这一条,不许按编号认领当前版本(审查第 2 轮 R3
        # 只修到入口,第 1 轮自由审查 R2 实测:确认文字点名 A,采集器访问了 B)。
        'expect': {str(problem['source_id']): _source_fingerprint(problem)},
        'title': '冷却后重试这一个账号' if cooling else
                 '重试此范围' if coverage else '重试这一个来源',
        'confirm': '开始重试',
        'picked': 1,
        'detail': '\n'.join(detail),
    }


def requeue(keys, kind: str, lives=None) -> dict:
    """**仅重新排队**:重新开一轮处理机会,一个字节的正文都不碰、这一下也不联网。

    三档各有自己的出口,**按条目此刻真正所在的那一档分流**(`lanes.split`),
    不按前端传来的说法 —— 页面上那一份可能已经过时几秒了。

    - 挂起(证据不足)→ **必须走既有通道** `fulltext.release`:那条路的 WHERE 里
      已经钉着「还没补到全文」「还挂着」「这条订阅现在还开着补抓」三道,面板不许
      另开一条判据(`docs/decisions/fulltext.md`)。
    - 正文重试耗尽 → 把额度还回去。判据待在 UPDATE 自己的 WHERE 里,和
      `release` 同一套纪律;**资格那一道转手 `fulltext.exhausted_sql`**,不重写。
    - 翻译重试耗尽 → 把翻译额度还回去,**不置 `translate_manual`**:那一列是
      「立即执行」那一轮的**付费边界**(只译带标记的),而它在每一轮重试开始时会被
      整类清掉(`translate_scope.requeue_failed` 的清场)—— 在这里打上既到不了那
      一轮,又会让下一次常规同步把这一批顶到队首。这颗按钮承诺的只是「下一轮同步
      会正常轮到它」,那就只做这件事。

    返回**实际写入结果**(元规则):给了 5 个 key 只放回 3 条就说 3,跳过的如实列出。

    **`lives` 是人勾选那一刻这几条各在第几世**(口径在 `warrant.life_sql`):列表
    开着的时候这条内容被清理删掉又从同一个链接收回来,这一下会把处理机会还给
    **新一世**,而人点的是另一条。三档的写语句各自 AND 上它,一处都不能漏 ——
    漏掉的那一档就是旧授权的入口。
    """
    reg.check_kind(kind)
    wanted = list(dict.fromkeys(keys))
    # **分流只问一次库**(`lanes.split` 一个连接、每档一条 `IN` 查询)。逐条问
    # 一次的写法要为每个 key 新开一个 `reg.db()`,而那一下连着建表、补列、种默认
    # 值和 YouTube 的初始化 —— 一次 40 条的批量处理就是 40 遍。
    buckets = lanes.split(wanted, kind)
    # **正文那两档整个交给 `fulltext.reopen`** —— 「哪几条够资格被放回补抓队列」
    # 只有那一份答案,而「立即执行」那条路在管道里也调它(见
    # `kb/collectors/feeds.py` 的 `materialize`)。各写一遍就会出现「面板说放回了
    # 2 条」而真跑那一轮一条都没取到(审查第 1 轮 F02)。
    opened = fulltext.reopen([*buckets.get('body_held', []),
                              *buckets.get('body_exhausted', [])], kind, lives=lives)
    done = {'released': opened['released'], 'body': opened['body'], 'translate': 0,
            'skipped': buckets.get('', [])}
    translate_cond, translate_args = lanes.where('translate_exhausted')
    with reg.db() as c:
        for key in buckets.get('translate_exhausted', []):
            life_cond, life_args = warrant.life_sql(warrant.life_of(lives, key))
            done['translate'] += c.execute(
                "UPDATE feed_items SET translate_tries=0,translate_error=''"
                f' WHERE key=? AND kind=? AND ({translate_cond}) AND ({life_cond})',
                (key, kind, *translate_args, *life_args)).rowcount
    for lane_id, items in buckets.items():
        if lane_id and lane_id not in ('body_held', 'body_exhausted', 'translate_exhausted'):
            done['skipped'] = [*done['skipped'], *items]
    done['requeued'] = done['released'] + done['body'] + done['translate']
    return done


#: 换链接只认网页地址。**PDF 不算网页**:它的字节解出来是一段 `%PDF-1.4 …` 加几
#: 百个替换字符,长度轻松越过正文门槛,于是「补抓成功」、条目离开队列,还拿这堆乱码
#: 去买一次翻译(口径同 `fulltext.fetch_one` 对 `readable.html_problem` 那段)。
_NOT_A_PAGE = ('.pdf', '.zip', '.epub', '.mp3', '.mp4', '.doc', '.docx', '.ppt', '.pptx')


def relink(key: str, kind: str, url: str, life: str = '') -> dict:
    """**换一个取正文的地址**,并把它放回补抓队列。

    **身份那一列(`feed_items.url`)一个字都不动** —— `item_key` 是按原网址算的:
    换掉 `url` 会让 `exclusion.failure()` 里那道 `item_key(channel,url)==key` 不再
    成立,失败明细里「不再抓取」那颗按钮会**静默消失**;而把 key 一起重算就不是
    这条内容了(订阅归属、收藏、已导出的那篇 md 全都挂在旧 key 上)。所以新地址
    写进 `fulltext_url`,只决定「这一次去哪儿抓」(`fulltext.fetch_pending` 的
    `fetch_url`)。

    **这一下不抓。** 它只把额度还回去、把待办清掉,下一轮同步(或者人点一次
    「立即执行」)才真去取 —— 和「仅重新排队」同一个承诺,理由同
    `/{kind}/held/release`:立刻跑一轮要占住管道,而 409 会把保存一起拒掉。

    **历史留着**:`fulltext_error` 清掉是因为那条原因已经作废(它说的是旧地址),
    而这一次换链接本身记进处理记录(`kb.intake_diag.ledger`)。

    **`fulltext_gen=fulltext_gen+1`:这是一代新的补抓工作。** 不能只改那个字符串
    —— 地址改成 B 再改回 A 之后,`work_version` 那个元组**逐字复原**,于是改地址
    之前发出去的那个请求重新获得写回权:实测 A→B→A 之后,旧 `_save` 返回 true、
    `fulltext_at` 被置上,面板上这一条显示「正文已补齐」,而新地址一次都没去过
    (审查第 2 轮 R2)。代次只增不复用,地址来回改也只会让它往前走。
    **这里照旧不碰 `content_rev`**:换地址不是换内容(理由见 `work_version`)。

    **`life` 是人打开那个表单时这一条在第几世**(口径在 `warrant.life_sql`):表单
    停留期间它被清理删掉又从同一个链接收回来时,这个新地址会写到**新一世**头上 ——
    而人看着的是另一条内容的失败原因。带了就 AND 进这条 UPDATE 自己的 WHERE;
    没带照旧。
    """
    reg.check_kind(kind)
    try:
        normalized = reg.normalize_url(url)
    except ValueError as error:
        raise ValueError(str(error)) from None
    from urllib.parse import urlsplit
    parts = urlsplit(normalized)
    if parts.scheme not in ('http', 'https'):
        raise ValueError('请填 http:// 或 https:// 的网页地址')
    if parts.path.lower().endswith(_NOT_A_PAGE):
        raise ValueError('请填能直接读到正文的网页地址，不是 PDF 或下载文件的地址')
    life_cond, life_args = warrant.life_sql(life)
    with reg.db() as c:
        changed = c.execute(
            "UPDATE feed_items SET fulltext_url=?,fulltext_tries=0,fulltext_hold='',"
            "fulltext_error='',fulltext_gen=fulltext_gen+1"
            f" WHERE key=? AND kind=? AND fulltext_at='' AND ({life_cond})",
            (normalized, key, kind, *life_args)).rowcount
    if not changed:
        raise ValueError('这一条的正文已经定下来了，它不属于这个来源，或者它在你打开'
                         '这个表单之后被重新收录过；地址没有改动')
    return {'key': key, 'url': normalized}


def scope_of(kind: str, keys, problem: dict | None = None) -> dict:
    """「这一下会跑什么」—— **确认框里那段话由服务端给,前端只负责显示。**

    留在前端的后果是改了阶段清单没人想起去改那句话,而那句话正是用户据以决定
    要不要花这笔钱的全部信息(先例:`browser_capture_api.CONTINUE_STAGES` 旁边
    那句 `detail`)。

    三种形状,按**条目此刻真正所在的那一档**选,不按前端说法:
    - 来源级问题(调用方给了 `problem`)→ 既有的 `/{kind}/sync`;
    - 这一批里只要有翻译档的 → 既有的 `/{kind}/translate-retry`(它的射程是整个
      范围,不是你点的那几条,`detail` 里如实写着);
    - 其余(正文三档 + 入库)→ 本模块的 `RUN_STAGES`。

    **混着选时拒绝,而不是挑一个跑。** 一次按两条不同的路、两份不同的射程跑,确认
    框里写不出一句不说谎的话 —— 让人分两次点,代价只是多一次点击。
    """
    reg.check_kind(kind)
    if problem is not None:
        plan = _source_scope(problem)
        return _signed(plan, f'source:{problem["id"]}', kind)
    buckets = lanes.split(list(dict.fromkeys(keys)), kind)
    runnable = {name: rows for name, rows in buckets.items()
                if name and 'run' in lanes.BY_ID[name].exits}
    if not runnable:
        raise ValueError('这一批里没有可以执行的条目（可能刚被处理过，或者已经搁置）')
    translate = {name for name in runnable if name.startswith('translate_')}
    if translate and len(translate) != len(runnable):
        raise ValueError('翻译和正文两类问题的执行范围不一样，请分开执行：'
                         '翻译那一轮按整个范围放回队列，正文那一轮按来源的补抓队列取。')
    picked = sum(len(rows) for rows in runnable.values())
    members = _members(kind, runnable)
    if translate:
        # **和既有端点同一个入口**(`translate.stuck` 原样再导出
        # `translate_scope.stuck`):那边读的是哪一份,这里就读哪一份。
        from kb.feeds import translate
        plan = _translate_scope(kind, None, translate.stuck(kind))
        return _signed(plan, plan['endpoint'], kind, members=members)
    return _signed(_body_scope(kind, picked), 'body', kind, members=members)


def _signed(plan: dict, route: str, kind: str, members=()) -> dict:
    """把这份计划的凭证贴上去(`plan` 那一键)。理由见 `_ticket`。"""
    bad = [one for one in plan['stages'] if one in FORBIDDEN_STAGES]
    if bad:
        # 这是一个**结构**错误,不是用户输入错误:本面板任何一条路都不许带
        # 全库提取。真写进去了就当场炸,别让它悄悄花钱(回归另有结构红)。
        raise AssertionError(f'本面板的执行计划里不许出现这些阶段：{bad}')
    plan['plan'] = _ticket(route, kind, plan['stages'],
                           source_id=plan.get('source_id'),
                           scope_sig=plan.get('scope_sig') or '',
                           picked=int(plan.get('picked') or 0), members=members)
    return plan


def run(kind: str, keys=()) -> dict:
    """起跑 `RUN_STAGES`。**只有这一档在本模块起跑**,翻译和来源那两档交给既有本体。

    `crawl_filter` 里**只点名这一类来源,不点名具体订阅** —— 形状逐字照
    `browser_capture_api` 那颗「继续处理」:`export` 这一段取的本来就是这一类来源
    自己的待办队列。**也不盖 `run_trigger.apply_force`**:这一下不采集,来源退避跟
    它无关,盖上去等于替一轮不采集的运行声明「人按的、跳过退避」。

    **但人选的那几条要跟着走** —— `fulltext_retry` 那一份意图由 export 阶段的
    `materialize()` 消费:先按现态解除挂起 / 还回额度,再让它们排在补抓队列最前面
    (见 `kb/collectors/feeds.py` 的 `_body_retry`)。上一版只传了 `kind`,于是
    `queued_sql` 把「待人工确认」和「重试已停止」两档排除在外,人选的那几条**一次
    也没被重试**,而页面说已开始(审查第 1 轮 F02 实测)。

    **意图只放在 `start_pipeline` 原子写进去的那份 `crawl_filter` 里**:占不到管道
    = 这份状态压根没写 = 库里一个字节都没动。先改库再占管道的写法在 409 那一支会
    把挂起和额度提前改掉,而人看到的是一句「没有开始」。

    **跟着走的是「这几世的这几条」,不是几个编号。** 这一份意图要等占住管道、
    跑到导出阶段才被消费,中间这条内容完全可能被清理删掉又从同一个链接收回来 ——
    key 一个字都没变,而它已经是另一世了。所以 `lives` 一起交下去
    (`lanes.identities`),由 `fulltext.reopen` AND 进它自己的 WHERE:对不上就
    一条都不放回,不许拿旧授权去动新一世(第 1 轮自由审查 R2)。

    占不到管道时抛 `RuntimeError`(调用方转 409),并把 `start_pipeline` 给的那句
    拒绝原因原样带出去 —— 光说一句「管道正忙」分不清是定时那一轮还是别人点的。
    """
    reg.check_kind(kind)
    refused: list[str] = []
    named = list(dict.fromkeys(keys))
    wanted: dict = {'channel': kind}
    if named:
        # **身份现取一次**(`lanes.identities`):取不到的 key 不进结果,那正是
        # 「它已经不在了」—— 这样的 key 带着空描述下去会被执行器那道一律拒掉。
        lives = {key: life for key, (life, _rev) in lanes.identities(named).items()}
        wanted['fulltext_retry'] = {'channel': kind, 'keys': named, 'lives': lives}
    started = pipeline.start_pipeline(list(RUN_STAGES),
                                      crawl_filter=wanted, refused=refused)
    if not started:
        raise RuntimeError(refused[0] if refused
                           else '其他同步正在运行，请结束后重试；这一批的状态没有改动')
    return {'run': started}


def kind_scope(kind: str) -> dict:
    """页头那颗「立即执行本类」的射程:没有选集,就是这一类来源自己的后续处理。

    **和逐条那颗按钮是同一段话、同一份阶段清单** —— 因为它们真的会跑同一件事
    (`export` 取的本来就是这一类来源的待办队列,`index` 是全库)。演示页把顶部
    那颗和单条那颗分成「执行全部」和「只执行这一条」,而「只执行这一条」在真实
    管道里做不到,所以这里只在文案上去掉「不只是你点的 N 条」那半句。
    """
    reg.check_kind(kind)
    return _signed(_body_scope(kind, 0), 'kind', kind)
