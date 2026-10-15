# -*- coding: utf-8 -*-
"""全局配置:vault 路径、数据库位置、来源定义。

设计原则:所有"哪里有内容"的知识集中在这一个文件;
新增一种来源(博客/RSS/播客)只需在 SOURCES 加一项。
"""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime
from dataclasses import dataclass, field
from pathlib import Path
from kb.source_types import feed_types

# obs vault 根
VAULT_ROOT = Path(os.environ.get("KB_VAULT_ROOT") or Path.home() / "obs" / "new-start").expanduser()

# 项目数据目录(索引数据库等,不进 git)
DATA_DIR = Path(os.environ.get("KB_DATA_DIR") or Path(__file__).resolve().parent.parent / "data").expanduser()
DB_PATH = DATA_DIR / "kb.db"
VIDEOS_ROOT = Path(os.environ.get("KB_VIDEOS_ROOT") or Path.home() / "code/daily-report/videos").expanduser()

# 本地 embedding 模型(bge-m3:中英混合效果好,1024 维 dense)
EMBEDDING_MODEL = "BAAI/bge-m3"
EMBEDDING_DIM = 1024

# 模型跑在子进程里(见 kb/embed_worker.py:MPS 显存池只涨不退,进程内收不回)。
# 空闲这么多秒后子进程自杀,把 ~3.7 GB 还给系统;下次要用重新起,冷启动 3.2 秒。
# 设 0 或负数 = 不自动回收(worker 一直留着)。
EMBED_IDLE_SECONDS = int(os.environ.get("KB_EMBED_IDLE_SECONDS", "600"))
# 应急开关:置 1 退回「模型直接加载在本进程」的老行为。子进程路径出问题时能
# 保住检索可用,代价是这个进程的内存再也降不下来。
EMBED_IN_PROCESS = os.environ.get("KB_EMBED_IN_PROCESS", "") == "1"
# 一批编码的「B × 字符数²」预算,决定批量索引的峰值内存(注意力张量随长度平方涨,
# MPS 显存池会涨到见过的最大批次那个规模)。单位是 B×L² 的乘积,写成
# 「几条 × 多长 × 多长」便于估算。
# 2026-09-08 实测(1536 条真实 chunk,p50 长度 632):
#   32×768²(原值) 峰值 17.0 GB  14.7 chunk/s   ← 36 GB 的机器上会打到 swap
#    8×768²        峰值  8.7 GB   9.0 chunk/s   ← 曲线拐点,取它
#    4×768²        峰值  8.0 GB   8.8 chunk/s   ← 再降换不来多少内存了
# 短 chunk 不受影响(200 字以内三档都是满批 32);预算只在长 chunk 上收紧批次。
EMBED_BATCH_BUDGET = int(os.environ.get("KB_EMBED_BATCH_BUDGET",
                                        str(8 * 768 * 768)))
# 单次编码请求最多等多久。宽到正常绝不会碰到(一批 128 条实测 ~15 秒),它防的是
# worker 卡死或管道错位——那种情况下父进程会攥着锁永久阻塞,搜索和索引一起冻住。
EMBED_REQUEST_TIMEOUT = float(os.environ.get("KB_EMBED_REQUEST_TIMEOUT", "600"))


@dataclass
class Source:
    """一种内容来源(obs 里的一棵树)。"""
    name: str                 # 来源标识,与 frontmatter source 字段对应
    root: Path                # 扫描根目录
    # 该来源是否已有 entity/topic 标签(dr-youtube-dig 的管线自带,直接解析;
    # 没有的来源走 Claude 提取)
    pre_tagged: bool = False
    # 跳过的子目录名(相对 root 的一级目录)
    skip_dirs: frozenset[str] = field(default_factory=frozenset)
    # 由其它来源条目化/转写出来的衍生树,内容与原文重叠。
    # 文章级枚举("这段时间有什么文章/信息")未显式指定来源时只看非衍生来源,
    # 避免同一内容以原文+条目两种形态重复出现;实体级枚举不受影响
    derived: bool = False
    # 还扫不扫这棵树。置 False = **停止收新内容,库里已有的一个都不动** ——
    # 名字、来源组、别名、`PRIMARY_SOURCE_NAMES` 全部原样保留,所以搜索、问答、
    # 图鉴照旧查得到存量(按元规则「要停用一列就停止读写它,别 DROP」)。
    # 两处配合才成立:`scanner.scan_all` 不再遍历它,而
    # `store.remove_missing_documents` 必须把它的存量豁免掉 —— 不豁免的话
    # "不再遍历"等于告诉下一轮索引「这些文件都没了」,库里那一批连 chunk、
    # 向量、标签一起被删,面板上只报一个 removed 计数。
    enabled: bool = True


