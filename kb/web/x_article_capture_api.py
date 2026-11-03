"""人工补 X 长文的三个端点(`kb.feeds.x_article_capture`)。

鉴权照搬「从浏览器采集」那一族:列表给知识库页面(`app_access`),问这一页、交正文给已配对的
扩展(`extension_access`,配对密钥同一把)。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field

from kb.feeds import x_article_capture as capture
from kb.web.browser_capture_api import app_access, extension_access, invoke

router = APIRouter()


@router.get('/api/x-article-capture/owed', dependencies=[Depends(app_access)])
def owed(source_id: int | None = None):
    return {'items': invoke(capture.owed, source_id)}


class HereRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    url: str = Field(min_length=1, max_length=4096)


@router.post('/api/x-article-capture/here', dependencies=[Depends(extension_access)])
def here(body: HereRequest):
    return invoke(capture.here, body.url)


class SaveRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    post_id: str = Field(min_length=1, max_length=40, pattern=r'^\d+$')
    page_url: str = Field(min_length=1, max_length=4096)
    #: 读页脚本(`x_article_read.js`)交回的那一份快照,原样转给 `x_article.read_snapshot` 判。
    snapshot: dict


@router.post('/api/x-article-capture/save', dependencies=[Depends(extension_access)])
def save(body: SaveRequest):
    return invoke(capture.save, body.post_id, body.page_url, body.snapshot)
