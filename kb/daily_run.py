# -*- coding: utf-8 -*-
"""日报的编排与定时:到点先把所有来源同步一遍,再生成当天的日报。

同步这一步不自己造轮子,而是**依次**借用已有的管道(`kb.pipeline`),分两段:
每个来源各跑一趟 抓取→导出,全部抓完再跑一趟全库的 索引→提取(只有前两段认
来源,后两段没有「只做这个来源那一份」的概念)。依次而不是并发:管道是单例
(全局一把运行锁,索引/提取都要单写者写 kb.db),两轮同时开等于一个必然被拒。

于是一轮日报是 N+1 次管道运行,而「最近一次运行」只有一个位子 —— 这几轮用
`pipeline.open_batch()` 归成一个**业务批次**,每一轮收尾都由结果存储层把整批合并着
落盘,否则抓取失败的明细和「重试失败项」会被最后那一轮入库的成功抹掉。细节仍旧
点进同步中心看,这里只记每一项的成败。
硬约束在 `docs/decisions/sync-schedule.md`。
"""

from __future__ import annotations

import os
import re
import threading
import time
import uuid
from datetime import datetime, timedelta

from kb import daily
from kb.config import (DATA_DIR, ScheduleUnreadable, announce_config_issue,
                       config_clock, config_day, config_flag, config_names, config_stamp,
                       derive_config, read_config_file, write_config_file)
# 运行状态那一块搬去了 `kb.daily_state`(行数上限)。**连 `_lock` 一起导进来**:
# 设置回写、定时账本、运行状态共用同一把锁,这里导的就是那一把。
from kb.daily_state import (STATE_FILE, _halting, _load_last_run, _lock, _log,
                            _save_last_run, _set, _state, _sync_issues,
                            get_state, is_running)
from kb.sched_ledger import Ledger

SETTINGS_FILE = DATA_DIR / 'daily_report.json'
# 同步分两段:**抓取那一段按通道分,入库那一段不分**。
# 只有 crawl / export 认 crawl_filter;`_run_index` 是全量扫 vault、`_run_extract`
# 扫的是全库待提取队列,两个函数都不收通道参数,压根没有"只做这个通道那一份"的
# 概念。所以挨个通道各跑一遍四段,等于把同一件全库的活重做 N 遍(5 个通道 = 5 次
# 全量扫描、5 次抢维护锁),而被处理的文档一篇不多一篇不少。
#
# **合并不会绕开任何一道闸**:哪些内容要买提取由**订阅级**开关回答
# (`kb.extract.pending_docs` 是唯一口径,feeds 那几类默认不提取),拦它的不是
# "这一轮由哪个通道起的"。条目化/wiki 两个重活仍旧不跑——日报不读它们的产物,
# 跑一遍要几十分钟。
FETCH_STAGES = ['crawl', 'export']
DIGEST_STAGES = ['index', 'extract']
# 入库那一段在成绩单上的身份。它不属于任何一个来源,所以 channel 留空 ——
# `_sync_issues`、日报正文那几行、面板横幅都只读 label,不认通道 id。
DIGEST_LABEL = '入库处理（索引与提取）'
# 抓取那一段的时间预算。超了就请求管道停下、跳过剩余通道,拿已有内容出日报
# ——"今天的日报没出来"比"日报少了两个来源"严重得多。
SYNC_BUDGET = int(os.environ.get('KB_DAILY_SYNC_BUDGET', str(3 * 3600)))
# 入库那一段的保底预算。**抓取超预算时它照样要跑**:新导出的文件没进索引,
# `daily.generate` 就查不到(它取材自 kb.db 的 documents),这一轮抓回来的东西会
# 整个落到这份日报外面 —— 那正是超预算本来想避免的"白跑一趟"。
DIGEST_BUDGET = int(os.environ.get('KB_DAILY_DIGEST_BUDGET', str(1800)))
# 管道被别人占着时,等它空出来的上限(每个通道各算各的)。
WAIT_BUSY = 900
# 定时那一轮临时失败后,隔多久重试、同一天最多补几次。
RETRY_AFTER = timedelta(minutes=30)
MAX_RETRIES = 2


def _new_namespace() -> str:
    """这个进程的身份段。**在模块加载时取一次**,重启就换一个。"""
    return uuid.uuid4().hex[:8]


