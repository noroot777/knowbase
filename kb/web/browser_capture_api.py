from __future__ import annotations

import hmac
import io
import zipfile
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, ConfigDict, Field

from kb import browser_capture as capture, pipeline

router = APIRouter()
ROOT = Path(__file__).resolve().parents[2]


def local(request: Request) -> None:
    if (request.client is None or request.client.host not in ('127.0.0.1', '::1')
            or request.url.hostname not in ('127.0.0.1', 'localhost', '::1')):
        raise HTTPException(403, '浏览器补抓只允许本机回环地址访问')


def app_access(request: Request) -> None:
    local(request)
    origin = request.headers.get('origin')
    expected = f'{request.url.scheme}://{request.url.netloc}'
    if ((origin and origin != expected)
            or request.headers.get('sec-fetch-site', 'none') not in ('none', 'same-origin')):
        raise HTTPException(403, '请从本机知识库页面操作，拒绝站外请求')


def extension_access(request: Request) -> None:
    local(request)
    supplied = request.headers.get('authorization', '')
    if not supplied.startswith('Bearer ') or not supplied[7:].isascii() or not hmac.compare_digest(
            supplied[7:], capture.pairing_key()):
        raise HTTPException(401, '尚未配对或配对密钥不正确，请在知识库采集页重新复制密钥')
    origin = request.headers.get('origin', '')
    if origin and not origin.startswith('chrome-extension://'):
        raise HTTPException(403, '此端点仅供已配对的浏览器扩展使用')


def invoke(action, *args):
    try:
        return action(*args)
    except LookupError as error:
        raise HTTPException(404, str(error)) from None
    except ValueError as error:
        raise HTTPException(409, str(error)) from None


@router.get('/browser-capture', dependencies=[Depends(app_access)])
def capture_page():
    return FileResponse(ROOT / 'kb/static/browser-capture.html',
                        headers={'Cache-Control': 'no-store', 'Referrer-Policy': 'no-referrer'})


@router.get('/api/browser-capture/extension.zip', dependencies=[Depends(app_access)])
def extension_download():
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED) as bundle:
        for name in ('manifest.json', 'popup.html', 'popup.css', 'popup.js', 'api.js', 'extract.js', 'README.md'):
            bundle.write(ROOT / 'kb/static/browser-extension/knowbase-capture' / name,
                         'knowbase-capture/' + name)
    return Response(archive.getvalue(), media_type='application/zip',
                    headers={'Content-Disposition': 'attachment; filename="knowbase-capture.zip"'})


@router.get('/api/browser-capture/pairing', dependencies=[Depends(app_access)])
def pairing(request: Request):
    if request.headers.get('sec-fetch-site') != 'same-origin':
        raise HTTPException(403, '请在知识库补抓页面主动显示配对密钥')
    return Response(content='{"key":"' + capture.pairing_key() + '"}',
                    media_type='application/json', headers={'Cache-Control': 'no-store'})


@router.get('/api/browser-capture/target/{key}', dependencies=[Depends(app_access)])
def capture_target(key: str):
    return invoke(capture.item_state, key)


class IntentRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    key: str = Field(min_length=1, max_length=100)
    page_url: str = Field(min_length=1, max_length=4096)


@router.post('/api/browser-capture/intents', dependencies=[Depends(app_access)])
def create_intent(body: IntentRequest):
    return invoke(capture.create, body.key, body.page_url)


@router.get('/api/browser-capture/intents/{identity}', dependencies=[Depends(app_access)])
def intent_status(identity: str):
    return invoke(capture.get, identity)


@router.delete('/api/browser-capture/intents/{identity}', dependencies=[Depends(app_access)])
def cancel_intent(identity: str):
    return invoke(capture.cancel, identity)


@router.post('/api/browser-capture/intents/{identity}/continue', dependencies=[Depends(app_access)])
def continue_processing(identity: str):
    intent = invoke(capture.saved, identity)
    if not intent:
        raise HTTPException(409, '正文尚未补入，不能启动后续处理')
    reasons: list[str] = []
    run = pipeline.start_pipeline(['export', 'index', 'extract'],
                                  crawl_filter={'channel': intent['kind']}, refused=reasons)
    if not run:
        raise HTTPException(409, reasons[0] if reasons else '管道正忙，正文已保留，请稍后再执行')
    return {'run': run, 'detail': '已启动该来源后续处理：本篇使用已补正文，其他待办可能补抓、翻译或调用模型；结果以同步中心为准'}


@router.get('/api/browser-capture/pending', dependencies=[Depends(extension_access)])
def extension_pending():
    return {'intents': capture.pending()}


#: 传输上限,**不是判据** —— 「正文够不够」只有 `capture._admit_body` 一份(按行去重的
#: 400 字门槛 + 20 万字上限)。这里特意留宽一截:卡在同一个数上的话,越界那一下回的
#: 是 pydantic 的英文校验话,而不是那句能照着做的中文。
_TRANSPORT_BODY = capture.MAX_BODY * 2
_METHOD_PATTERN = '^(' + '|'.join(capture.METHODS) + ')$'


class PrecheckRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    intent_id: str = Field(min_length=1, max_length=100)
    page_url: str = Field(min_length=1, max_length=4096)
    body: str = Field(default='', max_length=_TRANSPORT_BODY)
    method: str = Field(pattern=_METHOD_PATTERN)


@router.post('/api/browser-capture/precheck', dependencies=[Depends(extension_access)])
def precheck(body: PrecheckRequest):
    """扩展在**读页之前**和**渲染预览之前**各问一次:这一页、这一份正文写得了吗。

    判据与 `upload` 同一组(`capture._admit_*`),这里只是不落库 —— 扩展自己不许
    再判网址、长度或条目版本,否则放行口径一分叉,代价就是人白读一整篇。
    """
    return invoke(capture.precheck, body.intent_id, body.page_url, body.body, body.method)


class UploadRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    intent_id: str = Field(min_length=1, max_length=100)
    page_url: str = Field(min_length=1, max_length=4096)
    title: str = Field(max_length=1000)
    body: str = Field(min_length=1, max_length=_TRANSPORT_BODY)
    method: str = Field(pattern=_METHOD_PATTERN)
    confirmed_complete: bool


@router.post('/api/browser-capture/upload', dependencies=[Depends(extension_access)])
def upload(body: UploadRequest):
    if not body.confirmed_complete:
        raise HTTPException(400, '请先核对预览并确认这是目标文章的完整正文')
    return invoke(capture.save, body.intent_id, body.page_url, body.title, body.body, body.method)
