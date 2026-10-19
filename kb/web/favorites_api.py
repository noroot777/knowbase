# -*- coding: utf-8 -*-
"""收藏 API:备忘待试清单(整篇或片段,inbox 待试 / done 已处理 / archived 沉底)。

行动卡提取与整体分诊在 kb.favbrain,这里只管存取与状态流转。
"""

from __future__ import annotations

import json
import re

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, field_validator

from kb import favbrain, llm, store
from kb.favorite_metadata import FAVORITE_DOCUMENT_COLUMNS, FAVORITE_DOCUMENT_JOIN
from kb.maintenance import LockBusy, maintenance_lock
from kb.scanner import body_version
from kb.web.common import VAULT_NAME, read_doc_text, now as _now

router = APIRouter()


# ── 收藏(备忘待试清单:整篇或片段,inbox 待试 / done 已处理 / archived 沉底) ──────

STALE_DAYS = 60  # 待试超过这个天数算"陈旧",提示一键沉底

_FAV_STATUSES = ("inbox", "done", "archived")


# 锚点里的空白:一段连续空白一律压成一个空格。**这是采集和回跳共用的坐标系**,
# 前端 `kb/static/js/snip.js` 的 `snipTextIndex` 按同一条规则串正文,阅读页
# `kb/web/pages.py` 那份内联副本也一样 —— 同一份正文在两头的空白本来就不一样
# (`段一\n\n段二` 渲染成两个 <p>,串出来的文本里只剩一个换行),不压的话前后文
# 锚点一碰到段落边界就对不上。
#
# **只压前后文,不压引文。** 前后文是匹配用的表示,引文是用户收下来的原文 ——
# 压了它,收藏四行 Python 代码换回来的就是接成一行的
# `if ready: run_task() else: stop_task()`,换行和缩进在库里压根不存在了
# (审查实测:日报正文和独立阅读页都中)。读的那一头本来就会再压一遍
# (`snip.js` 的 `snipAnchor`、阅读页的 `anchorOf`),所以匹配不受影响。
_ANCHOR_WS = re.compile(r"\s+")

# 「半个字」:JS 的字符串按 UTF-16 存,emoji 一类字占两个单元(代理对),按单元
# 截断会把它切成两半。孤立的那一半 **Python 的 str 装得下、UTF-8 编不出来** ——
# sqlite3 在 INSERT 绑参数那一句当场抛 UnicodeEncodeError,而那是在 `try` 里面
# 的最后一步,于是整条 POST 变成一句 500(审查实测:正文里 emoji 后面隔着 29 个
# 字母,选中再后面那句普通中文,日报和独立阅读页都收藏失败)。
#
# 两头各修一道:**采集那头不许切开字符**(`kb/static/js/snip.js` 的 `snipWhole`,
# 阅读页那份内联副本里叫 `whole`),**这头如实拒成 400** —— 数据不合法该当场说
# 清楚,不该等到 INSERT 抛一句看不懂的 500。**不许替调用方把那半个字悄悄换掉**:
# 引文是用户收下来的原文,替换它等于静静改掉收藏内容(而且补不回来)。
_LONE_SURROGATE = re.compile("[\ud800-\udfff]")


# 绑文档的收藏等维护锁的上限(秒)。**比维护那一侧的 900 秒短得多**:那一侧是
# 批处理,等半小时也比漏一轮强;这一侧是用户按下按钮之后在等的一个请求,等不到
# 就该出声。拒了不丢东西 —— 调用方要么退回 origin=feed(订阅阅读器),要么如实
# 说一句让用户过会儿再收(图鉴条目、独立阅读页)。**不许改成无限等**:维护锁可
# 能被一轮大编码攥上几分钟,那是整条 HTTP 连接挂死。
_DOC_LOCK_WAIT = 10.0


def _doc_version_now(path: str) -> str:
    """盘上**此刻**那一篇的正文版本号;读不出来按 404 / 503 抛。

    口径只有一份:报版本的三处(`/api/doc`、图鉴条目详情、独立阅读页)和这一处
    都走 `scanner.body_version`,**读法和读不出来怎么报**都走
    `kb.web.common.read_doc_text` —— 落库这一道和核对那一道不许给出两种说法。
    """
    return body_version(read_doc_text(path))


