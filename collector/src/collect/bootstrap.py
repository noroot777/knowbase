# -*- coding: utf-8 -*-
"""采集前的环境准备。任何入口(CLI / web_app)开跑前都必须先调 install()。

这里装的两件事都属于"不装就静默走错路"的类型:

1. profile_ext 通道。微信关掉 appmsgpublish 之后,文章列表只能靠 Mac 微信的
   profile_ext Credential 拿。装载是 monkey-patch 到 WechatRequest 上的,
   忘了装不会报错——只会退回那个已经废掉的老接口,然后每个号都失败。

2. 微信域名绕过系统代理。Mac 上系统代理通常指向 mitmproxy(wxdown),而
   requests 不信任它的 CA,走过去就是 CERTIFICATE_VERIFY_FAILED。抓取本来也
   不该经过 mitm:mitm 只负责拦微信客户端的流量来抓凭证,采集侧是拿着凭证直连。
   以前这一步写在 start_all.sh 里,只在那条启动路径上成立;挪到这里之后,
   谁调用都对——CLI 被知识库当子进程拉起时,继承的是调用方的环境。
"""

from __future__ import annotations

import os

_WECHAT_NO_PROXY = "127.0.0.1,localhost,.weixin.qq.com"


def _bypass_proxy_for_wechat() -> None:
    for var in ("NO_PROXY", "no_proxy"):
        current = os.environ.get(var, "").strip()
        if ".weixin.qq.com" in current:
            continue
        os.environ[var] = f"{current},{_WECHAT_NO_PROXY}" if current else _WECHAT_NO_PROXY


def _install_profile_auth_fallback() -> None:
    """有客户端 Credential 时,即使公众平台扫码凭证失效也允许进入抓取循环。

    两套凭证是独立的:扫码 token 只用于搜号,抓文章列表走 profile_ext。
    不放行的话,扫码凭证一过期就把还能正常工作的抓取一起卡死。
    """
    import api

    from src.crawler.profile_ext import profile_credential_store

    current = api._check_auth_valid
    if getattr(current, "_profile_ext_aware", False):
        return

    def combined_check() -> tuple[bool, str]:
        valid, reason = current()
        if not valid and profile_credential_store.has_records():
            return True, "已切换为 Mac 微信 profile_ext Credential"
        return valid, reason

    combined_check._profile_ext_aware = True   # type: ignore[attr-defined]
    api._check_auth_valid = combined_check


def install() -> None:
    from src.crawler.profile_adapter import install_profile_ext_support

    _bypass_proxy_for_wechat()
    _install_profile_auth_fallback()
    install_profile_ext_support()
