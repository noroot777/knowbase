"""Feed-backed module UI API; collector execution remains in the shared pipeline."""
import time
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from kb import pipeline
from kb.feeds import registry as reg
from kb.feeds import x_channels
from kb.source_types import catalog

router = APIRouter(prefix='/api/intake')


def kind_checked(kind):
    try:
        return reg.check_kind(kind)
    except ValueError as e:
        raise HTTPException(404,str(e)) from None


def guard(kind):
    kind_checked(kind)
    state = pipeline.get_state()
    running = {scope['channel'] for scope in pipeline.crawl_channels(state.get('crawl_filter'))}
    if state['running'] and kind in running:
        raise HTTPException(409,'该来源正在同步，请等待结束后修改订阅')


def call(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except ValueError as e:
        raise HTTPException(400,str(e)) from None
    except LookupError as e:
        raise HTTPException(404,str(e)) from None


@router.get('/modules')
def modules():
    return {'modules':catalog()}


@router.get('/{kind}')
def overview(kind: str):
    kind_checked(kind)
    sources = reg.subscriptions(kind)
    settings = reg.get_settings(kind)
    for source in sources:
        source['configuration_error'] = reg.configuration_error(source, settings)
    with reg.db() as c:
        count = c.execute('SELECT count(*) FROM feed_items WHERE kind=?',(kind,)).fetchone()[0]
    return {'sources':sources, 'settings':settings, 'last_run':reg.latest_run(kind),
            'total':count, 'enabled':sum(s['enabled'] for s in sources),
            'ready':sum(s['enabled'] and not s['configuration_error'] for s in sources),
            'unconfigured':sum(s['enabled'] and bool(s['configuration_error']) for s in sources),
            'errors':sum(bool(s['last_error']) and s['enabled'] for s in sources)}


@router.get('/{kind}/items')
def list_items(kind: str, source_id: int | None = None,
               offset: Annotated[int,Query(ge=0)] = 0, limit: Annotated[int,Query(ge=1,le=100)] = 30,
               q: Annotated[str,Query(max_length=200)] = ''):
    kind_checked(kind)
    return reg.items(kind,source_id,offset,limit,q)


class Subscription(BaseModel):
    name: str = Field(min_length=1,max_length=120)
    url: str = Field(max_length=2048)
    feed_url: str = Field(default='',max_length=2048)
    group_name: str = Field(default='',max_length=80)
    enabled: bool = True
    # 「去原文链接把正文抓回来」(见 kb/feeds/fulltext.py)。默认关:只有链接
    # 聚合源需要它,对本来就给全文的 feed 再抓一次是拿启发式提取的结果去换
    # 作者亲手给的全文。
    fulltext: bool = False
    # 「这条订阅的内容要不要买一次大模型提取(实体/主题/摘要)」。**默认关** ——
    # 不开也照样全文/语义检索得到,只是没有标签(见 kb.extract.pending_docs)。
    extract: bool = False


class SubscriptionPatch(BaseModel):
    name: str | None = Field(default=None,min_length=1,max_length=120)
    url: str | None = Field(default=None,max_length=2048)
    feed_url: str | None = Field(default=None,max_length=2048)
    group_name: str | None = Field(default=None,max_length=80)
    enabled: bool | None = None
    fulltext: bool | None = None
    extract: bool | None = None


@router.post('/{kind}/sources',status_code=201)
def add_source(kind: str, body: Subscription):
    guard(kind)
    return call(reg.save_subscription,kind,body.model_dump())


@router.patch('/{kind}/sources/{source_id}')
def update_source(kind: str,source_id: int,body: SubscriptionPatch):
    guard(kind)
    return call(reg.save_subscription,kind,body.model_dump(exclude_none=True),source_id)


@router.delete('/{kind}/sources/{source_id}')
def delete_source(kind: str,source_id: int):
    guard(kind)
    call(reg.delete_subscription,kind,source_id)
    return {'ok':True,'detail':'已删除订阅，已收录内容保留'}


class Settings(BaseModel):
    enabled: bool
    interval_minutes: int = Field(ge=15,le=10080)
    bridges: list[str] | None = None
    x_channel: str | None = None
    x_feedgrab_bin: str | None = None
    x_chrome_path: str | None = None


@router.put('/{kind}/settings')
def set_settings(kind: str,body: Settings):
    kind_checked(kind)
    values = body.model_dump(exclude_none=True)
    if not x_channels.supports_channels(kind):
        # X 的通道字段只对 social 有意义。不摘掉的话它们会被原样存进 rss 的设置里
        # (那一类压根不走 `x_channels.validate`),留下一份谁也不读、也没校验过的垃圾。
        for key in ('x_channel', 'x_feedgrab_bin', 'x_chrome_path'):
            values.pop(key, None)
    # Changing the interval starts a new interval; enabling starts within the next tick.
    old = reg.get_settings(kind)
    values['next_run'] = (time.time() if body.enabled and not old['enabled'] else
                          time.time()+body.interval_minutes*60)
    return call(reg.save_settings,kind,values)


# ── X 的采集通道(只有 social 这一类有)──────────────────────────────────────


def _x_kind(kind: str) -> dict:
    """确认这一类来源有通道可选,并把当前设置取出来。"""
    kind_checked(kind)
    if not x_channels.supports_channels(kind):
        raise HTTPException(404, '该来源没有可选的采集通道')
    return reg.get_settings(kind)


@router.get('/{kind}/x-channel')
def x_channel(kind: str):
    """通道清单 + 当前选择 + 便宜的就绪判断。面板每次打开都调它,所以不连网。"""
    settings = _x_kind(kind)
    ready, why, hint = x_channels.readiness(settings)
    current = x_channels.channel_of(settings)
    # 这里不发别名表:通道选择只存在服务端的 feed_settings 里,而 `get_settings`
    # 读出来就已经 resolve 过(见 `x_channels.resolve`),浏览器手里拿不到退役名。
    # 公众号那边要发别名是因为那份选择存在 localStorage —— 在后端的射程之外。
    return {
        'current': current,
        'channels': [{'id': c, 'label': x_channels.LABELS[c],
                      'hint': x_channels.HINTS[c]} for c in x_channels.CHANNELS],
        'ready': ready, 'why': why, 'hint': hint,
        'login_command': x_channels.login_command(settings),
        'needs_login_window': current == x_channels.BROWSER,
    }


@router.post('/{kind}/x-channel/login')
def x_channel_login(kind: str):
    """开一个有头窗口让用户登录(只有浏览器通道走这条)。起完就返回。

    **拒绝的文案要说清「现在是哪一档」和「下一步怎么办」。** 这条路最常见的来法
    不是"用户在桥接通道上点了登录",而是**他已经把下拉改成了浏览器登录态、只是
    还没点「保存设置」** —— 这里读的是**存下来**的那一份(`reg.get_settings`),
    页面上那个下拉是还没提交的草稿。原来只说"只有「我的浏览器登录态」这条通道
    需要在这里登录",于是用户对着一个明明选着「我的浏览器登录态」的下拉,收到
    一句自相矛盾的拒绝,看不出差在哪一步(2026-10-09 实测:`feed_settings` 里
    压根没有 social 这一行,通道是默认的 bridge)。前端另有一道闸(未保存时这颗
    按钮是禁用的),这里是它漏掉时的兜底 —— 两道各有射程,别只留一道。
    """
    settings = _x_kind(kind)
    current = x_channels.channel_of(settings)
    if current == x_channels.ATTACH:
        # 这一档不该建议他换档:他要的登录态就在自己那个浏览器里,我们开一个
        # 独立 profile 的窗口对他毫无用处。说清「在哪儿登录」就够了。
        from kb.feeds import cdp_attach
        raise HTTPException(400, '这一档用的是你日常浏览器里现成的登录态，不在这里'
                                 '登录。要是它还没登录 X，在那个浏览器里打开 x.com '
                                 '登录一次就行。' + cdp_attach.ENABLE_HINT)
    if current != x_channels.BROWSER:
        raise HTTPException(400, f'当前保存的取法是「{x_channels.label(current)}」，'
                                 f'不在这里登录。先把取法改成'
                                 f'「{x_channels.LABELS[x_channels.BROWSER]}」'
                                 f'并点「保存设置」，再打开登录窗口。')
    from kb.feeds import x_browser
    try:
        detail = x_browser.open_login_window(str(x_channels.BROWSER_PROFILE),
                                             settings.get('x_chrome_path', ''))
    except Exception as e:                                # noqa: BLE001 - 成品文案
        raise HTTPException(400, str(e)) from None
    return {'ok': True, 'detail': detail}


@router.post('/{kind}/x-channel/check')
def x_channel_check(kind: str):
    """真连一次。这是用户点「检查连接」才做的事,所以允许慢。"""
    settings = _x_kind(kind)
    ready, detail = call(x_channels.check, settings)
    return {'ready': ready, 'detail': detail}


class TranslateRetryRequest(BaseModel):
    source_id: int | None = None
    # 确认框弹出那一刻的候选集合指纹(`translate.scope_signature`)。面板一定
    # 带着它;空串是「调用方没有确认快照」那一档,见下面那段。
    scope_sig: str = Field(default='', max_length=120)


@router.post('/{kind}/translate-retry')
def translate_retry(kind: str, body: TranslateRetryRequest):
    """把等待翻译的内容点名放到队首,并立刻跑一轮翻译 + 导出。

    **为什么要有这个端点**:试满 `MAX_TRIES` 的条目按设计离开翻译队列、按原文
    导出,于是 vault 里躺着英文而面板上一个标记都没有;而 `queued_sql` 的
    `translate_tries<MAX_TRIES` 让「同步」和「重试失败项」**都选不到它们** ——
    中转站一轮 503 就能造出上百条,原来只能靠手改 feeds.db 才解除得了
    (2026-10-13 实测 rss 42 条 + social 66 条)。只是还没轮到的普通积压同样
    走这里:一轮只译 `MAX_ITEMS` 条,剩下的本来要等下一次同步。

    **这里一个字节都不写库。** 要改的那一下(额度清零 + 点名)挪到了 export
    阶段(`kb/collectors/feeds.py` 的 `materialize`),意图只放在
    `start_pipeline` 原子写进去的那份 `crawl_filter` 里 —— 占不到管道就是这份
    状态压根没写,库里什么都没变。原来是先写库再占管道,于是 409 拒绝之后额度
    和标记已经落了盘,页面再读一次、失败说明和按钮一起消失,用户按拒绝文案说的
    「结束后重试」已经没有入口可点(2026-10-13 第 1 轮审查实测)。在开头补一次
    `running` 检查治不了它 —— 那和 `start_pipeline` 之间仍然能被抢占。

    **跑的阶段里没有 crawl**:要重译的内容早就在库里了,重新采集一遍既慢又可能
    因为源站退避什么都拿不到,而翻译就挂在 export 阶段上。index/extract 跟在
    后面是因为译文落盘之后那几篇 md 换了内容,不重建索引的话检索到的还是英文
    那一份。
    """
    kind_checked(kind)
    from kb.feeds import translate
    # 没配 key 时 `translate_pending` 整个跳过、一次额度都不计。如实拒掉,并说清
    # 下一步 —— 放过去就是跑一轮什么也不翻的管道,而面板上是一句「已开始」。
    if not translate.available():
        raise HTTPException(400, '还没有可用的翻译模型，先到顶栏「模型」里给'
                                 '「外文翻译」这一步配好 API key，再回来重试。')
    if body.source_id is not None:
        ids = {s['id'] for s in reg.subscriptions(kind)}
        if body.source_id not in ids:
            raise HTTPException(404, '找不到这条订阅')
    # 判据是「这个射程里现在有没有要译的」,而且**按用户确认的那个范围数** ——
    # 他筛着一条订阅时,确认框承诺的就是这条订阅的条数(见 `translate.stuck`)。
    # 两段加起来才是射程:还排在队列里的 + 已经试满额度离开队列的(后者会被
    # export 阶段那次 `requeue_failed` 放回来)。
    #
    # **判据不许写成「没失败过就拒」** —— 一条都没失败、只是还没轮到的积压正是
    # 这颗按钮要消化的另一档(定时同步默认关着,它们没有别的办法动起来)。
    # 反过来也不许只问 `failed`:`shipped`/`exhausted` 是 `RETRY_SQL` 的子集,
    # `failed=0` 时它们必然也是 0,拿它们做第二分支是死代码。
    #
    # 两段都排除了「正文还在补抓队列里」的条目(`queued_sql` / `manual_sql` 同
    # 一个口径),所以「有失败过的、但现在一条都译不了」是一个**真实可达**的档:
    # 那批内容在等原文正文。如实说出来 —— 不说的话它和「压根没有外文内容」长得
    # 一模一样,而两者的下一步完全不同(一个是等补抓,一个是没什么可做的)。
    # **两个数字都从 `stuck()` 那一份快照里取,不许另开一次读。** 它把两个数、
    # 那句原因和那份指纹锁在一个读事务里给出来(见 `translate_scope.stuck`);
    # 这里再调一次 `pending()` 的话就又是另一个时点,于是回包里能出现「这一轮
    # 1 条」配着「那两条的指纹」—— 而两道指纹校验都会把它当成有效授权
    # (2026-10-14 第 6 轮审查实测)。
    stuck = translate.stuck(kind, body.source_id)
    queued = stuck['queued']
    if not queued and not stuck['exhausted']:
        raise HTTPException(400, '这些内容此刻都在等正文补抓，补抓完会自动翻译。'
                            if stuck['failed'] else
                            '这个范围里没有等待翻译的内容了。')
    # **范围固定住了,范围里的那批内容还会变。** 判据里有订阅自己的设置 ——
    # 确认框开着的时候把那条订阅的「补抓原文正文」关掉,等正文的那几条当场
    # 变成可译的(2026-10-14 第 5 轮审查实测:确认框说 1 条,实际译了 2 条,
    # 而订阅没换、请求里的 `source_id` 也没丢)。所以除了「哪个范围」,还要比
    # 一次「这个范围里的哪几条」。**这一道是早一道**,它赶在占管道之前,于是
    # 用户点下去的那一刻就能看到为什么没开始;承重的是 `requeue_failed` 在
    # 写事务里那一次(端点放行到真开跑之间还隔着整个 crawl 阶段)。
    #
    # 空指纹放行:没有确认框就没有「承诺过的数字」这回事(curl / 脚本直调),
    # 拒绝一个什么都没被承诺的调用方没有意义。
    if body.scope_sig:
        now = translate.scope_signature(kind, body.source_id)
        if now != body.scope_sig:
            raise HTTPException(409, '这个范围里等着译的内容在你确认期间变过了'
                                     '（比如这条订阅的「补抓原文正文」被改动）。'
                                     '请回面板看一眼新的条数，再点一次。')
    # 收下真实原因再报:没起跑的另一种是持久化边界落不下来(理由同 `pipeline_api`)。
    why: list[str] = []
    if not pipeline.start_pipeline(['export','index','extract'], refused=why,
            crawl_filter={
            'channel':kind, 'translate_retry':{'channel':kind,
                                               'source_id':body.source_id,
                                               'scope_sig':body.scope_sig}}):
        raise HTTPException(409, why[0] if why
                            else '其他同步正在运行，请结束后重试；自动同步会顺延')
    # 一轮上限是 `translate.MAX_ITEMS`,所以队列比它长时要如实说清这一轮只译得完
    # 一截 —— 不说的话用户点一次、看到数字只掉了 40,会以为按钮漏了东西。
    # `queued` 不含那些已经试满额度、离开队列的,所以总量要把它们加回来 ——
    # 加的是 `exhausted` 而不是 `shipped`:后者只数真的落了盘的那些,而没落盘的
    # 照样会被放回队列,按它算就又变成「说 3 条、动 4 条」(见 `translate.stuck`)。
    total = queued + stuck['exhausted']
    this_round = min(total, translate.MAX_ITEMS)
    detail = (f'这一轮翻译 {this_round} 条' if this_round >= total else
              f'共 {total} 条，这一轮先译 {this_round} 条，其余留到下一轮')
    return {'ok':True,'pending':total,'detail':detail + '，译完会自动写入知识库。'}


class SyncRequest(BaseModel):
    source_ids: list[int] = Field(default_factory=list,max_length=500)


@router.post('/{kind}/sync')
def sync(kind: str,body: SyncRequest):
    kind_checked(kind)
    active = [s for s in reg.subscriptions(kind) if s['enabled']]
    selected = [s for s in active if not body.source_ids or s['id'] in body.source_ids]
    if not selected or (body.source_ids and len(selected) != len(set(body.source_ids))):
        raise HTTPException(400,'请选择存在且已启用的订阅')
    settings = reg.get_settings(kind)
    if all(reg.configuration_error(s, settings) for s in selected):
        raise HTTPException(400, reg.configuration_error(selected[0], settings))
    # extract 在列里,但「哪些条目值得提取」由订阅级开关回答(默认关,见
    # `kb.extract.pending_docs`):没开过开关的库跑到这一步是「没有待提取的
    # 文档」,一次模型都不调。把它排除在计划外才是错的 —— 那样用户给某条
    # 订阅开了提取,它同步完也不会提取,得等绿泡泡那轮全流程定时同步顺手捎上。
    why: list[str] = []
    if not pipeline.start_pipeline(['crawl','export','index','extract'], refused=why,
            crawl_filter={
            'channel':kind,'accounts':[str(s['id']) for s in selected], 'force':True}):
        raise HTTPException(409, why[0] if why
                            else '其他同步正在运行，请结束后重试；自动同步会顺延')
    return {'ok':True,'detail':'同步已开始，完成后可在全文检索中查找'}
