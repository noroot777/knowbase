#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""图谱归类:给条目化出来的开源项目 / skill 分门别类。

两张图谱(开源项目、skill)各有一套固定分类表,**类别是写死的**而不是让模型
自由发挥:自由标签每轮都会漂(同一个项目今天叫「命令行工具」明天叫「CLI」),
一漂图谱的分区就换一副样子,"经常看"的上层视角就没了。固定表 + 模型只选
code,分区因此稳定,改类目时 bump `Taxonomy.sig()` 会自动触发全量重判。

判定结果落 `data/taxonomy_{key}.json`,**按 norm 长期复用**:一个项目判一次,
之后每天重生成图谱都是零 LLM。新条目进来才判新增的那几个(build 脚本默认
带 `--max-new` 上限,日常一天也就一两批)。

用哪家的哪个模型由设置面板的「管道 · 图谱归类」阶段决定(kb.llm_stages 的
"taxonomy")。批量判定是「给 40 条 name+summary 选 code」这种活,便宜模型够用,
内置默认取的是实测一批 12 秒的那个。
"""
from __future__ import annotations

import fcntl
import json
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, field
from hashlib import blake2b
from pathlib import Path
from threading import RLock

REPO = Path(__file__).resolve().parents[2]
CACHE_DIR = REPO / "data"


# 分类表(Cat / Taxonomy / PROJECTS / SKILLS)搬到了 taxo_tables.py:这个文件
# 越过了 700 行上限,而分类表是里面最独立、最不可能被改绑定的一块。这里原样
# 再导出,`taxonomy.PROJECTS` 这些名字照旧可用。
from taxo_tables import (  # noqa: E402,F401
    Cat, Taxonomy, PROJECTS, SKILLS, TAXONOMIES,
)


# ── 缓存 ───────────────────────────────────────────────────────────────────────

# 可重入:publish_lock 持着它再走 save_cache(那里面也要这把锁)
_io_lock = RLock()


def cache_path(taxo: Taxonomy) -> Path:
    return CACHE_DIR / f"taxonomy_{taxo.key}.json"


def staging_path(taxo: Taxonomy) -> Path:
    """**本轮判出来、还没被验收的判定**。判定一律先攒在这儿,等调用方把新图
    验收通过、页面也落盘了才换上正式缓存(`Verdicts.commit()`)。

    全量重判要这个,是因为直接往正式缓存里写的话 `--max-new` 一截断,几千条
    完整旧判定就被几百条新判定盖掉了(2026-09-22 第 2 轮审查发现);增量同样
    要,是因为「页面验收不过」时得能把这一轮的判定原样撤掉——写进正式缓存的
    东西撤不回来,下一轮全命中坏缓存、不再问模型,流水线自己爬不出来
    (2026-09-22 第 6 轮审查发现)。
    """
    return CACHE_DIR / f"taxonomy_{taxo.key}.staging.json"


@contextmanager
def publish_lock(taxo: Taxonomy):
    """发布这一张图的跨进程独占锁(验收基线 → 落页面 → 提交缓存整段)。

    页面和缓存是两个文件、两次 rename,各自原子挡不住两个 writer 交错:A 写完
    页面 → B 写页面并提交 B 的缓存 → A 再提交 A 的缓存,两个进程都以 0 退出,
    最终页面来自 B 而缓存来自 A,下一轮又拿 A 的缓存把页面刷回去,一路没有任何
    告警(2026-09-22 复审第 3 轮发现)。CLI、wiki_daily 子进程、手动重跑都是
    独立进程,进程内的锁管不着,所以锁在文件上。

    锁的是**单独的 lockfile**,不是缓存或页面本身:那两个都是 rename 换文件,
    锁着的 inode 会被换走;lockfile 也只创建不删除,unlink 会让「A 持锁 →
    B 删文件 → C 新建再持锁」出现两个持锁者(和 secrets.json 那把锁同一套理由)。
    """
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    p = CACHE_DIR / f"taxonomy_{taxo.key}.publish.lock"
    with _io_lock:                       # 同进程内的并发先在这儿排队
        fh = p.open("a+")
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            yield
        finally:
            fh.close()                   # 关闭即释放 flock


def load_raw(taxo: Taxonomy, path: Path | None = None) -> tuple[dict[str, str], dict[str, str]]:
    """({norm: code}, {norm: 名字})。分类表指纹对不上(类目改过)就当空缓存,
    触发全量重判。名字只是给人翻这份 JSON 时对得上号,判定不读它。"""
    p = path or cache_path(taxo)
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}, {}
    if raw.get("sig") != taxo.sig():
        return {}, {}
    items = raw.get("items")
    if not isinstance(items, dict):
        return {}, {}
    codes, names = {}, {}
    for k, v in items.items():
        if isinstance(v, dict) and isinstance(v.get("c"), str):
            codes[k] = v["c"]
            names[k] = str(v.get("n") or "")
    return codes, names


def load_cache(taxo: Taxonomy) -> dict[str, str]:
    return load_raw(taxo)[0]


def cache_fingerprint(taxo: Taxonomy, codes: dict[str, str] | None = None) -> str:
    """正式缓存的内容指纹(判定开始时记一份,发布前在锁里再取一份比对)。

    发布锁只保证两次发布动作不交错,认不出**后拿到锁的那个是基于陈旧基线跑出来
    的**:旧任务读完空基线、慢慢判、停在锁外;新任务发布了 40 节点的页面和 40 条
    缓存;旧任务再拿到锁,以退出码 0 把页面刷回 30 节点、缓存刷回 30 条,新增的
    10 条判定当场蒸发(2026-09-23 复审第 6 轮发现)。`--no-llm` 那条路更隐蔽:
    它的 commit 是空操作,只把旧页面重发一遍,于是页面 30 节点、缓存 40 条,
    两边长期对不上。

    指纹算的是**内容**(分类表指纹 + 排好序的 norm→code),不是文件字节:
    另一个 writer 只更新了昵称之类的话,判定没变就不该让整轮白跑。
    """
    codes = load_cache(taxo) if codes is None else codes
    h = blake2b(taxo.sig().encode("utf-8"), digest_size=12)
    for k in sorted(codes):
        h.update(f"\x00{k}\x01{codes[k]}".encode("utf-8"))
    return h.hexdigest()


def _cleanup(path: Path, why: str) -> None:
    """清暂存:失败只告警,不往上抛。

    该丢的判定在内存里已经丢了,盘上这份下一轮照样会被重新识别、重新丢掉——
    没有理由让一次 unlink 失败把整轮带崩。`_promote()` 那头已经这么做了,恢复
    路径原来还在裸调 unlink:权限问题不消失的话,「下一轮自己收尾」的承诺就成了
    「以后每一轮都异常退出」(2026-09-23 复审第 6 轮发现)。
    """
    try:
        path.unlink(missing_ok=True)
    except OSError as e:
        print(f"[{path.name}] {why}没清掉({e}),不影响这一轮的结果", file=sys.stderr)


def _rewrite_stage(taxo: Taxonomy, stage: Path, codes: dict[str, str],
                   names: dict[str, str], kept_runs: list[list[str]]) -> None:
    """把暂存缩成「只剩验过的那几批」;一批都没剩就删掉。写盘失败同样只告警
    ——留在盘上的那份下一轮会被重新识别、重新丢掉,不该让它带崩这一轮。"""
    if not kept_runs:
        _cleanup(stage, "作废的暂存")
        return
    try:
        save_cache(taxo, {n: codes[n] for g in kept_runs for n in g},
                   names, stage, runs=kept_runs)
    except OSError as e:
        print(f"[{taxo.key}] 暂存没能改写({e}),不影响这一轮的结果", file=sys.stderr)


def stale_cache_size(taxo: Taxonomy) -> int:
    """正式缓存里「指纹已经对不上」的那份有多少条(0 = 没有/指纹是对的)。

    这就是「分类表改过、需要全量重判」的判据,也是重判期间**必须原样保住**的
    那份东西。
    """
    try:
        raw = json.loads(cache_path(taxo).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return 0
    items = raw.get("items")
    if raw.get("sig") == taxo.sig() or not isinstance(items, dict):
        return 0
    return len(items)


def save_cache(taxo: Taxonomy, codes: dict[str, str], names: dict[str, str],
               path: Path | None = None,
               runs: list[list[str]] | None = None) -> None:
    """写盘用「唯一临时文件 + rename」:截断重写会让并发读者读到半截 JSON,
    而半截 JSON 被 load_cache 吞成空缓存,下一次 build 就会把几千条重判一遍。

    `runs` 只有暂存文件用:记「哪几条是同一趟判出来的」。见 `load_runs`。
    """
    p = path or cache_path(taxo)
    p.parent.mkdir(parents=True, exist_ok=True)
    body: dict = {"sig": taxo.sig(),
                  "items": {k: {"c": v, "n": names.get(k, "")}
                            for k, v in sorted(codes.items())}}
    if runs is not None:
        body["runs"] = [sorted(g) for g in runs if g]
    tmp = p.with_name(f"{p.name}.{os.getpid()}.tmp")
    with _io_lock:
        tmp.write_text(json.dumps(body, ensure_ascii=False, indent=0), encoding="utf-8")
        tmp.replace(p)


def load_runs(path: Path, part: dict[str, str]) -> list[list[str]]:
    """暂存里记的批次分组:[[norm, ...], ...],一组 = 同一趟 classify 判出来的。

    暂存要有批次身份,是因为**质量判定跑在批次上**:进程在暂存写完、`finish()`
    的退化判定跑完之前被杀(或者另一个 writer 抢先读走),这批本该判为「不作数」
    的结果就留在盘上了;下一轮把它和更多正常结果合在一起算占比,20 条全废的
    被 180 条正常的稀释成 10%,当场随整份缓存提交,下一轮全命中、不再问模型
    (2026-09-22 复审第 4 轮发现)。记下分组,恢复时就能按原批次各验各的。

    老格式(没有 runs)整份当一个批次——那是同样没验过的东西,不能因为
    读不到分组就当它验过了。没被任何分组记到的零头同理,单独凑一组。
    """
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        got = raw.get("runs")
    except (OSError, json.JSONDecodeError):
        got = None
    groups: list[list[str]] = []
    seen: set[str] = set()
    for g in got if isinstance(got, list) else []:
        keep = [n for n in g if isinstance(n, str) and n in part and n not in seen]
        seen.update(keep)
        if keep:
            groups.append(keep)
    rest = [n for n in part if n not in seen]
    if rest:
        groups.append(rest)
    return groups


# ── LLM 批量判定 ───────────────────────────────────────────────────────────────

_PROMPT = """\
下面是一批{item_zh},请逐条判断它属于哪个类目。