# 给每一轮发一个身份,面板拿它认"有没有出现一轮新的已结束运行"。两件事:
# ①**不能拿 `finished_at` 当身份** —— 它只精确到秒,而这一档治的正是"跑得太快、
#   页面一次都没见过 running=true"那种运行;
# ②**光一个进程内计数器不够,身份得跨重启也唯一。** 计数器在模块加载时归零,
#   而旧页面手里还攥着重启前那个 id:重启之后紧接着生成一份(`./kb.sh restart`
#   之后手点一次同步正是日常路径),新那一轮又叫 `daily-1`,和页面记着的逐字
#   相同 —— 于是正文永远不刷新,新文章要整页刷新才看得见(审查实测:重启 + 生成
#   都落在一次空闲轮询的 15 秒窗口里,页面连中间那个空状态都没读到)。所以前面
#   再加一段**进程启动时现取的**命名空间。
_RUN_NS = _new_namespace()
_run_seq = 0


# ── 设置 ────────────────────────────────────────────────────────────────────────

def _channel_labels() -> dict[str, str]:
    from kb.collectors import collection_channels
    return {c['id']: c['label'] for c in collection_channels()}


def default_channels() -> list[str]:
    """默认同步范围:公众号 + 全部订阅类来源。"""
    from kb.collectors import active_collector
    from kb.source_types import feed_types
    return [active_collector().name, *feed_types()]


def _raw_settings() -> dict:
    """盘上那份原样内容(补齐缺省值),不去问采集器注册表。

    定时线程每 30 秒读一次,只为判断"到点没有";把通道校验也放进来的话,
    每一跳都要把所有采集器模块导入一遍,而绝大多数跳什么都不做。

    **读不出来就抛 `ScheduleUnreadable`,不许降级成"没开"**(见
    `kb.config.read_config_file`):原来 `OSError` / `JSONDecodeError` 一律吃成
    空 dict,派生出 `enabled=False`,API 还带着 200 回去 —— 第 1 轮实测把两个
    配置文件 chmod 000,两处界面都写「未启用」、来源说明断言「目前不会自动同步」,
    而恢复权限之后两条定时原样开着。没有这个文件(还没设置过)照旧是空 dict。
    """
    d = read_config_file(SETTINGS_FILE, '日报定时设置')
    return derive_config('日报定时设置', lambda: _settings_from(d))


def _settings_from(d: dict) -> dict:
    """原样内容 → 带缺省值的设置。字段里的乱值由 `derive_config` 归一成
    `ScheduleUnreadable`(见那边:不归一会把另一条健康的定时一起停掉)。"""
    return {'enabled': config_flag(d, 'enabled'),
            'time': config_clock(d, 'time', '08:00'),
            'sync': config_flag(d, 'sync', True),
            # 一份日报都没有、接不上任何截止时刻时回溯几天。老配置里这个键
            # 叫 days/max_days(当时是"覆盖最近几天"和"最长回溯几天"),窗口
            # 改成一律接上一份日报的截止时刻之后,它只剩"第一份回溯几天"。
            'first_days': max(1, min(7, int(d.get('first_days') or d.get('max_days')
                                            or d.get('days') or 0) or daily.FIRST_DAYS)),
            'channels': config_names(d, 'channels'),
            'last_fired': config_day(d, 'last_fired'),
            'fail_day': config_day(d, 'fail_day'),
            'fail_count': int(d.get('fail_count') or 0),
            'next_run': config_stamp(d, 'next_run')}


def load_settings() -> dict:
    from kb.collectors import known_channels
    d = _raw_settings()
    known = _channel_labels()
    # known_channels 先把存量里那条退役的公众号通道名换成接替它的那条:
    # 盘上那个旧名字不能被当成"不认识的通道"丢掉——丢掉等于公众号从此不进日报。
    channels = [c for c in known_channels(d['channels']) if c in known]
    # 通道被删掉/改名之后,存下来的那份可能一个都不认识了。回落到默认范围,
    # 而不是留一份空列表——那会让定时日报每天都跑出"一个来源都没同步"。
    return {**d, 'channels': channels or [c for c in default_channels() if c in known]}


def save_settings(enabled: bool, time_str: str, channels: list[str] | None = None,
                  sync: bool = True, first_days: int = daily.FIRST_DAYS) -> dict:
    if not re.fullmatch(r'([01]\d|2[0-3]):[0-5]\d', time_str or ''):
        raise ValueError(f'时间格式应为 HH:MM,收到 {time_str!r}')
    if not 1 <= int(first_days) <= 7:
        raise ValueError('第一份日报的回溯天数须在 1 到 7 天之间')
    from kb.collectors import known_channels
    known = _channel_labels()
    picked = [c for c in known_channels(channels or []) if c in known]
    if channels and not picked:
        raise ValueError('同步范围里没有一个可用的来源通道')
    with _lock:
        cur = load_settings()
        now = datetime.now()
        hour, minute = map(int, time_str.split(':'))
        nxt = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        # 保存只安排下一次,绝不"补跑"今天已经过去的时刻;同一天也只跑一次。
        if nxt <= now or cur['last_fired'] == now.strftime('%Y-%m-%d'):
            nxt += timedelta(days=1)
        cur.update({'enabled': bool(enabled), 'time': time_str, 'sync': bool(sync),
                    'first_days': int(first_days), 'channels': picked or cur['channels'],
                    'next_run': nxt.isoformat(timespec='minutes') if enabled else ''})
        # 原子替换:保存失败不许把上一份有效设置截成空文件(见
        # `kb.config.write_config_file`)。
        write_config_file(SETTINGS_FILE, cur)
        return cur


