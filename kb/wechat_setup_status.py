"""Strict setup status reads and lossless, explicit recovery backups."""

import json
import math
import os
import secrets
import shutil
import time

from kb.wechat_runtime import SetupError


UNAVAILABLE = "无法读取绿泡泡接入状态，不能确认任务是否运行。请刷新，或在确认空闲后仅重置状态；不会自动获取或覆盖材料。"


def _text(value):
    if not isinstance(value, str):
        return False
    try:
        value.encode("utf-8")
    except UnicodeError:
        return False
    return True


def _timestamp(value):
    try:
        return (isinstance(value, (int, float)) and not isinstance(value, bool)
                and math.isfinite(value) and value >= 0)
    except OverflowError:
        return False


def normalize(state):
    if (not isinstance(state, dict) or not _text(state.get("state"))
            or not state["state"].strip() or state["state"] == "status_unavailable"):
        raise SetupError(UNAVAILABLE)
    result = {"state": state["state"]}
    for field in ("message", "platform", "deployment"):
        if field in state:
            if not _text(state[field]):
                raise SetupError(UNAVAILABLE)
            result[field] = state[field]
    for field in ("running", "ready", "can_connect", "can_reset_status", "retryable",
                  "provider_ready", "models_ready"):
        if field in state:
            if not isinstance(state[field], bool):
                raise SetupError(UNAVAILABLE)
            result[field] = state[field]
    if "updated_at" in state:
        if not _timestamp(state["updated_at"]):
            raise SetupError(UNAVAILABLE)
        result["updated_at"] = state["updated_at"]
    if state["state"] == "account_selection_required":
        accounts = state.get("accounts")
        result["accounts"] = []
        if isinstance(accounts, list) and all(isinstance(account, dict)
                                            and _text(account.get("id")) for account in accounts):
            result["accounts"] = [{"id": account["id"]} for account in accounts]
    elif state["state"] == "authorization_required":
        for field in ("confirmation_token", "account_id", "provider_revision"):
            if _text(state.get(field)):
                result[field] = state[field]
        if _timestamp(state.get("expires_at")):
            result["expires_at"] = state["expires_at"]
    return result


def read(path):
    for attempt in range(6):
        try:
            return normalize(json.loads(path.read_text(encoding="utf-8")))
        except FileNotFoundError:
            return {"state": "unchecked", "running": False,
                    "message": "检查本机绿泡泡，已有可用配置会直接复用。"}
        except PermissionError:
            if attempt < 5:
                time.sleep(.02 * (attempt + 1))
                continue
        except (OSError, ValueError, RecursionError):
            pass
        raise SetupError(UNAVAILABLE) from None


def backup(path):
    if path.is_symlink() or not path.is_file():
        raise SetupError("状态文件不是普通文件，未执行重置。请先检查状态文件路径与权限。")
    destination = path.with_name(f"{path.name}.corrupt-{secrets.token_hex(12)}.bak")
    try:
        with path.open("rb") as source:
            descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "wb") as target:
                shutil.copyfileobj(source, target)
                target.flush()
                os.fsync(target.fileno())
        destination.chmod(0o600)
    except OSError:
        raise SetupError("无法完整备份原状态，未执行重置。请检查状态文件权限和磁盘空间后重试。") from None
    return destination