def _bind_and_insert(conn, path: str, want: str, insert) -> int:
    """绑文档的那一类:**「核验 → 写入 → 提交」整段待在维护锁 + 写事务里。**

    核过一次不等于绑对了 —— `doc_version` 是一次**取样**,临界区才是不变量。
    文件是谁在换:订阅同步的导出(`feeds.sync.export` 末尾那一句 `os.replace`)、
    重译写回、正文补抓、展示层改写(`doc_rewrite`)。这几条路**全都认同一把维护
    锁**(`kb/maintenance.py`),所以把核验和落库圈进那把锁里,它们就进不来;
    数据库那一头另有一道(`BEGIN IMMEDIATE` 排在读版本之前),于是 INSERT 不会
    在"核完之后"再去等写锁。

    **只缩短间隔是不够的**(上一版就是只把核验挪进同一个请求):INSERT 要等别人
    的写事务时,这个间隔由**对方**决定 —— 审查实测核验与落库之间等了约 1 秒,
    期间另一路真走 `translate._save → sync.export` 把那一篇换成新译文,收藏照旧
    落成 `origin=doc` 返回 200,立刻回看 0 个高亮。

    锁序是**先维护锁、再 BEGIN IMMEDIATE,不许反**(反过来报的
    `SQLITE_BUSY_SNAPSHOT` 不受 `busy_timeout` 管),取锁之前先把写事务落盘 ——
    同 `kb/extract.py` 的 `save_and_writeback`,口径在
    `docs/decisions/maintenance-lock.md`。

    等不到锁(`LockBusy`)按 **503** 收场、一个字节没写:这是"这一刻没法核对",
    不是"那一篇不在了"(404),也不是"已经是另一版"(409)。调用方按状态码决定
    退哪一档,见 `kb/static/js/snip.js` 的 `RETRY_NOTE`。
    """
    if conn.in_transaction:
        conn.commit()           # 取维护锁之前先落盘(锁序,见 docstring)
    try:
        with maintenance_lock(wait=_DOC_LOCK_WAIT):
            conn.execute("BEGIN IMMEDIATE")
            try:
                if conn.execute(
                    "SELECT 1 FROM documents WHERE path=?", (path,)
                ).fetchone() is None:
                    raise HTTPException(404, "文档不在索引里")
                if _doc_version_now(path) != want:
                    raise HTTPException(
                        409, "这一篇的正文已经不是你核对过的那一版了")
                new_id = insert()
                conn.commit()
                return new_id
            finally:
                if conn.in_transaction:
                    conn.rollback()
    except LockBusy as e:
        raise HTTPException(503, f"正文正在被另一轮维护改写,这一刻没法核对你"
                                 f"收藏的是哪一版:{e}")


def _anchor(prefix: str, quote: str, suffix: str) -> tuple[str, str, str]:
    """把一条锚点收成规范形态:前后文空白压平、外侧空白去掉、引文两头的空白挪进前后文。

    不变量是 **压平之后 `prefix + quote + suffix` 仍是原文里连着的一段**。所以
    不许只 `strip()` 引文:前后文照旧带着被剥掉的那几个空白,整段锚点就再也拼不
    回原文,定位只能退回"只找引文",于是静默落在**第一处**相同的话上(审查实测:
    收藏正文里第二处重复句、连着首尾空格一起选,回跳高亮的是第一处)。

    **引文本身原样留着**(只去首尾空白):它是收藏的内容,不是匹配表示。

    前端采集时已经是这个形态,这一道是**存储层自己的口径** —— 库里那条锚点永远
    规范,不押在客户端上。
    """
    p, s = (_ANCHOR_WS.sub(" ", x or "") for x in (prefix, suffix))
    # **外侧**那一头的空白去掉(接缝那一头留着,见下面):它什么位置信息也不带,
    # 却最容易在另一种渲染里差一个 —— 正文末尾那一段在 textContent 里后面跟着
    # 一个换行,渲染成 HTML 之后那儿什么都没有,整段锚点于是差一个空格就失配。
    p, s = p.lstrip(), s.rstrip()
    q = quote or ""
    # 挪过去的空白在接缝上**只留一个** —— 前后文自己的末尾已经是空格时再补一个,
    # 拼出来就是两个,而正文里压平之后只有一个,整段锚点照样对不上。
    if q[:1].isspace() and not p.endswith(" "):
        p += " "
    if q[-1:].isspace() and not s.startswith(" "):
        s = " " + s
    return p, q.strip(), s