SOURCES: list[Source] = [
    *(Source(name=kind, root=VAULT_ROOT / "source" / kind) for kind in feed_types()),
    Source(
        # claude-dr 生成的 AI 日报(英文博客/X/HN/AI 资讯,每天一篇)。
        # 内容是总结形态,但这些信息在本地只有此形态且与其它来源不重叠,按原始来源对待;
        # deep-dives 是单 URL 深挖(质量参差)、_preview 是 prompt 调试预览稿,都不入库
        #
        # **已停用(2026-10-15)**:这个上游项目被 kb 自己的日报取代了,它最近产出的
        # 一多半是空壳(实测 10-02~10-08 七篇正文都是「今日暂无收录条目」,10-07 那篇
        # 正文里直接写着「本段 LLM 调用失败」),继续扫只是每天往日报队列里塞一个空壳。
        # 停的只是"收新的";库里那 108 篇照旧在搜索/图鉴里,显式按来源(`日报`/`dr`)
        # 也照旧查得到 —— 组名和别名一个都没动。
        name="dr",
        root=VAULT_ROOT / "source" / "dr",
        skip_dirs=frozenset({"deep-dives", "_preview"}),
        enabled=False,
    ),
    Source(
        name="dr-gzh",
        root=VAULT_ROOT / "source" / "dr-gzh",
    ),
    Source(
        name="dr-youtube-dig",
        root=VAULT_ROOT / "source" / "dr-youtube-dig",
        pre_tagged=True,
        skip_dirs=frozenset({"_graph"}),  # 聚合页不是文档本体
    ),
    Source(
        # daily-report 视频原字幕的项目条目转写
        # (scripts/video_projects_export.py 产物,一个项目一个 md,自带标签)
        name="dr-video-projects",
        root=VAULT_ROOT / "source" / "dr-video-projects",
        pre_tagged=True,
        derived=True,
    ),
    Source(
        # 公众号文章的工具/项目条目化
        # (scripts/gzh_projects_export.py 产物,一个工具一个 md,自带标签)
        name="dr-gzh-projects",
        root=VAULT_ROOT / "source" / "dr-gzh-projects",
        pre_tagged=True,
        derived=True,
    ),
    Source(
        name="gzh",  # 实时 saver 的树(gzh_saver.py 产物,无 tags)
        root=VAULT_ROOT / "source" / "gzh",
    ),
]

# 原始来源名(非衍生),文章级枚举未指定 source 时的默认扫描范围
PRIMARY_SOURCE_NAMES: list[str] = [s.name for s in SOURCES if not s.derived]

# ── 来源组 ────────────────────────────────────────────────────────────────────
# 同一内容域的原始来源 + 条目化衍生树打包成组:查"视频里的工具"必须同时覆盖
# dr-youtube-dig(概览摘要)和 dr-video-projects(逐工具条目),单选其一都会漏。
SOURCE_GROUPS: dict[str, list[str]] = {
    "gzh": ["dr-gzh", "dr-gzh-projects", "gzh"],
    "video": ["dr-youtube-dig", "dr-video-projects"],
    "dr": ["dr"],
    **{kind: [kind] for kind in feed_types()},
}

# 组的自然语言别名(问答规划/工具参数里用户或模型可能给的写法)
SOURCE_ALIASES: dict[str, str] = {
    # 「微信」留着:界面上改叫绿泡泡了,用户问答里两种写法都可能给
    "公众号": "gzh", "微信": "gzh", "绿泡泡": "gzh", "wechat": "gzh", "公众号系": "gzh",
    "视频": "video", "视频系": "video", "youtube": "video", "yt": "video",
    "日报": "dr", "daily": "dr", "ai日报": "dr",
    "大v观点": "social", "大v": "social", "推特": "social", "x": "social",
    "rss订阅": "rss",
    **{spec.label.lower(): kind for kind, spec in feed_types().items()},
}

def expand_sources(value) -> list[str] | None:
    """把 source 参数归一成来源名列表:None=不过滤;组名/别名展开;
    具体来源名原样保留;列表逐项展开去重(保序)。未知名字原样传下去
    (SQL 过滤自然得到空集,比吞掉错误直观)。"""
    if value is None or value == "":
        return None
    items = value if isinstance(value, (list, tuple)) else [value]
    out: list[str] = []
    for it in items:
        key = str(it).strip()
        if not key:
            continue
        g = SOURCE_ALIASES.get(key.lower(), key.lower())
        if g in SOURCE_GROUPS:
            # 组名优先:"gzh"/"dr" 同时是来源名,但按组展开永远是超集,语义更符合意图
            out.extend(SOURCE_GROUPS[g])
        else:
            out.append(key)
    seen: set[str] = set()
    return [x for x in out if not (x in seen or seen.add(x))] or None


# ── 配置文件怎么读:**没有 ≠ 读不出来** ─────────────────────────────────────────

