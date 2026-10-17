# -*- coding: utf-8 -*-
"""`./kb.sh grab` 这层壳:**帮助和坏参数都不许先起一个守护进程**。

argparse 的 `--help` 让子进程以 0 退出,和「参数校验通过」一模一样 —— 光靠
`--parse-only || return` 分不开这两件事,于是帮助打完之后 shell 照旧往下走:
检查工作树、`web_start`、再把帮助打第二遍。worktree 里更难看:先撞上那道闸门,
帮助压根拿不到,还以 1 退出(实测)。

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
        # **五个文件一套**(入口 + 通道层 + 输入层的两趟 + 输出层):单文件
        # 700 行的上限之下按职责拆的,少拷一个,隔离环境里那条命令一 import 就炸,
        # 而红出来的话指不到「夹具没拷全」这件事。
        for name in ("grab_cli.py", "grab_http.py", "grab_input.py",
                     "grab_md.py", "grab_say.py"):
            shutil.copy2(REPO / "scripts" / name, self.root / "scripts")
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

    def test_api_指到别处时不起本机那一份(self) -> None:
        """`--api` 指到别处(另一台机器、自己另起的一份)时,这一层的闸门和
        `web_start` 管的是**另一个**服务:主检出会多起一份用户压根不需要的守护
        进程,worktree 里更糟 —— 在连上他给的那个地址之前就 die。那种情况下服务的
        生命周期归他自己。
        """
        r = self._run("--api", "http://127.0.0.1:1", "--no-wait",
                      "https://example.org/a")
        self.assertEqual([c for c in self._uv_calls() if "kb-web" in c], [],
                         "指了别处的服务,还起了本机这一份:" + repr(self._uv_calls()))
        self.assertNotIn("Traceback", r.stderr)

    def test_api_指的就是本机那份时照旧起服务(self) -> None:
        """**反过来钉一边**:判据是「目标是不是本机托管那一份」,不是「有没有给
        `--api`」。少了它,把那段写成「给了 --api 就一律不起」也全绿,而那会让
        `--api http://127.0.0.1:$KB_WEB_PORT` 这种写法从此收不进知识库。
        """
        self._run("--api", f"http://127.0.0.1:{self.port}", "--no-wait",
                  "--timeout", "1", "https://example.org/a")
        self.assertTrue(any("kb-web" in c for c in self._uv_calls()),
                        "目标就是本机那份,却没起:" + repr(self._uv_calls()))

    def test_协议名的大小写不改变谁管这份服务(self) -> None:
        """**同一个地址的两种合法拼写,不许得出两个「要不要管本机服务」的答案。**

        协议名按 RFC 3986 不分大小写,`urlsplit` 认得、urllib 也照打 —— 而外壳
        那道 `case ... in http://*|https://*` 是**大小写敏感**的。回传原串的那一版
        下 `HTTP://…` 不命中,于是被当成「目标是本机那份」:主检出多起一份用户
        压根不需要的守护进程(worktree 里更糟,那道闸门会抢在前面 die)。实测
        `HTTP://` 和 `HtTp://` 都真的起了一份 kb-web。

        出路是**在参数层规范化一次,打出去的和回传的是同一份**
        (`grab_http.normalize_api`),不是在外壳里多加几个大小写分支。
        """
        for api in ("HTTP://127.0.0.1:1", "HtTp://127.0.0.1:1"):
            with self.subTest(api=api):
                r = self._run("--api", api, "--no-wait", "https://example.org/a")
                self.assertEqual(
                    [c for c in self._uv_calls() if "kb-web" in c], [],
                    f"{api} 指的是别处,还起了本机这一份:" + repr(self._uv_calls()))
                self.assertNotIn("Traceback", r.stderr)


if __name__ == "__main__":
    unittest.main()