class FavoriteRequest(BaseModel):
    path: str


# 片段是从哪个界面选的。决定「点回去」怎么走:`doc` 回 `/doc/…` 阅读页按
# quote+前后文锚点定位(最可靠,那套定位在 kb/web/pages.py 里),其余三个回各自
# 面板。和前端 `kb/static/js/snip.js` 的 `SNIP_ORIGINS` 是同一份名单。
_ORIGINS = ("doc", "daily", "chat", "feed")


class SnippetRequest(BaseModel):
    # 对应的 vault 文档。**可以为空** —— 日报正文、问答回答、还没导出的订阅
    # 条目都能收藏片段,而它们在 documents 里没有行。
    path: str = ""
    quote: str
    prefix: str = ""   # 前后文锚点,回跳原文时定位用
    suffix: str = ""
    note: str = ""
    origin: str = "doc"
    origin_ref: str = Field(default="", max_length=200)
    origin_title: str = Field(default="", max_length=300)
    # 4096 而不是 2048:链接是**内容自带的**(订阅条目、图鉴条目的原文地址),
    # 带一串跟踪参数的真实 URL 过 2048 是现实存在的,而这条上限一卡就是整条
    # POST 被拒成 422 —— 用户选完才发现存不下,且 404 那条退路也接不住 422。
    # 共享采集层那边配着同一个数(`snip.js` 的 `SNIP_MAX_URL`):超长就不送
    # 链接,**不裁** —— 半截 URL 点过去是另一个地方。
    origin_url: str = Field(default="", max_length=4096)
    # 调用方核对过的那一版正文(`/api/doc` 等三处给出的 `version`)。**绑了
    # 文档就必须带**:核对和写入是两个请求,中间那篇随时会被重译、被补正文
    # 重导出 —— 没有这个数,两头就没有共同事实,核对读到的是旧译文、写入时
    # 盘上已是新译文,照旧落成 doc、点回去 0 个高亮(审查实测)。
    doc_version: str = Field(default="", max_length=64)
    # 采集那一刻这段引文在那一篇里是不是只有一处(1 = 只有一处)。回跳时两侧
    # 上下文都失配、退到「只找引文」那一档时按它决定能不能采信 —— 当时就有两处
    # 的话,「现在只剩一处」证明不了这一处就是当时那一处(见 `snip_text.js` 的
    # `snipSeek`)。**不在服务端重算**:服务端手里没有用户当时看的那一篇正文
    # (日报、问答回答压根不是文件),而且重算也只能算到「此刻」。
    quote_unique: int = 0
    # 采集那一刻**这条锚点定不定得住**(1 = 整段锚点在那一篇里唯一落回用户选的
    # 那一处)。回跳那一刻记着 0 的一律不定位:那一刻的「正好剩一处」只说明别处
    # 被删掉了,而采集那一刻谁也说不出用户选的是第几处 —— 采信它就是把一处从没
    # 被收藏过的文字高亮出来(审查用生产函数实测:采集偏移 740,删掉第二节之后
    # 回查到 244)。**默认 None = 没记下**,和 0 不是一回事:老数据、以及没带
    # 这个字段的调用方都是「不知道」,按原样放行;0 是比它更强的断言。
    # 同 `quote_unique`:**不在服务端重算**(服务端手里没有用户当时看的那一份)。
    anchor_exact: int | None = None
    # 问答片段:这段话出自哪一条**已经落库**的消息。见下面那道归属核验。
    origin_msg: int = 0

    @field_validator("*", mode="before")
    @classmethod
    def _no_half_character(cls, value, info):
        """截断切开的「半个字」在这儿就拦住,见 `_LONE_SURROGATE`。

        **抛 HTTPException 而不是 ValueError**,也**必须排在 `max_length` 前面**
        (所以是 `mode="before"`):校验失败走 FastAPI 的
        `RequestValidationError`,而那个错误体里带着 `input` 原串 —— 序列化它
        又是一次 UTF-8 编码,于是"受控的 422"当场变成一个崩在错误处理里的 500
        (实测:`origin_title` 带半个字时,带 `max_length` 的字段连 422 都发不
        出来)。这里回的那句话**不带原串**,所以一定编得出去。

        `"*"` = 所有字段都过这一道:哪个快照字段坏了都是同一件事,而漏掉一个
        的后果是一句看不懂的 500。
        """
        if isinstance(value, str) and _LONE_SURROGATE.search(value):
            # 哪个字段坏了要说出来:前端据此知道是自己截断切开了字,
            # 而不是用户选的那段话有问题
            raise HTTPException(
                400, f"{info.field_name} 里有半个字符(一个字被截断切开了),存不下来")
        return value