def _write_settings(**changes) -> None:
    """改定时状态的那几格(last_fired / next_run / 失败计数)并落盘。

    **调用方必须已经持着 `_lock`,而且从"算出要写什么"到这里是同一个临界区。**
    不然就有这条(第 5 轮用真实工作线程确定性复现):起跑那一步算好了「下一次 =
    今天 08:00」,还没写进去,工作线程先收尾、在锁里把配置写成「明天 07:30」;
    这一步再拿着那份更旧的参数写一遍,盘上就倒退成今天 08:00 —— 当天由账本的
    done 挡住看不出异常,**第二天 00:05 那一跳就判到期**,凌晨跑出一份日报,
    到了 07:30 反而因为"今天已经跑过"不再生成。
    原子替换只保证一份文件不会写一半,**保证不了两次更新的先后**。
    """
    if not _lock._is_owned():          # noqa: SLF001 - RLock 自带的持有判断
        raise RuntimeError('改定时状态要在 _lock 里:判断、账本、算参数、写盘'
                           '必须是同一个临界区(见本函数 docstring)')
    with _lock:
        try:
            cur = _raw_settings()
        except ScheduleUnreadable as e:
            # **读不出来就一个字都不改。** 拿缺省值当底打一份出去,等于把用户的
            # 开关、时刻、同步范围静默换成缺省 —— 那是不可恢复的。代价只是
            # 这一跳的 last_fired / next_run 没记上(最多今天再试一次)。
            announce_config_issue('日报定时设置', f'{e};这一次不改它')
            return
        cur.update(changes)
        try:
            write_config_file(SETTINGS_FILE, cur)
        except OSError as e:
            # 同 `pipeline._mark_fired`:这是定时自己的回写(last_fired /
            # next_run / 失败计数),抛出去会把这一跳算成"出错",而"今天到此为止"
            # 已经记在进程里了。盘上那份原封不动,说一句就够。
            announce_config_issue('日报定时设置',
                                  f'定时状态没能写进磁盘({e});本进程内照常,'
                                  '重启后可能再跑一轮')


# 日报定时这一轮的事实(起过没有、下一次最早什么时候、补了几次、今天到此为止没有)
# 记在进程里,盘上那份配置只是副本 —— **四格都不许用"回写没成功"反过来证明它没发生**。
# 为什么、以及只认盘上那份的三种事故形态,见 `kb.sched_ledger`。
_book = Ledger(lambda: SETTINGS_FILE)      # 跟着 SETTINGS_FILE 走,换库作废


def _report_done(day: str) -> bool:
    """今天这份日报已经落盘了吗。**重启之后唯一还作数的证据。**

    产物比配置里那个标记更硬:`last_fired` 可能根本没写上(写失败、进程在写之前
    被杀),而文件在不在是明摆着的。查不出来就说"不知道"(False),让定时照常跑 ——
    宁可多跑一轮,不要因为一次看不了目录就把今天的日报整个跳掉。
    """
    try:
        done = daily.report(day) is not None
    except Exception as e:     # noqa: BLE001 - 见上:查不出来不许当成"已经有了"
        # 这是重启之后唯一还作数的那条证据没法问了。**不默默当成"没跑过"** ——
        # 真跑过的话这一跳会再跑一整轮,留一句才查得出来是为什么。
        announce_config_issue('今天这份日报', f'查不出来({type(e).__name__}: {e});'
                                              '按"还没有"处理,可能多跑一轮')
        return False
    announce_config_issue('今天这份日报', '')
    return done


def _at(day: datetime, time_str: str) -> str:
    return day.strftime('%Y-%m-%d') + 'T' + time_str


