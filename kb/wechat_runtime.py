"""Pinned, private WeChat runtime. No skills or cloud model are needed."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import platform
import shlex
import shutil
import signal
import subprocess
import tarfile
import tempfile
from urllib.request import urlopen

HUB_REV = "f7d60b6d94c70f09ad92b89215ce40b2dd752e7f"
WXKEY_REV = "9b70eecdde47a7172b19465c3f977c86b6050e8a"
WXKEY_ARCHIVE_SHA = "f41ea4b15242f5671330b55c455e78549617c08908427b80e9e2bd73f24445de"
READER_FILES = {
    "rion_wechat_reader.py": "ad0ec2066fc259070780b1c858ee0b2f7112d5287b7bd0b6fe6963cd79158d7e",
    "rion_wechat_access.py": "1e718950ecaa7b02f4a0513d543d63adb13736ba19acab9b5562a6d55ec495a9",
}


class SetupError(Exception):
    """Only fixed, non-sensitive messages may cross the API boundary."""


def runtime_home() -> Path:
    return Path(os.getenv("KB_WECHAT_RUNTIME") or
                Path.home() / ".local/share/knowbase/wechat").expanduser()


def private_dir(path: Path) -> Path:
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise SetupError("接入目录不能是符号链接，请检查 KB_WECHAT_RUNTIME。")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)
    if os.name == 'nt':
        from kb.wechat_windows_security import restrict
        try:
            restrict(path)
        except (OSError, subprocess.TimeoutExpired):
            raise SetupError('无法保护 Windows 接入目录权限，已停止；请检查本机目录所有者和文件系统权限。') from None
    return path


def run(argv, *, timeout=120, env=None, cwd=None):
    # Capture output only in memory; never include raw stderr in exceptions/logs.
    try:
        child_env = dict(os.environ if env is None else env)
        child_env.update(PYTHONIOENCODING='utf-8', PYTHONUTF8='1')
        proc = subprocess.Popen([str(a) for a in argv], stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
                                text=True, encoding='utf-8', errors='replace', env=child_env,
                                cwd=cwd, start_new_session=True,
                                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        try:
            stdout, _ = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            if os.name == "nt":
                try:
                    subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
                except (OSError, subprocess.TimeoutExpired):
                    proc.kill()
            else:
                os.killpg(proc.pid, signal.SIGTERM)
            try:
                proc.communicate(timeout=3)
            except subprocess.TimeoutExpired:
                if os.name == "nt":
                    proc.kill()
                else:
                    os.killpg(proc.pid, signal.SIGKILL)
                try:
                    proc.communicate(timeout=3)
                except subprocess.TimeoutExpired:
                    pass
            raise SetupError("步骤超时，已停止本次命令；获取阶段请检查绿泡泡及系统授权状态后再处理。") from None
    except OSError:
        raise SetupError("所需命令无法启动，请检查运行环境和执行权限。") from None
    return proc.returncode, stdout


def checked(argv, **kwargs):
    code, output = run(argv, **kwargs)
    if code:
        raise SetupError("工具准备失败，请检查网络、磁盘空间及编译依赖后重新检查。")
    return output


def json_call(argv, *, timeout=120):
    code, output = run(argv, timeout=timeout)
    try:
        result = json.loads(output)
        if not isinstance(result, dict):
            raise ValueError
    except (ValueError, TypeError):
        raise SetupError("绿泡泡读取器返回格式不兼容，请检查或更新读取器。") from None
    # Nonzero is also used by upstream for actionable onboarding states.
    if result.get("ok") is True and isinstance(result.get("data"), dict):
        return result["data"]
    error = result.get("error")
    name = error.get("code") if isinstance(error, dict) else ""
    known = {
        "previous_run_requires_review": "上次获取尚待恢复检查，不能重复启动。请先检查绿泡泡和残留获取进程。",
        "authorization_or_worker_failed_review_required": "系统授权被取消或获取失败，请检查绿泡泡；本次不会自动重试。",
        "provider_config_exists_use_connect": "已有 wxkey 材料，需先验证复用，不能重新获取。",
        "reader_config_exists_not_overwritten": "已有 Reader 配置，已保留。请先检查配置。",
        "access_validation_failed": "数据库验证未通过，请检查账号、文件权限和依赖。",
    }
    raise SetupError(known.get(name, "绿泡泡接入未完成，已有配置已保留；请检查本机授权、绿泡泡状态和读取器兼容性。"))


def reader_command() -> list[str] | None:
    explicit = os.getenv("RION_WECHAT_CLI_BIN") or os.getenv("RION_WECHAT_READER_BIN")
    if explicit:
        return [explicit]
    codex = Path(os.getenv("CODEX_HOME") or Path.home() / ".codex")
    # Keep exactly the same order as collector/src/crawler/local_wechat.py.
    for candidate in (shutil.which("rion-wechat-cli"),
                      codex / "skills/wechat-cli/scripts/reader.sh",
                      codex / "bin/rion-wechat-cli",
                      runtime_home() / "bin/rion-wechat-cli"):
        if candidate and os.name == 'nt' and Path(candidate).suffix.lower() != '.exe':
            continue
        if candidate and Path(candidate).is_file() and os.access(candidate, os.X_OK):
            return [str(candidate)]
    if os.name == 'nt':
        from kb.wechat_windows import reader_paths
        _, python, launcher = reader_paths()
        if python.is_file() and launcher.is_file():
            return [str(python), str(launcher)]
    return None


def download(url: str, target: Path, expected: str):
    if target.is_file() and hashlib.sha256(target.read_bytes()).hexdigest() == expected:
        return
    try:
        with urlopen(url, timeout=60) as response:
            data = response.read(64 * 1024 * 1024 + 1)
        if len(data) > 64 * 1024 * 1024 or hashlib.sha256(data).hexdigest() != expected:
            raise SetupError("固定版本文件校验失败，已停止准备工具。")
        temporary = target.with_suffix(target.suffix + ".download")
        temporary.write_bytes(data)
        temporary.chmod(0o600)
        temporary.replace(target)
    except OSError:
        raise SetupError("无法下载固定版本工具，请检查 GitHub 连接后重试。") from None


def prepare_reader(progress) -> list[str]:
    root = private_dir(runtime_home())
    source = private_dir(root / HUB_REV)
    progress("preparing", "正在准备固定版本 Reader 和数据库读取依赖…")
    for filename, digest in READER_FILES.items():
        download(f"https://raw.githubusercontent.com/Rion-Wu-tech/wechat-intelligence-hub/"
                 f"{HUB_REV}/projects/rion-wechat-reader/{filename}", source / filename, digest)
    python = source / "venv/bin/python"
    uv = shutil.which("uv")
    if not python.is_file():
        if not uv:
            raise SetupError("需要 uv 来准备隔离 Python 环境，请安装本项目要求的 uv 后重试。")
        checked([uv, "venv", "--python", "3.11", source / "venv"], timeout=300)
    code, _ = run([python, "-c", "from sqlcipher3 import dbapi2; import zstandard; "
                   "assert dbapi2.connect(':memory:').execute('PRAGMA cipher_version').fetchone()"])
    if code:
        if not uv:
            raise SetupError("缺少 SQLCipher / Zstandard，请安装 uv 后重新准备。")
        checked([uv, "pip", "install", "--python", python,
                 "sqlcipher3==0.6.2", "zstandard==0.25.0"], timeout=600)
    checked([python, source / "rion_wechat_reader.py", "self-test", "--require-sqlcipher"])
    # No installation into Codex, no Skill/Profile changes. Collector discovers this launcher.
    launcher = private_dir(root / "bin") / "rion-wechat-cli"
    launcher.write_text("#!/bin/sh\nexec " + shlex.join([str(python), str(source / "rion_wechat_reader.py")])
                        + ' "$@"\n')
    launcher.chmod(0o700)
    return [str(python), str(Path(__file__).with_name("wechat_access_adapter.py")), str(source)]


def prepare_provider(progress) -> tuple[Path, str]:
    if platform.system() != "Darwin":
        raise SetupError("自动获取 key 目前仅支持 macOS；其他系统可以复用已配置的 Reader。")
    root = private_dir(runtime_home() / WXKEY_REV)
    binary, receipt = root / "wxkey", root / "build.json"
    if binary.is_file() and receipt.is_file():
        digest = hashlib.sha256(binary.read_bytes()).hexdigest()
        try:
            if json.loads(receipt.read_text()) == {"revision": WXKEY_REV, "sha256": digest}:
                return binary, digest
        except ValueError:
            pass
        raise SetupError("获取工具与已记录构建不一致，已停止。请检查私有运行目录。")
    go = shutil.which("go")
    if not go:
        raise SetupError("缺少 Go 编译器。安装 Go 后重新检查，项目会自动构建固定版本 wxkey。")
    code, _ = run(["/usr/bin/xcrun", "--find", "lldb"])
    if code:
        raise SetupError("缺少 Apple Command Line Tools（LLDB），安装后重新检查即可继续。")
    progress("preparing", "正在构建已核对的固定版本 wxkey，首次可能需要下载 Go 工具链…")
    archive = root / "source.tar.gz"
    download(f"https://codeload.github.com/r266-tech/wxkey/tar.gz/{WXKEY_REV}", archive, WXKEY_ARCHIVE_SHA)
    with tempfile.TemporaryDirectory(dir=root, prefix="build-") as folder:
        work = Path(folder)
        with tarfile.open(archive) as tar:
            for item in tar.getmembers():
                if not (work / item.name).resolve().is_relative_to(work) or not (item.isfile() or item.isdir()):
                    raise SetupError("源码归档包含不支持的路径，已停止。")
            tar.extractall(work, filter="data")
        source = work / f"wxkey-{WXKEY_REV}"
        env = {k: v for k, v in os.environ.items() if k in ("HOME", "PATH", "TMPDIR", "LANG")}
        env.update(GOTOOLCHAIN="go1.26.5", GOPROXY="https://proxy.golang.org", GOSUMDB="sum.golang.org",
                   GOENV="off", GOWORK="off")
        checked([go, "mod", "download"], cwd=source, env=env, timeout=600)
        checked([go, "mod", "verify"], cwd=source, env=env)
        checked([go, "test", "./..."], cwd=source, env=env, timeout=300)
        checked([go, "build", "-trimpath", "-o", binary, "./cmd/wxkey"], cwd=source, env=env, timeout=300)
    binary.chmod(0o700)
    digest = hashlib.sha256(binary.read_bytes()).hexdigest()
    receipt.write_text(json.dumps({"revision": WXKEY_REV, "sha256": digest}))
    receipt.chmod(0o600)
    return binary, digest
