# -*- coding: utf-8 -*-
"""`./kb.sh grab` 这层壳:**帮助和坏参数都不许先起一个守护进程**。

argparse 的 `--help` 让子进程以 0 退出,和「参数校验通过」一模一样 —— 光靠
`--parse-only || return` 分不开这两件事,于是帮助打完之后 shell 照旧往下走:
检查工作树、`web_start`、再把帮助打第二遍。worktree 里更难看:先撞上那道闸门,
帮助压根拿不到,还以 1 退出(第 2 轮审查实测)。

**判据落在后果上:uv 被调到过没有。** 「打印了 usage」证明不了没起服务,而起没
起服务这件事只有 uv 那一笔调用记录说得准。反过来钉一边:正常参数照旧走到起服务
那一步 —— 不然把 `cmd_grab` 写成「总是 exec --help」也能全绿。

命令级:kb.sh 原样拷进临时目录跑(所以它不在任何 git 仓库里,worktree 那道闸门
用不上),uv 用 PATH 上的桩挡住。
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

#: uv 的桩:记一笔调用;收到起服务那条命令时占住端口(`web_start` 要等到有人听),
#: 并且**对每个请求立刻回 500** —— 只 listen 不答的话,后面那句真 CLI 会在一个
#: 建立好却没有回答的连接上等满单次上限,这一组就要跑一分钟。
STUB = """#!/bin/sh
LOG='{log}'
printf 'argv:%s\\n' "$*" >> "$LOG"
case " $* " in
  *" kb-web "*)
    exec python3 -c "
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
class H(BaseHTTPRequestHandler):
    def _a(self):
        self.send_response(500); self.send_header('Content-Length', '4')
        self.end_headers(); self.wfile.write(b'boom')
    do_GET = do_POST = _a
    def log_message(self, *a): pass
ThreadingHTTPServer(('127.0.0.1', int('{port}')), H).serve_forever()
" ;;
esac
exit 0
"""


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class GrabCommandTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.mkdtemp()
        base = Path(os.path.realpath(self._tmp))
        self.root = base / "repo"
        self.vault = base / "vault"       # graph_stale 别去读用户真 vault
        self.bin = base / "bin"
        for d in (self.root / "scripts", self.vault, self.bin):
            d.mkdir(parents=True)
        shutil.copy2(REPO / "kb.sh", self.root / "kb.sh")
        (self.root / "kb.sh").chmod(0o755)
        shutil.copy2(REPO / "scripts" / "grab_cli.py", self.root / "scripts")
        self.log = base / "uv.log"
        self.port = _free_port()
        stub = self.bin / "uv"
        stub.write_text(STUB.format(log=self.log, port=self.port), encoding="utf-8")
        stub.chmod(0o755)

    def tearDown(self) -> None:
        # 桩起的监听进程脱离了会话,按端口收
        pids = subprocess.run(["lsof", "-ti", f"tcp:{self.port}"],
                              capture_output=True, text=True).stdout.split()
        for pid in pids:
            subprocess.run(["kill", "-9", pid], capture_output=True)
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _run(self, *argv: str) -> subprocess.CompletedProcess:
        env = {**os.environ,
               "PATH": f"{self.bin}{os.pathsep}{os.environ.get('PATH', '')}",
               "KB_VAULT_ROOT": str(self.vault),
               "KB_WEB_PORT": str(self.port)}
        return subprocess.run([str(self.root / "kb.sh"), "grab", *argv],
                              cwd=self.root, env=env, capture_output=True,
                              text=True, timeout=120)

    def _uv_calls(self) -> list[str]:
        if not self.log.exists():
            return []
        return [l[len("argv:"):] for l in self.log.read_text(encoding="utf-8").splitlines()
                if l.startswith("argv:")]

    def test_帮助打完就走_一个服务都不起(self) -> None:
        for flag in ("--help", "-h"):
            with self.subTest(flag=flag):
                r = self._run(flag)
                self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
                self.assertIn("手动收录", r.stdout)
                self.assertEqual(r.stdout.count("usage:"), 1, "帮助打了两遍:" + r.stdout)
                self.assertEqual(self._uv_calls(), [], "帮助这一档不该起服务")
                self.assertEqual(list((self.root / ".run").glob("web-*.pid")), [])

    def test_帮助夹在链接后面也认(self) -> None:
        """skill 那个 wrapper 是 `exec kb.sh grab "$@"`,位置由调用方决定。"""
        r = self._run("https://example.org/a", "--help")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("usage:", r.stdout)
        self.assertEqual(self._uv_calls(), [], "帮助这一档不该起服务")

    def test_坏参数在起服务之前就被挡住(self) -> None:
        r = self._run("--no-such-flag", "https://example.org/a")
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertEqual(self._uv_calls(), [], "参数都没过就起服务了")

    def test_正常参数照旧走到起服务那一步(self) -> None:
        """**反过来钉一边。** 少了它,把 `cmd_grab` 写成「总是打帮助」也全绿。"""
        self._run("--no-wait", "--timeout", "1", "https://example.org/a")
        self.assertTrue(any("kb-web" in c for c in self._uv_calls()),
                        "正常参数被帮助那道短路挡掉了:" + repr(self._uv_calls()))


if __name__ == "__main__":
    unittest.main()