def _mark_attempt(now: datetime, day: str) -> None:
    """定时起了一轮:把下一跳推到重试时刻,**不记"今天已出报"**。

    记"已出报"要等真的出来了(`_finish_schedule`)。在这里就记的话,这一轮
    因为模型 502 之类的临时故障失败,当天再也不会自己试第二次,用户一整天
    等不到日报、面板上也只有一句失败原因。

    **账本先推进,再去写盘。** 盘上那份写不进去是常事(配置一时读不出、满盘),
    而"下一次最早什么时候"跟着写失败一起丢掉的话,30 秒后这一跳又判到期 ——
    失败的日报会每 30 秒重起一轮,各花一次模型钱(第 3 轮实测 3 分钟 7 轮)。

    **判断 done、推进账本、算参数、写盘必须在同一把锁里**(见 `_write_settings`):
    中间放开锁的话,工作线程的收尾能插进来,然后这一步拿更旧的计划把它盖回去。
    """
    with _lock:
        book = _book.book(day)
        if book.done:
            # 这一轮已经收尾了(出报成功 / 补满次数):它写的 last_fired 和"明天那
            # 一格"才是最新的,这里再写一次会把明天那一格盖成今天 + 30 分钟。
            return
        book.attempt((now + RETRY_AFTER).isoformat(timespec='minutes'))
        _write_settings(next_run=book.next_due)


def _finish_schedule(day: str, retry: bool) -> None:
    """定时这一轮收尾。retry=True 表示临时故障,同一天内补几次;False 表示
    今天到此为止(出报成功、没有素材、或者用户自己按了停止)。

    **这一轮的事实先记进账本,再去写盘。** 盘上那三格(`last_fired` / `fail_count` /
    `next_run`)写不进去是常事(配置一时读不出、满盘),而只认它们的话:今天跑完了会
    被当成"还没跑过"再跑一整轮;补跑次数每次都从 0 算起,`MAX_RETRIES` 永远到不了;
    退避也一起丢掉。见 `kb.sched_ledger`。
    """
    with _lock:
        now = datetime.now()
        # 跨零点的那一轮在第二天才收尾:它的 day 比账本里那天早,这时**不许把账本
        # 挪回去**(挪回去等于把今天的退避清掉),只照常写盘。
        book = _book.book(day) if day >= _book.book().day else None
        try:
            cur = _raw_settings()
        except ScheduleUnreadable as e:
            # 读不出来就别拿缺省值当底写回去(见 `_write_settings`);但下面这些事实
            # 照样记进账本,否则下一跳会再跑一轮、而且把这次失败当成第一次。
            cur = None
            announce_config_issue('日报定时设置', f'{e};这一次不改它')
        # 补到第几次:盘上那个和账本里那个**取大的**。只认盘上那个的话,回写失败之后
        # 每一次失败都是"第一次",补跑永远停不下来(第 3 轮实测)。
        on_disk = cur['fail_count'] if cur and cur['fail_day'] == day else 0
        count = (book.retries(on_disk) if book else on_disk) + (1 if retry else 0)
        done = not retry or count > MAX_RETRIES
        later = (now + RETRY_AFTER).isoformat(timespec='minutes')
        if book:
            # done 之后把退避清空:到期判据回落到盘上那个"明天"那一格,而今天再不再
            # 起由 `done` 自己挡(盘上那格要是也没写进去,挡住的就是这一格)。
            book.finish(count if retry else 0, done, '' if done else later)
        if cur is None:
            return
        _write_settings(
            last_fired=day if done else cur['last_fired'],
            fail_day=day if retry else '', fail_count=count if retry else 0,
            next_run=(_at(datetime.strptime(day, '%Y-%m-%d') + timedelta(days=1), cur['time'])
                      if done else later))


STOP_IDLE = '当前没有正在运行的日报任务'
STOP_GENERATING = ('日报正在生成,这一阶段不能中途停止:模型已经在出稿,'
                   '这一轮会照常保存(需要的话生成完可以删掉这一份)')
STOP_OK = '当前阶段结束后停止,这一轮不生成日报;已同步的内容会保留'


def request_stop() -> tuple[bool, str]:
    """停下这一轮日报:剩下的通道不同步了,**日报也不生成**。

    返回 `(受理了没有, 说明)`。两种不受理:没有在跑的日报;已经进了生成阶段
    ——那时候模型钱已经花出去了,`daily.generate()` 自己把文件写完才返回,
    受理下来只会承诺一件做不到的事(界面上写着"这一轮不生成日报",实际照样
    多出一份日报,2026-09-30 审查实测)。所以那一档如实拒掉、按钮也置灰。

    只停自己起的那一轮管道:在别人的管道后面排队等的时候按停止,只结束我们
    自己的等待。
    """
    from kb import pipeline as pl
    with _lock:
        if not _state['running']:
            return False, STOP_IDLE
        # 这个判断和 `_run` 里"进入生成阶段"那一步共用 `_lock`,所以两者严格
        # 二选一:要么停止先落下、生成整段跳过,要么生成已经开跑、停止被拒。
        if _state['phase'] == 'generate':
            return False, STOP_GENERATING
        mine = _state['pipeline_run']
        if not _state['stop_requested']:
            _state['stop_requested'] = True
            _log('已请求停止:当前阶段结束后不再继续,这一轮不生成日报')
    # 带上 id:我们那一轮可能已经收尾、现在跑的是别人起的同步,那就不该停它。
    # 这个判断交给 pipeline 在它自己的锁里做,免得"读状态"和"去停"之间又开一道缝。
    if mine:
        pl.request_stop(mine)
    return True, STOP_OK


