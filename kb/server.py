# -*- coding: utf-8 -*-
"""知识库 Web 服务入口:建 app、装载各功能 router、起调度线程。

    uv run kb-web            # http://127.0.0.1:8787

索引只读(更新走 kb index / kb extract 或管道面板);问答会调 LLM API 并把
问答记录写进 conversations/messages 表。
每个请求独立开 SQLite 连接(WAL 模式),与后台索引/提取进程并存无碍。

具体端点按功能拆在 kb/web/ 下,路由路径与拆分前一致:
    pages          /、/static、/graph、/doc/<path>  前端外壳与文章阅读页
    library_api    /api/search、/api/facets、/api/stats、/api/doc、/api/projects
    chat_api       /api/ask*、/api/conversations*、/api/notes
    favorites_api  /api/favorites*
    settings_api   /api/models*、/api/stages、/api/keys、/api/mcp/*
    pipeline_api   /api/pipeline/*
    daily_api      /api/daily/*  每日日报:定时、立即生成、历史日报
"""

from __future__ import annotations

from fastapi import FastAPI

from kb import pipeline as pl
from kb.web import (
    chat_api,
    daily_api,
    favorites_api,
    grab_api,
    library_api,
    pages,
    pipeline_api,
    settings_api,
    sources_api,
    intake_api,
    wechat_api,
)

app = FastAPI(title="knowbase")

for _r in (pages.router, library_api.router, chat_api.router,
           favorites_api.router, settings_api.router, pipeline_api.router,
           sources_api.router, intake_api.router, wechat_api.router,
           daily_api.router, grab_api.router):
    app.include_router(_r)

pl.start_scheduler()   # 定时同步调度线程(跟随 kb-web 进程存活)


@app.get("/api/health")
def health():
    return {"status": "ok"}


def main() -> None:
    import os

    import uvicorn
    uvicorn.run(app, host=os.environ.get("KB_HOST", "127.0.0.1"),
                port=int(os.environ.get("PORT", "8787")))


if __name__ == "__main__":
    main()
