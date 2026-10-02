# -*- coding: utf-8 -*-
"""采集前的环境准备。任何入口开跑前都必须先调 install()。

只剩一件事,但属于"不装就静默走错路"的类型:绿泡泡域名绕过系统代理。抓正文是
直连 mp.weixin.qq.com,不该经过任何 MITM 代理——requests 不信任这类代理自签的
CA,走过去就是 CERTIFICATE_VERIFY_FAILED,而报错指向证书、看不出是代理的事。
写在这里而不是某个启动脚本里:CLI 被知识库当子进程拉起时继承的是调用方的环境,
谁调用都得先过这一步。
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