def _chat_message_id(conn, body: "SnippetRequest") -> int:
    """问答片段的**归属核验**:这段话出自哪一条消息,那条消息在不在这个会话里。

    **会话号非空只说明那个对话存在。** 追问时它早就有了,而后端只在整条 NDJSON
    收完、`_save_qa` 提交之后才发 `done` 帧 —— 传输提前结束(代理断了、流被掐
    在中间)时页面上那条回答一个字都没落库,可它旁边的下一轮正常保存会给出一个
    真实的会话号。按「当前是哪个对话」记归属的话,那条收藏就指向了一个**从来没
    有这句话的会话**:当场提示成功,点回去打开的是别的轮次、一句「没找到」
    (审查实测的失败注入:第一轮只有 200 + delta + EOF、没有 done,第二轮正常落
    库拿到会话 3,第一轮残留气泡被收成 `origin=chat, origin_ref=3`)。

    所以**身份由后端确认过的消息 id 给**(`done` 帧带回来、或者历史消息自带),
    这一道再核一次「它确实在那个会话里」。核不过就如实拒掉:那条收藏存下来也只
    是一条点不回去的死收藏,而用户此刻还看着那段话、重问一次就能收。

    客户端那一头另有一道(`chat.js` 的 `SNIP_CID`:没有 `done` 帧不给身份),
    **两道都要**:这一道是存储层自己的口径,不押在页面状态上。
    """
    mid = int(body.origin_msg or 0)
    ref = body.origin_ref.strip()
    if mid <= 0 or not ref.isdigit():
        raise HTTPException(
            400, "问答片段要带已落库的消息身份(这一轮可能还没保存,重问一次再收藏)")
    row = conn.execute(
        "SELECT 1 FROM messages WHERE id=? AND conversation_id=?",
        (mid, int(ref)),
    ).fetchone()
    if row is None:
        raise HTTPException(
            400, "这段回答还没落库(或者不属于这个对话),刷新后重问一次再收藏")
    return mid


class FavoritePatch(BaseModel):
    status: str | None = None
    note: str | None = None


def _stale_cutoff() -> str:
    from datetime import datetime, timedelta
    return (datetime.now() - timedelta(days=STALE_DAYS)).isoformat(timespec="seconds")


# 元数据有两个来源:索引里那篇文档(最新),和收藏那一刻存下的快照(`origin_*`)。
# **LEFT JOIN,不是 JOIN。** 内连接会让两类条目在列表里静默消失:没有 path 的
# (日报/问答片段),以及文档已经不在索引里的 —— 而计数(`counts`)是全表数的,
# 于是标签上写着「待试 12」、列表里只有 9 条,剩下 3 条点不到也删不掉。
_FAV_SELECT = (
    "SELECT f.id, f.path, f.quote, f.prefix, f.suffix, f.note, f.status,"
    " f.action_json, f.origin, f.origin_ref, f.created_at, f.updated_at,"
    # 回跳那一头要用它决定能不能走裸引文那一档(见 `snip_text.js` 的
    # `snipSpecOf`/`snipSeek`)。**老数据是 NULL,前端据此按「说不清」办** ——
    # 这儿不许 COALESCE 成 0 或 1:两者分别是「当时不唯一」和「当时唯一」,
    # 都是比「没记下」更强的断言。`anchor_exact`(采集那一刻这条锚点定不定得住)
    # 同理:0 = 当时就定不住、哪一档都不给用,NULL = 老数据没记下、按原样放行。
    " f.quote_unique, f.anchor_exact, f.origin_msg,"
    # 采集那一刻核对过的正文版本。**回跳那一头拿它和阅读页此刻的版本号相比**,
    # 相等才敢只听锚点的一侧、必要时退到裸引文(见 `snip_text.js` 的 `snipSeek`)。
    # 同样**不许 COALESCE 成空串**:老数据的 NULL 是「没记下」,而空串在那一头
    # 和「版本号读不出来」撞在一起 —— 两种都该按「证明不了」办,可前者是历史
    # 遗留、后者是这一刻的故障,混在一起就没法再分。
    " f.doc_version,"
    + FAVORITE_DOCUMENT_COLUMNS + FAVORITE_DOCUMENT_JOIN
)

