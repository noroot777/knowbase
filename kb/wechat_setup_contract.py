"""Validate interactive setup states at the shared publication boundary."""

import re
import time

from kb.wechat_setup_status import normalize


def validate(state, roots, revision, platform):
    state = normalize(state)
    kind = state.get("state")
    if kind not in {"account_selection_required", "authorization_required"}:
        return state
    common = {key: state[key] for key in ("updated_at", "running", "platform") if key in state}
    if kind == "account_selection_required":
        accounts = state.get("accounts")
        identifiers = []
        if isinstance(accounts, list) and accounts:
            for account in accounts:
                identifier = account.get("id") if isinstance(account, dict) else None
                if (not isinstance(identifier, str) or not identifier or len(identifier) > 32
                        or identifier not in roots or identifier in identifiers):
                    break
                identifiers.append(identifier)
            else:
                message = state.get("message")
                return {**common, "state": kind,
                        "message": message if isinstance(message, str) else "发现多个本机账号，请选择要接入的本人账号。",
                        "accounts": [{"id": identifier, "label": roots[identifier].parent.name}
                                     for identifier in identifiers]}
        return {**common, "state": "needs_database_location", "ready": False,
                "can_connect": True, "retryable": True,
                "message": "Reader 未提供可选择的有效账号。请将数据库目录修正为本人单账号的 db_storage，再重新检查；不会自动获取材料。"}
    token = state.get("confirmation_token")
    expires = state.get("expires_at")
    account_id = state.get("account_id")
    if (isinstance(token, str) and re.fullmatch(r"[A-Za-z0-9_-]{32,128}", token)
            and isinstance(expires, (int, float)) and not isinstance(expires, bool)
            and time.time() < expires <= time.time() + 900
            and isinstance(account_id, str) and 0 < len(account_id) <= 32 and account_id in roots
            and isinstance(revision, str) and revision and state.get("provider_revision") == revision
            and platform in {"darwin", "windows"}
            and state.get("platform", "darwin") == platform):
        return state
    return {**common, "state": "blocked", "ready": False, "can_connect": True,
            "retryable": True, "message": "获取授权信息不完整、已过期或账号范围已变化。请重新检查并再次确认影响；本次未执行获取。"}
