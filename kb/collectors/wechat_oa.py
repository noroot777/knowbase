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
import subprocess
import sys
import time
from pathlib import Path

from kb.collectors import FetchScope, SourceStatus
from kb.pipeline_state import _log, _stage

# 采集器所在目录。它自带 venv,用它自己的解释器跑,免得依赖污染知识库这边。
BACKEND_DIR = Path.home() / "code" / "gongzhonghao" / "WeChatOA_Aggregation"
BACKEND_PY = BACKEND_DIR / ".venv" / "bin" / "python"

# 采集产物落在 vault 里的 source 名(历史上换过两次,一并认)
VAULT_SOURCES = ("dr-gzh", "wechat", "gzh")

# CLI 的退出码约定,见采集器仓库的 cli.py
EXIT_AUTH, EXIT_FREQ = 2, 3

# 状态缓存时长。面板每 1.5 秒轮询一次,而每次 status 都要起一个 Python 子进程、
# 并向微信发一次凭证探测请求——不缓存就是每秒起进程 + 高频打微信接口(风控)。
# 凭证状态本来也是分钟级才变的东西。
_STATUS_TTL = 60.0
_status_cache: tuple[float, SourceStatus] | None = None


def _python() -> str:
    """采集器自己的解释器;没装 venv 就退回当前解释器。"""
    return str(BACKEND_PY) if BACKEND_PY.exists() else sys.executable


def _run_cli(*argv: str, timeout: int) -> subprocess.CompletedProcess:
    return subprocess.run(
        [_python(), "-m", "cli", *argv],
        cwd=BACKEND_DIR, capture_output=True, text=True, timeout=timeout,
    )


class WeChatOACollector:
    """公众号采集器。"""

    name = "wechat_oa"
    label = "公众号"
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
        if not BACKEND_DIR.is_dir():
            return SourceStatus(
                online=False,
                message="找不到采集器",
                hint=f"预期在 {BACKEND_DIR}",
            )
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
        argv = ["collect"]
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
        if code == EXIT_AUTH:
            _stage("crawl", "error",
                   "凭证失效,到采集器目录跑 `python -m cli login` 扫码续期",
                   failures=failures)
        elif code == EXIT_FREQ:
            _stage("crawl", "error",
                   f"微信频率限制,已中断(新增 {new} 篇)。等 30-60 分钟后重跑",
                   failures=[{"title": str(e), "reason": "触发频率限制"}
                             for e in errors])
        elif code != 0:
            _stage("crawl", "error", f"采集失败(退出码 {code})", failures=failures)
        else:
            _stage("crawl", "ok",
                   f"新增 {new} 篇" + (f",{len(errors)} 个号失败" if errors else ""),
                   failures=failures)

    # ── 落地:采集器本地库 → obs md ───────────────────────────────────────────

    def materialize(self) -> None:
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


collector = WeChatOACollector()
