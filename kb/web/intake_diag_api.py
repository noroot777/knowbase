# -*- coding: utf-8 -*-
"""「问题与待办」的后端:**还有多少在等、卡在哪一环、我现在能做什么。**

**硬约束在 `docs/decisions/intake-diagnostics.md` 第一节 —— 改这块之前先读那一份。**

这一份答的是「**全库**还有多少**条内容**卡住」,同步中心那个「需处理」区答的是
「**这一轮/今天**那几**步**欠不欠账」(`kb.pipeline_retry`)。两个数字天生不一样,
所以屏幕上**不共用那个词**,页头那行明说另一份在哪 —— 口径只有一份,见
`kb.intake_diag.lanes` 的模块说明。

**起跑那一下尽量不自己写。** 翻译走既有的 `/api/intake/{kind}/translate-retry`、
来源走既有的 `/api/intake/{kind}/sync`,而且是**在服务端直接调那两个函数** ——
它们各自带着跑过审查的付费边界(`translate_manual`、`scope_sig`)和拒绝文案。
从前端分别去点那两个端点也行,但那样这块面板的处理记录里就少了那两笔,而
「我上次对这一条做过什么」正是它存在的理由。只有正文/入库那一档没有现成的起跑
路径,那一段用 `actions.run`(阶段清单和 `browser_capture_api.CONTINUE_STAGES` 相等)。
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from kb import browser_capture
from kb.feeds import readable, registry as reg
from kb.intake_diag import actions, lanes, ledger, running, shelf, sources
from kb.web import intake_api

router = APIRouter(prefix='/api/intake-care')

#: 请求体里 key 的上限。和 `intake_api` 那几个端点同一个数。
MAX_KEYS = 500


def _kind(kind: str) -> str:
    try:
        return reg.check_kind(kind)
    except ValueError as error:
        raise HTTPException(404, str(error)) from None


def _call(fn, *args, **kwargs):
    """ValueError → 400、LookupError → 404、RuntimeError → 409。

    和 `intake_api.call` 同一套映射,多认一个 `RuntimeError`(占不到管道)。
    """
    try:
        return fn(*args, **kwargs)
    except ValueError as error:
        raise HTTPException(400, str(error)) from None
    except LookupError as error:
        raise HTTPException(404, str(error)) from None
    except RuntimeError as error:
        raise HTTPException(409, str(error)) from None


class Batch(BaseModel):
    model_config = ConfigDict(extra='forbid')
    #: **一次只处理一类来源。** 判据要约束写语句本身(元规则):路由过了合法性
    #: 校验不等于请求体里每个 key 都属于这一类。前端按条目自己的来源分组发。
    kind: str
    keys: list[str] = Field(default_factory=list, max_length=MAX_KEYS)
    #: **页面上那一刻这几条各在第几世**(`{key: life}`,列表和详情接口都带着
    #: `life` 这一列出去)。服务端把它 AND 进每条写语句 —— 「现取这一世」防的是
    #: 并发写入方,防不了旧表单:列表开着的时候这条内容被清理删掉又从同一个链接
    #: 收回来,这一下的决定会按在新一世上(审查第 1 轮 R2,口径在
    #: `kb.intake_diag.warrant`)。**缺了就照旧放行**:存量页面、脚本直调点的就是
    #: 「现在这一条」,一律拒会让那几条路跑不起来。
    lives: dict[str, str] = Field(default_factory=dict)


class RunRequest(Batch):
    #: 来源级问题走这里:`sources.problems` 给的那个 id。服务端**现查一遍**,
    #: 问题已经变了就整批拒绝 —— 确认框里那段话是按旧问题写的。
    problem_id: str = Field(default='', max_length=120)
    # 这里**刻意没有** `scope_sig`:翻译那条路的射程指纹只认服务端重签那一份
    # (`plan['scope_sig']`,和它一起在同一个读事务里给出来)。收下客户端带来的
    # 指纹等于让页面自己声明「这一轮的付费射程是多大」—— 而那正是这道闸要挡的
    # 事(审查第 1 轮 F03)。
    #: 页头那颗「立即执行本类」:没有选集,直接跑这一类来源的导出与索引。
    whole_kind: bool = False
    #: `/scope` 签的那份执行计划凭证(`actions._ticket`)。**`/run` 必须带**,
    #: 服务端按现态再签一张比一比 —— 不比的话,确认框写着「条目化提取不在这一
    #: 步」而提交时那一条已经改走了另一条含 extract 的路(审查第 1 轮 F03)。
    plan: str = Field(default='', max_length=64)


class ShelfRequest(Batch):
    decision: str = Field(pattern='^(snooze|exclude)$')
    #: 上限和 `shelf.shelve` 里那一刀同一个数,不然越界那一下是静默截断。
    reason: str = Field(default='', max_length=300)


class BodyRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    key: str = Field(min_length=1, max_length=64)
    #: 传输上限留宽一截,**判据不在这里**(`browser_capture._admit_body` 一份)。
    body: str = Field(min_length=1, max_length=browser_capture.MAX_BODY * 2)
    #: 打开这个粘贴框时这一条在第几世,理由同 `Batch.lives`。
    life: str = Field(default='', max_length=64)


class LinkRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    kind: str
    key: str = Field(min_length=1, max_length=64)
    url: str = Field(min_length=1, max_length=4096)
    #: 打开这个表单时这一条在第几世,理由同 `Batch.lives`。
    life: str = Field(default='', max_length=64)


def _lane_meta(lane) -> dict:
    return {'id': lane.id, 'group': lane.group, 'stage': lane.stage,
            'state': lane.state, 'summary': lane.summary, 'step': lane.step,
            'exits': list(lane.exits)}


@router.get('')
def overview(kind: str = ''):
    """页头那行事实数字 + 各档计数 + 来源级问题 + 这一轮在跑什么。

    **计数一律从库里查**(`lanes.counts`,元规则),不从任何展示文案反推。
    `kind` 为空 = 所有来源类别合计。
    """
    wanted = [_kind(kind)] if kind else list(reg.KINDS)
    tally = lanes.counts(wanted)
    totals = {lane.id: sum(tally[k].get(lane.id, 0) for k in wanted) for lane in lanes.LANES}
    groups: dict[str, int] = {name: 0 for name in lanes.GROUPS}
    for lane in lanes.LANES:
        groups[lane.group] += totals[lane.id]
    problem_rows = [p for k in wanted for p in sources.problems(k)]
    # **来源级问题不计进这几组的「条」数**(审查第 1 轮 B08):`groups` 的量词是
    # 「条内容」,而一条来源问题不是一篇文章 —— 同一条订阅还可能同时有
    # `last_error` 和 `last_note`,贡献**两条**来源问题。它单独报一个数,页签上
    # 「待决定」显示两者之和但在页头分开说。
    source_problems = len(problem_rows)
    # 搁置那一组的总数走它自己那条取数(同一个 WHERE),不另写一条 count。
    groups[lanes.ARCHIVED] = lanes.shelved(kind, '', 0, 1)['total']
    return {
        'kinds': [{'id': k, 'label': reg.KINDS[k]} for k in reg.KINDS],
        'kind': kind,
        'counts': tally,
        'lane_totals': totals,
        'groups': groups,
        'lanes': [_lane_meta(lane) for lane in lanes.LANES],
        'stages': list(lanes.STAGES),
        'item_steps': list(lanes.ITEM_STEPS),
        'source_steps': list(sources.SOURCE_STEPS),
        'sources': problem_rows,
        'source_problems': source_problems,
        'running': running.snapshot(kind),
        'resolved_days': lanes.RESOLVED_DAYS,
        'min_body_chars': readable.MIN_CHARS,
        'page': lanes.PAGE,
        # 这一句在页头上原样显示,**它就是「两屏不打架」那条约束的落点**。
        'note': ('这里数的是全库还有多少**条内容**没走完收录；同步中心那个'
                 '「需处理」数的是这一轮/今天欠了哪几**步**，两个数字本来就不一样。'
                 '来源连接问题单独计数——它说的是**订阅**读不读得通，不是一篇文章。'),
    }


@router.get('/items')
def items(group: str = '', lane: str = '', kind: str = '', q: str = Query('', max_length=120),
          offset: int = 0, limit: int = lanes.PAGE):
    """一组(或一档)的条目。**按档分节返回**,每一档自带它自己的总数。

    总数按**同一个 WHERE** 查(`lanes.rows`),不按返回的行数推 —— 列表只给一页。

    **分页协议是 `offset` + 定长 `limit`,不是「累加的展开数」。** `limit` 在这里
    被夹到 `lanes.PAGE` 封顶(一次全交出去既慢又没人读得完),所以前端把 limit 加
    大到 41 再请求一次拿回来的还是最前 40 条 —— 上一版就是这么写的,一个总数 41
    的档位反复点「展开更多」永远看不到第 41 条(审查第 1 轮 F09)。往后翻要把
    `offset` 推到已经拿到的条数,并**追加**而不是整屏替换。
    """
    if kind:
        _kind(kind)
    limit = max(1, min(limit, lanes.PAGE))
    if group == lanes.ARCHIVED:
        found = lanes.shelved(kind, q, offset, limit)
        return {'sections': [{'lane': {'id': lanes.ARCHIVED, 'group': lanes.ARCHIVED,
                                       'stage': '', 'state': '已搁置', 'summary': '',
                                       'step': -1, 'exits': ['release']},
                              **found}]}
    if lane:
        if lane not in lanes.BY_ID:
            raise HTTPException(404, '未知的积压档位')
        picked = [lanes.BY_ID[lane]]
    elif group:
        picked = [one for one in lanes.LANES if one.group == group]
        if not picked:
            raise HTTPException(404, '未知的分组')
    else:
        picked = list(lanes.LANES)
    sections = []
    for one in picked:
        found = lanes.rows(one.id, kind, q, offset, limit)
        sections.append({'lane': _lane_meta(one), **found})
    return {'sections': sections}


@router.get('/items/{key}')
def item(key: str):
    """详情抽屉。**可用出口按这一条此刻真正所在的那一档给**,不按前端记的那一份。"""
    row = lanes.one(key)
    if not row:
        raise HTTPException(404, '找不到这条内容')
    bucket = lanes.split([key])
    lane_id = next((name for name, hit in bucket.items() if key in hit), '')
    meta = _lane_meta(lanes.BY_ID[lane_id]) if lane_id in lanes.BY_ID else None
    return {
        'item': row,
        'lane': meta,
        'shelved': lane_id == lanes.ARCHIVED,
        # 「手动补正文」对某些来源压根不开放(`permits_link_body`:X 观点、
        # YouTube、简报的正文不许被原文链接里的页面替换)。如实交出去,按钮
        # 由前端禁掉并说清原因 —— 不说清的话人会一直点那颗没反应的按钮。
        'can_paste': browser_capture.permits_link_body(row['kind']),
        # **按这一世取**(`life`):只按 key 查会把上一世的处理记录列在一条删掉
        # 重收的新内容上(审查第 1 轮 F07)。
        'log': ledger.for_item(key, row['life']),
    }


@router.get('/log')
def log(kind: str = '', limit: int = ledger.LIMIT):
    if kind:
        _kind(kind)
    return {'entries': ledger.recent(kind, limit), 'keep_days': ledger.KEEP_DAYS}


def _problem_of(kind: str, problem_id: str) -> dict:
    for one in sources.problems(kind):
        if one['id'] == problem_id:
            return one
    raise HTTPException(409, '这条来源问题已经不在了（可能刚刚同步过），请刷新再看一次')


def _refuse_unactionable(problem: dict) -> None:
    """这一条来源问题现在跑不了,就**先拒**,不要先给一段承诺。

    确认框里那段话是「这一下会发生什么」的声明;而 `/{kind}/sync` 对停用的订阅和
    没配好的通道各有一句 400。先承诺再被退回,人看到的是两句互相矛盾的话 ——
    尤其 X 限流那一档,那句退回正是「还没有登录过 X」,恰好把人引到这块面板**最
    不该**引他去的地方(反复登录)。判据仍然只有一份,这里只是提前问。
    """
    if problem['actionable']:
        return
    if problem.get('blocked'):
        raise HTTPException(400, problem['blocked'] + '。这一条的重试要等通道配好才跑得起来；'
                                 '这跟它这一次为什么失败是两件事。')
    raise HTTPException(400, '这条订阅现在是停用的，单独重试它不会跑；'
                             '要跑请先到「我的订阅」里启用它。')


@router.post('/scope')
def scope(body: RunRequest):
    """确认框里那段话。**由服务端给**,前端只负责显示(理由见 `actions.scope_of`)。"""
    kind = _kind(body.kind)
    problem = _problem_of(kind, body.problem_id) if body.problem_id else None
    if problem:
        _refuse_unactionable(problem)
        return _call(actions.scope_of, kind, body.keys, problem)
    if body.whole_kind:
        return _call(actions.kind_scope, kind)
    return _call(actions.scope_of, kind, body.keys, None)


@router.post('/run')
def run(body: RunRequest):
    """立即执行。**三条路,射程各不相同,都由服务端决定走哪一条。**

    - 来源级问题 → 既有本体 `intake_api.start_sync`(点名这一条订阅,跳过退避);
    - 这一批全是翻译档 → 既有本体 `intake_api.start_translate_retry`(带射程指纹);
    - 其余(正文三档 + 入库)→ `actions.run`(`export` + `index`,带上点名那几条)。

    **三条路的阶段清单都由本面板给**(`plan['stages']`,一条都不含 `extract`);
    付费边界、指纹校验和拒绝文案照旧是那两条既有路自己的(理由见 `actions`)。

    **混着选时由 `scope_of` 拒绝**,不在这里挑一个跑:一次按两份不同的射程跑,
    确认框里写不出一句不说谎的话。

    **提交前按现态重签一次计划凭证**:不等就整批拒绝,让人重看一眼 —— 他确认的是
    一份计划,而这期间那几条可能换了档、换了世、来源问题可能已经不在了
    (审查第 1 轮 F03)。
    """
    kind = _kind(body.kind)
    if not body.plan:
        raise HTTPException(400, '这一批没有带执行计划，没有开始任何处理；'
                                 '请在面板上重新点一次（确认框会说清这一轮跑什么）')
    problem = _problem_of(kind, body.problem_id) if body.problem_id else None
    if problem:
        _refuse_unactionable(problem)
    plan = (_call(actions.kind_scope, kind) if body.whole_kind and not problem
            else _call(actions.scope_of, kind, body.keys, problem))
    if plan['plan'] != body.plan:
        raise HTTPException(409, '这一批的执行范围在你确认期间变过了（有的已经被'
                                 '处理掉、换了环节，或者来源问题已经不在了）。'
                                 '这一下什么都没有开始，请回面板看一眼新的说明，'
                                 '再点一次。')
    if problem:
        # **把确认时那份来源描述一起交下去**,让起跑那一下按同一份再核一次,
        # 并由它一路交到执行器手里 —— 不是让谁按编号重读一份新配置(口径在
        # `kb.intake_diag.warrant.source_sig` 与 `intake_api.start_sync`)。
        # **描述算不出来就一律拒**,理由同翻译那条路上的空指纹:放过去就是拿
        # 「脚本直调」的待遇跑一次人确认过的操作。
        if not plan.get('expect', {}).get(str(problem['source_id'])):
            raise HTTPException(409, '这一条来源的执行描述取不到，没有开始；'
                                     '请回面板重新点一次')
        started = intake_api.start_sync(
            kind, intake_api.SyncRequest(source_ids=[problem['source_id']]),
            plan['stages'], expect=plan['expect'])
        ledger.record('run_source', kind, f'重试来源「{problem["name"]}」：{plan["title"]}',
                      detail=plan['detail'], affected=1)
        return {**started, 'scope': plan}
    if plan.get('endpoint', '').endswith('/translate-retry'):
        # **空指纹在这条路上一律拒**。那个端点把空串解释成「没有确认框、没有承诺
        # 过的数字」(curl / 脚本直调)而放行,可这里恰恰是从确认框来的 —— 放过去
        # 就是拿「脚本直调」的待遇跑一次人确认过的付费操作(审查第 1 轮 F03)。
        if not plan.get('scope_sig'):
            raise HTTPException(409, '这一轮的翻译范围取不到指纹，没有开始；'
                                     '请回面板重新点一次')
        started = intake_api.start_translate_retry(kind, intake_api.TranslateRetryRequest(
            source_id=None, scope_sig=plan['scope_sig']), plan['stages'])
        ledger.record('run_translate', kind, f'重试翻译：{plan["picked"]} 条进入这一轮',
                      detail=plan['detail'], affected=plan['picked'], keys=body.keys)
        return {**started, 'scope': plan}
    started = _call(actions.run, kind, body.keys)
    ledger.record('run_body', kind,
                  f'起跑导出与索引（选中 {plan["picked"]} 条，实际范围是整类来源）',
                  detail=plan['detail'], affected=plan['picked'], keys=body.keys)
    return {**started, 'scope': plan,
            'detail': '已启动导出与索引：正文补抓按这一类来源的队列取，'
                      '可能联网并调用翻译模型；索引会检查全库。'
                      '条目化提取不在这一步。结果以同步中心为准'}


@router.post('/requeue')
def requeue(body: Batch):
    """仅重新排队:**不联网、不写正文**,只把处理机会还回去。报实际写入结果。"""
    kind = _kind(body.kind)
    if not body.keys:
        raise HTTPException(400, '请先选要放回队列的内容')
    done = _call(actions.requeue, body.keys, kind, body.lives)
    ledger.record('requeue', kind,
                  f'把 {done["requeued"]} 条放回队列'
                  + (f'，{len(done["skipped"])} 条跳过' if done['skipped'] else ''),
                  detail=f'挂起恢复 {done["released"]} 条、正文额度 {done["body"]} 条、'
                         f'翻译额度 {done["translate"]} 条',
                  affected=done['requeued'], keys=body.keys)
    return {**done, 'asked': len(set(body.keys)),
            'detail': f'已放回 {done["requeued"]} 条，下一轮同步会正常轮到它们；'
                      '这一下没有联网，也没有改动正文'}


@router.post('/body')
def paste_body(body: BodyRequest):
    """手动补正文。**准入判据和浏览器采集那条路逐字同一份**(`_admit_body`)。

    **不把「正文已补入」算成「已入库」**(状态契约):这一下只换正文、打上
    `extract_stale`,写进知识库是下一轮导出的事 —— 返回的那句话照这个口径写。
    """
    saved = _call(browser_capture.save_pasted, body.key, body.body, body.life)
    row = lanes.one(body.key)
    kind = row['kind'] if row else ''
    ledger.record('paste_body', kind, f'手动补入正文 {saved["characters"]} 字',
                  detail='正文已换成粘贴的那一份，来源记为「人工粘贴」；'
                         '下一轮导出才写进知识库',
                  affected=1, keys=[body.key])
    return {**saved,
            'detail': f'正文已补入（{saved["characters"]} 字）。它现在等着写进知识库：'
                      '点「立即执行」或者等下一轮同步。这一下还没有入库'}


@router.post('/link')
def relink(body: LinkRequest):
    """换一个取正文的地址。**身份那一列不动**(理由见 `actions.relink`)。"""
    kind = _kind(body.kind)
    done = _call(actions.relink, body.key, kind, body.url, body.life)
    ledger.record('relink', kind, '换了取正文的地址',
                  detail=f'新地址 {done["url"]}；原文链接和条目身份没有改动',
                  affected=1, keys=[body.key])
    return {**done, 'detail': '地址已保存，这一条回到了补抓队列。这一下还没有去抓：'
                              '点「立即执行」或者等下一轮同步'}


@router.post('/shelf')
def shelve(body: ShelfRequest):
    """暂缓 / 不再收录。**可撤销**,和永久排除(`kb.feeds.exclusion`)不是一回事。"""
    kind = _kind(body.kind)
    if not body.keys:
        raise HTTPException(400, '请先选要搁置的内容')
    done = _call(shelf.shelve, body.keys, kind, body.decision, body.reason,
                 body.lives)
    shelved = done['shelved']
    word = '暂缓' if body.decision == shelf.SNOOZE else '不再收录'
    # **记的是真搁置成了的那几条和各自那一次决定的编号**,不是整份请求:被跳过的
    # 那些后来可能有了别的决定,而这一行上那颗「恢复」只该取消它自己这一次
    # (审查第 1 轮 F08)。
    ledger.record(f'shelf_{body.decision}', kind, f'{word} {shelved} 条',
                  detail=body.reason or '（没有填原因）',
                  affected=shelved, keys=done['keys'], undo=ledger.UNDO_RELEASE,
                  decisions=done['decisions'])
    return {**done, 'asked': len(set(body.keys)),
            'decision': body.decision,
            'detail': f'已{word} {shelved} 条：它们离开了处理队列，'
                      '已经入库的内容和文件一个字都没动，随时可以在「已搁置」里恢复'}


class ReleaseRequest(Batch):
    #: 要取消的是**哪一次决定**,和 `keys` 一一对应(`shelf.shelve` 发的那个编号,
    #: 列表里按 `shelf_decision_id` 带出来)。**不是可选的**:只按 key 删的话,
    #: 「先暂缓 → 恢复 → 再不再收录」之后点第一条记录上那颗「恢复」会把后来那次
    #: 决定一起取消(审查第 1 轮 F08)。
    decisions: list[str] = Field(default_factory=list, max_length=MAX_KEYS)


@router.post('/shelf/release')
def shelf_release(body: ReleaseRequest):
    """从「已搁置」恢复:删掉**那一次决定**,条目回到它本来所在的那一档。

    判据待在 DELETE 自己的 WHERE 里(`shelf.release`),所以「这颗按钮是不是已经
    失效」由实际删掉几行回答 —— 0 行就如实说 0 行(元规则:0 行不是失败)。
    """
    kind = _kind(body.kind)
    if not body.keys:
        raise HTTPException(400, '请先选要恢复的内容')
    released = _call(shelf.release, body.keys, kind, body.decisions)
    ledger.record('shelf_release', kind, f'恢复 {released} 条',
                  affected=released, keys=body.keys)
    asked = len(set(body.keys))
    if not released:
        return {'released': 0, 'asked': asked,
                'detail': '没有恢复任何内容：这几条的搁置决定在那之后已经变过了'
                          '（或者已经恢复过）。请刷新「已搁置」再看一次'}
    return {'released': released, 'asked': asked,
            'detail': f'已恢复 {released} 条，它们回到了原来所在的那一档'
                      + ('' if released == asked else
                         f'；另外 {asked - released} 条的决定在那之后变过了，没有改动')}