class ScheduleUnreadable(RuntimeError):
    """盘上那份定时配置读不出来(权限、介质、内容坏了)。**不许当成"没开"。**

    降级成空 dict 之后派生出来的 `enabled=False` 是一句**假的确定话**:API 还会
    带着 200 回去,界面照它写「未启用」「这一类来源目前不会自动同步」,而盘上那份
    其实开着 —— 人于是去等一个他以为关掉了、实际上随时会来的同步,反过来也一样。
    读不出来的正确答案是「不知道」,那一档界面上本来就有(见 `schedules.js`)。
    """


def read_config_file(path, what: str) -> dict:
    """读一份 JSON 配置并保证上面那条区分。

    - **文件不在** = 还没设置过:返回 `{}`,由调用方补缺省值。这是**确定**的事实。
    - **空文件**(或只有空白)= `ScheduleUnreadable`。**它跟"文件不在"不是一回事**:
      一个 0 字节的配置文件只可能是写坏的残骸(就地 `write_text` 先截断再写、中途
      失败/进程退出),里面原来那份开关、时刻、范围、当天已跑标记全被截掉了 ——
      按"还没设置过"处理就是拿缺省值冒充用户的设置。第 2 轮实测:给保存进程加
      `RLIMIT_FSIZE=0`,日报那份从 185 字节变 0 字节,之后的 GET 照旧 200、
      `enabled=false`、时刻回到缺省。写口那一侧的保证见 `write_config_file`。
    - 权限/介质错、编码不是 UTF-8、内容不是合法 JSON、顶层不是对象 =
      `ScheduleUnreadable`。**解码错也要归到这里**:`UnicodeDecodeError` 是
      `ValueError` 不是 `OSError`,漏掉它就会原样抛到调度线程那层
      `except Exception: pass`,把另一条健康的定时一起停掉(第 2 轮实测)。
    """
    try:
        text = path.read_text(encoding='utf-8')
    except FileNotFoundError:
        return {}
    except OSError as e:
        raise ScheduleUnreadable(f'{what}读不出来({e})') from e
    except ValueError as e:                 # UnicodeDecodeError 走这条
        raise ScheduleUnreadable(f'{what}不是 UTF-8 文本({e})') from e
    if not text.strip():
        raise ScheduleUnreadable(f'{what}是个空文件(多半是上次保存写坏了)')
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise ScheduleUnreadable(f'{what}内容坏了({e})') from e
    if not isinstance(data, dict):
        raise ScheduleUnreadable(f'{what}的内容不是一个配置对象')
    return data


def derive_config(what: str, build):
    """把"原样内容 → 带缺省值的设置"这一步的字段错误也归到 `ScheduleUnreadable`。

    `int(d['first_days'])`、时刻字段这类转换遇到乱值会抛 `ValueError`/`TypeError`,
    那同样是"这份配置读不出来",不是"用户关掉了" —— 而且**不归一的话它会原样抛到
    调度线程那层 `except Exception: pass`**,把另一条健康的定时一起停掉。
    """
    try:
        return build()
    except ScheduleUnreadable:
        raise
    except (TypeError, ValueError, AttributeError) as e:
        raise ScheduleUnreadable(f'{what}里有读不懂的字段({e})') from e


# 定时配置里那几种字段的口径,**两条定时共用一份**(口径只有一份)。
# **缺字段 = 用缺省值**(老配置少一格要照旧能用);**有字段但不合格 = 读不懂**,一律
# 抛 —— 由上面那个 `derive_config` 归一成 `ScheduleUnreadable`(面板 503、这条定时
# 暂停并留原因,另一条健康的照常)。
#
# 为什么不能只靠 JSON 合法:`{"enabled": true, "time": 7}` 放过去的话,GET 回 200、
# 来源说明写「每天 7」,而调度里是两个字符串比大小,拿它跟数字比当场抛 —— **界面说
# 着一套、实际跑的是另一套**,比直接报错难查得多。字符串 `"false"` 同理:`bool()`
# 眼里它是真的,一条关掉的定时会被显示成开着。
_CLOCK = re.compile(r'([01]\d|2[0-3]):[0-5]\d')
_DAY = re.compile(r'\d{4}-\d{2}-\d{2}')


def config_flag(d: dict, key: str, default: bool = False) -> bool:
    """开关:只认真正的 true / false。"""
    v = d.get(key, default)
    if isinstance(v, bool):
        return v
    raise ValueError(f'{key} 要的是 true 或 false,拿到 {v!r}')