# path 为空、`origin_title` 也空(只可能来自 v4 迁移时置空的孤儿行)时列表总得
# 有一行字可点。写清是什么情况,不要留一张无字卡片。
_FAV_NO_TITLE = "(原文已不在索引里)"


@router.get("/api/favorites")
def api_favorites(status: str = "inbox"):
    """收藏列表(带文档元数据),按收藏时间倒序。

    返回指定状态或 all 全部条目,同时附带各状态计数、陈旧数(inbox 超过 STALE_DAYS
    没动过的)和整篇收藏 path 集合(搜索卡片/reader 的 ★ 状态用,不分状态)。
    """
    if status not in (*_FAV_STATUSES, 'all'):
        raise HTTPException(400, f"status 只能是 all/{'/'.join(_FAV_STATUSES)}")
    conn = store.connect()
    try:
        # **`id` 是必须的那一半**:`created_at` 只到秒(`kb/web/common.py` 的
        # `now()`),而「收藏此段」让人在同一段正文上连着选好几句 —— 同一秒落库的
        # 那几条排序就成了未定义,列表每次刷新顺序都可能不一样(实测:同一份日报
        # 里两句话同秒收藏,回跳按钮的先后在两次加载之间换过位置)。
        rows = conn.execute(
            _FAV_SELECT
            + (" WHERE f.status = ?" if status != 'all' else "")
            + " ORDER BY f.created_at DESC, f.id DESC",
            (status,) if status != 'all' else (),
        ).fetchall()
        favorites = []
        for r in rows:
            item = {**dict(r), "vault": VAULT_NAME, "action": None}
            # path 为空时前端那套 docLink/obsLink 会拼出 `/doc/undefined`,
            # 一律给空串,由前端按 origin 走自己那条回跳。
            item["path"] = item["path"] or ""
            item["document_available"] = bool(item["document_available"])
            item["title"] = item["title"] or _FAV_NO_TITLE
            aj = item.pop("action_json", "")
            if aj:
                try:
                    item["action"] = json.loads(aj)
                except ValueError:
                    pass
            favorites.append(item)
        counts = {s: 0 for s in _FAV_STATUSES}
        for r in conn.execute("SELECT status, COUNT(*) AS n FROM favorites GROUP BY status"):
            if r["status"] in counts:
                counts[r["status"]] = r["n"]
        stale = conn.execute(
            "SELECT COUNT(*) FROM favorites WHERE status='inbox' AND updated_at < ?",
            (_stale_cutoff(),),
        ).fetchone()[0]
        # `path IS NOT NULL`:整篇收藏只存在于 doc 来源,但迁移置空过的孤儿行
        # 也满足 quote='' —— 不排掉就会往这个数组里塞一个 null,而前端拿它当
        # Set 的成员去比对每张搜索卡片。
        doc_paths = [r["path"] for r in conn.execute(
            "SELECT DISTINCT path FROM favorites WHERE quote='' AND path IS NOT NULL"
        )]
        return {
            "favorites": favorites,
            "counts": counts, "stale": stale, "stale_days": STALE_DAYS,
            "doc_paths": doc_paths,
        }
    finally:
        conn.close()