# ── 执行 ────────────────────────────────────────────────────────────────────────

def _wait_idle(deadline: float, halted=None) -> bool:
    """等管道空下来。True = 空了;False = 等超时或被要求停止。"""
    from kb import pipeline as pl
    halted = halted or _halting
    while pl.get_state()['running']:
        if halted() or time.monotonic() > deadline:
            return False
        time.sleep(2)
    return True


def _sync_round(stages: list[str], crawl_filter: dict | None, label: str,
                deadline: float, channel: str = '', halted=None,
                batch: str = '') -> dict:
    """跑一轮管道并等它结束,返回这一轮在成绩单上的那条记录。

    `channel` 只是成绩单上的身份:抓取那一段按通道一轮一个,入库那一段是全库的
    一次、不属于任何通道(空串)。判红只看 `stages` 里点名的那几个阶段。

    `batch` 原样传给 `start_pipeline`:带着它的几轮算同一个业务批次,每一轮收尾
    都由结果存储层把整批合并着落盘。**这里不再自己攒结果** —— 账本由 `_end_run`
    喂,所以"没亲眼看到自己那一轮收尾"的那一档(被下一轮挤出记录)照样进账,
    不像这个函数还得去 `run_result` 找回来。

    `halted` 是"要不要半路收手"的判据,默认 `_halting`(停止 + 超预算都算)。
    入库那一段传的是**只认用户停止**的那一份:超预算的本意是"别再抓了,拿已经
    抓到的内容出报",而不入库的话这一轮抓回来的东西连 `daily.generate` 都查不到
    (它取材自 kb.db 的 documents),那正好把它要保的东西丢了。
    """
    from kb import pipeline as pl

    halted = halted or _halting
    entry = {'channel': channel, 'label': label, 'status': 'pending', 'detail': ''}
    _set('sync', f'{label}:等待同步管道空闲')
    if not _wait_idle(min(deadline, time.monotonic() + WAIT_BUSY), halted):
        entry.update(status='skipped', detail='同步管道一直被占用,本轮跳过')
        return entry
    run = pl.start_pipeline(stages, crawl_filter=crawl_filter, batch=batch)
    if not run:
        entry.update(status='skipped', detail='同步管道已被其它任务占用,本轮跳过')
        return entry
    with _lock:
        _state['pipeline_run'] = run
    # 停止请求正好落在"等到空闲"和"起跑"之间时,request_stop 那一刻还不归我们,
    # 它不会去停这一轮;补一次,免得用户按了停止还要眼看着它跑完整个通道。
    if halted():
        pl.request_stop(run)
    _set('sync', f'{label}:同步中')
    # 只认 run id 相同的快照。两秒一跳的轮询里,我们那一轮跑完、另一项同步
    # 马上接手全局管道是完全可能的:只看 running 的话看到的是连绵不断的 True,
    # 既不知道已经换人,还会把别人的阶段结果当成自己这一轮的成绩单。
    #
    # `mine` 只装**终态**(自己那一轮 running 已经放下的那份快照)。中途快照不算:
    # 那时阶段还是 running/pending,拿它数 error 一个都数不出来,于是"被接替"和
    # "跑完了还全是绿的"长得一模一样——报成功却没有任何证据。被接替的那条路统一
    # 去问管道按 id 留的收尾记录。
    mine: dict | None = None
    over = False
    while True:
        snapshot = pl.get_state()
        if snapshot.get('run_id') != run:
            break                    # 我们那一轮已经收尾,现在跑的是别人的
        if not snapshot['running']:
            mine = snapshot          # 正常收尾:这份快照就是最终结果
            break
        stage = snapshot.get('current_stage') or ''
        detail = (snapshot['stages'].get(stage) or {}).get('detail') or ''
        _state['detail'] = f'{label}:{detail}' if detail else f'{label}:同步中'
        # `over` 这个一次性开关**不能省**:原来靠 `not _halting()` 兼任"只说一次",
        # 而入库那一段的 `halted` 不认超预算 —— 没有它的话,超时之后每两秒就会
        # 重新 request_stop 一次、日志里刷同一行。
        if not over and time.monotonic() > deadline and not halted():
            over = True
            _log(f'{label}:超出时间预算,请求停止后直接生成日报')
            _state['over_budget'] = True
            pl.request_stop(run)
        time.sleep(2)
    with _lock:
        if _state['pipeline_run'] == run:
            _state['pipeline_run'] = ''
    # 没能亲眼看到自己那一轮收尾(它跑得比下一跳还快,接替者已经顶上来):
    # 管道按 run id 留了收尾快照,照 id 去取自己那一份。
    if mine is None:
        mine = pl.run_result(run)
    if mine is None:
        # 连收尾记录都没有(被更多轮挤出去了)。**这一档不许算成功**:我们既没
        # 看到 run-1 的任何阶段状态,也不知道它是跑完了、失败了还是被停的,写
        # 一句"同步完成"等于拿一份没人证明过的结果去记账(2026-09-30 第 3 轮
        # 审查实测)。如实记未知,日报照样生成——素材是库里已有的那些,不因为
        # 一个通道的结果查不到就整轮不出。
        entry.update(status='unknown',
                     detail='没拿到这一轮的同步结果(管道已被下一轮运行接替),'
                            '成败未知')
        _log(f'{label}:{entry["detail"]}')
        return entry
    # 只看这一轮请求了的那几个阶段:没选中的阶段一开跑就落 skipped(见
    # `pipeline.start_pipeline`),全收进来的话每一轮都要多报几句"条目化/
    # wiki 未执行"。
    graded = [(name, mine['stages'].get(name) or {}) for name in stages]
    bad = [f'{name} {s.get("detail") or "失败"}'
           for name, s in graded if s.get('status') == 'error']
    # skipped / stopped = 这个阶段该做的事一件没做,**不能跟着算成功**。
    # 2026-09-30 实测:51 个大V账号全都缺 X 桥接配置,crawl 因此整个落 skipped
    # (「待配置 51 个」),而这里只数 error —— 那一轮 social 通道报的是绿色的
    # 「同步完成」,日报里那行只剩下 extract 的结果,"一条都没抓到"这件事连同
    # 原因一起看不见。超预算/被停止时管道落 stopped,同理。
    idle = [f'{name} {s.get("detail") or "本轮未执行"}'
            for name, s in graded if s.get('status') in ('skipped', 'stopped')]
    entry.update(status='error' if bad else 'skipped' if idle else 'ok',
                 detail='；'.join(bad + idle)[:400] if bad or idle else '同步完成')
    _log(f'{label}:{entry["detail"]}')
    return entry