def config_text(d: dict, key: str, default: str = '') -> str:
    """取一个字符串字段:**缺键或空串才用缺省值**,别的类型一律算读不懂。

    `d.get(key) or default` 这个写法会把 `0`、`[]`、`{}`、`false` 一起当成"没填"
    悄悄换成缺省 —— 于是 `"time": []` 的配置读出来是一份"每天 07:30 的正常定时",
    面板照它显示,而盘上那份根本不是这么写的(第 4 轮按代码指出)。
    """
    if key not in d or d[key] == '' or d[key] is None:
        return default
    v = d[key]
    if isinstance(v, str):
        return v
    raise ValueError(f'{key} 要的是字符串,拿到 {v!r}')


def config_clock(d: dict, key: str, default: str) -> str:
    """每天几点:HH:MM。缺键/空串 = 缺省值。"""
    v = config_text(d, key, default)
    if _CLOCK.fullmatch(v):
        return v
    raise ValueError(f'{key} 要的是 HH:MM 时刻,拿到 {v!r}')


def _calendar_day(text: str) -> bool:
    """这一天**日历上真的存在**吗。

    只用 regex 的话 `2026-99-99` 照样放行:它比大小比得过任何真实日期,于是"今天
    已经跑过了"永远成立、"下一次"永远不到期 —— 定时从此不再触发,而面板上一切
    正常(第 4 轮实测两个接口都回 200)。
    """
    try:
        datetime.strptime(text, '%Y-%m-%d')
        return True
    except ValueError:
        return False


def config_day(d: dict, key: str) -> str:
    """哪一天:YYYY-MM-DD(日历上真的存在)。空 = 没有(还没跑过/没失败过)。"""
    v = config_text(d, key, '')
    if not v or (_DAY.fullmatch(v) and _calendar_day(v)):
        return v
    raise ValueError(f'{key} 要的是 YYYY-MM-DD 日期,拿到 {v!r}')


def config_stamp(d: dict, key: str) -> str:
    """到分的时刻:YYYY-MM-DDTHH:MM。空 = 没安排。

    **要跟 `datetime.now().isoformat(timespec='minutes')` 逐字可比** —— 到期判据就是
    两个字符串比大小(见 `daily_run.tick`),格式不齐(多了秒、少了 T)比出来的结果
    是错的,而面板上一切正常。日期那一半同样要日历上真的存在。
    """
    v = config_text(d, key, '')
    if not v or (len(v) == 16 and v[10:11] == 'T' and _DAY.fullmatch(v[:10])
                 and _calendar_day(v[:10]) and _CLOCK.fullmatch(v[11:])):
        return v
    raise ValueError(f'{key} 要的是 YYYY-MM-DDTHH:MM 时刻,拿到 {v!r}')


def config_names(d: dict, key: str) -> list[str]:
    """一串名字(来源通道):**必须是数组,元素必须是字符串**。缺键/空 = 空数组。

    不验类型的话 `"channels": "rss"` 会被逐字符当成四个通道名(`r`/`s`/`s`),一个
    都认不出来,于是"同步范围"静默回落到默认范围 —— 用户以为只同步 RSS,实际跑的
    是别的(第 4 轮实测接口回 200)。
    """
    if key not in d or d[key] is None:
        return []
    v = d[key]
    if isinstance(v, list) and all(isinstance(x, str) for x in v):
        return list(v)
    raise ValueError(f'{key} 要的是一串字符串,拿到 {v!r}')


def write_config_file(path, data: dict) -> None:
    """原子替换着写一份 JSON 配置。**一次保存失败不许动上一份有效快照。**

    就地 `write_text` 是"先把文件截成 0 字节,再往里写":写到一半失败(配额、
    满盘、进程被杀)盘上剩的就是半截或者整个空文件,而原来那份**已经没了** ——
    开关、时刻、同步范围、当天已跑标记一起丢。先写同目录临时文件再 `os.replace`,
    失败时原来那份一字未动(口径同 `pipeline_state._write_state`)。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + '.tmp')
    try:
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding='utf-8')
        os.replace(tmp, path)
    except OSError:
        # 失败就把残骸带走:留着既占地方(满盘那一档正是因为没地方),也让下一个
        # 看目录的人以为那是份配置。原来那份一字未动,所以直接往上抛。
        tmp.unlink(missing_ok=True)
        raise


_announced: dict[str, str] = {}


def announce_config_issue(what: str, reason: str) -> None:
    """定时线程读不出配置时留一句话;`reason` 空串 = 又读得出来了。

    **只在状态变化时说**:每 30 秒一跳,每跳都喊会把服务日志刷满;一声不吭则是
    另一头 —— 调度线程那层 `except Exception: pass` 不留话,「定时从此不再触发」
    就成了一个没有任何痕迹的静默故障。
    """
    if _announced.get(what, '') == reason:
        return
    _announced[what] = reason
    from datetime import datetime
    print(f'[{datetime.now().strftime("%m-%d %H:%M:%S")}] '
          + (reason or f'{what}又读得出来了,定时照常'), file=sys.stderr, flush=True)
