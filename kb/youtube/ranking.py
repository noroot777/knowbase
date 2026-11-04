import math
import re
import time
from datetime import datetime, timedelta, timezone
from statistics import median
from zoneinfo import ZoneInfo

from kb.youtube.channels import config
from kb.youtube import storage
from kb.youtube.settings import video_minutes


AI = re.compile(r'\b(?:ai|agi|llm|gpt\w*|claude|gemini|openai|anthropic|deepseek|qwen|'
                r'agent\w*|cursor|codex|copilot|whisper|comfyui|rag|mcp|chatgpt|'
                r'machine learning|neural|diffusion|transformer\w*|ollama|hugging face)\b|'
                r'人工智能|大模型|智能体|机器学习|深度学习|生成式', re.I)
VALUE = re.compile(r'\b(?:research|paper|benchmark|tutorial|build|coding|code|implementation|'
                   r'workflow|open.source|analysis|explained|engineer|evaluation|interview)\b|'
                   r'教程|实测|开源|论文|实现|访谈|评测', re.I)
NOISE = re.compile(r'\b(?:insane|shocking|secret|get rich|make money|millionaire|you won.t believe)\b|'
                   r'震惊|暴富|躺赚|颠覆一切', re.I)
TZ = ZoneInfo('Asia/Shanghai')


def stamp(value):
    try:
        result = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return result.replace(tzinfo=timezone.utc) if result.tzinfo is None else result
    except (ValueError, AttributeError):
        return None


#: 「这一条判不了,得先核实」的那句话。它**不是拒绝理由**:会落到候选的 `reason` 上,
#: 等排到它的时候 `process` 探一次元数据、换成事实值,再过同一个 `rejection`。
UNKNOWN = '发布时间与简介仍是估算值，排到就先核实一次元数据'


def rejection(row, settings, scope, now, floor, length=True):
    """返回 `(理由, 判得准)`。理由为空就是合格;**`判得准=False` 不是拒绝,是「还判不了」**。

    flat 列表(`windowed_discovery.lightweight`)的条目日期是粗估的、简介是空的 —— 拿它去
    判日期范围、窗口下界或 AI 相关性,判出来的「不合格」其实只是「不知道」。**未知不等于
    不合格**:这一档既不放行也不判死,留在榜上等 `collector.process` 探一次元数据
    (`admission.calibrate` 换成事实值),再过**同一个** `rejection` 定论。

    写成普通拒绝理由的后果是**静默丢数据、而且这一轮再也救不回来**:`score()` 会把它
    排除在 ranked 之外并标 `filtered`,而核实只发生在进了 ranked 的那些条目上 —— 于是
    「只补抓 10-26」那一轮里,真在 10-26 发布的视频报「不在本次指定日期范围」、一条不收;
    标题没提 AI 而简介明确讲 AI 的那条,也没有机会再确认(审查冒烟实测两种)。

    **判得准的那几道不许跟着放水**:用户移除、观察名单、已确认不可处理、连估算日期都
    解析不出来,这几条和估算无关,照旧是拒绝。
    """
    policy, entry = config(row), row['entry']
    published = stamp(entry['published'])
    # 估算条目的日期和简介都是未知:日期只够决定「这条值不值得探一次元数据」,简介是空的。
    estimated = bool(entry.get('approximate'))
    history = stamp(row['history_floor'])
    if history is not None and len(row['history_floor']) == 10:
        history = history.replace(tzinfo=TZ)
    if row.get('excluded'):
        return '用户已永久移除此视频，不再处理', True
    if not policy.get('ranking_enabled', True):
        return '观察或隔离名单，不自动进入排名', True
    if row['state'] == 'excluded' and not scope.force:
        return row['reason'] or '已确认不可自动处理', True
    if not published:
        return '缺少可靠的发布时间', True
    verdicts = []
    if scope.ranged and not scope.since <= published.astimezone(TZ).date().isoformat() <= scope.until:
        verdicts.append(('不在本次指定日期范围', not estimated))
    discovered_after = stamp(row.get('discovery_after'))
    if not scope.ranged and discovered_after is not None and published < discovered_after:
        verdicts.append(('早于本次频道发现窗口', not estimated))
    if not scope.ranged and not row['repair_pending'] and (published < floor or
            (history is not None and published < history)):
        verdicts.append(('早于本轮窗口或订阅历史下界', not estimated))
    if published > now + timedelta(minutes=5):
        verdicts.append(('尚未发布', not estimated))
    if settings['ai_only'] and not AI.search(entry['title'] + ' ' + entry.get('description', '')):
        verdicts.append(('标题与简介未发现明确的 AI 相关信息', not estimated))
    # **探到过、确实超过单条时长上限的,不再占准入名次。** 只留 `deferred` 的话它每轮都排回
    # 前几名、每轮再探一次、再被 `admission.gate` 拒一次 —— 名额被它永久占着,后面的普通
    # 视频永远轮不到(实测前 10 名里两条长视频连占三天)。判据是**当前**上限:用户调高之后
    # 下一轮这里放行,它照常回榜。已完成的不受影响(它们根本不排名,见 `score`)。
    # 上限按**这个归属**的频道算(`video_minutes`):频道自己可以设得比全局宽或窄。
    limit = video_minutes(settings, policy)
    if length and row['state'] not in ('ready', 'processing') and row['duration'] > limit * 60:
        verdicts.append((f"超过单条视频时长预算（{row['duration'] / 60:.0f} 分钟，上限 {limit} 分钟）", True))
    # **判得准的拒绝胜过任何一道「还判不了」,不论排在哪一道。** 逐道早退的话,估算日期或
    # 空简介那几道会抢在后面的事实之前返回「判不了」:探到超长的估算条目就这样照旧拿到
    # 第 1 名、每轮被选中(审查冒烟实测)。全是事实值的条目只会落进判得准那一支,
    # 结论和逐道早退一模一样。
    for reason, verified in verdicts:
        if verified:
            return reason, True
    return verdicts[0] if verdicts else ('', True)