@router.post("/api/favorites/toggle")
def api_favorite_toggle(body: FavoriteRequest):
    """收藏/取消收藏一篇文章(整篇条目,quote='';片段收藏不受影响)。"""
    conn = store.connect()
    try:
        if conn.execute(
            "SELECT 1 FROM documents WHERE path=?", (body.path,)
        ).fetchone() is None:
            raise HTTPException(404, "文档不在索引里")
        cur = conn.execute(
            "DELETE FROM favorites WHERE path=? AND quote=''", (body.path,)
        )
        favorited = cur.rowcount == 0
        new_id = None
        if favorited:
            now = _now()
            cur = conn.execute(
                "INSERT INTO favorites (path, created_at, updated_at) VALUES (?, ?, ?)",
                (body.path, now, now),
            )
            new_id = cur.lastrowid
        conn.commit()
        if new_id:
            favbrain.extract_action_async(new_id)  # 行动卡后台补全,不阻塞收藏
        return {"favorited": favorited}
    finally:
        conn.close()


@router.post("/api/favorites")
def api_favorite_snippet(body: SnippetRequest):
    """收藏一个片段(任意能展示正文的界面里选中文字后调用)。

    来源分两类,**按 `path` 有没有分,不按 `origin` 分**:
    - 有 `path`:那段话对应 vault 里一篇被索引过的文档(阅读页、图鉴条目、
      已导出的订阅译文)。照 v1 的样子存,回跳走 `/doc/…` 的锚点定位。
    - 没有 `path`:日报正文、问答回答、还没导出的订阅条目。`path` 落 NULL
      (**不是空串** —— 外键照查空串,当场 FOREIGN KEY constraint failed),
      标题/链接靠收藏那一刻的快照,回跳由前端按 `origin` 回各自面板。

    `path` 给了但不在索引里 → 404,**不悄悄降级成"没有 path"那一类**:那等于
    把一条本该能定位回原文的收藏,静默换成一条只能看引文的。调用方(前端)知道
    自己手里那篇有没有入库,该降级时它自己传空 path 配 `origin=feed`。

    **`path` 给了就必须带 `doc_version`**,而且落库前按**盘上此刻**那一份再算
    一次,不等就拒(409):核对(调用方读 `/api/doc`)和写入是两个请求,中间
    那篇随时会变。缺这一道的后果不是"偶尔定位不准",是**一条点回去永远找不到
    的收藏被固化下来**,而 HTTP 还是 200(审查实测:核对读到旧译文,其间另一路
    真走 `_save → export → index` 换成新译文,收藏照旧落成 doc、回看 0 个高亮)。
    **而"同一个请求里核过"还不算绑住** —— 核验到提交之间谁都能换掉那个文件,
    所以这一段整体待在维护锁和写事务里,见 `_bind_and_insert`。
    拒的四种形态各报各的,调用方据此决定退回哪一档:**404 不在了**(索引里没有
    这一行,或者文件已经移走)、**503 读不出来 / 这一刻没法核对**(不等于不存在,
    等维护锁超时也落这一档)、**409 已经是另一版**。口径同 `/api/doc`(两头共用
    `common.read_doc_text`)。
    """
    prefix, quote, suffix = _anchor(body.prefix, body.quote, body.suffix)
    if not quote:
        raise HTTPException(400, "片段内容不能为空")
    if body.origin not in _ORIGINS:
        raise HTTPException(400, f"origin 只能是 {'/'.join(_ORIGINS)}")
    path = body.path.strip()
    version = body.doc_version.strip()
    title = body.origin_title.strip()
    if not path:
        if body.origin == "doc":
            raise HTTPException(400, "doc 来源的收藏必须带 path")
        if not title:
            # 列表、分诊、行动卡都靠它指代这一条。缺了就是一张无字卡片,
            # 而收藏当场是成功的 —— 宁可现在拒掉。
            raise HTTPException(400, "没有对应文档的收藏必须带 origin_title")
    if path and not version:
        raise HTTPException(400, "绑文档的收藏必须带 doc_version(核对过的正文版本)")
    conn = store.connect()
    try:
        msg_id = _chat_message_id(conn, body) if body.origin == "chat" else 0

        def insert() -> int:
            now = _now()
            cur = conn.execute(
                "INSERT INTO favorites (path, quote, prefix, suffix, note,"
                " origin, origin_ref, origin_title, origin_url,"
                " quote_unique, anchor_exact, origin_msg, doc_version,"
                " created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (path or None, quote, prefix, suffix, body.note.strip(),
                 body.origin, body.origin_ref.strip(), title,
                 body.origin_url.strip(),
                 1 if body.quote_unique else 0,
                 # **没带这个字段就存 NULL,不许折成 0。** 0 的意思是「采集那一刻
                 # 就定不住」,回跳那一头据它一律不定位;老调用方和老数据是
                 # 「没记下」,折成 0 等于把它们的收藏一次性废掉。
                 None if body.anchor_exact is None
                 else (1 if body.anchor_exact else 0),
                 msg_id or None,
                 # **核对过的那一版要跟着落库**,不只是在写入这一刻用一次:
                 # 回跳那一头要靠「库里这一版 == 阅读页此刻这一版」才敢采信
                 # 锚点的弱档(只听一侧、裸引文)。写进来这一刻它已经被
                 # `_bind_and_insert` 按盘上那一份核过,所以这个数是事实。
                 # path 为空时一律 NULL:没有文档可绑,它就没有意义。
                 (version if path else "") or None, now, now),
            )
            return cur.lastrowid

        if path:
            new_id = _bind_and_insert(conn, path, version, insert)
        else:
            new_id = insert()
            conn.commit()
        favbrain.extract_action_async(new_id)   # 行动卡后台补全
        return {"id": new_id}
    finally:
        conn.close()


