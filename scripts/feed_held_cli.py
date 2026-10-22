# -*- coding: utf-8 -*-
"""挂起等人看一眼的那几条原文 —— 列出来,或者把其中几条放回补抓队列。

**这是「证据不足」那一档的人工出口。** 准入判出「我没能确认这一页哪一段是这一篇
的正文」时(`grab_page.Admission.unsure`:份额、拒绝说明样板、内容确认不了这三
道),补抓既不写库、也不扣「对方真拦着/真连不上」那份重试预算 —— 它一次就挂起,
把那半句话记在 `feed_items.fulltext_hold` 上等人看。**没有这个出口那一列就是摆设**
(审查第 1 轮点的就是这一条:状态落了盘,而人既看不到也恢复不了)。

**为什么不是「再自动试两次」**:再敲同一扇门不会让我们知道得更多 —— 同一张页面、
同一套判据,答案不会变。而扣掉那份预算的后果是三轮之后**静默**退回摘要:面板上
再没有任何痕迹,而它和「对方真拦着」的区别恰恰是**我们并不知道**那一页是什么。

**同一份判据只有一处**(`fulltext.held` / `fulltext.release`),网页那两个端点
(`GET /api/intake/{kind}/held`、`POST /api/intake/{kind}/held/release`)和这个
脚本都过它 —— 两处各写一遍 SQL 的话,其中一头迟早漏掉「这条订阅还开着补抓吗」
那一条,于是报了「已恢复 1 条」而它压根回不到队列。

**这个脚本不起服务、不碰 vault、不发一个请求。** 它只读 `feeds.db` 的那几列,
`--release` 那一下的判据待在 UPDATE 自己的 WHERE 里(元规则),所以和 kb-web 里
正在跑的那一轮补抓并发安全:那期间已经补到全文的条目放行不了,`rowcount==0` 就是
「不用管了」。恢复之后**下一轮同步**会再抓它(`./kb.sh sync`),这里不替它跑。
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from kb.feeds import fulltext            # noqa: E402
from kb.feeds import registry as reg     # noqa: E402


def _kinds(kind: str | None) -> list[str]:
    """哪几个来源。缺省扫全部 —— 「哪个来源有待办」正是用户要问的那个问题。"""
    if kind:
        return [reg.check_kind(kind)]
    return list(reg.KINDS)


def _show(kind: str, rows: list[dict]) -> None:
    print(f'== {kind}:{len(rows)} 条挂起等人看一眼 ==' if rows else f'== {kind}:没有 ==')
    for row in rows:
        # key 排在最前面:`--release` 要的就是它(整行可以直接复制粘贴)
        print(f"  {row['key']}  {row['title'][:40]}")
        print(f"      {row['url']}")
        print(f"      {row['fulltext_hold']}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split('\n\n')[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('kind', nargs='?', help='只看这一个来源(缺省:全部)')
    ap.add_argument('--release', nargs='+', metavar='KEY',
                    help='把这几条放回补抓队列(下一轮同步会再抓)')
    ap.add_argument('--release-all', action='store_true',
                    help='把列出来的全部放回队列')
    args = ap.parse_args(argv)
    try:
        kinds = _kinds(args.kind)
    except ValueError as e:
        print(f'{e}', file=sys.stderr)
        return 2
    if args.release and args.release_all:
        print('--release 和 --release-all 只能给一个', file=sys.stderr)
        return 2

    if args.release:
        # **按 key 恢复时不限来源**:用户手里只有那串 key(上面列出来的那一行),
        # 让他再想一遍它属于哪个来源是没有意义的。判据在 `release` 自己的 WHERE 里。
        done = fulltext.release(args.release)
        # **报实际写入结果,不报计划长度**(元规则)。少的那几条照实说清为什么。
        print(f'已放回队列 {done} 条（给了 {len(args.release)} 个 key）')
        if done < len(args.release):
            print('  少的那几条:已经补到全文、key 不存在,或者那条订阅关掉了补抓')
        return 0

    total = 0
    for kind in kinds:
        rows = fulltext.held(kind)
        total += len(rows)
        if rows or args.kind:
            _show(kind, rows)
        if rows and args.release_all:
            done = fulltext.release([r['key'] for r in rows])
            print(f'  已放回队列 {done} 条（下一轮 ./kb.sh sync 会再抓）')
    if not total:
        print('没有挂起的条目。' if not args.kind else '')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