def _run(since: datetime | None, until: datetime | None, sync: bool,
         channels: list[str], first_days: int) -> None:
    sched_day, retry, publish_issue = _state['sched_day'], False, ''
    try:
        if sync:
            from kb import pipeline as pl     # 延迟:它反过来 import 本模块
            labels = _channel_labels()
            budget_end = time.monotonic() + SYNC_BUDGET
            # 这一轮日报是 N+1 次管道运行,而「最近一次运行」只有一个位子。开一个
            # **业务批次**,之后每一轮子轮收尾都由结果存储层把整批合并着写进去
            # (见 `pipeline_state._publish_in_batch`)—— 盘上那份于是始终等于
            # "已经收尾的这几轮合起来",进程在任何一刻退出都不会把前几轮的抓取
            # 失败和失败名单弄丢。**这件事不能由这里事后补**:子轮收尾和我们下一
            # 句之间进程就可能没了。
            batch = pl.open_batch()
            for channel in channels:
                if _halting():
                    _state['channels'].append(
                        {'channel': channel, 'label': labels.get(channel, channel),
                         'status': 'skipped',
                         'detail': '已停止,未同步' if _state['stop_requested']
                                   else '超出时间预算,未同步'})
                    continue
                _state['channels'].append(
                    _sync_round(FETCH_STAGES, {'channel': channel},
                                labels.get(channel, channel), budget_end, channel,
                                batch=batch))
            # 入库那一段:等所有通道都抓完才跑,跑一次 —— 这时待索引的文件和
            # 待提取的队列都最全。**用户按了停止才跳过**:接下来本来就不出报了。
            # 超预算照跑(见 DIGEST_BUDGET),所以这里不能用 `_halting()`。
            if _state['stop_requested']:
                _state['channels'].append(
                    {'channel': '', 'label': DIGEST_LABEL, 'status': 'skipped',
                     'detail': '已停止,未入库'})
            else:
                _state['channels'].append(_sync_round(
                    DIGEST_STAGES, None, DIGEST_LABEL,
                    max(budget_end, time.monotonic() + DIGEST_BUDGET),
                    halted=lambda: bool(_state['stop_requested']), batch=batch))
            # 发布不成不是日报的失败(素材都在库里,照常出报),但**不许咽下去**:
            # 咽下去的后果是同步中心那边看不到这一轮的失败、也点不了重试,而日报
            # 这边一句"已生成"看着一切正常。
            publish_issue = pl.close_batch()
            if publish_issue:
                _log(f'同步结果未发布:{publish_issue}')
        # 用户按的停止到此为止:接着生成等于把一份"同步到一半"的日报当成功交出去,
        # 还要再花一次模型钱。超预算是另一回事——那本来就是"别再同步了,拿现有的
        # 内容出报",继续生成正是它的目的。
        # 判断和"进入生成阶段"必须在同一把锁里:分两步的话,停止请求正好挤在中间
        # 时它既看不到 phase=generate(于是受理、承诺不出报)、又赶不上这道检查
        # (于是照样生成),承诺和行为当场对不上。
        with _lock:
            if _state['stop_requested']:
                _set('stopped', '已按请求停止,这一轮没有生成日报')
                return
            _set('generate', '正在生成日报')
        # 窗口的截止时刻要在**同步跑完之后**才定下来(没显式指定时 generate 自己
        # 取 now):日报覆盖的是"到生成这一刻为止",同步花掉的那几小时里产出的
        # 内容也算数。开跑时就把 until 钉死的话,那几小时会掉进两份日报的缝里。
        issues = _sync_issues()
        res = daily.generate(since=since, until=until, first_days=first_days,
                             progress=lambda text: _set('generate', text),
                             sync_issues=issues)
        _state.update(result=res, date=res['date'], since=res['since'], until=res['until'])
        # 完成这句话要把同步缺口说出来。"日报已生成"配一个绿勾、外加一份看起来
        # 完整的日报,用户没法区分"全都同步到了"和"一个来源都没同步到、直接拿
        # 旧素材出报"——后者实测就长这样(2026-09-30 审查:三个来源全部连接失败)。
        note = f"日报已生成:{res['docs']} 条素材"
        if res['late']:
            note += f"(另有 {res['late']} 条补录)"
        if issues:
            note += (f";{len(issues)} 项未完成("
                     + '、'.join(i['label'] for i in issues) + ')')
        # 记账没写成不该糊过去:日报是出了,但这一天的素材会在下一份里重复出现
        # 一遍(`daily.generate` 的失败方向,见那一段的注释)。只写进 log 的话
        # 面板上那句收尾文案仍是一句干净的"已生成",用户看不出这一轮是降级完成的。
        if publish_issue:
            note += f'；{publish_issue}'
        if res.get('carry_error'):
            note += f";上报记录没写进库({res['carry_error']}),这一天的素材会在下一份里重复出现"
        _set('done', note)
    except LookupError as e:
        # "这段时间没有新内容"不是故障,重试多少次都是同一个结果。
        _state['error'] = str(e)
        _set('failed', str(e))
    except Exception as e:                       # noqa: BLE001 - 面板要看到原因
        retry = True
        _state['error'] = str(e)
        _set('failed', f'日报生成失败: {e}')
    finally:
        # 批次一定要关掉:半路抛出来的时候 `close_batch` 那一句走不到,而开着的
        # 批次会把下一轮管道运行算进一个早就结束的批次(`_publish_in_batch` 还认
        # `_state['batch']`,所以漏关不至于出错 —— 但也没有理由把它留着)。
        from kb import pipeline as pl
        pl.close_batch()
        with _lock:
            _state['pipeline_run'] = ''
            _state['finished_at'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            _save_last_run()
            _state['running'] = False
        if sched_day:
            _finish_schedule(sched_day, retry)


def start(since: datetime | None = None, until: datetime | None = None,
          sync: bool = True, channels: list[str] | None = None,
          first_days: int | None = None, trigger: str = 'manual',
          sched_day: str = '') -> bool:
    """起一轮日报。已经在跑返回 False。

    `since`/`until` 都不给时由 `daily.next_window()` 接着上一份日报算,那是
    日常路径;显式给窗口是为了补一份旧的、或者重算一段。`sched_day` 是定时
    那一跳的日期(定时专用):这一轮的成败要记回那一天,而不是记在收尾时刻
    所在的那一天——同步跨过零点的话两者差一天,记错会把明天那一跳也吃掉。
    """
    from kb.collectors import known_channels
    settings = load_settings()
    known = _channel_labels()
    picked = [c for c in known_channels(
        channels if channels is not None else settings['channels']) if c in known]
    if since and until and since >= until:
        raise ValueError('日报的起始时刻必须早于截止时刻')
    # 勾了"先同步"却一个来源都没选,同步阶段会静静地什么都不做,面板上仍旧
    # 一路"成功"——"同步全部并生成"被悄悄降级成"不同步直接出报",用户不知道
    # 素材根本没刷新(2026-09-30 审查实测)。要么给个来源,要么明确走不同步那条。
    if sync and not picked:
        raise ValueError('同步范围里一个可用来源都没有:先勾选至少一个,'
                         '或者改用不同步那颗生成按钮(只用已入库的内容出报)')
    with _lock:
        if _state['running']:
            return False
        global _run_seq
        _run_seq += 1
        _state.update({
            'running': True, 'phase': 'starting', 'detail': '',
            'run_id': f'daily-{_RUN_NS}-{_run_seq}',
            'date': until.date().isoformat() if until else '',
            'since': since.strftime(daily.STAMP) if since else '',
            'until': until.strftime(daily.STAMP) if until else '',
            'sync': bool(sync), 'trigger': trigger, 'error': '', 'log': [],
            'stop_requested': False, 'over_budget': False, 'sched_day': sched_day,
            'channels': [], 'result': None,
            'started_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'), 'finished_at': '',
        })
        window = f'{since:%m-%d %H:%M} 起' if since else '接上一份日报'
        _log(f'开始生成日报({window})'
             + (f',先同步 {len(picked)} 个来源' if sync else ''))
    threading.Thread(target=_run, args=(since, until, bool(sync), picked,
                                        first_days or settings['first_days']),
                     daemon=True).start()
    return True