@router.patch("/api/favorites/{fav_id}")
def api_favorite_patch(fav_id: int, body: FavoritePatch):
    """状态流转(待试/已处理/沉底)或改备注。"""
    if body.status is not None and body.status not in _FAV_STATUSES:
        raise HTTPException(400, f"status 只能是 {'/'.join(_FAV_STATUSES)}")
    sets, args = ["updated_at=?"], [_now()]
    if body.status is not None:
        sets.append("status=?")
        args.append(body.status)
    if body.note is not None:
        sets.append("note=?")
        args.append(body.note.strip())
    conn = store.connect()
    try:
        cur = conn.execute(
            f"UPDATE favorites SET {', '.join(sets)} WHERE id=?", (*args, fav_id)
        )
        if cur.rowcount == 0:
            raise HTTPException(404, "收藏不存在")
        conn.commit()
        return {"ok": True}
    finally:
        conn.close()


@router.delete("/api/favorites/{fav_id}", status_code=204)
def api_favorite_delete(fav_id: int):
    conn = store.connect()
    try:
        cur = conn.execute("DELETE FROM favorites WHERE id=?", (fav_id,))
        if cur.rowcount == 0:
            raise HTTPException(404, "收藏不存在")
        conn.commit()
    finally:
        conn.close()


@router.post("/api/favorites/archive-stale")
def api_favorite_archive_stale():
    """一键沉底:待试超过 STALE_DAYS 没动过的全部转 archived。"""
    conn = store.connect()
    try:
        cur = conn.execute(
            "UPDATE favorites SET status='archived', updated_at=?"
            " WHERE status='inbox' AND updated_at < ?",
            (_now(), _stale_cutoff()),
        )
        conn.commit()
        return {"archived": cur.rowcount}
    finally:
        conn.close()


class FavSettingsRequest(BaseModel):
    model: str = ""   # 空 = 清除,回落默认问答模型


@router.get("/api/favorites/settings")
def api_favorite_settings():
    """收藏整理(行动卡/分诊)当前用的模型;下拉候选用 /api/models。"""
    return {"model": llm.get_fav_model()}


@router.post("/api/favorites/settings")
def api_favorite_settings_set(body: FavSettingsRequest):
    llm.set_fav_model(body.model)
    return {"model": llm.get_fav_model()}


@router.get("/api/favorites/triage")
def api_favorite_triage():
    """分诊状态 + 缓存结果。fresh=False 表示 inbox 变过,结果可能过时(仍可展示)。"""
    return favbrain.triage_status()


@router.post("/api/favorites/triage/run")
def api_favorite_triage_run():
    """后台重跑分诊;前端轮询 GET /api/favorites/triage 直到 running=False。"""
    started = favbrain.run_triage_async()
    return {"started": started}


@router.post("/api/favorites/{fav_id}/extract")
def api_favorite_extract(fav_id: int):
    """手动补一张行动卡(收藏时提取失败/老数据没有时,前端点重试)。"""
    action = favbrain.extract_action(fav_id)
    if action is None:
        raise HTTPException(502, "行动卡生成失败(检查模型与 key 配置)")
    return {"action": action}
