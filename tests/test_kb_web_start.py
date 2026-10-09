# -*- coding: utf-8 -*-
"""`./kb.sh web start` 不许在后台 daemon 里去联网。

2026-10-08 的故障:pyproject.toml 10-02 改过一次,于是 `uv run kb-web` 要重建那个
editable 包、重建要解析 `build-system.requires` 里的 hatchling —— 不带 --offline 时
即使 uv 缓存里已经有也会去 pypi 问一次最新版,当天那次 tls handshake eof,服务整天
起不来。而失败发生在 daemonize 之后的后台进程里,用户看到的只有「kb-web 起不来,
看 web.log」,日志尾巴是一段跟知识库毫无关系的网络错。

命令级测试:把 kb.sh 原样拷进临时目录跑,uv 用 PATH 上的桩挡住(它记下每次调用的
参数和当时的代理变量,收到起服务那条命令时真占住端口,好让 web_start 等得到)。
判定逻辑一行都不打桩 —— 这件事的全部风险就在「哪条命令带了 --offline、哪一次才
准联网」上。
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
KB_SH = REPO / "kb.sh"

PROXY = "http://127.0.0.1:59999"

STUB = """#!/bin/sh
# kb.sh 的 uv 桩:记一笔调用,按场景决定离线确认成不成。
LOG='{log}'
printf 'argv:%s\\n' "$*" >> "$LOG"
printf 'proxy:%s\\n' "${{HTTPS_PROXY:-}}" >> "$LOG"
case " $* " in
  *" kb-web "*)
    # 真占住端口:web_start 起完要等到端口有人听才算成
    exec python3 -c "
import socket, time
s = socket.socket()
s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
s.bind(('127.0.0.1', int('$PORT')))
s.listen(5)
time.sleep(300)
" ;;
esac
case " $* " in
  *" --offline "*) exit {offline_rc} ;;
esac
exit {online_rc}
"""


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class WebStartOfflineTest(unittest.TestCase):
    """起服务那条命令一律 --offline;联网只准发生在前台的准备那一步。"""

    def setUp(self) -> None:
        self._tmp = tempfile.mkdtemp()
        base = Path(os.path.realpath(self._tmp))
        self.root = base / "repo"
        self.vault = base / "vault"       # graph_stale 别去读用户真 vault
        self.bin = base / "bin"
        for d in (self.root, self.vault, self.bin):
            d.mkdir(parents=True)
        shutil.copy2(KB_SH, self.root / "kb.sh")
        (self.root / "kb.sh").chmod(0o755)
        self.log = base / "uv.log"
        self.port = _free_port()

    def tearDown(self) -> None:
        # 桩起的监听进程脱离了会话,按端口收
        pids = subprocess.run(["lsof", "-ti", f"tcp:{self.port}"],
                              capture_output=True, text=True).stdout.split()
        for pid in pids:
            subprocess.run(["kill", "-9", pid], capture_output=True)
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _run(self, offline_rc: int, online_rc: int) -> subprocess.CompletedProcess:
        stub = self.bin / "uv"
        stub.write_text(STUB.format(log=self.log, offline_rc=offline_rc,
                                    online_rc=online_rc), encoding="utf-8")
        stub.chmod(0o755)
        env = {**os.environ,
               "PATH": f"{self.bin}{os.pathsep}{os.environ.get('PATH', '')}",
               "KB_VAULT_ROOT": str(self.vault),
               "KB_WEB_PORT": str(self.port),
               "HTTPS_PROXY": PROXY}
        return subprocess.run([str(self.root / "kb.sh"), "web", "start"],
                              cwd=self.root, env=env, capture_output=True,
                              text=True, timeout=120)

    def _calls(self) -> list[tuple[str, str]]:
        if not self.log.exists():
            return []
        lines = self.log.read_text(encoding="utf-8").splitlines()
        argv = [l[len("argv:"):] for l in lines if l.startswith("argv:")]
        proxy = [l[len("proxy:"):] for l in lines if l.startswith("proxy:")]
        return list(zip(argv, proxy))

    def test_缓存命中时一次网络都不碰(self) -> None:
        r = self._run(offline_rc=0, online_rc=0)
        self.assertEqual(r.returncode, 0, r.stderr)
        calls = self._calls()
        self.assertTrue(calls, "uv 压根没被调到")
        for argv, _ in calls:
            self.assertIn("--offline", argv, f"这一次没带 --offline:{argv}")
        self.assertTrue(any("kb-web" in a for a, _ in calls), "服务没起")

    def test_起服务那条命令带_offline(self) -> None:
        # 这条单独钉:准备那一步全带 --offline 也不代表起服务那条带了
        self._run(offline_rc=0, online_rc=0)
        serve = [a for a, _ in self._calls() if "kb-web" in a]
        self.assertEqual(len(serve), 1, serve)
        self.assertIn("--offline", serve[0])

    def test_离线确认不成才联网而且带着代理(self) -> None:
        r = self._run(offline_rc=1, online_rc=0)
        self.assertEqual(r.returncode, 0, r.stderr)
        calls = self._calls()
        online = [(a, p) for a, p in calls if "--offline" not in a]
        self.assertEqual(len(online), 1, f"联网那次应当只有一次:{calls}")
        self.assertNotIn("kb-web", online[0][0], "联网那次只准准备环境,不准起服务")
        # env -i 把 *_proxy 一起清掉了,这台机器上 pypi 常常得走代理
        self.assertEqual(online[0][1], PROXY, "联网那次没把代理带进去")
        serve = [a for a, _ in calls if "kb-web" in a]
        self.assertEqual(len(serve), 1, serve)
        self.assertIn("--offline", serve[0], "补完环境之后起服务照旧离线")

    def test_依赖装不上就在前台停住_不去起服务(self) -> None:
        r = self._run(offline_rc=1, online_rc=1)
        self.assertNotEqual(r.returncode, 0, "装不上还报成功\n" + r.stderr)
        self.assertIn("依赖装不上", r.stderr)
        self.assertNotIn("kb-web 起不来,看", r.stderr, "不该退到那句让人翻日志的话")
        self.assertFalse(any("kb-web" in a for a, _ in self._calls()),
                         "前台没准备成,不该再 daemonize 一个必死的后台进程")
        self.assertEqual(
            list((self.root / ".run").glob("web-*.pid")), [], "不该留下 pidfile")


if __name__ == "__main__":
    unittest.main()
