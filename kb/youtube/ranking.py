import math
import re
import time
from datetime import datetime, timedelta, timezone
from statistics import median
from zoneinfo import ZoneInfo

from kb.youtube.channels import config
from kb.youtube import storage


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


def rejection(row, settings, scope, now, floor):
    """返回 `(理由, 判得准)`。理由为空就是合格;**`判得准=False` 不是拒绝,是「还判不了」**。

    后路(`discovery.approximate`)的条目日期是按频道页列表顺序估的、简介是空的 —— 拿它去
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
    if row.get('excluded'):
        return '用户已永久移除此视频，不再处理', True
    if not policy.get('ranking_enabled', True):
        return '观察或隔离名单，不自动进入排名', True
    if row['state'] == 'excluded' and not scope.force:
        return row['reason'] or '已确认不可自动处理', True
    if not published:
        return '缺少可靠的发布时间', True
    if scope.ranged and not scope.since <= published.astimezone(TZ).date().isoformat() <= scope.until:
        return '不在本次指定日期范围', not estimated
    if not scope.ranged and not row['repair_pending'] and (published < floor or
            (stamp(row['history_floor']) is not None and published < stamp(row['history_floor']))):
        return '早于本轮窗口或订阅历史下界', not estimated
    if published > now + timedelta(minutes=5):
        return '尚未发布', not estimated
    if settings['ai_only'] and not AI.search(entry['title'] + ' ' + entry.get('description', '')):
        return '标题与简介未发现明确的 AI 相关信息', not estimated
    return '', True


def qualify(row, settings, scope, now, floor):
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
        reason, verified = rejection(member, settings, scope, now, floor)
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
