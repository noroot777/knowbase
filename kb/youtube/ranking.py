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


def score(rows, settings, scope, now=None):
    now = now or datetime.now(timezone.utc)
    floor = now - timedelta(days=settings['window_days'])
    baselines = {}
    for row in rows:
        baselines.setdefault(row['source_id'], []).append(row['views'])
    ranked = []
    for row in rows:
        entry, policy = row['entry'], config(row)
        published = stamp(entry['published'])
        reason = ''
        if not policy.get('ranking_enabled', True):
            reason = '观察或隔离名单，不自动进入排名'
        elif not published:
            reason = '缺少可靠的发布时间'
        elif scope.ranged and not scope.since <= published.astimezone(TZ).date().isoformat() <= scope.until:
            reason = '不在本次指定日期范围'
        elif not scope.ranged and (published < floor or
                (stamp(row['history_floor']) is not None and published < stamp(row['history_floor']))):
            reason = '早于本轮窗口或订阅历史下界'
        elif published > now + timedelta(minutes=5):
            reason = '尚未发布'
        elif settings['ai_only'] and not AI.search(entry['title'] + ' ' + entry.get('description', '')):
            reason = '标题与简介未发现明确的 AI 相关信息'
        if reason:
            storage.update(row['video_id'], heat_rank=0, value_rank=0)
            if row['state'] not in ('ready', 'processing'):
                storage.update(row['video_id'], state='filtered', reason=reason, heat_rank=0, value_rank=0)
            continue
        hours = max(1, (now - published).total_seconds() / 3600)
        views = row['views']
        uplift = min(4, views / max(100, median(baselines[row['source_id']])))
        title = entry['title']
        weight = policy.get('score_multiplier', 1.0)
        noise = len(NOISE.findall(title))
        heat = max(0, (math.log1p(views) * 2 + math.log1p(views / hours) * 3 + uplift - noise * 3) * weight)
        value = max(0, (5 + min(4, len(VALUE.findall(title))) * 2 +
                       math.log1p(views / hours) * .5 - noise * 3) * weight)
        row.update(heat=round(heat, 3), value=round(value, 3))
        ranked.append(row)
    for field, rank_key in (('heat', 'heat_rank'), ('value', 'value_rank')):
        for position, row in enumerate(sorted(ranked, key=lambda item: (-item[field], item['video_id'])), 1):
            row[rank_key] = position
    for row in ranked:
        patch = {key: row[key] for key in ('heat', 'value', 'heat_rank', 'value_rank')}
        if row['state'] in ('candidate', 'filtered', 'deferred', 'processing'):
            patch.update(state='candidate', reason='等待排名与处理预算准入')
        storage.update(row['video_id'], **patch)
    return ranked


def select(rows, settings, force=False):
    eligible = [row for row in rows if row['state'] not in ('ready', 'excluded') and
                (force or row['retry_at'] <= time.time()) and
                (row['heat_rank'] <= settings['heat_limit'] or row['value_rank'] <= settings['value_limit'])]
    return sorted(eligible, key=lambda row: (min(row['heat_rank'], row['value_rank']),
                                           -row['value'], -row['heat'], row['video_id']))
