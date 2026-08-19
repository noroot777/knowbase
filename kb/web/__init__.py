# -*- coding: utf-8 -*-
"""kb-web 的 HTTP 层,按功能拆成若干 APIRouter 模块。

kb/server.py 只负责建 app 并把这里的 router 装上去;每个模块自带一个
`router = APIRouter()`,路由路径与拆分前完全一致(前端无需改动)。
"""
