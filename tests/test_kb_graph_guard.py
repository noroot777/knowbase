# -*- coding: utf-8 -*-
"""`./kb.sh graph` 那道「仓库内 data/ 只许在主检出跑」的闸门。

命令级测试:真起一个 git 仓库 + 一个 linked worktree,把 kb.sh 原样拷进去跑。
不打桩任何路径逻辑 —— 这道闸门的全部风险就在「两个字符串是不是同一个目录」上,
把判定抄一份到测试里等于把要测的东西自己实现一遍。

构建器用 PATH 上的 uv 桩挡住:闸门放行之后的事(读库、发布页面)不在本文件范围内,
而真跑一次要几秒、还要一份真库。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
KB_SH = REPO / "kb.sh"

GIT_ENV = {
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
    "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull,
}

UV_STUB = "#!/bin/sh\necho \"uv $*\" >&2\nexit 0\n"


def _git(*args: str, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, env={**os.environ, **GIT_ENV},
                          capture_output=True, text=True, check=True)


def _git_has_path_format() -> bool:
    r = subprocess.run(["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
                       cwd=REPO, capture_output=True, text=True)
    return r.returncode == 0


@unittest.skipUnless(_git_has_path_format(), "git 太老,不认 --path-format")
class GraphGuardTest(unittest.TestCase):
    """闸门只认「这个目录是不是仓库内的 data/」,别名换写法不算换目录。"""

    @classmethod
    def setUpClass(cls) -> None:
        # realpath:macOS 的 mktemp 在 /var 下,而 /var 是指向 /private/var 的软链接。
        # kb.sh 的 ROOT 取的是 cd+pwd(逻辑路径),git 回的是真实路径,不统一的话
        # 主检出那一格会被误判成 worktree。
        cls._tmp = tempfile.mkdtemp()
        base = Path(os.path.realpath(cls._tmp))
        cls.main = base / "repo"
        cls.wt = base / "wt"
        cls.vault = base / "vault"
        cls.外置 = base / "outside-data"
        cls.空外置 = base / "outside-empty"
        for d in (cls.main, cls.vault, cls.外置, cls.空外置):
            d.mkdir(parents=True)

        _git("init", "-q", "-b", "main", cwd=cls.main)
        (cls.main / "seed").write_text("x", encoding="utf-8")
        _git("add", "seed", cwd=cls.main)
        _git("commit", "-qm", "seed", cwd=cls.main)
        _git("worktree", "add", "-q", "-b", "side", str(cls.wt), cwd=cls.main)

        # 两个检出各放一份 kb.sh,和各自的 data/(里面备齐归类缓存 —— 这样
        # 「没被拦下」一定是闸门放行,而不是撞上了后面那道缺缓存检查)。
        for root in (cls.main, cls.wt):
            shutil.copy2(KB_SH, root / "kb.sh")
            (root / "kb.sh").chmod(0o755)
            cls._seed_cache(root / "data")
        cls._seed_cache(cls.外置)
        (cls.wt / "alias").symlink_to(cls.wt / "data")   # 同一个目录的第三种写法

        stub = base / "bin"
        stub.mkdir()
        (stub / "uv").write_text(UV_STUB, encoding="utf-8")
        (stub / "uv").chmod(0o755)
        cls.path = f"{stub}{os.pathsep}{os.environ.get('PATH', '')}"

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls._tmp, ignore_errors=True)

    @staticmethod
    def _seed_cache(d: Path) -> None:
        d.mkdir(parents=True, exist_ok=True)
        for name in ("taxonomy_projects.json", "taxonomy_skills.json"):
            (d / name).write_text('{"sig": "x", "items": {}}', encoding="utf-8")

    def _run(self, root: Path, data_dir: str | None) -> subprocess.CompletedProcess:
        env = {**os.environ, **GIT_ENV, "PATH": self.path,
               "KB_VAULT_ROOT": str(self.vault)}
        env.pop("KB_DATA_DIR", None)
        if data_dir is not None:
            env["KB_DATA_DIR"] = data_dir
        return subprocess.run([str(root / "kb.sh"), "graph"], cwd=root, env=env,
                              capture_output=True, text=True, timeout=120)

    def _assert_blocked(self, data_dir: str | None, why: str) -> None:
        r = self._run(self.wt, data_dir)
        self.assertEqual(r.returncode, 1, f"{why}:应当被拦下\n{r.stderr}")
        self.assertIn("这是 worktree", r.stderr, why)
        self.assertNotIn("重建三张图谱", r.stderr, f"{why}:不该进构建器")

    def test_默认数据目录在_worktree_被拦下(self) -> None:
        self._assert_blocked(None, "没设 KB_DATA_DIR")

    def test_显式指向仓库_data_被拦下(self) -> None:
        self._assert_blocked(str(self.wt / "data"), "写全路径")

    def test_父目录回退写法被拦下(self) -> None:
        # 复审第 2 轮报的就是这条:pathlib 不消解 `..`,原来按字符串比当场放行,
        # 话题图谱拿空库发布了一张 0 节点页面还报「好了」。
        self._assert_blocked(f"{self.wt / 'data'}/../data", "data/../data")

    def test_混着点和父目录的写法被拦下(self) -> None:
        self._assert_blocked(f"{self.wt}/./data/./../data", "./data/./../data")

    def test_相对路径写法被拦下(self) -> None:
        self._assert_blocked("data/../data", "相对路径")

    def test_符号链接别名被拦下(self) -> None:
        self._assert_blocked(str(self.wt / "alias"), "软链接")

    def test_外置数据目录照常放行(self) -> None:
        r = self._run(self.wt, str(self.外置))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("这是 worktree", r.stderr)
        self.assertIn("重建三张图谱", r.stderr)

    def test_外置目录缺归类缓存时报那个目录(self) -> None:
        r = self._run(self.wt, str(self.空外置))
        self.assertEqual(r.returncode, 1)
        self.assertIn(str(self.空外置), r.stderr)
        self.assertNotIn("这是 worktree", r.stderr)

    def test_主检出不受闸门影响(self) -> None:
        r = self._run(self.main, None)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("这是 worktree", r.stderr)
        self.assertIn("重建三张图谱", r.stderr)


if __name__ == "__main__":
    unittest.main()
