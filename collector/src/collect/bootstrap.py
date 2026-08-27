# -*- coding: utf-8 -*-
"""采集前的环境准备。任何入口开跑前都必须先调 install()。

现在只剩一件事,但属于"不装就静默走错路"的类型:微信域名绕过系统代理。Mac 上
系统代理通常指向 mitmproxy(wxdown),而 requests 不信任它的 CA,走过去就是
CERTIFICATE_VERIFY_FAILED。抓取本来也不该经过 mitm:mitm 只负责拦微信客户端的
流量来抓凭证,采集侧是拿着凭证直连。以前这一步写在 start_all.sh 里,只在那条启动
路径上成立;挪到这里之后,谁调用都对——CLI 被知识库当子进程拉起时,继承的是调用
方的环境。

原来这里还装两个 monkey-patch:把文章列表从 appmsgpublish 切到 profile_ext,以及
"有客户端 Credential 时就当扫码凭证也有效"。前者随旧接口一起删了(WechatRequest
现在本来就是 profile_ext 实现);后者的实际效果只是让 status 那一栏把失效的扫码
凭证报成可用——采集根本不查它,所谓"放行采集"是假的。
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


def install() -> None:
    _bypass_proxy_for_wechat()
