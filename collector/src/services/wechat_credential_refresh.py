"""通过 Mac 微信界面触发公众号 Credential 刷新。"""

from __future__ import annotations

import platform
import subprocess
from dataclasses import asdict, dataclass


_JXA_SCRIPT = r'''
ObjC.import("AppKit")

function run(argv) {
  const accountName = String(argv[0] || "").trim()
  if (!accountName) throw new Error("EMPTY_ACCOUNT_NAME")

  const events = Application("System Events")
  if (!events.uiElementsEnabled()) throw new Error("ACCESSIBILITY_DISABLED")

  const pasteboard = $.NSPasteboard.generalPasteboard
  const previousItems = pasteboard.pasteboardItems
  const wechat = Application("WeChat")

  try {
    wechat.activate()
    delay(0.8)

    const process = events.processes.byName("WeChat")
    process.frontmost = true

    pasteboard.clearContents
    pasteboard.setStringForType($(accountName), $.NSPasteboardTypeString)

    events.keystroke("f", { using: ["command down"] })
    delay(0.25)
    events.keystroke("a", { using: ["command down"] })
    events.keystroke("v", { using: ["command down"] })
    delay(2.0)

    // 微信搜索结果没有公开稳定的 Accessibility 标识；使用键盘选择第一项。
    events.keyCode(125)
    delay(0.15)
    events.keyCode(36)
    delay(1.0)

    return JSON.stringify({ ok: true, account_name: accountName })
  } finally {
    // 尽量恢复用户原有剪贴板；恢复失败时保留公众号名，便于手工粘贴搜索。
    try {
      if (previousItems && previousItems.count > 0) {
        pasteboard.clearContents
        pasteboard.writeObjects(previousItems)
      }
    } catch (_) {}
  }
}
'''


@dataclass(frozen=True)
class WeChatRefreshResult:
    started: bool
    account_name: str
    message: str

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


class WeChatRefreshError(RuntimeError):
    """Mac 微信自动刷新无法启动。"""


def trigger_wechat_credential_refresh(account_name: str) -> WeChatRefreshResult:
    """唤起微信、搜索公众号并打开第一条匹配结果。"""
    name = str(account_name or "").strip()
    if not name:
        raise WeChatRefreshError("公众号名称为空")
    if len(name) > 100 or any(ord(char) < 32 for char in name):
        raise WeChatRefreshError("公众号名称格式不正确")
    if platform.system() != "Darwin":
        raise WeChatRefreshError("自动刷新目前仅支持运行知识库服务的 Mac")

    try:
        completed = subprocess.run(
            [
                "/usr/bin/osascript",
                "-l",
                "JavaScript",
                "-e",
                _JXA_SCRIPT,
                name,
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=12,
        )
    except FileNotFoundError as exc:
        raise WeChatRefreshError("当前系统缺少 osascript，无法操作 Mac 微信") from exc
    except subprocess.TimeoutExpired as exc:
        raise WeChatRefreshError("微信自动搜索超时，请确认微信已经登录") from exc

    if completed.returncode != 0:
        details = f"{completed.stderr}\n{completed.stdout}".strip()
        if "ACCESSIBILITY_DISABLED" in details or "not authorized" in details.lower():
            raise WeChatRefreshError(
                "需要在系统设置 → 隐私与安全性 → 辅助功能中允许终端或 Codex 操作微信"
            )
        if "Application isn't running" in details or "Invalid index" in details:
            raise WeChatRefreshError("未能打开微信，请确认 Mac 微信已安装并已登录")
        raise WeChatRefreshError("微信自动搜索失败，请打开微信后重试")

    return WeChatRefreshResult(
        started=True,
        account_name=name,
        message="已在 Mac 微信中搜索该公众号，正在等待文章请求和 Credential",
    )