def qualify(row, settings, scope, now, floor, length=True):
    """逐个**有效归属**判资格,返回 `(代表, 拒绝理由列表)`;一个归属都不合格时代表为 None。

    **一条视频属于多条订阅时,一个归属失格不等于这条视频失格。** 同一个视频可以同时挂在
    编号链接和 handle 链接下、也可以挂在两条不同历史下界的订阅下:新订阅的历史下界拦住它,
    老订阅本来收得到 —— 只看代表那一个归属就会把这条视频对**所有**订阅一起判死
    (审查实测:两归属、真实日期三天前,新订阅下界一天前、老订阅六天前,结果全局 excluded,
    下一轮只同步老订阅时入选 0、处理 0)。

    代表取权重最高的那个归属,同权取较小订阅 ID —— `score()` 和事实日期到手后的那次重判
    **都过这一份**,否则两处的「代表是谁」有一天会不一致。

    **结局有三种,不是两种**:合格、拒绝、**还判不了**(见 `rejection` 的「判得准」)。
    判不了的那一档**也要给代表** —— 它要留在榜上等核实,而排名和分数总得记在某个归属
    名下;代表身上打 `unverified`,`score()` 据此把候选的 `reason` 写成 `UNKNOWN`,
    而不是把它标成 `filtered`。一个归属合格就走合格那一档:那时别的归属判不了也不影响
    这条视频进榜(口径同上面那条「一个归属失格不等于失格」)。
    """
    eligible, unsure, reasons, unknown = [], [], [], []
    for owner in row['owners']:
        member = {**row, **owner}
        reason, verified = rejection(member, settings, scope, now, floor, length)
        if not reason:
            eligible.append(member)
        elif verified:
            reasons.append(reason)
        else:
            unknown.append(reason)
            unsure.append(member)
    candidates = eligible or unsure
    if not candidates:
        return None, reasons, unknown
    chosen = max(candidates, key=lambda member: (config(member).get('score_multiplier', 1.0), -member['source_id']))
    return ({**chosen, 'eligible_sources': [member['source_id'] for member in candidates],
             'unverified': not eligible}, reasons, unknown)


def over_length(rows, settings, scope, now=None):
    """只差单条时长上限这一道才没做的视频,`eligible_sources` 是卡在时长上的那几个归属,
    `limits` 是判它们时各自用的上限(分钟)。

    它们在窗口里、其余资格全过得去,只是按**当前**设置不做 —— 同步结果里得点名说出来。
    一个字不提的话,面板上只有「排名 0 条、入库 0 条」,人只会以为频道没更新(真机形态:
    AI Search 三天前那期 54 分钟的周报,上限 45 分钟)。判据跟 `rejection` 是同一份,而且
    **逐归属判**:同一条视频挂在 45 分钟和 60 分钟两个订阅下时,前者自己的说明不能因为后者
    放行就没了 —— 后者可能还没轮到处理,前者那边看到的仍是零新增。某个归属不带上限合格、
    带上限被拒,就是那个归属只差这一道(两次的唯一差别就是时长那一道)。
    """
    now = now or datetime.now(timezone.utc)
    floor = now - timedelta(days=settings['window_days'])
    found = []
    for row in rows:
        if row['state'] in ('ready', 'processing') or not row['duration']:
            continue
        owners = [owner for owner in row['owners']
                  if not rejection({**row, **owner}, settings, scope, now, floor, length=False)[0]
                  and rejection({**row, **owner}, settings, scope, now, floor)[0]]
        if owners:
            # 说明里那个上限数字要跟判它的是同一份频道设置(`limits`),别另读一份:
            # 同步途中改了频道上限的话,两份对不上,说明里写的上限跟实际判据不一致。
            found.append({**row, 'eligible_sources': [owner['source_id'] for owner in owners],
                          'limits': {owner['source_id']: video_minutes(settings, config(owner))
                                     for owner in owners}})
    return found


