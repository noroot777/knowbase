# -*- coding: utf-8 -*-
"""回写:把 tag 写进 md frontmatter、把 content_hash 刷到当前那一份。

从 `kb/extract.py` 拆出来的一块(700 行上限逼出来的第三次拆分,手法同
`kb.extract_optin` / `kb.extract_promo`)。这里只有**碰 md 文件的那三件**:
加一行 tag、摘一行 tag、刷 `documents.content_hash`。协调(取维护锁、确认
这一篇还在、按判定对齐软广那行)仍在 `kb.extract.writeback_extraction`。

**三个函数都在函数内 `from kb.extract import VAULT_ROOT`**,不在模块级把它
绑成自己的全局:夹具一律打桩 `extract.VAULT_ROOT`(那是生产里唯一的那一份),
绑成本模块的全局就等于在测试里读真实 vault。
"""
import os
import re
import tempfile
from pathlib import Path


def replace_md(full: Path, text: str) -> None:
    """把一份 md 整份换成 `text` —— **同目录临时文件 + 原子替换,不就地截断。**

    `Path.write_text` 是 `open(mode="w")`:它**先把原文件截成 0 字节**,再写新
    内容。那中间进程没了(SIGKILL、OOM、掉电),磁盘上剩的是个空文件,而我们
    的兜底认不出这件事 —— 待办里存的是**目标标签**,不是原文。实测过:在
    `open('w')` 刚返回、`write` 还没发生的那一刻 SIGKILL,加一行 tag 和摘一行
    tag 两种变换都把整篇原文变成 0 字节;重启后 `sync-tags` 修不回来(空文件
    压根没有 frontmatter,加标签那一侧直接报错、摘标签那一侧还会因为"确实没有
    这个 tag"把待办清零),再索引一次反而把空正文收进库 —— `doc_body` 从此是
    空串,阅读页上一片空白,文章正文永久没了(2026-10-14 审查,两个方向各一次
    真实中断,外加页面截图)。**维护锁防不了这一档**:它挡的是别的协作进程插进
    来,挡不住本进程在打开写模式之后没了。

    原子替换把末态收到两个:**要么还是原来那一份完整的,要么已经是新的那一份
    完整的。** 临时文件和目标同目录(`os.replace` 跨设备会抛),写完 `flush` +
    `fsync` 再 replace,中途失败就把临时文件清掉。**后缀是 `.tmp` 不是 `.md`**
    —— 索引扫的是 `rglob("*.md")`,真被 SIGKILL 留下一个临时文件也不会被当成
    一篇文档收进库。**权限位要沿用原文件的**:replace 之后留下的是临时文件那个
    inode,`mkstemp` 给的是 0600,别的工具(编辑器、同步)会读不了它。
    """
    try:
        mode = full.stat().st_mode & 0o777
    except OSError:
        mode = None                 # 原来不在就按新建算,交给 umask
    fd, tmp = tempfile.mkstemp(dir=full.parent, prefix=f".{full.name}.",
                               suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())    # 元数据落盘前被打断也不要留半份新内容
        if mode is not None:
            os.chmod(tmp, mode)
        os.replace(tmp, full)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def writeback_tags(doc_path: str, new_tags: list[str]) -> bool:
    """把新 tag 合并进 md 的 frontmatter tags 列表。只改 frontmatter,不碰正文。"""
    from kb.extract import VAULT_ROOT

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
    replace_md(full, new_fm + body)
    return True


def remove_writeback_tag(doc_path: str, tag: str) -> bool:
    """从 md frontmatter 的 tags 列表里摘掉一个 tag。writeback_tags 的反向操作。

    只删 tags 列表里那一行,正文和其余 frontmatter 一个字节不碰。没有
    frontmatter、本来就没这个 tag 都返回 False(没改文件)。

    **读不出来不吞。** 原来这里 `read_text` 的 `OSError` 被静默换成 False,
    而 False 跟「本来就没有」长得一模一样 —— 没权限、文件刚被挪走的那次「摘
    标签」于是报成了成功,库里是 none、md 里那行还在,再没有人回来看它。
    这一档现在交给调用方:`extract_promo.align_promo_tag` 回读确认之后留待办。

    **值带不带引号都要摘到。** 我们自己写进去的是 `  - flag/软广`,但同一份
    frontmatter 被别的工具或人重排成 `- "flag/软广"` 是合法 YAML,索引照旧
    读得出这个 tag(`scanner.frontmatter_tags` 走的是 yaml.safe_load)。按行删
    的这条正则覆盖不到的写法还有 flow-style(`tags: [flag/软广]`)——**那一档
    不在这里修,由回读确认兜住**:摘不掉就抛,留下待办、如实记一次没完成的
    交付,而不是返回一个「本来就没有」。
    """
    from kb.extract import VAULT_ROOT

    full = VAULT_ROOT / doc_path
    text = full.read_text(encoding="utf-8")
    m = re.match(r"\A(---\n.*?\n---\n)", text, re.S)
    if not m:
        return False
    fm_block, body = m.group(1), text[m.end():]
    new_fm = re.sub(
        rf"""^[ \t]*-[ \t]+(?P<q>["']?){re.escape(tag)}(?P=q)[ \t]*\n""", "",
        fm_block, flags=re.M)
    if new_fm == fm_block:
        return False
    replace_md(full, new_fm + body)
    return True


def refresh_content_hash(conn, doc_path: str, expect: str) -> None:
    """writeback 改了 frontmatter 后刷新库里的 hash,避免下次 index 无谓重建。
    正文没变(chunker 只看 body),chunks/FTS/向量都无需动。

    **只认「库里记的还是我动手之前那一份」,`expect` 是必填的**(判据待在
    UPDATE 自己的 WHERE 里)。这一句写的是「这个文件已经索引过了」——
    `content_hash` 就是索引判「要不要重建」的唯一依据。改 frontmatter 之前
    文件就已经和库里记的不一样(别人刚改完、索引还没跟上)时,无条件刷新等于
    **替那份改动签了一张已索引的收据**:下一轮 `doc_needs_reindex` 说「未变」,
    那份改动永远进不了 chunks。实测过最坏的一种:磁盘上换成了新正文、chunks
    还是旧的,新正文一个字都搜不到,点开文章却是新的,再同步也不恢复
    (2026-10-14 审查)。0 行不是失败,正是想要的末态 —— 这一篇继续带着
    「待索引」,下一轮重建一次就对了(误报的代价只是白重建一次,漏报是静默丢
    数据)。

    **没有「不给 expect」这一档。** 原来它有个无条件分支留给两个「自己刚写完
    这个文件、紧接着刷」的单线程脚本(`scripts/backfill_youtube_summaries.py`、
    `scripts/wiki/wiki_daily.py`),理由是它们手里没有动手之前那个值 —— 可那个
    值它们一直有(写之前读的那份原文),而「单线程」只排除了自己,排除不掉
    另一个进程里正在跑的导出。一个无条件分支就够把上面那个末态造出来,所以
    这个参数没有默认值:新调用方要么拿得出动手之前那份 hash,要么别刷。
    """
    from kb.extract import VAULT_ROOT
    from kb.scanner import file_hash

    full = VAULT_ROOT / doc_path
    try:
        text = full.read_text(encoding="utf-8")
    except OSError:
        return
    conn.execute(
        "UPDATE documents SET content_hash=? WHERE path=? AND content_hash=?",
        (file_hash(text), doc_path, expect))
