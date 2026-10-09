# -*- coding: utf-8 -*-
"""entity/topic/summary/软广 提取。

两条路:
- pre_tagged 来源(dr-youtube-dig):frontmatter 里已有 entity/xxx、topic/xxx
  标签,直接解析进 entities 表,零成本。
- 其他来源(dr-gzh、gzh):调 LLM API 读正文提取,结果进 DB;用哪个模型看设置
  面板的「管道 · 提取打标」阶段;--writeback 时同步回写 md frontmatter 的
  tags(obs 里可见可点)。

**软广判定搭在这一步,不单开一次调用**:提取是「每篇原文各跑一次、覆盖所有
非 pre_tagged 来源」的唯一一步,正文已经读进 prompt 了,多要一个 promo 字段
只多几十个输出 token。单开一个阶段等于把同一篇正文再买一遍。判定结果落
documents.promo_level,下游(条目化 gate、检索、wiki)一律读这一列。

回写只动 frontmatter 的 tags 列表,正文一个字节不碰 —— 但它**写的是 vault 文件**,
所以整段在 `kb.maintenance` 那把维护锁里,而且锁内先确认这一篇还在(见
`writeback_extraction`)。
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime

from kb.config import SOURCES, VAULT_ROOT
from kb.norm import entity_norm, kind_norm

# 提取走 kb.llm 的 API 通道,用哪家的哪个模型由设置面板的「管道 · 提取打标」
# 阶段决定(kb.llm_stages 的 "extract");没配就用那里登记的内置默认 gpt-5.5。
# 别在这里写死字面量:改模型要能在面板上看见、也能按供应商分开选。

# 软广的判定口径。抽成一份常量:提取阶段(随 entity/topic 顺带判)和存量补判
# 脚本(只判软广)共用同一段文字,两处的标准才不会各漂各的。
#
# 「只看正文主体」那条是拿真实样本调出来的:GitHub猿 这类号在每篇文末固定挂
# 一段代充值/加群广告,不写清楚的话模型会因为那条尾巴把正经资讯文也判成 hard
# (2026-09-16 随机采样 50 篇时抓到两例误杀)。
_PROMO_RULES = """\
  * hard:整篇就是为某个商业产品/服务服务的——通篇围绕它、只讲好话没有独立
    对比与缺点,并且在做转化引导:堆促销权益话术(免费不限量、注册就送、每天
    领取、限时白嫖、专属优惠码/邀请码、活动价)、给注册落地页或下载地址、
    让人扫码进群/加客服/点「阅读原文」领取;或文中明示是推广/赞助/合作;
    代充值、代开会员、共享账号、API 中转站一类导流文也算。
  * soft:有推广倾向但仍给了实质信息——厂商自家发布通稿、带邀请返利链接的推荐、
    通篇只夸一家产品但确实写清了能力和用法。
  * none:正常内容——开源项目介绍、技术教程、行业资讯报道、观点评论、论文解读。
  只看正文主体,不看文末的固定推广位:公众号普遍在结尾挂加群、加客服、代充值
  一类的尾巴,那是这个号每篇都有的版位,不是这一篇的性质。正文本身是资讯报道、
  开源项目介绍或教程的,即便文末挂着广告也判 none/soft;
  hard 留给「把整篇文章买下来卖那个产品」的投放稿。
  被推广的东西是开源项目(有公开仓库、能自己部署、不花钱)时一律不判 hard,
  最多 soft:号主把它吹上天、顺带拉群拉关注,那是这类号的文风,不是买量投放。
  hard 的推广对象必然是要花钱或要注册账号的商业产品/服务。
  判断依据是「有没有在做拉新转化 + 是不是只讲好话」,不是「有没有提到商业产品」:
  介绍一个免费开源项目、报道某厂新模型发布,本身都算 none;
  有实测截图、有使用教程也不能反证不是软广——投放稿通常就长这样。"""

_PROMPT = """\
你是知识库索引员。读下面这篇文章,提取检索用元数据,只输出一个 JSON 对象,不要任何其他文字:

{{
  "entities": [{{"name": "实体规范名", "kind": "project|product|org|model|other"}}],
  "topics": ["主题词"],
  "summary": "一句话说明文章讲了什么(<=60字)",
  "promo": {{"level": "none|soft|hard", "reason": "一句话理由"}}
}}

规则:
- entities 是文章实际介绍/讨论的具体对象:开源项目名、产品名、公司名、模型名。
  用官方拼写(如 book-to-skill、Claude Code),最多 8 个,只提文章真正着墨的,顺带一提的不要。
