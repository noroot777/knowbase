"""Bounded background onboarding with cross-process exclusion and resumable status."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import threading
import time

from kb import wechat_runtime as runtime

MESSAGES = {
    "needs_database_location": "未发现微信数据库。请在运行本项目的 Mac 上登录自己的微信并同步消息，再重新检查。",
    "existing_configuration_requires_review": "已有 Reader 配置目前不可读，已保留。请检查微信账号、数据库位置和读取依赖，不能直接重新获取 key。",
    "dependency_required": "数据库读取依赖尚未就绪，请检查 SQLCipher 和 Zstandard。",
    "unsafe_key_permissions": "本机密钥文件权限过宽，请在本机收紧至 0600 后重新检查。",
    "partial": "仍有数据库未通过读取验证，未发布配置。请检查覆盖缺口后再继续。",
    "verification_failed": "材料或数据库结构验证失败，请检查账号、密钥映射及微信版本。",
    "access_material_requires_review": "已有 wxkey 材料未通过验证，已保留，请先检查材料与账号是否匹配。",
    "filesystem_access_required": "数据库文件无法访问，请检查本项目的文件访问权限。",
    "acquisition_platform_not_supported": "自动获取 key 目前仅支持 macOS。可复用其他系统上已配置的 Reader。",
    "database_layout_unsupported": "尚未识别到可用微信数据库结构，请检查登录、同步和微信版本。",
    "database_layout_ambiguous": "核心数据库候选不唯一，请检查是否混入其他账号。",
    "scan_incomplete": "数据库扫描达到上限，未声称完整可读。请缩小数据库范围。",
}
_mutex = threading.Lock()


def account_roots() -> dict[str, Path]:
    base = Path.home() / "Library/Containers/com.tencent.xinWeChat/Data/Documents/xwechat_files"
    return {hashlib.sha256(str(p).encode()).hexdigest()[:16]: p
            for p in sorted(base.glob("*/db_storage")) if p.is_dir() and not p.is_symlink()}


def _state_file():
    return runtime.runtime_home() / "setup-state.json"


def _read():
    try:
        result = json.loads(_state_file().read_text())
        if isinstance(result, dict):
            return result
    except (OSError, ValueError):
        pass
    return {"state": "unchecked", "message": "检查本机微信，已有可用配置会直接复用。", "running": False}


def _write(state):
    target = _state_file()
    temporary = target.with_suffix(".tmp")
    temporary.write_text(json.dumps(state, ensure_ascii=False))
    temporary.chmod(0o600)
    temporary.replace(target)


def _lock():
    path = runtime.private_dir(runtime.runtime_home()) / "setup.lock"
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        raise runtime.SetupError("已有微信接入任务正在执行，请等待当前任务结束。") from None
    return fd


def snapshot():
    state = _read()
    if state.get("running"):
        # A crashed server must not leave an eternal spinner or auto-resume acquisition.
        try:
            fd = _lock()
        except runtime.SetupError:
            pass
        else:
            os.close(fd)
            state = {"state": "interrupted", "running": False,
                     "message": "上次接入已中断。请检查微信及系统授权状态，然后重新检查；不会自动重试获取。"}
    return state


def _reader_ready():
    command = runtime.reader_command()
    if not command:
        return False
    try:
        data = runtime.json_call([*command, "doctor"])
        status = data.get("status", {})
        status = status.get("status", status)
        return status.get("live_database_read_ok") is True
    except (runtime.SetupError, AttributeError):
        return False


def _config():
    return Path(os.getenv("RION_WECHAT_READER_CONFIG") or
                Path.home() / ".config/rion-wechat-reader/config.json").expanduser()


def _recovery_lock():
    return Path.home() / ".config/rion-wechat-reader/access-runs/recovery-required.lock"


def _ready(progress):
    if not _reader_ready():
        raise runtime.SetupError("配置已验证，但采集器实际使用的 Reader 仍不可读。请检查 RION_WECHAT_CLI_BIN 和读取器依赖。")
    # Invalidate the existing source badge after a successful connection.
    from kb.collectors.wechat_local import collector
    collector._local_cache = None
    progress("ready", "微信数据库已验证可读。可以切换到本地微信通道，选择公众号并开始同步。", ready=True)


def _backup(root: Path, progress):
    progress("backing_up", "正在备份本机加密数据库，随后会请求 macOS 系统授权…")
    parent = runtime.private_dir(runtime.runtime_home() / "backups")
    destination = runtime.private_dir(parent / secrets.token_hex(8))
    # Encrypted files and sidecars only; never decrypt/export messages here.
    for path in root.rglob("*"):
        if path.is_symlink():
            raise runtime.SetupError("数据库目录含符号链接，已停止备份和获取。")
        if path.is_file() and path.name.endswith((".db", ".db-wal", ".db-shm")):
            target = destination / path.relative_to(root)
            runtime.private_dir(target.parent)
            shutil.copyfile(path, target)
            target.chmod(0o600)


def _connect(progress, account_id, acquire=False):
    progress("checking", "正在检查已有 Reader 配置与本地数据库…")
    if _reader_ready():
        return _ready(progress)
    if _recovery_lock().exists():
        raise runtime.SetupError("上次获取需要恢复检查：请先确认微信和残留获取进程正常。不会自动清除恢复锁或再次获取。")
    roots = account_roots()
    if not _config().exists():
        if not roots:
            return progress("needs_database_location", MESSAGES["needs_database_location"])
        if not account_id and len(roots) > 1:
            return progress("account_selection_required", "发现多个本机账号，请选择要接入的本人账号。",
                            accounts=[{"id": key, "label": p.parent.name} for key, p in roots.items()])
        if account_id and account_id not in roots:
            raise runtime.SetupError("所选账号目录已变化，请重新检查。")
    root = roots.get(account_id) if account_id else next(iter(roots.values()), None)
    helper = runtime.prepare_reader(progress)
    args = [*helper, "onboard", "--config", str(_config())]
    # Existing configuration must be checked without explicit roots overriding it.
    if not _config().exists() and root:
        args += ["--database-root", str(root)]
        source = Path.home() / ".config/wxcli/config.json"
        if source.is_file():
            args += ["--source", str(source)]
    progress("verifying", "正在验证本机已有材料和数据库，密钥不会显示在页面中…")
    plan = runtime.json_call(args)
    state = plan.get("state")
    if state == "ready":
        return _ready(progress)
    if state == "ready_to_configure":
        result = runtime.json_call([*args, "--apply"])
        if result.get("state") == "ready":
            return _ready(progress)
        state = result.get("state")
    if state not in ("provider_required", "needs_access"):
        return progress(state if state in MESSAGES else "blocked",
                        MESSAGES.get(state, "接入检查未通过，请检查本机配置、权限和微信兼容性。"))
    provider, digest = runtime.prepare_provider(progress)
    reviewed = [*args, "--provider", str(provider), "--sha256", digest]
    plan = runtime.json_call(reviewed)
    if plan.get("state") != "authorization_required":
        raise runtime.SetupError("固定工具未通过获取前检查，本次未执行获取。")
    selected = next((key for key, p in roots.items() if p == root), "")
    if not acquire:
        return progress("authorization_required", "工具已准备好。确认下方影响后，点击自动获取并验证。",
                        confirmation_token=secrets.token_urlsafe(32), expires_at=time.time() + 900,
                        account_id=selected, provider_revision=runtime.WXKEY_REV)
    if root is None:
        raise runtime.SetupError("账号目录不可用，已停止获取。")
    _backup(root, progress)
    progress("acquiring", "请在这台 Mac 上完成系统授权；可能需要重新登录微信。获取最多 10 分钟，授权等待另有 3 分钟。")
    result = runtime.json_call([*reviewed, "--apply", "--confirm-reviewed-provider",
                                "--confirm-side-effects", "--timeout", "600"], timeout=840)
    if result.get("state") != "ready":
        return progress("blocked", MESSAGES.get(result.get("state"), "获取结束，但数据库尚未验证可读。请检查微信和恢复状态。"))
    progress("verifying", "正在确认采集器能使用新配置读取数据库…")
    return _ready(progress)


def _work(fd, account_id, acquire):
    def progress(state, message, **extra):
        _write({"state": state, "message": message, "running": True,
                "updated_at": time.time(), **extra})
    try:
        _connect(progress, account_id, acquire)
    except runtime.SetupError as error:
        progress("failed", str(error))
    except Exception:
        # Raw exceptions may contain paths, material or subprocess output.
        progress("failed", "接入未完成，已有配置已保留。请检查本机文件权限、磁盘空间和读取器环境。")
    finally:
        try:
            state = _read()
            state["running"] = False
            _write(state)
        finally:
            os.close(fd)


def start(action="connect", account_id="", confirmation_token="", confirmed=False):
    from kb.deployment import in_container, wechat_container_status
    if in_container():
        raise runtime.SetupError(wechat_container_status()["message"])
    with _mutex:
        fd = _lock()
        try:
            if action == "acquire":
                previous = _read()
                token = previous.get("confirmation_token", "")
                if (previous.get("state") != "authorization_required" or not confirmed or not token
                        or not secrets.compare_digest(token, confirmation_token)
                        or previous.get("expires_at", 0) <= time.time()):
                    raise runtime.SetupError("获取授权已失效或未确认影响，请重新检查后再执行。")
                account_id = previous.get("account_id", "")
            elif action != "connect":
                raise runtime.SetupError("不支持的微信接入操作。")
            _write({"state": "checking", "message": "正在检查微信接入…", "running": True,
                    "updated_at": time.time()})
            thread = threading.Thread(target=_work, args=(fd, account_id, action == "acquire"), daemon=True)
            thread.start()
        except Exception:
            os.close(fd)
            raise
    return snapshot()