def score(rows, settings, scope, now=None):
    now = now or datetime.now(timezone.utc)
    floor = now - timedelta(days=settings['window_days'])
    baselines = {}
    for row in rows:
        if row.get('excluded'):
            continue
        for owner in row['owners']:
            baselines.setdefault(owner['source_id'], []).append(row['views'])
    ranked = []
    for row in rows:
        # **已入库的先拿掉再排名**(口径同 daily-report 剔除历史榜单):名次只给还没做的
        # 视频。留着名次的话,窗口里靠前的陆续入库之后前 N 名大半是已完成的,一轮只剩
        # 一两条可做(实测前 10 名里 7 条已入库)。频道基线照旧把它们算进去 —— 那是
        # 「这个频道平时多少播放」,跟谁还没做无关。
        # 名次在它变成 ready 那一刻就清零了(`storage` 的 `youtube_ready_unranked`),这里只管不给它排。
        if row['state'] == 'ready':
            continue
        representative, reasons, unknown = qualify(row, settings, scope, now, floor)
        if representative is None:
            storage.update(row['video_id'], score_rank=0)
            if row['state'] not in ('ready', 'processing', 'excluded'):
                # `unknown` 在这里必然是空的(判不了的那一档也有代表,走不到这个分支),
                # 两个都取只是不留一个会 IndexError 的口子。
                storage.update(row['video_id'], state='filtered', reason=(reasons or unknown)[0], score_rank=0)
            continue
        row = representative
        entry, policy = row['entry'], config(row)
        published = stamp(entry['published'])
        hours = max(1, (now - published).total_seconds() / 3600)
        views = row['views']
        uplift = min(4, views / max(100, median(baselines[row['source_id']])))
        title = entry['title']
        weight = policy.get('score_multiplier', 1.0)
        noise = len(NOISE.findall(title))
        # **只有一个分、一张榜**(仓库主人拍板:不再区分热度和价值)。两组信号仍然都在,
        # 只是加在同一个式子里:播放量和速度那三项管「多少人在看」,`VALUE` 命中管「是不是
        # 干货」,`NOISE` 扣分管标题党,最后乘频道权重。**别再拆回两个分**:两张榜意味着
        # 准入要写成「名次 A 以内**或**名次 B 以内」,实际准入面是两个上限之和,面板上那两个
        # 数字谁都说不清一轮会处理几条。
        score = max(0, (math.log1p(views) * 2 + math.log1p(views / hours) * 3 + uplift +
                        min(4, len(VALUE.findall(title))) * 2 - noise * 3) * weight)
        row.update(score=round(score, 3))
        ranked.append(row)
    for position, row in enumerate(sorted(ranked, key=lambda item: (-item['score'], item['video_id'])), 1):
        row['score_rank'] = position
    for row in ranked:
        patch = {key: row[key] for key in ('score', 'score_rank')}
        if row['state'] in ('candidate', 'filtered', 'processing') or (
                row['state'] == 'deferred' and row['retry_at'] <= time.time()):
            if row.get('_metadata_reason'):
                patch['reason'] = row['reason'] or row['_metadata_reason']
                if row['state'] not in ('deferred', 'processing') and not row.get('metadata_pending'):
                    patch['state'] = 'candidate'
            else:
                patch.update(state='candidate', reason=(UNKNOWN if row.get('unverified')
                                                       else '等待排名与处理预算准入'))
        storage.update(row['video_id'], **patch)
    return ranked


def select(rows, settings, force=False):
    eligible = [row for row in rows if not row.get('excluded') and row['state'] != 'ready' and
                (row['state'] != 'excluded' or force) and
                (force or row['retry_at'] <= time.time()) and
                row['score_rank'] <= settings['rank_limit']]
    return sorted(eligible, key=lambda row: (row['score_rank'], -row['score'], row['video_id']))
