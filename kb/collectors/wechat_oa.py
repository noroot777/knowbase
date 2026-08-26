# -*- coding: utf-8 -*-
"""微信公众号采集器:实现 kb.collectors 的 SourceCollector 契约。

微信特有的一切都关在这个文件里——凭证会不会过期、反爬要等多久、
频率限制长什么样、文章先落 SQLite 再转 md,管道一概不知情。

当前实现走 HTTP 调 WeChatOA 后端(:8000)+ 子进程跑它的 export_obsidian.py。
这只是契约的一种实现:后端换成按需跑的 CLI、或换成完全不同的抓取方式,
只要还满足契约,knowledge-base 侧不用改任何一行。
"""

from __future__ import annotations

import json
import re
import subprocess
import time
import urllib.request
from pathlib import Path

from kb.collectors import FetchScope, SourceStatus
from kb.pipeline_state import _log, _stage

# 采集器后端的位置。改用其它部署方式时只动这两行。
BACKEND_DIR = Path.home() / "code" / "gongzhonghao" / "WeChatOA_Aggregation"
BACKEND_API = "http://127.0.0.1:8000"
# 采集器自己的管理页:扫码续期凭证、增删订阅号都在这儿
CONSOLE_URL = "http://localhost:5173/#/config"

# 采集产物落在 vault 里的 source 名(历史上换过两次,一并认)
VAULT_SOURCES = ("dr-gzh", "wechat", "gzh")


def _http_json(url: str, method: str = "GET", payload: dict | None = None,
               timeout: int = 10) -> dict | None:
    try:
        data = None
        headers = {}
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None