# ── 定时 ────────────────────────────────────────────────────────────────────────

def tick() -> bool:
    """由 kb.pipeline 的调度线程每 30 秒调一次。到点了就起一轮,返回是否起了。

    **读不出设置就跳过这一跳并留原因**,不许按"没开"处理:那是把一个故障
    说成"用户关掉了开关",而日报从此不再出现,面板上一切正常。
    """
    with _lock:
        try:
            s = _raw_settings()
        except ScheduleUnreadable as e:
            announce_config_issue('日报定时设置', f'{e};这一跳跳过,定时暂停')
            return False
        announce_config_issue('日报定时设置', '')
        if not s['enabled'] or _state['running']:
            return False
        now = datetime.now()
        today = now.strftime('%Y-%m-%d')
        book = _book.book(today)
        # **到期时刻取盘上那个和账本里那个更晚的一个。** 失败之后的退避是写在
        # `next_run` 上的,那一次回写失败了的话盘上还停在今天的定时时刻,只认它就是
        # 用"回写没成功"证明"已经可以再试了"(第 3 轮实测:30 秒一轮)。
        due_at = book.due_at(s['next_run'])
        due = (now.isoformat(timespec='minutes') >= due_at if due_at
               else now.strftime('%H:%M') >= s['time'])
        if not due:
            return False
        # **"今天到此为止"有三个来源,任一成立就不再起**:盘上那个 last_fired、
        # 账本里那一格、以及今天这份日报是不是已经落盘了。只认配置里那个标记的话,
        # 一次写失败就会让同一天再自动跑一整轮(还要再花一次模型钱)。
        if book.done or today == s['last_fired'] or _report_done(today):
            return False
        # **退避是"受理过一轮"的记账,不是"试过一下"的记账。** 下面这两步都可能
        # 一轮都没起来:起跑前还要再读一次设置(可能一时读不出),`start` 自己也会
        # 拒(别人占着、一个来源都没剩)。在它们之前就把下一次推后 30 分钟的话,
        # 一次瞬时失败就把今天的日报推迟半小时,而本来下一跳(30 秒后)就能恢复。
        try:
            settings = load_settings()
        except ScheduleUnreadable as e:
            announce_config_issue('日报定时设置', f'{e};这一跳跳过,定时暂停')
            return False
        # **受理和记账在同一个临界区里。** 放开锁再记的话,`start` 起的那条工作线程
        # 能在"算好下一次"和"写进去"之间收尾,于是更旧的计划把它的收尾计划盖回去
        # (第 5 轮真实线程实测:第二天 00:05 就跑出一份日报)。工作线程一进来就要
        # 这把锁(`_set` / `_log` / `_finish_schedule`),所以它只会排在这之后。
        # 定时这一轮不指定窗口:起点接上一份日报的截止时刻,终点是同步跑完的那一刻。
        started = start(sync=settings['sync'], channels=settings['channels'],
                        first_days=settings['first_days'],
                        trigger='schedule', sched_day=today)
        if started:
            _mark_attempt(now, today)      # 受理了才记
        return started