类目表(只能从这些 code 里选):
{table}

判定要求:
- 按这个东西**本身是什么/主要用来做什么**归类,不要被它顺带支持的能力带偏。
- 每条只给一个 code,拿不准就选最接近的那个;确实判不出来才给 {fallback}。
{extra}
条目(格式「编号. 名字 :: 简介」):
{items}

只输出 JSON 数组,不要解释、不要代码围栏,每个元素 {{"id": <编号>, "c": "<code>"}}。"""


def _llm(prompt: str, model, timeout: int = 300, max_tokens: int = 8192) -> str:
    import sys
    if str(REPO) not in sys.path:
        sys.path.insert(0, str(REPO))
    from kb.llm import chat

    return chat(prompt, model, max_tokens=max_tokens, timeout=timeout)


def default_model():
    import sys
    if str(REPO) not in sys.path:
        sys.path.insert(0, str(REPO))
    from kb.llm_stages import stage_model

    return stage_model("taxonomy")


def _parse(out: str) -> dict[int, str]:
    out = re.sub(r"^```(?:json)?|```$", "", out.strip(), flags=re.M).strip()
    try:
        arr = json.loads(out)
    except json.JSONDecodeError:
        # 整段解析不了时退回逐对象扫描:模型偶尔在数组后面多写一句话
        arr = [json.loads(m.group(0)) for m in re.finditer(r"\{[^{}]*\}", out)]
    got = {}
    for x in arr:
        if isinstance(x, dict) and "id" in x and "c" in x:
            try:
                got[int(x["id"])] = str(x["c"]).strip()
            except (TypeError, ValueError):
                continue
    return got


@dataclass(frozen=True)
class Verdicts:
    """一轮归类的结果。

    `complete=False` 只在**全量重判没做完**时出现,意思是「这份 codes 是半成品,
    别拿它覆盖页面」:分类表刚改过、旧缓存整份失效,而这一轮没能给每个候选都
    拿到一个有效 code(批次抛异常,或模型返回合法 JSON 却漏了一截)。日常增量
    判定永远是 complete——那时候没判到的几条显示成「待归类」是既定行为,不该
    拦着图谱更新。

    **判完 ≠ 已提交**。重判的新结论一直攥在半成品文件里,要等调用方把新图验收
    通过、页面也落盘了,才 `commit()` 换上正式缓存:两件事不在一个事务里的话,
    「缓存已经换掉、页面校验才拦下」就成了半吊子状态——页面保住了,可那份还能
    用的旧缓存已经没了,之后每一轮都会命中新缓存、不再问模型,流水线自己爬不
    出来(2026-09-22 第 4 轮审查发现)。
    """
    codes: dict[str, str]
    complete: bool = True
    migrating: bool = False
    pending: int = 0               # 还差多少条没判(仅重判期间有意义)
    commit_fn: object = None       # 待提交的正式缓存;None = 没什么可提交的
    discard_fn: object = None      # 扔掉半成品;None = 没有半成品要扔
    reason: str = ""               # complete=False 时:为什么不能用(给人看的)
    # 这一轮是**基于哪一版正式缓存**判出来的(`cache_fingerprint`)。发布前要在
    # 锁里拿它跟盘上的再比一次:发布锁只保证两次发布动作不交错,认不出后拿到锁
    # 的那个是基于陈旧基线跑出来的(2026-09-23 复审第 6 轮发现)。
    baseline: str = ""

    def commit(self) -> None:
        """新图验收通过、页面已落盘之后调:把半成品原子换成正式缓存。"""
        if self.commit_fn is not None:
            self.commit_fn()

    def discard(self) -> None:
        """新图验收**没**通过时调:连半成品一起扔掉,下一轮重新问模型。

        判定本身完整、只是结果不作数(全判成「不是 skill」之类)时,半成品留着
        的话下一轮全命中、不再问模型,同一个坏结果会一直卡在那儿——扔掉才能在
        模型恢复后自己走出来。**判到一半**那种不要扔(那是进度,下次接着判)。
        """
        if self.discard_fn is not None:
            self.discard_fn()


# 重判时漏项的补判轮数:模型偶尔漏一条,补判只问缺的那些,批次更小更容易回全。
# 补不齐也不放宽 promote 的判据,只是少一次「因为漏一条而整轮白等」。
_RESIGN_RETRIES = 2

# 「这一趟等于没判」的判据:**这一趟新拿到的判定**里,落在 void(待归类 /
# 不是 skill)上的占到 VOID_SHARE 就不作数。分母只算新判定——拿整份缓存当分母
# 的话,3188 条缓存能把「新增 400 条全废」稀释成 12.6%,整批失败照样入库。
# 不是只有 100% 才算坏:1 条正常 + 100 条待归类发出去也是一张废页,而且会把那
# 100 条固化进正式缓存。反过来,少于 VOID_MIN_N 条时连 100% 也不算坏:skill
# 候选里近四分之一本来就判「不是 skill」,某天只进来两三条、碰巧全是它是正常的。
VOID_SHARE = 0.9
VOID_MIN_N = 20


def _void_bad(codes: list[str], void: frozenset[str]) -> int:
    """这一**批**判定算不算「等于没判」?算就返回落在 void 上的条数(>0),
    不算返回 0。判据只有这一份:这一轮自己判的那批、和从暂存里续上的每一批,
    都拿它各验各的(见 `load_runs`)。"""
    n_void = sum(1 for c in codes if c in void)
    return n_void if len(codes) >= VOID_MIN_N and n_void >= len(codes) * VOID_SHARE else 0


def classify(taxo: Taxonomy, items: list[tuple[str, str, str]], *,
             model=None, workers: int = 6, batch: int = 40,
             max_new: int | None = None, void_codes: frozenset[str] = frozenset(),
             allow_degenerate: bool = False, log=print) -> Verdicts:
    """items = [(norm, name, summary)],**按重要性排好序**(热度高的在前)。

    `void_codes`:判成这些 code 等于「什么也没判出来」(fallback 自动算一个;
    skill 图谱还要把 X = 不是 skill 算进去,它判完就被踢出图谱)。压倒性地
    落在这里面时按「这一趟不作数」处理,见下面的 `finish()`;维护者确认这就是
    预期结果(比如故意收紧了成员资格规则)时用 `allow_degenerate` 放行,
    对应命令行的 --allow-shrink。

    两种模式,区别在于「旧缓存还算不算数」:

    - **增量**(指纹没变):只判缓存里没有的那几条,`--max-new` 是上限——某天
      涌入几百条新条目时不至于把一轮管道拖成半小时。没判到的落 fallback。
    - **全量重判**(指纹变了,分类表/规则改过):旧结论整份作废,`--max-new`
      **不适用**。截断 + 就地覆盖的话,3183 条完整旧判定会被 400 条新判定盖掉,
      剩下 2783 个节点当场变「待归类」,不可逆(2026-09-22 第 2 轮审查发现)。
      判不完就保住旧缓存、下一轮接着判。

    **两种模式都只写暂存文件**(`staging_path`),正式缓存一律等调用方验收完新图
    再 `commit()`。增量那一侧原来是直接写正式缓存的,于是「页面验收不过」时
    `discard()` 无从撤起:坏判定当场固化,下一轮全命中、不再问模型
    (2026-09-22 第 6 轮审查发现)。
    """
    codes, names = load_raw(taxo)
    official = dict(codes)     # 进来时的正式缓存,判「这一轮有没有新东西可提交」
    baseline = cache_fingerprint(taxo, official)   # 发布前在锁里再比一次
    base = set(codes)          # 正式缓存里已经有的那些(重判时这里是空的)
    stale = 0 if codes else stale_cache_size(taxo)
    migrating = bool(stale)
    stage = staging_path(taxo)
    void = void_codes | {taxo.fallback}
    # 续上上一轮判出来、还没提交的那些(指纹对不上的暂存会被 load_raw 当空的,
    # 那种留在盘上也没用,顺手删掉)。**续之前先按原批次各验各的**:暂存里的
    # 东西都是没走完质量判定的半成品,进程被杀/被另一个 writer 抢读都会留下
    # 一整批坏判定,直接合进来就被这一轮的正常结果稀释掉了(见 load_runs)。
    part, pnames = load_raw(taxo, stage)
    kept_runs: list[list[str]] = []
    if part:
        # **正式缓存里已经有的 key,一律以正式缓存为准。** 暂存不记它是基于哪一版
        # 正式缓存判出来的:旧 writer 在别人提交之后才把自己那份写进共享暂存、随即
        # 崩掉,下一轮 `codes.update(part)` 就拿旧结论盖掉了新提交的那条——条数还
        # 没变,`len(codes) == n_official` 又判成「无事可提交」,于是页面按暂存发、
        # 缓存留在正式值上,两边当场对不上,而且每一轮都这样(2026-09-22 复审第 5
        # 轮发现)。老格式暂存(整份 codes 落盘,含正式基线)也要靠这一步把基线摘
        # 掉,不然「整份当一批验」正好把第 3 轮那个错分母又请回来:20 条全废的新增
        # 混进 100 条正式基线,占比 16.7%,照样提交(同轮发现)。
        clash = {n for n in part if n in base}
        stale_part = {n for n in clash if part[n] != official[n]}
        part = {n: c for n, c in part.items() if n not in clash}
        dropped = len(clash)
        if clash:
            log(f"  [{taxo.key}] 暂存里有 {len(clash)} 条正式缓存已经有了"
                f"({len(stale_part)} 条结论还不一样),以正式缓存为准,丢掉")
        for g in load_runs(stage, part):
            n_void = _void_bad([part[n] for n in g], void)
            if n_void and not allow_degenerate:
                dropped += len(g)
                log(f"  [{taxo.key}] 暂存里有一批 {len(g)} 条、{n_void} 条落在 "
                    f"{sorted(void)} 上(上一趟没走完质量判定就被打断),不作数,"
                    f"这一轮重判")
                continue
            kept_runs.append(g)
            codes.update({n: part[n] for n in g})
            names.update({n: pnames.get(n, "") for n in g})
        if dropped:                       # 丢掉的当场从盘上抹掉,别留到下一轮
            _rewrite_stage(taxo, stage, codes, names, kept_runs)
    elif stage.exists():
        _cleanup(stage, "作废的暂存")
    resumed = {n for g in kept_runs for n in g}
    if migrating:
        log(f"  [{taxo.key}] 分类表改过,{stale} 条旧判定整份作废,本轮全量重判"
            f"(--max-new 不适用){f';续上暂存 {len(resumed)} 条' if resumed else ''}")

    names.update({n: nm for n, nm, _ in items})

    def finish() -> Verdicts:
        """收尾:判完了没?判出来的东西作不作数?作数也只是**待提交**。"""
        left = sum(1 for it in items if it[0] not in codes)
        if migrating and (left or not codes):
            why = (f"分类规则改过,全量重判还没做完(已判 {len(codes)} 条,"
                   f"还差 {left} 条:批次失败或模型漏项),进度已存盘,下次跑接着判")
            log(f"  [{taxo.key}] {why};正式缓存与旧图原样保留")
            return Verdicts(codes, False, migrating, left, reason=why,
                            baseline=baseline)
        # **分母是这一趟自己判的那些**:不是整份缓存(3188 条缓存能把「新增 400 条
        # 全废」稀释成 12.6%,2026-09-22 复审第 3 轮发现),也不含从暂存里续上的
        # 批次(那些已经在上面各验过一遍了;混在一起算,180 条正常的照样能把 20 条
        # 全废的稀释过关,复审第 4 轮发现)。
        mine = {n: c for n, c in codes.items() if n not in base and n not in resumed}
        n_void = _void_bad(list(mine.values()), void)
        if n_void and not allow_degenerate:
            # 这一趟拿到的判定压倒性地落在「等于没判」的那几个 code 上(全待归类、
            # skill 全判成「不是 skill」、或者 101 条里 100 条待归类),这不是判定
            # 结果,是这一趟坏了(提示词/模型/中转站)。**不是只有 100% 才算坏**:
            # 1 条正常 + 100 条 fallback 一样会被发成一张满屏待归类的页面,还把
            # 那 100 条固化进正式缓存(2026-09-22 复审第 2 轮发现)。反过来,
            # 少于 VOID_MIN_N 条时连 100% 也不算:skill 候选里本来就有近四分之一
            # 判出来「不是 skill」,某天只进来两三条、碰巧全是它,那是正常结果,
            # 按坏结果拦下会让图谱天天红着、还得人工加 --allow-shrink 才能过。
            # 这一趟的判定从暂存里抹掉:留着的话下一轮全命中、不再问模型,同一个
            # 坏结果会一直卡在那儿(2026-09-22 第 4、5 轮审查发现)。验过的那些
            # 批次留着——它们是别的趟的进度,不该被这一趟的失败连坐。
            _rewrite_stage(taxo, stage, codes, names, kept_runs)
            share = "全" if n_void == len(mine) else f"{n_void}/{len(mine)} 条"
            why = (f"{'重判' if migrating else '本轮新判'}结果 {share}落在 "
                   f"{sorted(void)} 上(等于什么也没判出来),这一趟不作数,下次重判")
            log(f"  [{taxo.key}] {why};正式缓存与旧图原样保留")
            return Verdicts(codes, False, migrating, n_void, reason=why,
                            baseline=baseline)
        if not migrating and codes == official:
            # 「有没有新东西可提交」按**内容**比,不是比条数:旧 writer 留下的
            # 同名判定被上面摘掉之后条数一样、内容也一样才叫没事干;只比长度的话
            # 「盖掉一条又补进一条」这种也会被当成无事可提交(复审第 5 轮发现)。
            return Verdicts(codes, True, False, left, baseline=baseline)
        log(f"  [{taxo.key}] {len(codes)} 条待提交(新图验收通过后才换上正式缓存)")
        ready, kept = dict(codes), dict(names)
        return Verdicts(codes, True, migrating, left,
                        lambda: _promote(taxo, ready, kept),
                        lambda: _cleanup(stage, "不作数的暂存"),
                        baseline=baseline)

    pending = [it for it in items if it[0] not in codes]
    if max_new is not None and len(pending) > max_new and not migrating:
        log(f"  [{taxo.key}] 待判 {len(pending)} 条,本轮只判前 {max_new} 条(按热度)")
        pending = pending[:max_new]
    if not pending:
        log(f"  [{taxo.key}] 全部命中缓存({len(codes)} 条)")
        return finish()

    model = model or default_model()
    valid = {c.code for c in taxo.cats}
    log(f"  [{taxo.key}] 新判 {len(pending)} 条,模型 {model},{workers} 并发")

    def run(chunk: list[tuple[str, str, str]]) -> dict[str, str] | None:
        lines = "\n".join(
            f"{i}. {nm} :: {(sm or '(无简介)')[:140]}"
            for i, (_n, nm, sm) in enumerate(chunk))
        prompt = _PROMPT.format(
            item_zh=taxo.item_zh, table=taxo.prompt_table(), fallback=taxo.fallback,
            extra=(taxo.extra_rules + "\n") if taxo.extra_rules else "", items=lines)
        try:
            got = _parse(_llm(prompt, model))
        except Exception as e:                      # 单批失败不该带崩整轮
            log(f"  [{taxo.key}] 一批失败({type(e).__name__}: {e}),留到下次")
            return None
        return {chunk[i][0]: c for i, c in got.items()
                if 0 <= i < len(chunk) and c in valid}

    def stash() -> None:
        """把 codes 落到暂存,并记下「哪些是这一趟判的」(见 load_runs)。"""
        mine = [n for n in codes if n not in base and n not in resumed]
        save_cache(taxo, {n: codes[n] for n in codes if n not in base}, names,
                   stage, runs=kept_runs + [mine])

    def do_round(todo: list[tuple[str, str, str]]) -> None:
        """判一轮 todo,结果并进 codes 并落到**暂存文件**。

        **一批「没抛异常」不等于「这批判完了」**:模型返回语法合法但只含几条的
        数组、输出被截断、id 重复、给的 code 在表外被过滤掉,都会让这批悄悄少
        回来一截。所以漏项要和抛异常一样记下来,别当成功批次
        (2026-09-22 第 3 轮审查发现)。
        """
        batches = [todo[i:i + batch] for i in range(0, len(todo), batch)]
        done, saved = 0, len(codes)
        with ThreadPoolExecutor(max_workers=workers) as ex:
            for chunk, got in zip(batches, ex.map(run, batches)):
                if got is not None:
                    codes.update(got)
                    short = sum(1 for it in chunk if it[0] not in got)
                    if short:
                        log(f"  [{taxo.key}] 一批只回了 {len(chunk) - short}/"
                            f"{len(chunk)} 条(模型漏项/被截断),缺的留到下一轮")
                done += 1
                # 只在真判出新东西时落盘;落的始终是暂存文件,正式缓存要等
                # 调用方验收完新图才动。
                if len(codes) > saved and (done % 5 == 0 or done == len(batches)):
                    stash()                      # 中途落盘:被打断也不白跑
                    saved = len(codes)
                    log(f"  [{taxo.key}] {done}/{len(batches)} 批,已判 {len(codes)} 条")
        if len(codes) > saved:
            stash()

    do_round(pending)
    if migrating:
        # 漏的那些再问一轮(只问缺的,批次更小、更容易回全)。这是为了别因为模型
        # 偶尔漏一条就永远 promote 不了——但补不齐也绝不放宽 finish() 的完成判据。
        for _ in range(_RESIGN_RETRIES):
            todo = [it for it in items if it[0] not in codes]
            if not todo:
                break
            log(f"  [{taxo.key}] 还差 {len(todo)} 条没拿到判定,补判一轮")
            do_round(todo)

    # **完成 = 每一条都拿到了有效 code**,不是「没有批次抛异常」。漏项响应造成的
    # 后果和第 2 轮那次截断一模一样:3 条判定 promote 成正式缓存、覆盖掉 101 条
    # 旧判定,页面上 98 个节点变待归类(2026-09-22 第 3 轮审查发现)。
    return finish()


def _promote(taxo: Taxonomy, codes: dict[str, str], names: dict[str, str]) -> None:
    """把暂存换成正式缓存(rename 是原子的),再删掉暂存。

    只由 `Verdicts.commit()` 调——判完**并且**新图验收通过、页面落盘之后。

    **`save_cache` 一成功就算提交完成**,后面那句清暂存失败只是个可恢复的收尾
    问题,不能让它抛出去:调用方(`build_catgraph._publish`)把 `commit()` 的
    任何异常都理解成「缓存没提交」,于是把页面回滚成旧的——缓存已经是新的 120
    条、页面却被换回旧的 100 节点,反方向的半提交是自己制造出来的
    (2026-09-22 复审第 5 轮发现)。留在盘上的暂存下一轮会被识别成「正式缓存
    已经有了」而丢掉,清理本身能自愈。
    """
    save_cache(taxo, codes, names)
    _cleanup(staging_path(taxo), "正式缓存已提交,但暂存")
