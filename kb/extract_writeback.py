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
import re


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
    full.write_text(new_fm + body, encoding="utf-8")
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
    full.write_text(new_fm + body, encoding="utf-8")
    return True


def refresh_content_hash(conn, doc_path: str, expect: str) -> str:
    """writeback 改了 frontmatter 后刷新库里的 hash,避免下次 index 无谓重建。
    正文没变(chunker 只看 body),chunks/FTS/向量都无需动。
    返回空串 = 刷了,非空 = 没刷的原因。

    **判据不在这儿,在 `kb.doc_rewrite.register_indexed`** —— 那一份是「登记
    `content_hash` 即声明已索引」这个不变量的唯一口径(两样证明:库里记的还是我
    动手之前那一份、磁盘上那组索引输入还是索引里那一组)。这里只多做一件事:
    把磁盘上**此刻**那一份读出来交给它。

    **读不出来就什么都不刷。** 回写那条路上这一档的主路径本来就是异常(文件刚被
    清理删掉),这里只保证万一走到刷新也不会替谁签一张「已索引」的收据。

    **没有「不给 expect」这一档。** 原来它有个无条件分支留给两个「自己刚写完这个
    文件、紧接着刷」的单线程脚本,理由是它们手里没有动手之前那个值 —— 可那个值它
    们一直有(写之前读的那份原文),而「单线程」只排除了自己,排除不掉另一个进程
    里正在跑的导出。那两条旁路现在整段走 `kb.doc_rewrite.rewriting()`,不再自己
    碰这一句;这个参数也没有默认值:新调用方要么拿得出动手之前那份 hash,要么
    别刷。
    """
    from kb.doc_rewrite import register_indexed
    from kb.extract import VAULT_ROOT

    try:
        text = (VAULT_ROOT / doc_path).read_text(encoding="utf-8")
    except OSError as e:
        return f"读不出来({e})"
    return register_indexed(conn, doc_path, text, expect)
