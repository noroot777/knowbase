# -*- coding: utf-8 -*-
"""微信公众号采集器:实现 kb.collectors 的 SourceCollector 契约。

微信特有的一切都关在这个文件里——凭证会不会过期、反爬要等多久、
文章先落 SQLite 再转 md,管道一概不知情。

采集器后端是个**一次性 CLI**,不是常驻服务:每次调用起一个子进程,跑完就退。
进度靠子进程的 stdout 逐行送回来(每行一个 JSON 事件),比轮询 HTTP 状态端点
少一个常驻进程、少一层状态缓存,也不会出现"轮询到上一轮的旧状态"那类问题。
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

from kb.collectors import CollectorError, FetchScope, SourceStatus, Target
from kb.pipeline_state import _log, _stage

# 采集器在本仓库内(<repo>/collector)。刻意用相对于本文件的路径而不是绝对路径:
# 开 worktree 时它会自动指向那个 worktree 里的 collector,改采集器的代码
# 才真的被隔离——写死绝对路径的话,worktree 里跑的仍是主仓库那一份。
# 数据与 venv 不在版本库里(见根 .gitignore),新 worktree 要先跑一次
# collector/setup.sh 把它们链过去。
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
BACKEND_DIR = Path(os.getenv("KB_COLLECTOR_DIR", "").strip() or _REPO_ROOT / "collector")
BACKEND_PY = BACKEND_DIR / ".venv" / "bin" / "python"

# 采集产物落在 vault 里的 source 名(历史上换过两次,一并认)
VAULT_SOURCES = ("dr-gzh", "wechat", "gzh")

# CLI 的凭证四态 → 契约的 ready/文案/色调。三种"不绿"对用户是三回事:
# missing 这个号还没去点过文章;expired 点过但服务端已经否了,得重点一次;
# stale 点过、还没被否掉,但截下来有一阵子了——微信这凭证撑不了多久,
# 采集时照样会拿它去试,只是别再当成"已就绪"骗自己。
_CRED_READY = {"valid": True, "stale": False, "expired": False, "missing": False}
_CRED_TEXT = {"valid": "凭证就绪", "stale": "凭证放太久,可能已失效",
              "expired": "凭证已失效", "missing": "还没有凭证"}
_CRED_TONE = {"valid": "ok", "stale": "warn", "expired": "bad", "missing": "bad"}

# CLI 的退出码约定,见采集器仓库的 cli.py
EXIT_AUTH = 2

# 状态缓存时长。面板每 1.5 秒轮询一次,而每次 status 都要起一个 Python 子进程、
# 并向微信发一次凭证探测请求——不缓存就是每秒起进程 + 高频打微信接口(风控)。
# 凭证状态本来也是分钟级才变的东西。
_STATUS_TTL = 60.0
_status_cache: tuple[float, SourceStatus] | None = None


def _backend_problem() -> str:
    """采集器没装好就返回一句"缺什么、怎么补",装好了返回空串。

    `collector/data`(库+凭证)和 `collector/.venv` 都在 .gitignore 里,是
    `collector/setup.sh` 链到仓库外那份真实目录的软链接——`git worktree add`
    出来的新工作树只有源码,清过一次 untracked 文件的旧目录也会把它们清掉。
    缺哪个都不是"配置不全"而是根本跑不了:没 .venv 就没有 requests/lxml,
    没 data 就没有文章库和凭证。
    """
    if not BACKEND_DIR.is_dir():
        return f"找不到采集器目录,预期在 {BACKEND_DIR}"
    missing = [shown for shown, probe in ((".venv", ".venv/bin/python"), ("data", "data"))
               if not (BACKEND_DIR / probe).exists()]
    if missing:
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
    return subprocess.run(
        [_python(), "-m", "cli", *argv],
        cwd=BACKEND_DIR, capture_output=True, text=True, timeout=timeout,
    )


class WeChatOACollector:
    """公众号采集器。"""

    name = "wechat_oa"
    label = "公众号"
    channel_label = "凭证抓取（原通道）"
    cli_channel = "profile_ext"
    channel_hint = "用各公众号的文章凭证获取列表；同步前通过 wxdown 准备凭证。"
    range_hint = ("提示:微信服务端一般只保留近 30 天可翻页的历史,"
                  "跨度过大的旧文章可能拉不到。")

    # ── 状态 ────────────────────────────────────────────────────────────────

    def status(self) -> SourceStatus:
        global _status_cache
        now = time.monotonic()
        if _status_cache and now - _status_cache[0] < _STATUS_TTL:
            return _status_cache[1]
        result = self._probe()
        _status_cache = (now, result)
        return result

    def _probe(self) -> SourceStatus:
        problem = _backend_problem()
        if problem:
            return SourceStatus(online=False, message="采集器还没装好", hint=problem)
        try:
            proc = _run_cli("status", timeout=60)
        except subprocess.TimeoutExpired:
            return SourceStatus(online=False, message="采集器无响应(超时)")
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

    def fetch(self, scope: FetchScope) -> None:
        """跑采集子进程,把它 stdout 的进度事件转成管道的阶段状态。"""
        problem = _backend_problem()
        if problem:
            _stage("crawl", "skipped", f"采集器还没装好,跳过抓取:{problem}")
            return
        argv = ["collect", "--channel", self.cli_channel]
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
            return
        except OSError as e:
            _stage("crawl", "skipped", f"采集器起不来,跳过抓取: {e}")
            return
        finally:
            # 采集过程会用掉/作废凭证,面板别再拿开跑前的旧结论
            global _status_cache
            _status_cache = None
        self._report(code, last)

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
                    _stage("crawl", "running",
                           f"采集中 {event.get('done', 0)}/{event.get('total', 0)}"
                           f" — {event.get('current', '')}")
            proc.wait(timeout=timeout)
        finally:
            if proc.poll() is None:
                proc.kill()
        return proc.returncode, last

    @staticmethod
    def _report(code: int, last: dict) -> None:
        errors = last.get("errors") or []
        failures = [{"title": str(e), "reason": "账号采集失败"} for e in errors]
        new = last.get("new_articles", 0)
        coverage = ""
        if last.get("missing_accounts"):
            coverage += f",本机暂无推送 {len(last['missing_accounts'])} 个号（订阅并同步后可采集）"
        if last.get("deferred_articles"):
            coverage += f",等待重试 {last['deferred_articles']} 篇"
        if last.get("unavailable_articles"):
            coverage += f",原文暂不可用 {last['unavailable_articles']} 篇（一周后复查）"
        if code == EXIT_AUTH:
            # 一份文章凭证都没有,采集器直接没开跑。login 那套扫码凭证救不了它
            _stage("crawl", "error",
                   "一份文章凭证都没有:先 `./kb.sh prep` 起 wxdown,"
                   "再在 Mac 微信里打开各号的一篇文章",
                   failures=failures)
        elif code != 0:
            detail = (f"新增 {new} 篇,失败 {len(errors)} 项" if errors
                      else f"采集失败(退出码 {code})")
            _stage("crawl", "error", detail + coverage, failures=failures)
        else:
            _stage("crawl", "error" if errors else "ok",
                   f"新增 {new} 篇" + (f",失败 {len(errors)} 项" if errors else "") + coverage,
                   failures=failures)

    # ── 落地:采集器本地库 → obs md ───────────────────────────────────────────

    def materialize(self) -> None:
        problem = _backend_problem()
        if problem:
            _stage("export", "skipped", f"采集器还没装好,跳过导出:{problem}")
            return
        _stage("export", "running", "导出到 obs")

        def _export(extra: list[str], tmo: int):
            return _run_cli("export", *extra, timeout=tmo)

        try:
            proc = _export([], 30 * 60)
        except subprocess.TimeoutExpired:
            # 逐篇下图太慢导致超时:降级为 --no-images 重跑(保留远程图片 URL),
            # 保证正文仍能导出,不把新文章卡在库外每天复发"导出超时"。
            # export 幂等,重跑只补上次没导完的剩余篇。见 gzh-full-pipeline 记忆。
            _stage("export", "running", "导出超时,降级 --no-images 重跑")
            try:
                proc = _export(["--no-images"], 30 * 60)
            except subprocess.TimeoutExpired:
                _stage("export", "error", "导出超时(降级 --no-images 仍超时)")
                return
        except OSError as e:
            _stage("export", "error", f"采集器起不来: {e}")
            return

        event = {}
        for line in proc.stdout.splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
        if proc.returncode != 0:
            _stage("export", "error",
                   event.get("detail") or (proc.stderr or "").strip()[-300:] or "导出失败")
            return
        fails = [{"title": t,
                  "reason": "无正文(转载stub或已删),导不出;"
                            "持续多轮可标 is_wx_deleted"}
                 for t in (event.get("no_body") or [])]
        _stage("export", "ok", event.get("detail") or "完成", failures=fails)

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
                ready=_CRED_READY.get(str(row.get("cred_state") or "")),
                state=_CRED_TEXT.get(str(row.get("cred_state") or ""), ""),
                tone=_CRED_TONE.get(str(row.get("cred_state") or ""), ""),
            )
            for row in (data.get("accounts") or [])
            if row.get("name")
        ]

    def refresh_readiness(self) -> str:
        """从 wxdown 抓包结果里把新拿到的文章凭证并进来,再报一句就绪情况。"""
        data = self._accounts_raw("creds", timeout=60)
        if data.get("warning"):
            raise CollectorError(str(data["warning"]))
        got, ready, total = (data.get("imported", 0),
                             data.get("ready", 0), data.get("total", 0))
        return (f"新拿到 {got} 个号的凭证," if got else "没有新凭证,") + \
               f"当前 {ready}/{total} 个号凭证新鲜"

    def search_targets(self, query: str) -> list[Target]:
        data = self._accounts("search", query, timeout=60)
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
        # 没给 fakeid 时要先搜一次微信,比纯写库慢得多
        return self._mutate("add", name, *extra,
                            timeout=20 if target_id else 60)

    def remove_target(self, name: str) -> str:
        return self._mutate("remove", name)

    def set_target_enabled(self, name: str, enabled: bool) -> str:
        return self._mutate("enable" if enabled else "disable", name)

    # ── 上面几个共用的 CLI 调用 ─────────────────────────────────────────────

    @staticmethod
    def _accounts(action: str, name: str = "", *extra: str,
                  timeout: int = 20) -> dict:
        """跑 `cli accounts ...` 并解析它最后一行 JSON。"""
        argv = ["accounts", action] + ([name] if name else []) + list(extra)
        return WeChatOACollector._accounts_raw(*argv, timeout=timeout)

    @staticmethod
    def _accounts_raw(*argv: str, timeout: int = 20) -> dict:
        """跑任意子命令(都加 --json)并解析最后一行 JSON。"""
        problem = _backend_problem()
        if problem:
            raise CollectorError(f"采集器还没装好:{problem}")
        try:
            proc = _run_cli(*argv, "--json", timeout=timeout)
        except subprocess.TimeoutExpired:
            raise CollectorError(f"采集器无响应(超过 {timeout} 秒)") from None
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
        global _status_cache
        _status_cache = None
        return str(data.get("detail") or "已完成")


collector = WeChatOACollector()
