# -*- coding: utf-8 -*-
"""公众号采集器:实现 kb.collectors 的 SourceCollector 契约。

绿泡泡特有的一切都关在这个文件里——文章怎么发现、反爬要等多久、先落 SQLite
再转 md,管道一概不知情。

采集器后端是个**一次性 CLI**,不是常驻服务:每次调用起一个子进程,跑完就退。
进度靠子进程的 stdout 逐行送回来(每行一个 JSON 事件),比轮询 HTTP 状态端点
少一个常驻进程、少一层状态缓存,也不会出现"轮询到上一轮的旧状态"那类问题。

文章从**本机绿泡泡客户端已经同步下来的推送**里发现(只读那个库),正文再按
链接公开抓取。曾经还有一条"逐号文章凭证"的通道(profile_ext,凭证靠 mitm 代理
从客户端流量里截):它要人去 Mac 绿泡泡里一个号一个号地打开文章,凭证二十分钟
就不新鲜,而且抓的是同一批号、写进同一套库——留着就得在每个入口上维持一个
二选一。它整条搬去 wechat-oa 分支了,主干只剩这一条。
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import time
from pathlib import Path

from kb.collectors import (CollectorError, CollectorTimeout, Coverage,
                           DropIncomplete, Fetched, FetchScope, SourceStatus,
                           Target)
from kb.pipeline_state import _log, _stage

# 采集器在本仓库内(<repo>/collector)。刻意用相对于本文件的路径而不是绝对路径:
# 开 worktree 时它会自动指向那个 worktree 里的 collector,改采集器的代码
# 才真的被隔离——写死绝对路径的话,worktree 里跑的仍是主仓库那一份。
# 数据与 venv 不在版本库里(见根 .gitignore),新 worktree 要先跑一次
# collector/setup.sh 把它们链过去。
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
BACKEND_DIR = Path(os.getenv("KB_COLLECTOR_DIR", "").strip() or _REPO_ROOT / "collector")
BACKEND_PY = BACKEND_DIR / '.venv' / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')

# 采集产物落在 vault 里的 source 名(历史上换过两次,一并认)
VAULT_SOURCES = ("dr-gzh", "wechat", "gzh")

# 状态缓存时长。面板每 1.5 秒轮询一次,而每次 status 都要起一个 Python 子进程、
# 让它去探一次本机那个库读不读得动——不缓存就是每秒起进程。这个结论本来也是
# 分钟级才变的东西(人去开一次绿泡泡、读取器掉线)。
_STATUS_TTL = 30.0


def _backend_problem() -> str:
    """采集器没装好就返回一句"缺什么、怎么补",装好了返回空串。

    `collector/data`(文章库)和 `collector/.venv` 都在 .gitignore 里,是
    `collector/setup.sh` 链到仓库外那份真实目录的软链接——`git worktree add`
    出来的新工作树只有源码,清过一次 untracked 文件的旧目录也会把它们清掉。
    缺哪个都不是"配置不全"而是根本跑不了:没 .venv 就没有 requests/lxml,
    没 data 就没有文章库。
    """
    if not BACKEND_DIR.is_dir():
        return f"找不到采集器目录,预期在 {BACKEND_DIR}"
    executable = '.venv/Scripts/python.exe' if os.name == 'nt' else '.venv/bin/python'
    missing = [shown for shown, probe in ((".venv", executable), ("data", "data"))
               if not (BACKEND_DIR / probe).exists()]
    if missing:
        if os.name == 'nt':
            return f"缺 {'、'.join(missing)}——在仓库根目录运行 .\\kb.cmd setup 准备 Windows 采集器"
        return (f"缺 {'、'.join(missing)}——在仓库根目录跑一次 "
                f"./collector/setup.sh 把它们链好")
    return ""


def _python() -> str:
    """采集器自己的解释器。

    别在这里退回 `sys.executable`:那是知识库的解释器,装的是 kb 的依赖,
    拿它跑 `-m cli` 一定死在 `import requests` 上——用户看到的是一句
    ModuleNotFoundError 的 traceback 尾巴,而真正该说的是"跑一次 setup.sh"。
    缺 venv 由 _backend_problem() 在起进程之前拦下并说清楚。
    """
    return str(BACKEND_PY)


def _run_cli(*argv: str, timeout: int) -> subprocess.CompletedProcess:
    options = {'text': True, 'timeout': timeout}
    if os.name == 'nt':
        options.update(encoding='utf-8', errors='replace',
                       env={**os.environ, 'PYTHONIOENCODING': 'utf-8', 'PYTHONUTF8': '1'},
                       creationflags=subprocess.CREATE_NO_WINDOW)
    return subprocess.run(
        [_python(), "-m", "cli", *argv],
        cwd=BACKEND_DIR, capture_output=True, **options,
    )


def _receipt(event: dict) -> frozenset[Fetched] | None:
    """子进程那份「写了哪几篇」换算成带身份的回执。换算不了返回 None。

    事件里是 `out`(导出根目录的绝对路径)+ `items`(每篇一条
    `{"id", "round", "name"}`,name 是 `<天>/<文件名>`),两样都由
    `collector/cli.py` 的 `cmd_export` 从导出脚本自己打的行里收上来。

    身份两件事各有出处:`id` 是 `articles.id`(稳定内容身份,挂上采集器名字做
    前缀免得和别的来源撞),`round` 是 `article_fetch_rounds` 里那一行 ——
    发现这一篇的那一次业务抓取。`round` 空串是「说不清」(这张表建出来之前就在库里的那些文章、手工
    导入的链接),不许当成「不是本次」。

    **`out` 不在 vault 里就返回 None**:那说明采集器被配去写别处(测试夹具、
    两边 `KB_VAULT_ROOT` 对不上),算出来的相对路径不在知识库的取材范围里 ——
    而范围过滤正是按它判的,一批对不上的路径会被整批筛掉、报成确定的 0 条。
    宁可报「不可确认」。

    **没有 `items` 这个键一律 None**,哪怕事件本身是成功的:那说明
    `KB_COLLECTOR_DIR` 指着一份还不打清单的旧采集器,它写了多少篇我们不知道。
    """
    from kb.config import VAULT_ROOT
    from kb.fetch_identity import text_field

    out = str(event.get("out") or "")
    rows = event.get("items")
    if not out or not isinstance(rows, list):
        return None
    try:
        prefix = Path(out).resolve().relative_to(Path(VAULT_ROOT).resolve())
    except (OSError, ValueError):
        return None
    got = []
    for one in rows:
        if not isinstance(one, dict):
            return None                 # 清单读不懂:当成说不出来,不挑着收
        name = (text_field(one.get("name")) or "").strip()
        if not name:
            # **答不出文件名的那一行也是「读不懂」,不许挑着收。** 挑着收交上去的是
            # 一份少记的清单,而上层拿它当确定值用(「这一次抓了 N 篇」)—— 少记是
            # 静默丢数据。`cmd_export` 那一端对空名字本来就是整份作废,两端得一个
            # 口径,不然中间这一道就成了把缺口洗成确定零条的地方。
            return None
        # 身份退回路径只在「旧采集器打了名字、没打 id」时发生。那时条数照旧
        # 确定,只有分解说不出来 —— 比整轮报「不可确认」更接近事实。
        # **但「没打」和「打了个读不懂的东西」是两件事**:后者一律整份作废,不许
        # `str()` 成一个看着确定的身份(口径在 `fetch_identity.text_field`)。
        aid, round_key = text_field(one.get("id")), text_field(one.get("round"))
        if aid is None or round_key is None:
            return None
        aid, round_key = aid.strip(), round_key.strip()
        rel = str(prefix / name)
        got.append(Fetched(f"wechat_local:{aid}" if aid else f"path:{rel}",
                           round_key, rel))
    return frozenset(got)


class WeChatLocalCollector:
    """公众号采集器。读本机已同步的推送发现文章,正文按链接公开抓取。"""

    name = "wechat_local"
    label = "公众号"
    channel_hint = "读取本机已同步的公众号推送，再获取原文。"
    range_hint = "只能覆盖本机已同步的推送，按文章发布时间筛选；新订阅不能保证补齐历史。"

    # 状态缓存。挂在实例上而不是模块全局:本模块只有末尾那一个单例,实例状态
    # 就是全局状态,而挂在实例上的那份能被 _mutate 直接 self._cache = None 作废。
    _cache: tuple[float, SourceStatus] | None = None

    # ── 状态 ────────────────────────────────────────────────────────────────

    def status(self, targets: tuple[str, ...] = ()) -> SourceStatus:
        """见 `kb.collectors.SourceCollector.status`。

        **这条来源的就绪跟抓哪几个号无关,所以原样忽略 `targets`**:它问的是
        "采集器装没装好、本机那个库读不读得出来",整条通道一个答案(凭证那条逐号
        授权的路在另一条分支上,主干这边没有)。缓存也因此不用按范围分格。
        """
        now = time.monotonic()
        if self._cache and now - self._cache[0] < _STATUS_TTL:
            return self._cache[1]
        result = self._probe()
        self._cache = (now, result)
        return result

    def _probe(self) -> SourceStatus:
        problem = _backend_problem()
        if problem:
            return SourceStatus(online=False, message="采集器还没装好", hint=problem)
        try:
            # 读取器要去探一次本机那个库(wechat-cli doctor),比纯写库慢
            proc = _run_cli("status", timeout=75)
        except subprocess.TimeoutExpired:
            return SourceStatus(online=False, message="采集器无响应(超时)")
        except OSError as e:
            return SourceStatus(online=False, message="采集器起不来", hint=str(e))
        if proc.returncode != 0:
            return SourceStatus(
                online=False,
                message="采集器执行失败",
                hint=(proc.stderr or proc.stdout).strip()[-120:],
            )
        try:
            data = json.loads(proc.stdout.strip().splitlines()[-1])
        except (ValueError, IndexError):
            return SourceStatus(online=False, message="采集器输出无法解析")

        return SourceStatus(
            online=True,
            ready=bool(data.get("ready")),
            message=str(data.get("message") or ""),
            hint=str(data.get("hint") or ""),
            targets=tuple(data.get("targets") or ()),
        )

    # ── 采集:远端 → 采集器本地库 ─────────────────────────────────────────────

    def fetch(self, scope: FetchScope) -> Coverage:
        """跑采集子进程,把它 stdout 的进度事件转成管道的阶段状态。

        返回这一轮**真跑到了哪几个号**(见 `kb.collectors.Coverage`)。**没起跑的
        那三档一律是零覆盖** —— 采集器没装好、起不来这两档的阶段结论是
        `skipped`,而 `skipped` 不算失败:按请求范围记账的话,这一句「跳过抓取」
        会去结清今天早些时候那笔真失败,面板上「重试失败项」当场消失(同 `Coverage`
        里那条,只是触发的是另一种没跑)。超时那一档的结论是失败、照旧记在账上,
        而它跑到哪儿说不清,所以也不拿它结清谁。
        """
        problem = _backend_problem()
        if problem:
            _stage("crawl", "skipped", f"采集器还没装好,跳过抓取:{problem}")
            return Coverage()
        # 把这一次业务抓取的身份带进子进程:它新发现的文章各记一笔,导出时原样
        # 交回来当回执里的抓取身份。不带的话只能事后按时间窗口反猜归属,而那正是
        # 「别处同期落下的内容被算进这一轮」的来源(口径见 `kb.fetch_rounds`)。
        from kb.fetch_rounds import round_key

        argv = ["collect", "--round", round_key()]
        if scope.ranged:
            argv += ["--since", scope.since, "--until", scope.until]
            timeout_min = 120        # 区间补抓页间/号间等待远长于日常增量
            _stage("crawl", "running",
                   f"区间补抓 [{scope.since} ~ {scope.until}]")
        else:
            timeout_min = 60
            _stage("crawl", "running", "增量采集")
        if scope.targets:
            argv += ["--targets", ",".join(scope.targets)]

        try:
            code, last = self._stream(argv, timeout_min * 60)
        except subprocess.TimeoutExpired:
            _stage("crawl", "error", f"采集超时({timeout_min}分钟)")
            return Coverage()
        except OSError as e:
            _stage("crawl", "skipped", f"采集器起不来,跳过抓取: {e}")
            return Coverage()
        finally:
            # 这一轮多半改了"最近采集时间"、也可能让读取器掉线,
            # 面板别再拿开跑前的旧结论
            self._cache = None
        self._report(code, last)
        # 子进程跑完了:这一轮真跑的就是请求点名的那几个号(没点名 = 整条来源)。
        # **它的失败是文章级的,摊不到号上** —— 账号那一层这一轮确实都跑过,所以
        # 覆盖范围照请求算;「这一格算不算失败」另有一份口径(`stage_failed` 认
        # `_report` 写下的那句话),两件事互不代替。
        return Coverage(targets=tuple(scope.targets), whole=not scope.targets)

    def _stream(self, argv: list[str], timeout: int) -> tuple[int, dict]:
        """边跑边读。返回 (退出码, 最后一个事件)。"""
        proc = subprocess.Popen(
            [_python(), "-m", "cli", *argv], cwd=BACKEND_DIR,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, bufsize=1,
        )
        last: dict = {}
        try:
            for line in proc.stdout:                       # type: ignore[union-attr]
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except ValueError:
                    _log(f"crawl: {line[:120]}")
                    continue
                last = event
                if event.get("status") == "running":
                    _stage("crawl", "running", self._progress_detail(event))
            proc.wait(timeout=timeout)
        finally:
            if proc.poll() is None:
                proc.kill()
        return proc.returncode, last

    @staticmethod
    def _progress_detail(event: dict) -> str:
        if event.get("phase") == "discovery":
            label = "发现文章 · 首次建立索引" if event.get("initial_index") else "发现文章 · 更新索引"
            return (f"{label} · 已读推送 {event.get('scanned_messages', 0)}/"
                    f"{event.get('message_total', 0)} 条 · 图文 {event.get('indexed_articles', 0)} 篇")
        if event.get("phase") == "download":
            detail = (f"下载正文 {event.get('download_done', 0)}/{event.get('download_total', 0)} 篇"
                      f" · 成功 {event.get('download_succeeded', 0)}"
                      f" · 失败 {event.get('download_failed', 0)}"
                      f" · 已有正文 {event.get('existing_articles', 0)}"
                      f" · 等待重试 {event.get('deferred_articles', 0)}")
            return detail + (f" — {event['current']}" if event.get("current") else "")
        return (f"采集中 {event.get('done', 0)}/{event.get('total', 0)}"
                f" — {event.get('current', '')}")

    @classmethod
    def _report(cls, code: int, last: dict) -> None:
        errors = last.get("errors") or []
        articles = last.get("article_failures") or []
        described = {item.get("error") for item in articles if isinstance(item, dict)}
        failures = [{"title": str(error), "reason": "文章发现或入库失败"}
                    for error in errors if error not in described]
        failures += [cls._no_body_failure(item, pending=False,
                                         reason="文章发现或入库失败") for item in articles]
        error_count = len(failures)
        for item in last.get("unreadable_accounts") or []:
            reason = (f"本机现有 {item['messages']} 条推送无法解析"
                      if item["identity_confirmed"] else
                      f"同名本机会话有 {item['messages']} 条推送无法解析，公众号归属待核实")
            failures.append({"title": item["name"], "reason": reason + "；请检查读取器或推送格式", "pending": True})
        if last.get("unattributed_messages"):
            candidates = last.get("coverage_unknown_accounts") or []
            hint = f"；可能相关账号：{'、'.join(candidates)}（归属待核实）" if candidates else ""
            if last.get("missing_accounts"):
                hint += "；这些推送也可能来自未匹配到推送的账号"
            failures.append({"title": "推送归属待核实", "pending": True,
                             "reason": f"另有 {last['unattributed_messages']} 条本机推送无法解析，"
                                       "无法确认所属公众号，未计入各号条数"
                                       + hint + "；请检查读取器或推送格式"})
        # 账号级诊断先于正文条目，避免首次大量正文待补挤掉账号提示（面板最多 50 条）。
        failures += [cls._no_body_failure(item, reason="正文暂未获取，已安排自动重试")
                     for item in (last.get("body_failures") or [])]
        new = last.get("new_articles", 0)
        coverage = ""
        if "download_succeeded" in last:
            coverage += f",补齐正文 {last['download_succeeded']} 篇"
        if last.get("missing_accounts"):
            count = len(last["missing_accounts"])
            if last.get("unattributed_messages"):
                coverage += (f",未匹配到本机推送 {count} 个号（若未关注或未同步，请在绿泡泡关注并同步；"
                             f"另有 {last['unattributed_messages']} 条无法确认归属的推送，可能属于其中某个号）")
            else:
                coverage += f",本机暂无推送 {count} 个号（在绿泡泡关注并同步后可采集）"
        if last.get("coverage_unknown_accounts"):
            coverage += f",推送覆盖待核实 {len(last['coverage_unknown_accounts'])} 个号"
        if last.get("unreadable_accounts"):
            coverage += f",推送解析待核查 {len(last['unreadable_accounts'])} 个号"
        if last.get("deferred_articles"):
            coverage += f",等待重试 {last['deferred_articles']} 篇"
        if last.get("body_failures"):
            coverage += f",正文待重试 {len(last['body_failures'])} 篇"
        if last.get("unreadable_messages"):
            coverage += f",所选公众号现存不可读推送 {last['unreadable_messages']} 条"
        if last.get("unattributed_messages"):
            coverage += f",另有归属待核实的不可读推送 {last['unattributed_messages']} 条（存量）"
        if last.get("unavailable_articles"):
            coverage += f",原文暂不可用 {last['unavailable_articles']} 篇（一周后复查）"
        # 这一轮跑着的时候用户在面板上按下了「不再抓取」:如实说一句,别让它消失
        # 在「下载失败」里 —— 它既不是失败,也不会再被重试。
        if last.get("excluded_articles"):
            coverage += f",已永久排除 {last['excluded_articles']} 篇（同步期间移除）"
        if code != 0:
            detail = (f"新增 {new} 篇,失败 {error_count} 项" if error_count
                      else f"采集失败(退出码 {code})")
            _stage("crawl", "error", detail + coverage, failures=failures)
        else:
            _stage("crawl", "error" if error_count else "ok",
                   f"新增 {new} 篇" + (f",失败 {error_count} 项" if error_count else "") + coverage,
                   failures=failures)

    # ── 落地:采集器本地库 → obs md ───────────────────────────────────────────

    def materialize(self) -> frozenset[Fetched] | None:
        """落 md,返回这一轮新写的那几篇(见 `kb.collectors` 的契约)。

        **只有子进程自己交出清单的那一支给确定值**,其余一律 None(不可确认):

        - 采集器没装好 → 一个字节都没写,`frozenset()` 是确定的空集。
        - 超时后降级 `--no-images` 重跑 → 第一次那趟已经写下去的文章在第二趟里
          算「已存在」被跳过,清单里没有它们,交上去就是个少记的下界。
        - 子进程判错、或者清单压根没来(`KB_COLLECTOR_DIR` 指着一份旧采集器)
          → 写到哪一行断在哪里,同上。
        """
        problem = _backend_problem()
        if problem:
            _stage("export", "skipped", f"采集器还没装好,跳过导出:{problem}")
            return frozenset()
        _stage("export", "running", "导出到 obs")

        def _export(extra: list[str], tmo: int):
            return _run_cli("export", *extra, timeout=tmo)

        degraded = False
        try:
            proc = _export([], 30 * 60)
        except subprocess.TimeoutExpired:
            # 逐篇下图太慢导致超时:降级为 --no-images 重跑(保留远程图片 URL),
            # 保证正文仍能导出,不把新文章卡在库外每天复发"导出超时"。
            # export 幂等,重跑只补上次没导完的剩余篇。见 gzh-full-pipeline 记忆。
            _stage("export", "running", "导出超时,降级 --no-images 重跑")
            degraded = True
            try:
                proc = _export(["--no-images"], 30 * 60)
            except subprocess.TimeoutExpired:
                _stage("export", "error", "导出超时(降级 --no-images 仍超时)")
                return None
        except OSError as e:
            _stage("export", "error", f"采集器起不来: {e}")
            return None

        event = {}
        for line in proc.stdout.splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
        if proc.returncode != 0:
            _stage("export", "error",
                   event.get("detail") or (proc.stderr or "").strip()[-300:] or "导出失败")
            return None
        fails = [self._no_body_failure(one) for one in (event.get("no_body") or [])]
        _stage("export", "ok", event.get("detail") or "完成", failures=fails)
        return None if degraded else _receipt(event)

    @classmethod
    def _no_body_failure(cls, item, *, reason=None, pending=True) -> dict:
        """把 CLI 报上来的一条「没正文」转成面板那份失败明细里的一行。

        **三种形态都认**:新版 CLI 给 `{name,id,account,title,link}`;它读不到结构化
        报告时给的是**带空身份的字典**;更旧的 CLI 给一个 `账号 · 标题` 的裸字符串。
        后两种没有身份 —— 那就**不带 `url` / `drop`**,面板据此把这一行显示成不可
        点、不可删,而不是指着另一篇文章让人删。**但那一条仍然要列得出来**:漏报
        是静默丢数据。
        """
        reason = reason or "缺少正文，暂未导出；保留待补，后续同步会按退避时间重试"
        if not isinstance(item, dict):
            return {"title": str(item), "reason": reason, "pending": pending}
        reason = str(item.get("reason") or reason)
        title = str(item.get("name") or item.get("title") or "")
        out: dict = {"title": title, "reason": reason, "pending": pending}
        if link := str(item.get("link") or ""):
            out["url"] = link
        if aid := str(item.get("id") or ""):
            # 面板原样回传这一份:这条到底怎么删只有采集器知道(见 `drop_item`)。
            # **`link` 一起带上,它不是冗余**:库里已经没有这一行时,档案 ID 算不出
            # 链接身份,而永久排除要记的正是那个身份(`exclusion_keys`)。
            out["drop"] = {"channel": cls.name, "id": aid,
                           "account": str(item.get("account") or ""),
                           "link": link}
        return out

    # ── 积压 ────────────────────────────────────────────────────────────────

    def pending(self) -> int | None:
        """采集器库里有、但还没落进 vault 的文章数(近似)。"""
        try:
            import sqlite3

            from kb.scanner import scan_all

            wdb = sqlite3.connect(BACKEND_DIR / "data" / "wechatoa.db")
            try:
                total = wdb.execute(
                    "SELECT COUNT(*) FROM articles "
                    "WHERE is_user_deleted=0 AND is_wx_deleted=0"
                ).fetchone()[0]
            finally:
                wdb.close()
            exported = sum(1 for d in scan_all() if d.source in VAULT_SOURCES)
            return max(0, total - exported)
        except Exception:
            return None

    # ── 订阅的公众号(TargetManager 契约)────────────────────────────────────

    def list_targets(self) -> list[Target]:
        data = self._accounts("list")
        return [
            Target(
                id=str(row.get("fakeid") or ""),
                name=str(row.get("name") or ""),
                display_name=str(row.get("display_name") or ""),
                enabled=bool(row.get("in_crawl_config")),
                detail=f"最近采集 {row.get('latest_crawl_at') or '—'}",
                # 本机库读得动 ≠ 每个号都有推送,不能把全局可用伪装成逐号就绪。
                # ready=None 让面板完全不显示那一列(见 Target.ready 的注释)。
                ready=None, state="按本机推送采集", tone="",
            )
            for row in (data.get("accounts") or [])
            if row.get("name")
        ]

    def search_targets(self, query: str) -> list[Target]:
        """从本机会话列表里找候选:不登录公众平台,也不走网络。

        归属(fakeid=__biz)优先从发现索引里查,查不到的才现翻一次它的推送。
        """
        data = self._accounts("discover", query, timeout=60)
        return [
            Target(
                id=str(row.get("fakeid") or ""),
                name=str(row.get("name") or ""),
                detail=" · ".join(x for x in (row.get("alias"),
                                              row.get("signature")) if x)[:80],
            )
            for row in (data.get("candidates") or [])
            if row.get("name")
        ]

    def add_target(self, name: str, target_id: str = "") -> str:
        extra = ["--fakeid", target_id] if target_id else []
        # 没给 fakeid 时要先翻一次本机会话解归属,比纯写库慢
        return self._mutate("add", name, *extra,
                            timeout=20 if target_id else 60)

    def remove_target(self, name: str) -> str:
        return self._mutate("remove", name)

    def set_target_enabled(self, name: str, enabled: bool) -> str:
        return self._mutate("enable" if enabled else "disable", name)

    # ── 单条内容的「别再抓了」(kb.collectors.ItemDropping)───────────────────

    def document_failure(self, document, reason: str) -> dict | None:
        from kb.collectors.wechat_documents import document_failure

        return document_failure(self, VAULT_SOURCES, document, reason)

    def item_aliases(self, ref: dict) -> tuple[str, ...]:
        from collector.src.utils.article_identity import stable_article_id
        from kb.collectors.wechat_documents import identity

        link = str(ref.get('link') or '')
        return (stable_article_id(link),) if identity(link) else ()

    def drop_item(self, ref: dict) -> str:
        from kb.collectors.wechat_documents import cleanup, identity
        from kb.maintenance import LockBusy, maintenance_lock

        link = str(ref.get('link') or '').strip()
        if not identity(link):
            return self._drop_item(ref)
        try:
            with maintenance_lock(wait=5):
                detail = self._drop_item(ref)
                try:
                    cleanup(link, VAULT_SOURCES)
                except (OSError, sqlite3.Error, CollectorError) as error:
                    raise DropIncomplete(f'已永久排除，但已导出内容清理未完成，请重试：{error}') from None
                return detail
        except LockBusy:
            raise CollectorError('正在导出或建立索引，尚未移除；请稍后重试') from None

    def _drop_item(self, ref: dict) -> str:
        """这一篇永久不再采集:删掉它、把它的链接身份记进黑名单。

        **身份只认 `id`,`account` 只用来核对。** CLI 那边拿 id 查出它真正的归属,
        跟这里给的对不上就整个拒掉 —— 面板上那条明细和库里那一行必须是同一篇,
        否则删下去的是另一个号的文章,而那是**不可恢复**的(连正文一起删)。
        **幂等**:库里已经没有这一行时 CLI 照样返回成功 —— 但那条路要先拿出**排除
        凭据**,拿不出来就拒;`link` 带过去正是为了让它算得出身份(见那边的注释)。
        """
        aid = str(ref.get("id") or "").strip()
        if not aid:
            raise CollectorError("这一条没有可用的文章标识，无法移除")
        extra = ["--account", str(ref.get("account") or "")] if ref.get("account") else []
        if link := str(ref.get("link") or "").strip():
            extra += ["--link", link]
        data = self._accounts_raw("articles", "drop", "--id", aid, *extra, timeout=20)
        # 库里少了一篇,面板下次拿到的积压数要是新的
        self._cache = None
        return str(data.get("detail") or "已移除")

    # ── 上面几个共用的 CLI 调用 ─────────────────────────────────────────────

    @staticmethod
    def _accounts(action: str, name: str = "", *extra: str,
                  timeout: int = 20) -> dict:
        """跑 `cli accounts ...` 并解析它最后一行 JSON。"""
        argv = ["accounts", action] + ([name] if name else []) + list(extra)
        return WeChatLocalCollector._accounts_raw(*argv, timeout=timeout)

    @staticmethod
    def _accounts_raw(*argv: str, timeout: int = 20) -> dict:
        """跑任意子命令(都加 --json)并解析最后一行 JSON。"""
        problem = _backend_problem()
        if problem:
            raise CollectorError(f"采集器还没装好:{problem}")
        try:
            proc = _run_cli(*argv, "--json", timeout=timeout)
        except subprocess.TimeoutExpired:
            raise CollectorTimeout(f"采集器无响应(超过 {timeout} 秒)") from None
        except OSError as e:
            raise CollectorError(f"采集器起不来:{e}") from None

        data: dict = {}
        for line in proc.stdout.splitlines():
            try:
                data = json.loads(line)
            except ValueError:
                continue
        if proc.returncode != 0 or data.get("ok") is False:
            # CLI 的 detail 已经是带处置建议的成品文案,优先用它
            raise CollectorError(
                str(data.get("detail") or "").strip()
                or (proc.stderr or "").strip()[-200:]
                or f"操作失败(退出码 {proc.returncode})"
            )
        return data

    def _mutate(self, action: str, name: str, *extra: str, timeout: int = 20) -> str:
        data = self._accounts(action, name, *extra, timeout=timeout)
        # 订阅列表变了,面板下次要拿到新的 targets
        self._cache = None
        return str(data.get("detail") or "已完成")


collector = WeChatLocalCollector()
