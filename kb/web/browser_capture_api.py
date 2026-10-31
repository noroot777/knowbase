from __future__ import annotations

import hmac
import io
import zipfile
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
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


EXTENSION_DIR = ROOT / 'kb/static/browser-extension/knowbase-capture'


@router.get('/api/browser-capture/setup', dependencies=[Depends(app_access)])
def setup():
    """装扩展这一步只要知道一件事:装哪个目录。

    **目录给的是绝对路径,不是只给一个 ZIP。** 这个扩展是「加载已解压的扩展程序」
    装的,而仓库里本来就躺着那个目录 —— 下载再解压一份等于把同一份代码在磁盘上放
    两处,之后改了代码,装着的还是解压出来那一份,而人不会知道自己装的是旧的。指到
    仓库里这一份,改完在扩展管理页点一次「重新加载」就生效(弹窗头部那个版本号就是
    用来确认这一下点过没有的)。ZIP 留着,给「浏览器不在这台机器上」那种情形。

    **这里不回答「配过没有」。** 它曾经捎一个 `used_before` 回去(判据是
    `browser_capture_audit` 里有没有过一行),抽屉据此决定首次引导摆在哪 —— 而那是
    **另一个范畴的事实**:回执是全库共享的历史,引导问的是此刻**这个浏览器**的配对
    状态。服务端手上根本没有后者,它只知道密钥存在、以及有人拿着密钥来过 —— 换个
    浏览器、重装一次扩展,它一个字都看不出来。那一问现在由扩展自己在页面上回答
    (`present.js` 标一个 data 属性),这张回执表回到它本来的职责:记采集回执。
    """
    return {'extension_dir': str(EXTENSION_DIR)}


@router.get('/api/browser-capture/extension.zip', dependencies=[Depends(app_access)])
def extension_download():
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED) as bundle:
        for name in ('manifest.json', 'popup.html', 'popup.css', 'popup.js', 'api.js',
                     'extract.js', 'present.js', 'README.md'):
            bundle.write(EXTENSION_DIR / name,
                         'knowbase-capture/' + name)
    return Response(archive.getvalue(), media_type='application/zip',
                    headers={'Content-Disposition': 'attachment; filename="knowbase-capture.zip"'})


@router.get('/api/browser-capture/pairing', dependencies=[Depends(app_access)])
def pairing(request: Request):
    if request.headers.get('sec-fetch-site') != 'same-origin':
        raise HTTPException(403, '请在知识库的采集抽屉里主动显示配对密钥')
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


#: 这颗按钮**只跑这两段**,而且页面、确认框、下面那句回执必须跟这份清单一致。
#:
#: `extract`(条目化提取)**不在里面**:它的队列是 `kb.extract.pending_docs`,压根
#: 没有来源参数,按全库取数 —— 一颗开在某一篇文章页上的按钮不该顺手买下全库那一轮
#: 模型调用。`export` 这一段认 `crawl_filter`(只有这一类来源,见
#: `pipeline_stages._run_export`),`index` 是整个 vault 的增量比对、不调模型,这两件
#: 事页面上都明写着。要提取就到同步中心的起跑弹窗里勾,那边的射程写在脸上
#: (见 `docs/decisions/sync-entry.md`);这一篇的 `extract_stale=1` 还在,下一轮同步
#: 自己会把它重新提一遍。
CONTINUE_STAGES = ('export', 'index')


@router.post('/api/browser-capture/intents/{identity}/continue', dependencies=[Depends(app_access)])
def continue_processing(identity: str):
    intent = invoke(capture.saved, identity)
    if not intent:
        raise HTTPException(409, '正文尚未补入，不能启动导出与索引')
    reasons: list[str] = []
    run = pipeline.start_pipeline(list(CONTINUE_STAGES),
                                  crawl_filter={'channel': intent['kind']}, refused=reasons)
    if not run:
        raise HTTPException(409, reasons[0] if reasons else '管道正忙，正文已保留，请稍后再执行')
    # 这一句会原样显示在页面的 `#run-note` 上,所以只说「起了哪两段、钱花在哪」;
    # 完整的射程说明在页面那块警告里,别在这里再抄一遍。
    return {'run': run, 'detail': '已启动导出与索引：本篇使用已补正文；同类来源其他待办会一起导出，'
                                  '可能联网补抓并调用翻译模型；索引会检查全库。'
                                  '条目化提取不在这一步，等下一轮同步。结果以同步中心为准'}


@router.get('/api/browser-capture/pending', dependencies=[Depends(extension_access)])
def extension_pending():
    return {'intents': capture.pending()}


class HereRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    url: str = Field(min_length=1, max_length=4096)


@router.post('/api/browser-capture/here', dependencies=[Depends(extension_access)])
def here(body: HereRequest):
    """扩展开起来的那一下问这一次:**我站着这一页,此刻能做什么。**

    一问一答,替掉原先那个下拉框 —— 「这一页是哪一条」地址栏里就写着,让人再
    回答一遍是白跑三个界面(前因后果在 `capture.here` 的 docstring)。

    **POST 而不是 GET**,因为正文还欠着的那一档它会顺手把待办建好(`create`
    幂等,同一(条目, 页面)上已有 pending 就复用)。扩展因此不必先去采集抽屉。
    """
    return invoke(capture.here, body.url)


@router.post('/api/browser-capture/here/adopt', dependencies=[Depends(extension_access)])
def adopt_here(body: IntentRequest):
    """**「等着的那个待办指着另一个地址,而我正站的这一页就是那一篇」。**

    和采集抽屉里那颗「改用这个地址」是同一个动作、同一个 `create`(旧待办由服务端
    顶掉,不是先取消再发起),只是把它搬到了人所在的地方 —— 站点按浏览器语言跳转
    那一类里,人手上拿着的恰恰就是对的地址。

    **记住:这不是放行。** 改用要人点,正文照旧要过 `_admit_*` 全套、照旧要人核
    对预览勾确认,所以「扩展不会用另一篇文章静默覆盖」一个字都没松。
    """
    return invoke(capture.create, body.key, body.page_url)


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


@router.post('/api/browser-capture/upload', dependencies=[Depends(extension_access)])
def upload(body: UploadRequest):
    """把读到的正文写回原条目。

    **原先这里还有一个 `confirmed_complete` 必须为真**,对应弹窗上那道「我已核对:
    这是目标文章的完整正文」。那道勾连同预览区一起撤了(产品决定,取舍记在
    `docs/decisions/browser-capture.md`),这个字段也跟着撤 —— 留着它只会变成
    **扩展签给自己的证词**:界面不再问人,却照旧报一句「人已经核对过了」。
    何况它从来挡不住非界面调用,随手填个 `true` 就过。

    **撤掉的只有这一句,准入一道都没少**:正文够不够(`_admit_body`,按行去重后
    的字数)、是不是指定的那一页(`_admit_page`)、条目还是绑定的那一版吗
    (`_admit_item`),连同 `save` 那条 UPDATE 自己 WHERE 里的 CAS,全在
    `capture.save` 里原样判。
    """
    return invoke(capture.save, body.intent_id, body.page_url, body.title, body.body, body.method)