- topics 从这个固定候选里选,最多 4 个:AI编程, AI Agent, skill工具, 开源项目, 大模型,
  提示词工程, 知识管理, AI资讯, 效率工具, 机器人, AI视频图像, 其他
- summary 说清楚"是什么+能干什么",让人不点开也知道要不要看。
- promo 判断这篇是不是「软广」——受商业委托、以拉新转化为目的的投放稿:
""" + _PROMO_RULES + """
- JSON 字符串值里不要出现英文双引号,引用词一律用中文引号「」。

标题: {title}
公众号: {account}

正文:
{body}
"""

# 只判软广的轻量 prompt:存量补判用。entity/topic/summary 早就提过了,没必要
# 连同整套元数据重来一遍——输入砍到头尾各一段,输出只剩一个小 JSON。
_PROMO_ONLY_PROMPT = """\
你是内容审核员。判断下面这篇公众号文章是不是「软广」——受商业委托、以拉新转化\
为目的的投放稿。只输出一个 JSON 对象,不要任何其他文字:

{{"level": "none|soft|hard", "reason": "一句话理由,用中文"}}

口径:
""" + _PROMO_RULES + """
- JSON 字符串值里不要出现英文双引号,引用词一律用中文引号「」。

标题: {title}
公众号: {account}

正文:
{body}
"""

# 喂给模型的正文上限。总量沿用原来的 6000 字符不变(token 成本不变),但拆成
# 头 + 尾:软广的转化钩子(注册地址、「阅读原文领取」、推广落款)基本都在文末,
# 只截开头的话这批信号一个都看不到。
BODY_HEAD = 4800
BODY_TAIL = 1200
# 只判软广时的正文用量。软广信号密集在开头(钩子 + 产品名)和结尾(注册地址、
# 促销),中间的实测细节对这个判断没什么增量,所以砍到全量提取的四成。
PROMO_HEAD = 1600
PROMO_TAIL = 800

PROMO_LEVELS = ("none", "soft", "hard")
# hard 软广在 obs 里也标出来。doc_tags 在重索引时会按 frontmatter 重建,所以
# 这个 tag 跟 topic/entity 一样要靠 writeback 回写 md 才持久。
PROMO_TAG = "flag/软广"


def clip_body(body: str, head: int = BODY_HEAD, tail: int = BODY_TAIL) -> str:
    """正文超长时取「头 + 尾」,中间略去。"""
    if len(body) <= head + tail:
        return body
    return f"{body[:head]}\n\n……(中间略)……\n\n{body[-tail:]}"


def normalize_promo(raw) -> dict:
    """把模型给的 promo 归一成 {level, reason}。

    认不出的一律落 none:软广判定是「宁可漏、不可错杀」——错杀会让一篇正常
    文章连同它的实体一起从条目化和检索里消失,而漏判只是留着一篇广告。
    """
    if isinstance(raw, str):                    # 模型偶尔直接给 "hard"
        raw = {"level": raw}
    if not isinstance(raw, dict):
        raw = {}
    level = str(raw.get("level") or "").strip().lower()
    if level not in PROMO_LEVELS:
        level = "none"
    return {"level": level, "reason": str(raw.get("reason") or "").strip()[:200]}


def _tag_slug(name: str) -> str:
    s = re.sub(r"\s+", "-", name.strip().lower())
    return re.sub(r"[#,\[\]{}|^]", "", s)


def _topic_slug(name: str) -> str:
    """主题词的 slug:比实体多吃掉斜杠。

    `topic/` 之后的那一段会被 wiki 层当成页面文件名(`wiki/topics/主题-X.md`),
    斜杠在那里是目录分隔符。模型偶尔给出候选表外的词(2026-07-16 那篇 InfoQ
    出过 `CI/CD流程`),整个 wiki 阶段就挂在 FileNotFoundError 上。实体名里的
    斜杠是正当的(`shadcn/ui` 这类仓库名),所以只收紧主题这一侧。"""
    return re.sub(r"[/\\]+", "-", _tag_slug(name)).strip("-")


def parse_pretagged(doc) -> dict | None:
    """从已有 tags 解析 entity/topic(pre_tagged 来源的管线产物)。

    条目化来源(dr-video-projects / dr-gzh-projects)的 frontmatter 还带
    project/kind/summary:主实体的 kind、文档 summary 一并取用。"""
    entities, topics = [], []
    meta = getattr(doc, "meta", None) or {}
    proj = str(meta.get("project") or "").strip()
    proj_norm = entity_norm(proj) if proj else None
    proj_kind = kind_norm(str(meta.get("kind") or "")) if meta.get("kind") else None
    for t in doc.tags:
        if t.startswith("entity/"):
            name = t.split("/", 1)[1]
            kind = "other"
            if proj_norm and proj_kind and entity_norm(name) == proj_norm:
                kind = proj_kind
            entities.append({"name": name, "kind": kind})
        elif t.startswith("topic/"):
            topics.append(t.split("/", 1)[1])
    if not entities and not topics:
        return None
    return {"entities": entities, "topics": topics,
            "summary": str(meta.get("summary") or "")}


def extract_with_llm(title: str, body: str, timeout: int = 120,
                     account: str = "") -> dict:
    """调 LLM API 提取。异常往上抛,由调用方决定跳过还是中止。

    模型偶发在字符串值里输出未转义双引号产生坏 JSON(概率性,同文重跑
    通常就好),解析失败原地重试一次;API 报错/超时不重试,留给下轮管道。"""
    last_err: Exception | None = None
    for _ in range(2):
        try:
            return _extract_once(title, body, timeout, account)
        except (json.JSONDecodeError, ValueError) as e:
            last_err = e
    raise last_err


def _extract_once(title: str, body: str, timeout: int, account: str = "") -> dict:
    from kb.llm import chat
    from kb.llm_stages import stage_model

    prompt = _PROMPT.format(title=title, account=account or "(未知)",
                            body=clip_body(body))
    # 产物是一个小 JSON 对象,2048 足够;OpenAI 侧推理 token 另有内部下限兜底
    out = chat(prompt, stage_model("extract"), max_tokens=2048, timeout=timeout)
    # 容错:模型可能包 ```json 围栏
    m = re.search(r"\{.*\}", out, re.S)
    if not m:
        raise ValueError(f"输出里没有 JSON: {out[:200]}")
    data = json.loads(m.group(0))
    data.setdefault("entities", [])
    data.setdefault("topics", [])
    data.setdefault("summary", "")
    data["promo"] = normalize_promo(data.get("promo"))
    return data


def judge_promo(title: str, body: str, account: str = "",
                timeout: int = 120) -> dict:
    """只判软广,不重做 entity/topic/summary(存量补判走这条)。

    重试语义与 extract_with_llm 一致:坏 JSON 原地重试一次,API 报错直接抛。
    """
    from kb.llm import chat
    from kb.llm_stages import stage_model

    prompt = _PROMO_ONLY_PROMPT.format(
        title=title, account=account or "(未知)",
        body=clip_body(body, PROMO_HEAD, PROMO_TAIL))
    last_err: Exception | None = None
    for _ in range(2):
        out = chat(prompt, stage_model("extract"), max_tokens=1024, timeout=timeout)
        m = re.search(r"\{.*\}", out, re.S)
        if m is None:
            last_err = ValueError(f"输出里没有 JSON: {out[:200]}")
            continue
        try:
            return normalize_promo(json.loads(m.group(0)))
        except json.JSONDecodeError as e:
            last_err = e
    raise last_err


class StaleBody(Exception):
    """这次提取结果针对的那份正文,已经不是当前那一份了 —— 结果作废。

    不是故障:这一篇留在待提取集合里(`extracted_at` 一个字不写),下一轮按
    新正文重提一次就对了。三条提取路径都把它归到「跳过」那一档,不记失败
    ——记失败会让面板标红,而这里什么都没坏。
    """


def body_fingerprint(text: str) -> str:
    """一份正文的指纹。提取开始时按读到的那份算一次,落库前再比一次。

    口径只有这一份:调用方拿自己手里那份 body 算,`save_extraction` 落库前
    按**索引里此刻**那份算,两个数相等才说明"这个结论还是针对当前正文的"。
    """
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def save_promo(conn, path: str, promo: dict, now: str | None = None) -> bool:
    """写软广判定,并同步 doc_tags 里的 flag。

    返回 True = **md 里那行 flag 需要调用方去摘掉**(原来判的是 hard,这次不是)。
    DB 侧这里就清干净了,但 frontmatter 只有 writeback 那条路能碰,而且
    writeback_tags 只会追加;不摘的话,这篇文件之后任何一次改动触发
    upsert_document,doc_tags 都会按 frontmatter 重建,误判的标记又回来
    (第 2 轮审查发现)。改判是小概率事件,所以做成「要不要碰文件」的信号
    返回给调用方,而不是每篇都去读一次 md。
    """
    now = now or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    old = conn.execute("SELECT promo_level FROM documents WHERE path=?",
                       (path,)).fetchone()
    was_hard = bool(old and old["promo_level"] == "hard")
    conn.execute(
        "UPDATE documents SET promo_level=?, promo_reason=?, promo_at=? WHERE path=?",
        (promo["level"], promo.get("reason") or "", now, path),
    )
    if promo["level"] == "hard":
        conn.execute("INSERT OR IGNORE INTO doc_tags (path, tag) VALUES (?, ?)",
                     (path, PROMO_TAG))
        return False
    # 重跑改判时把旧标记摘掉,不然面板上永远挂着
    conn.execute("DELETE FROM doc_tags WHERE path=? AND tag=?", (path, PROMO_TAG))
    return was_hard


def sync_promo_tag(conn, doc_path: str, level: str, stale: bool) -> None:
    """把 md frontmatter 里的 flag/软广 对齐到当前判定(存量补判走这条)。

    存量补判不重做 entity/topic,所以喂给 writeback_extraction 的是一份只有
    promo 的结果——口径仍然共用那一份,不另写一遍。stale 取自 save_promo:
    只有「上一次判 hard、这次不是」才需要去摘那行,正常的 none 文章连文件
    都不用读。
    """
    writeback_extraction(conn, doc_path, {"promo": {"level": level}}, stale)


def _body_moved(conn, path: str, body_seen: str) -> str:
    """这次结果针对的那份正文还是**当前**那一份吗 —— 变了就返回一句原因。

    **口径只有这一份,两个消费点**:落库前 `_require_current_body` 拿它抛
    `StaleBody`,回写前 `writeback_extraction` 在同一把维护锁里拿它判一次布尔。
    两处各写一遍的话,拦下来的和写回去的不是同一批。

    两道各管一种交错时序,少哪一道哪一种就漏过去:

    ①**索引还没扫到当前这份 md**(`documents.content_hash` 对不上文件)。这是
      「导出完成、索引还没跟上」那一档:`fulltext.retire_stale` 在导出之后就把
      这一篇排回了待提取集合,而提取读的是**索引里**那份正文(`store.doc_body`
      拼的是 chunks)—— 旁路的一轮 `kb extract` 正好落在这个缝里,就会拿旧壳
      再提一遍。那一刻 chunks 压根没变,所以 ② 对它是瞎的。
    ②**索引里那份正文已经和我读到的不是同一份了**。这是「旧正文的提取请求
      在途、模型回包晚于全文更新」那一档:中间那次 index 已经把 chunks 换成
      新正文,`content_hash` 跟着对上了文件 —— 所以 ① 对它是瞎的。

    **拦不住的后果是永久的**:落库会把 `extracted_at` 设上,这一篇从此再也不进
    `pending_docs`,摘要、主题、实体永久停在那个跳转壳上,再同步、再索引都不会
    自己纠正(2026-10-09 第 2 轮审查两种时序各三次稳定复现)。

    **md 压根读不出来(清理正把这一篇删掉)不算"版本不对",直接放行。** 这一档
    本来就有人管:`writeback_extraction` 在锁内复查「这一篇还在吗」,落进库的判定
    随后被外键级联一并带走(真半途失败也由下一轮 `remove_missing_documents` 收掉),
    末态干净 —— 那是本来就对的一条路。在这里改判成作废只是把它换了个形状,代价
    却是实打实的:回写压根不会被调到,**于是"回写在锁里、读也不许读"那组回归对它
    不再敏感**(把回写那把锁去掉也照样全绿)。这道判据只回答「我读到的正文还是
    当前那一份吗」,"这一篇还在不在"不是它的问题。
    """
    from kb import store
    from kb.scanner import file_hash
    try:
        text = (VAULT_ROOT / path).read_text(encoding="utf-8")
    except OSError:
        return ""                   # 见 docstring:这一档交给回写那道锁内复查
    if store.doc_needs_reindex(conn, path, file_hash(text)):
        return "索引还没扫到当前这份 md,提取结果作废"
    if body_fingerprint(store.doc_body(conn, path)) != body_seen:
        return "正文已更新,提取结果作废"
    return ""


def _require_current_body(conn, path: str, body_seen: str) -> None:
    """落库之前确认正文版本,对不上抛 `StaleBody`(判据见 `_body_moved`)。

    **这道检查本身给不了保证,得由 `commit_extraction` 把它和写入圈进同一个
    维护锁 + 写事务里。** 单独跑只回答"检查的那一瞬间正文还是不是我读到的
    那份",答不了"我写进去的那一瞬间还是不是" —— 两者之间那个窗口有一份实测
    后果,见 `commit_extraction` 的说明。
    """
    reason = _body_moved(conn, path, body_seen)
    if reason:
        raise StaleBody(f"{reason}: {path}")


def save_extraction(conn, path: str, data: dict, body_seen: str | None = None) -> bool:
    """把一篇的提取结果落库。`body_seen` 给了就先校验正文版本(见 `StaleBody`)。

    **提取是「读正文 → 等几十秒模型 → 落库」,正文在这中间可能整篇换掉。**
    补抓把跳转壳换成全文正是这么一条路:它会把这一篇送回待提取集合,于是
    「模型读走旧壳 → 期间正文换新并重新索引 → 旧结论落库」成了常规时序,
    而落库会把 `extracted_at` 设上 —— 这一篇从此再也不进 `pending_docs`,
    摘要、主题、实体永久停在那个壳上,再同步也不会自己纠正(2026-10-09
    第 2 轮审查两种交错时序各三次稳定复现)。所以落库之前比一次指纹,
    对不上就整份丢掉。

    判据有两道,**各管一种交错时序、缺一不可**(都在 `_body_moved`)。校验和这
    一串写入之间还有个窗口,**那由 `commit_extraction` 把两头圈进同一个维护锁
    + 写事务里关掉** —— 三条读正文的提取路径一律走它,不要在外面自己拼。
    不校验的那几条路(`body_seen=None`)是压根不读索引正文的:`parse_pretagged`
    直接解析 frontmatter,软广补判只改 promo —— 它们没有"基于哪一份正文"可言。
    """
    if body_seen is not None:
        _require_current_body(conn, path, body_seen)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn.execute("DELETE FROM entities WHERE path=?", (path,))
    for e in data["entities"]:
        name = str(e.get("name") or "").strip()
        if name:
            conn.execute(
                "INSERT OR IGNORE INTO entities (path, entity, kind, norm)"
                " VALUES (?, ?, ?, ?)",
                (path, name, kind_norm(str(e.get("kind") or "")), entity_norm(name)),
            )
    # topic 以 tag 形式统一存(和 pre_tagged 来源对齐)
    for t in data["topics"]:
        t = str(t).strip()
        if t and _topic_slug(t):
            conn.execute(
                "INSERT OR IGNORE INTO doc_tags (path, tag) VALUES (?, ?)",
                (path, f"topic/{_topic_slug(t)}"),
            )
    for e in data["entities"]:
        name = str(e.get("name") or "").strip()
        if name:
            conn.execute(
                "INSERT OR IGNORE INTO doc_tags (path, tag) VALUES (?, ?)",
                (path, f"entity/{_tag_slug(name)}"),
            )
    conn.execute(
        "UPDATE documents SET extracted_at=?, summary=? WHERE path=?",
        (now, str(data.get("summary") or ""), path),
    )
    # promo 单独一条 UPDATE:pre_tagged 来源走的是 parse_pretagged,产物里没有
    # promo 字段,一并写的话会把源头文章判过的结果覆盖成空。
    # 返回值透传 save_promo 的「md 里的 flag 该摘了」,调用方据此决定碰不碰文件。
    promo = data.get("promo")
    if isinstance(promo, dict) and promo.get("level"):
        return save_promo(conn, path, promo, now)
    return False


def extraction_tags(data: dict) -> list[str]:
    """一篇提取结果该回写进 frontmatter 的 tag(topic/entity,判 hard 再加软广)。

    topic 走 `_topic_slug`(比实体多吃掉斜杠)、entity 走 `_tag_slug`,和
    save_extraction 写 doc_tags 用的是同一对函数——两侧不一致的话,重索引会按
    frontmatter 重建 doc_tags,带斜杠的主题当场回到库里,wiki 阶段照样崩。
    slug 完为空的主题直接丢掉,不写一个 `topic/` 空标签。

    PROMO_TAG 在 slug 之后追加:它本身就是最终形态(`flag/软广`),跟着过一遍
    会被改写成别的东西。
    """
    tags = [f"topic/{_topic_slug(t)}" for t in data.get("topics") or []
            if _topic_slug(str(t))]
    tags += [f"entity/{_tag_slug(e['name'])}" for e in data.get("entities") or []
             if e.get("name")]
    if (data.get("promo") or {}).get("level") == "hard":
        tags.append(PROMO_TAG)
    return tags


def _still_indexed(conn, doc_path: str) -> bool:
    """这一篇此刻还在 vault、也还在库里吗。**只在持着维护锁时问才有意义。**"""
    if not (VAULT_ROOT / doc_path).exists():
        return False
    return conn.execute("SELECT 1 FROM documents WHERE path=? LIMIT 1",
                        (doc_path,)).fetchone() is not None


def writeback_extraction(conn, doc_path: str, data: dict, stale: bool,
                         body_seen: str | None = None) -> bool:
    """把提取结果的 tag 对齐到 md frontmatter,改了文件就刷新 content_hash。

    提取有三条路(管道 `_extract_pass`、`kb extract --writeback`、
    `scripts/extract_parallel.py`),口径必须只有这一份:各写各的话,软广标记
    会只在其中一条路上跟着判定走,另外两条要么写不进 md(重索引即丢)、要么
    改判后摘不掉(重索引又把误判带回库里)——第 1、2 轮审查各栽过一次,
    第 3 轮就栽在这两条旁路上。

    **读文件 → 改 frontmatter → 写回 → 刷 hash 整段在维护锁里**
    (`kb.maintenance`,模型请求留在锁外、不占它)。这里写的是 vault 文件,而
    `writeback_tags` 是「`read_text` 拿一份内存快照 → 过一会儿 `write_text`
    整份写回」——存量清理在这中间把这一篇从 feeds.db / vault / kb.db 三处删干净
    的话,那次 `write_text` 会把**文件重新创建出来**:下一轮索引合法地扫到它、
    upsert 回库,旧文从此搜得到、点得开,而 feeds.db 里已经没有这一条,重跑清理
    只会说「无事可做」——比 index 那条路更糟,那一条下一轮 `remove_missing_documents`
    还收得掉,这一条文件真的回来了,谁也收不掉(2026-10-01 第 2 轮审查两种合法
    时序各实测一次:`kb extract --writeback` 与同步的并行存量提取)。
    **外键级联挡不住这一步**:标签是在删除**之前**写进去并提交的,清理的级联
    删除照样全部成功,它管的是库里那几张表,管不着谁在之后重建文件。

    **取锁之前先把调用方那个写事务提交掉**(见下面 `conn.in_transaction` 那段):
    SQLite 的写锁不认维护锁,攥着写事务等锁会把持锁的清理卡死在它自己的 DELETE 上。

    **锁拿到手之后先确认这一篇还在**(`_still_indexed`)——等锁的这段时间里清理
    很可能正是把它删掉了,那就什么都不做、返回 False(「没改文件」)。不抛异常:
    文章是用户主动清理掉的,把它记成提取失败会让面板标红、谎报一次故障,而判定
    本身早就落库了(随后被外键级联一并删掉,末态干净)。

    **给了 `body_seen` 就在锁内再确认一次「这份 tag 算的还是当前那份正文吗」**
    (`_body_moved`,和落库那道同一份口径)。正常路径上 `commit_extraction` 已经
    把落库和回写圈在同一个锁区间里、中间没有窗口,所以这一道是**这个公开入口
    自己的契约**:谁在锁外隔着一段时间拿一份基于旧正文的结果来回写,都该被挡
    下来 —— 旧 tag 一写进新全文就摘不掉了(`writeback_tags` 只追加),而它还顺手
    刷了 content_hash,下一次索引未必重建(2026-10-09 第 3 轮审查实测:新全文的
    frontmatter 里混着 `topic/link-only` + `topic/ai-coding` 两代主题)。
    同样返回 False、不抛异常 —— 判定要么已经落库、要么由落库那道挡掉了,这里
    没有任何坏事发生,下一轮按新正文重提会写进对的那一批。
    """
    tags = extraction_tags(data)
    need_remove = stale and (data.get("promo") or {}).get("level") != "hard"
    if not tags and not need_remove:
        return False     # 没什么要写的,不必为此读一遍文件
    if conn.in_transaction:
        # **取锁之前把调用方手里那个写事务落盘,这件事由这里负责、不靠调用方自觉。**
        # SQLite 的写锁不认维护锁:攥着它去等锁,持锁的清理就卡在自己的 DELETE 上,
        # 两边互等到 SQLite 的 busy 超时(默认 30 秒)把清理打断 —— 清理报
        # `database is locked`、索引那一步没做完,搜索里还留着刚删掉的旧文
        # (2026-10-01 第 3 轮审查跨进程实测 32.5 秒后报错,末态 documents=1、
        # 全文命中 1,要重试一次才收得干净)。原来这条契约写在**调用方**那一侧,
        # 于是软广补判这个经 `sync_promo_tag` 进来的第四个调用方被漏掉,而回归
        # 只枚举了三条提取路径、全绿照过。改成在共用入口自己 flush:多一个调用
        # 方就少一处可漏的地方。
        # 提前提交不新增副作用 —— 改了文件本来也要在锁内 `conn.commit()`,这里
        # 只是把同一个提交挪早;四个调用方的判定都是逐篇独立、可重跑的,组内
        # 那次提交没有「要么全要么全不」的语义。
        conn.commit()
    from kb.maintenance import maintenance_lock

    with maintenance_lock():
        if not _still_indexed(conn, doc_path):
            return False
        if body_seen is not None and _body_moved(conn, doc_path, body_seen):
            return False            # 见 docstring:旧 tag 贴到新全文上就摘不掉了
        changed = writeback_tags(doc_path, tags) if tags else False
        if need_remove:
            changed = remove_writeback_tag(doc_path, PROMO_TAG) or changed
        if changed:
            refresh_content_hash(conn, doc_path)
            # 在锁里提交:攥着 SQLite 写事务走出锁的话,下一个拿到维护锁的清理
            # 会卡在它的 DELETE 上(SQLite 的写锁不认我们这把锁)。
            conn.commit()
        return changed


def commit_extraction(conn, path: str, data: dict, body_seen: str, *,
                      writeback: bool = True, on_error=None) -> bool:
    """交付一次提取的结果:校验正文版本 → 落库 → 提交 → 回写 md,整段在
    「维护锁 → kb.db 写锁」里。返回回写有没有改文件。

    **这个协调区间就是它存在的理由。** `save_extraction` 里那道版本校验只回答
    "检查的那一瞬间正文还是不是我读到的那份",答不了"我写进去的那一瞬间还是
    不是" —— 校验到第一条 SQL、以及落库到回写之间各有一个窗口,而把正文整篇
    换掉的那条路(补抓 → 翻译 → 导出 → 作废旧提取 → 索引)全程持着**同一把**
    维护锁。两个窗口各有一份实测后果(2026-10-09 第 3 轮审查,各三次稳定复现):

    - **校验到写入**:校验合法通过,随后另一边走完补抓和索引、`extract_stale`
      也清了零,这才放行旧结果落库 —— 末态是新全文配旧结论
      (`summary="只有一条链接和 HN 得分。"`、`entities=["htmx"]`),`extracted_at`
      非空、`pending=[]`,**再同步一轮也不变**。
    - **落库到回写**:旧结果合法落库并提交,期间新全文进了索引、md 里那批旧 tag
      已经被作废摘掉,这才放行回写 —— `topic/link-only` 重新写进新全文的
      frontmatter,而 `writeback_tags` 只追加不摘除、还顺手刷了 content_hash,
      所以下一次索引未必重建它。

    **原来那条兜底论证不成立,记在这儿备查**:`_require_current_body` 曾写着
    「窗口里真被写进去了也不是死局 —— `feed_items.extract_stale` 那个待办只在
    作废**做完**之后才放下,下一轮导出会再作废一次」。那句话的前提是旧保存落在
    作废**之前**;上面第一个时序正好相反 —— 待办已经清零,没有第二次作废。

    **锁序是维护锁 → SQLite 写锁,不许反过来**(见 `kb.maintenance` 和
    `writeback_extraction` 里 `conn.in_transaction` 那两段):攥着一个没提交的写
    事务去等维护锁,持锁的清理会卡在它自己的 DELETE 上,两边互等到 SQLite 的
    busy 超时把清理打断。所以进锁之前先 flush,进锁之后才 `BEGIN IMMEDIATE`。

    **模型请求留在锁外,粒度是每篇各取一次。** 这一趟几千篇要跑几十分钟,把整趟
    圈起来等于攥着锁等几十次 LLM 往返,面板上的同步、定时同步和日报一起冻住。

    **锁拿到手之后先确认这一篇还在**(`_still_indexed`,原来只有回写那一侧问)。
    等锁的这段时间里清理很可能正是把它删掉了,而 `doc_tags` / `entities` 对
    `documents(path)` 都是外键 —— 落库会当场报 `FOREIGN KEY constraint failed`,
    被调用方记成一次提取失败、面板标红,而文章是用户主动清理掉的。安静返回
    False(什么都没写、`extracted_at` 一个字没动)才是这一档的末态。
    把 save 挪进锁里之后这道检查就只能在这儿问:原来它赶在清理之前跑完,所以
    外键那时是合法的。

    `writeback=False` 给 `kb extract` 不带 `--writeback` 的那条路。`on_error`
    收回写阶段的异常:判定已经落库了,回写只是展示层,单篇回写失败不该把这一篇
    记成提取失败 —— 不给就原样抛出去(管道那条路靠外层统一记账)。
    """
    from kb.maintenance import maintenance_lock

    if conn.in_transaction:
        conn.commit()           # 取维护锁之前先落盘,理由同 writeback_extraction
    with maintenance_lock():
        conn.execute("BEGIN IMMEDIATE")
        try:
            if not _still_indexed(conn, path):
                return False    # 见 docstring:等锁期间清理把这一篇删掉了
            stale = save_extraction(conn, path, data, body_seen)
            conn.commit()
        finally:
            if conn.in_transaction:
                conn.rollback()
        if not writeback:
            return False
        try:
            return writeback_extraction(conn, path, data, stale,
                                        body_seen=body_seen)
        except Exception as e:
            if on_error is None:
                raise
            on_error(e)
            return False


def writeback_tags(doc_path: str, new_tags: list[str]) -> bool:
    """把新 tag 合并进 md 的 frontmatter tags 列表。只改 frontmatter,不碰正文。"""
    full = VAULT_ROOT / doc_path
    text = full.read_text(encoding="utf-8")
    m = re.match(r"\A(---\n.*?\n---\n)", text, re.S)
    if not m:
        return False
    fm_block, body = m.group(1), text[m.end():]

    existing = set(re.findall(r"^\s*-\s+(\S+)\s*$", fm_block, re.M))
    to_add = [t for t in new_tags if t not in existing]
    if not to_add:
        return False

    add_lines = "".join(f"  - {t}\n" for t in to_add)
    if re.search(r"^tags:\s*$", fm_block, re.M):
        # 已有 tags: 列表 → 追加到列表末尾(即 frontmatter 结束线前)
        new_fm = re.sub(r"\n---\n\Z", f"\n{add_lines}---\n", fm_block)
    else:
        new_fm = re.sub(r"\n---\n\Z", f"\ntags:\n{add_lines}---\n", fm_block)
    full.write_text(new_fm + body, encoding="utf-8")
    return True


def remove_writeback_tag(doc_path: str, tag: str) -> bool:
    """从 md frontmatter 的 tags 列表里摘掉一个 tag。writeback_tags 的反向操作。

    只删 tags 列表里那一行(格式与 writeback_tags 写进去的一致),正文和其余
    frontmatter 一个字节不碰。文件不在/没有 frontmatter/本来就没这个 tag
    都返回 False,调用方据此决定要不要刷新 hash。
    """
    full = VAULT_ROOT / doc_path
    try:
        text = full.read_text(encoding="utf-8")
    except OSError:
        return False
    m = re.match(r"\A(---\n.*?\n---\n)", text, re.S)
    if not m:
        return False
    fm_block, body = m.group(1), text[m.end():]
    new_fm = re.sub(rf"^[ \t]*-[ \t]+{re.escape(tag)}[ \t]*\n", "",
                    fm_block, flags=re.M)
    if new_fm == fm_block:
        return False
    full.write_text(new_fm + body, encoding="utf-8")
    return True


def refresh_content_hash(conn, doc_path: str) -> None:
    """writeback 改了 frontmatter 后刷新库里的 hash,避免下次 index 无谓重建。
    正文没变(chunker 只看 body),chunks/FTS/向量都无需动。"""
    from kb.scanner import file_hash
    full = VAULT_ROOT / doc_path
    try:
        text = full.read_text(encoding="utf-8")
    except OSError:
        return
    conn.execute("UPDATE documents SET content_hash=? WHERE path=?",
                 (file_hash(text), doc_path))


def pending_docs(conn, limit: int = 0) -> list:
    """还没提取过的、需要走 LLM 的文档(pre_tagged 来源除外)。"""
    pre_tagged_sources = {s.name for s in SOURCES if s.pre_tagged}
    ph = ",".join("?" * len(pre_tagged_sources))
    sql = f"""
        SELECT path, title, account FROM documents
        WHERE extracted_at IS NULL AND source NOT IN ({ph})
        ORDER BY published DESC
    """
    rows = conn.execute(sql, list(pre_tagged_sources)).fetchall()
    return rows[:limit] if limit > 0 else rows


def pending_promo_docs(conn, sources: list[str], limit: int = 0) -> list:
    """已提取过、但还没做软广判定的文档(存量补判用)。

    新入库的文章在提取时就顺带判了,这个查询只会捞出「软广判定上线之前」
    提取的那批存量;补完一次就永远是空的。
    """
    ph = ",".join("?" * len(sources))
    sql = f"""
        SELECT path, title, account FROM documents
        WHERE promo_level = '' AND source IN ({ph})
        ORDER BY published DESC
    """
    rows = conn.execute(sql, list(sources)).fetchall()
    return rows[:limit] if limit > 0 else rows
