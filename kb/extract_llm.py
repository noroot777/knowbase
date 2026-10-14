# -*- coding: utf-8 -*-
"""提取这一步跟模型打交道的那一层:提示词、正文截断、调用与回包归一。

从 `kb/extract.py` 拆出来的 —— 那个文件写到 761 行、越过仓库约定的 700 行
上限,而这一块是里面最独立的:**没有模块级可变状态、不碰 kb.db、不碰 vault
文件**,只把「一篇文章的标题和正文」变成「一个 dict」。手法同
`scripts/wiki/taxo_llm.py` 那次(提示词/调模型/解析回包从 `taxonomy.py` 拆走)。

`kb/extract.py` 把这里的名字**原样再导出**,所以 `extract.clip_body`、
`extract.PROMO_HEAD`、`patch.object(extract, 'extract_with_llm', …)` 这些写法
一个字都不用改 —— 调用方(cli / pipeline_stages / extract_parallel)是在函数
内部 `from kb.extract import extract_with_llm` 的,每次都现从模块全局取,
打桩照旧打得到。
"""

from __future__ import annotations

import json
import re

# 软广的判定口径。抽成一份常量:提取阶段(随 entity/topic 顺带判)和存量补判
# 脚本(只判软广)共用同一段文字,两处的标准才不会各漂各的。
#
# 「只看正文主体」那条是拿真实样本调出来的:号主在每篇文末固定挂一段加群/课程
# 广告,不写清楚的话模型会因为那条尾巴把这个号自己写的资讯文也判成 hard。
# 后来发现这条当初放得过宽——「代充值」原先既列在 hard 的清单里、又列在「可以
# 忽略的尾巴」里,同一个词在相邻两段里自相矛盾,模型只能在矛盾处抛硬币:同一份
# 搬运稿的 53 个副本判出 52 个 none、1 个 hard,给的理由逐字是「文末的充值广告
# 是公众号固定版位而非本篇文章内容」。所以把代充值/中转站这一类从「可忽略」里
# 拿出来单列,并在规则里直接钉住反面(报道泄露事件、介绍镜像源项目、拿「中转站」
# 当比喻的不算)——那 24 篇词面命中的全是这三种,语义上跟导流毫无关系。
# 完整的事故形态和证据在 docs/decisions/promo.md。
_PROMO_RULES = """\
  * hard:整篇就是为某个商业产品/服务服务的——通篇围绕它、只讲好话没有独立
    对比与缺点,并且在做转化引导:堆促销权益话术(免费不限量、注册就送、每天
    领取、限时白嫖、专属优惠码/邀请码、活动价)、给注册落地页或下载地址、
    让人扫码进群/加客服/点「阅读原文」领取;或文中明示是推广/赞助/合作;
    代充值、代开会员、共享账号、API 中转站一类导流文也算。
  * soft:有推广倾向但仍给了实质信息——厂商自家发布通稿、带邀请返利链接的推荐、
    通篇只夸一家产品但确实写清了能力和用法。
  * none:正常内容——开源项目介绍、技术教程、行业资讯报道、观点评论、论文解读。
  只看正文主体,不看文末的固定推广位:公众号普遍在结尾挂关注引导、加群、课程、
  自家产品一类的尾巴,那是这个号每篇都有的版位,不是这一篇的性质。正文本身是
  资讯报道、开源项目介绍或教程的,即便文末挂着这类广告也判 none/soft;
  hard 留给「把整篇文章买下来卖那个产品」的投放稿。
  这一条不含「借壳」那一种:文末挂的是代充值、代开会员、共享账号、API 中转站
  (加微信办理、扫码充值、「官方一半价格」「无需魔法」「无视封号」一类话术)时
  一律 hard,不按「固定版位」放过。这类号的套路是把别人的资讯稿整篇搬过来当壳
  (常能看到照抄的「XX报道」抬头和原号的编辑署名),文末接上那段联系方式,整篇的
  存在就是为了把人导到那里;正文那段写得多正经也不是它自己的东西。
  但只认真的在做这桩生意的那一种:文末那段自己得在提供交易——有具体对接方式
  (微信号、二维码、落地页地址)加上「办理/代充/代开/续费」的动作。下面这些
  一律不算,照旧按正文性质判:正文在报道中转站泄露事件、介绍镜像源监控项目,
  或者把「中转站」当比喻用(「学术走向产业的中转站」「人肉中转站」);文末那串
  是本号自己的历史文章导航、系列文章推荐,其中某条标题带「怎么买、怎么充值」;
  正文是在讲官方自己的订阅档位、计费方式怎么选;文末那段是加本号社群、加作者
  好友、关注引导,哪怕写了「加微信」也不算。分野在于那段在卖什么:代充、代开
  会员、卖号、中转站额度才算,拉人进群聊天不算。光出现「充值」两个字更不构成
  判据,要看那段是不是在卖代充服务。
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

def clip_body(body: str, head: int = BODY_HEAD, tail: int = BODY_TAIL) -> str:
    """正文超长时取「头 + 尾」,中间略去。"""
    if len(body) <= head + tail:
        return body
    return f"{body[:head]}\n\n……(中间略)……\n\n{body[-tail:]}"


def normalize_promo(raw, strict: bool = False) -> dict:
    """把模型给的 promo 归一成 {level, reason}。

    认不出的一律落 none:软广判定是「宁可漏、不可错杀」——错杀会让一篇正常
    文章连同它的实体一起从条目化和检索里消失,而漏判只是留着一篇广告。

    **这条兜底只在「这一篇还没有结论」时成立**,`strict=True` 就是给另一种
    场合用的:这一篇**已经有**结论、这次是去覆盖它(存量重判)。那时兜底的
    none 不再是「保守」而是「把已经判出来的 hard 擦掉」——广告重新进检索、
    `flag/软广` 一起摘掉,而调用方以为自己拿到了一个判定。所以严格模式下
    认不出就抛 `ValueError`,由调用方重试或扣留,**不许返回「确定没有」**。
    """
    if isinstance(raw, str):                    # 模型偶尔直接给 "hard"
        raw = {"level": raw}
    if not isinstance(raw, dict):
        raw = {}
    level = str(raw.get("level") or "").strip().lower()
    if level not in PROMO_LEVELS:
        if strict:
            raise ValueError(f"没有拿到合法的 level: {str(raw)[:120]}")
        level = "none"
    return {"level": level, "reason": str(raw.get("reason") or "").strip()[:200]}


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
                timeout: int = 120, require_verdict: bool = False) -> dict:
    """只判软广,不重做 entity/topic/summary(存量补判走这条)。

    重试语义与 extract_with_llm 一致:坏 JSON 原地重试一次,API 报错直接抛。

    `require_verdict=True` 把「回包里没有合法 level」也算进那次重试,两次都
    拿不到就抛 —— **重判已有结论的那条路必须给它开**,否则一个 `{}` 回包会
    经 `normalize_promo` 的兜底变成「这篇不是广告」,把已经判出来的 hard 擦掉
    (判据和理由都在 `normalize_promo` 的 strict 那一段)。
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
            return normalize_promo(json.loads(m.group(0)),
                                   strict=require_verdict)
        except (json.JSONDecodeError, ValueError) as e:
            last_err = e
    raise last_err