class WeChatOACollector:
    """公众号采集器。"""

    name = "wechat_oa"
    label = "公众号"
    range_hint = ("提示:微信服务端一般只保留近 30 天可翻页的历史,"
                  "跨度过大的旧文章可能拉不到。")

    # ── 状态 ────────────────────────────────────────────────────────────────

    def status(self) -> SourceStatus:
        auth = _http_json(f"{BACKEND_API}/api/auth/status")
        if auth is None:
            return SourceStatus(
                online=False,
                message="采集器不在线,抓取阶段将跳过",
                hint="启动采集器后端后重试",
            )
        targets = self._targets()
        expiry = str(auth.get("credential_expiry_label") or "").strip()
        # valid 只是后端内存里的缓存结论,checked_at 为空表示本次启动后从未真正
        # 探测过(初值就是 True);而过期天数来自磁盘上的保存时间,是硬事实。
        # 两者矛盾时以后者为准——否则面板会并排显示"凭证有效 · 已过期"。
        if not auth.get("valid") or auth.get("credential_expired"):
            return SourceStatus(
                online=True, ready=False,
                message=f"凭证{expiry or '已失效'}",
                hint="到采集器配置页扫码续期",
                console_url=CONSOLE_URL,
                targets=targets,
            )
        return SourceStatus(
            online=True, ready=True,
            message="凭证有效" + (f" · {expiry}" if expiry else ""),
            console_url=CONSOLE_URL,
            targets=targets,
        )

    def _targets(self) -> tuple[str, ...]:
        """订阅的公众号名列表(后端不在线返回空)。"""
        n2f = _http_json(f"{BACKEND_API}/api/name2fakeid")
        return tuple(sorted(n2f.keys())) if isinstance(n2f, dict) else ()

    # ── 采集:远端 → 采集器本地库 ─────────────────────────────────────────────

    def fetch(self, scope: FetchScope) -> None:
        """触发抓取并轮询到结束。

        scope.ranged 时走范围抓取(指定公众号 + 时间区间),否则走全量增量。
        """
        if _http_json(f"{BACKEND_API}/api/crawl/status") is None:
            _stage("crawl", "skipped", "采集器后端不在线,跳过抓取")
            return

        if scope.ranged:
            accounts = list(scope.targets) or list(self._targets())
            if not accounts:
                _stage("crawl", "error", "拿不到公众号列表,无法范围抓取")
                return
            _stage("crawl", "running",
                   f"触发范围抓取:{len(accounts)} 个号 "
                   f"[{scope.since} ~ {scope.until}]")
            started = _http_json(
                f"{BACKEND_API}/api/crawl/range", method="POST",
                payload={"accounts": accounts,
                         "since_date": scope.since, "until_date": scope.until})
            # 范围抓取页间/号间等待比全量爬取长得多,超时相应放宽
            timeout_min = 120
        else:
            _stage("crawl", "running", "触发全量爬取")
            started = _http_json(f"{BACKEND_API}/api/crawl", method="POST")
            timeout_min = 30

        if started is None:
            # 409(已有任务在跑)也会走到这:继续轮询即可
            _log("crawl: 启动返回异常(可能已在跑),继续轮询状态")
        self._poll_until_done(timeout_min)

    def _poll_until_done(self, timeout_min: int) -> None:
        # 后端的爬取线程把 running 置 true 有个短暂窗口,先等一拍;
        # 且 running=false 时连续确认三次再收工,避免拿上一轮的旧状态提前结束
        time.sleep(2)
        deadline = time.time() + timeout_min * 60
        idle_polls = 0
        while time.time() < deadline:
            st = _http_json(f"{BACKEND_API}/api/crawl/status")
            if st is None:
                _stage("crawl", "error", "轮询状态失败(后端掉线?)")
                return
            if not st.get("running"):
                idle_polls += 1
                if idle_polls < 3:
                    time.sleep(1)
                    continue
                self._report_result(st)
                return
            idle_polls = 0
            _stage("crawl", "running",
                   f"爬取中 {st.get('done', 0)}/{st.get('total', 0)} "
                   f"— {st.get('current', '')}")
            time.sleep(2)
        _stage("crawl", "error", f"爬取超时({timeout_min}分钟)")

    @staticmethod
    def _report_result(st: dict) -> None:
        errs = st.get("errors") or []
        if st.get("auth_error"):
            _stage("crawl", "error", "凭证失效,请到采集器配置页扫码续期后重跑")
        elif st.get("freq_control"):
            # 账号级限流:已抓到的部分照常进入后续阶段,但要明确提示等待
            done, total = st.get("done", 0), st.get("total", 0)
            _stage("crawl", "error",
                   f"微信频率限制,已中断(完成 {done}/{total} 个号,新增 "
                   f"{st.get('new_articles', 0)} 篇)。等 30-60 分钟后重跑抓取",
                   failures=[{"title": str(e), "reason": "触发频率限制"}
                             for e in errs])
        else:
            _stage("crawl", "ok",
                   f"新增 {st.get('new_articles', 0)} 篇"
                   + (f",{len(errs)} 个账号失败" if errs else ""),
                   failures=[{"title": str(e), "reason": "账号抓取失败"}
                             for e in errs])

    # ── 落地:采集器本地库 → obs md ───────────────────────────────────────────

    def materialize(self) -> None:
        _stage("export", "running", "导出到 obs")

        def _do_export(extra_args, tmo):
            return subprocess.run(
                ["uv", "run", "python", "scripts/export_obsidian.py", *extra_args],
                cwd=BACKEND_DIR, capture_output=True, text=True, timeout=tmo,
            )

        try:
            proc = _do_export([], 30 * 60)
        except subprocess.TimeoutExpired:
            # 逐篇下图太慢导致超时:降级为 --no-images 重跑(保留远程图片 URL),
            # 保证正文仍能导出,不把新文章卡在库外每天复发"导出超时"。
            # export 幂等,重跑只补上次没导完的剩余篇。见 gzh-full-pipeline 记忆。
            _stage("export", "running", "导出超时,降级 --no-images 重跑")
            try:
                proc = _do_export(["--no-images"], 30 * 60)
            except subprocess.TimeoutExpired:
                _stage("export", "error", "导出超时(降级 --no-images 仍超时)")
                return
        if proc.returncode != 0:
            _stage("export", "error",
                   f"导出失败: {(proc.stderr or proc.stdout)[-300:]}")
            return
        # 取脚本的完成行作为 detail;失败行(无正文)解析成明细供面板展开
        fails = []
        for line in proc.stdout.splitlines():
            m = re.search(r"无正文,跳过 (\S+) (.*)", line)
            if m:
                fails.append({
                    "title": m.group(2).strip() or m.group(1),
                    "reason": "无正文(转载stub或已删),导不出;"
                              "持续多轮可标 is_wx_deleted"})
        tail = [line for line in proc.stdout.splitlines() if "完成" in line]
        _stage("export", "ok",
               tail[-1].replace("[export] ", "") if tail else "完成",
               failures=fails)

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
