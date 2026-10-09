# -*- coding: utf-8 -*-
"""往 daily_sandbox 摆好的那个沙箱里再加一条「开了正文补抓」的 RSS 订阅,
给 tests/fulltext_probe.mjs 的浏览器回归用。

    python3 tests/daily_sandbox.py /tmp/kb-fulltext-sandbox 8801
    python3 tests/fulltext_sandbox.py /tmp/kb-fulltext-sandbox

**单独一个脚本、不往 daily_sandbox.py 里加东西**:那一份是 daily_probe 和
subs_probe 的共同依赖,它们有好几条检查认的是"条目列表第一行"、"素材清单
第 N 条"。往里插条目会把那些位置挪开,于是本轮的改动会表现成别人那两个
探针变红 —— 而红的原因跟它们要验的事情毫无关系。

摆四条条目,各占一档:
  ① 还没补抓过、**也还没导出**的壳(新进来的那种:导出闸门扣着它)
  ② 还没补抓过、**已经导出过**的壳 —— 打开这个开关最常见的时机就是"已经同步
     过、几百条壳就摆在眼前",那一批全是这个形态。它在 vault 里真的有,所以
     面板不该说"抓到才写进知识库",也不该收走「展开阅读」。
  ③ 补抓过、译好了的(正常形态,证明补抓完成之后那几档状态照旧)
  ④ 试满 MAX_TRIES 的(两道闸门都该放它过去,面板上不该还挂着"正在补抓")
"""
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main():
    base = Path(sys.argv[1] if len(sys.argv) > 1 else '/tmp/kb-fulltext-sandbox')
    data, vault = base / 'data', base / 'vault'
    sys.path.insert(0, str(ROOT))
    os.environ.update(KB_DATA_DIR=str(data), KB_VAULT_ROOT=str(vault))
    from kb.feeds import fulltext
    from kb.feeds import registry as reg

    now = datetime.now().replace(second=0, microsecond=0)

    def at(hours):
        return (now - timedelta(hours=hours)).strftime('%Y-%m-%d %H:%M')

    source = reg.save_subscription('rss', {
        'name': 'Link Aggregator', 'url': 'https://agg.example.org/feed.xml',
        'group_name': '聚合源', 'fulltext': True})
    # 普通订阅各一条:开关是**逐条订阅**的,面板上不该把别人家的条目也标成待补抓。
    plain = reg.save_subscription('rss', {
        'name': 'Full Text Blog', 'url': 'https://solo.example.org/feed.xml'})
    reg.put_items(source, [
        {'url': 'https://news.example.org/post-1', 'published': at(1),
         'title': 'Yes, and',
         # feed 自己给的就是这么一个跳转壳,不是我们截断的。
         'body': 'Yes, and (\nhtmx.org\n)\n10-08\n↑ 108 HN Points'},
        {'url': 'https://news.example.org/post-shipped', 'published': at(2),
         'title': 'A shell that already made it into the vault',
         'body': 'A shell that already made it into the vault (\nexample.org\n)\n10-08\n↑ 77 HN Points'},
        {'url': 'https://news.example.org/post-2', 'published': at(3),
         'title': 'An article whose body did come back',
         'body': 'The full body of this one was fetched from the original link already.'},
        {'url': 'https://news.example.org/post-3', 'published': at(4),
         'title': 'A link that could not be read',
         'body': 'Blocked (\nexample.dev\n)\n10-07\n↑ 42 HN Points'},
    ])
    reg.put_items(plain, [
        {'url': 'https://solo.example.org/own-1', 'published': at(5),
         'title': '这条订阅自己就给全文', 'body': '所以它压根不该排进补抓队列,面板上也不该标。'}])
    with reg.db() as c:
        # ② 还没补抓、但壳已经导出进 vault 了(exported_hash == content_hash)。
        c.execute('UPDATE feed_items SET exported_hash=content_hash WHERE url=?',
                  ('https://news.example.org/post-shipped',))
        # ③ 补抓完成 + 译好:正文已经是整篇,状态该显示「已译为中文」。
        c.execute("UPDATE feed_items SET fulltext_at=?, title_zh=?, body_zh=?, translated_at=?"
                  " WHERE url=?",
                  (reg.now(), '正文确实抓回来了的那一篇',
                   '这一篇的正文是顺着原文链接抓回来的,随后译成了中文。',
                   reg.now(), 'https://news.example.org/post-2'))
        # ④ 试满重试上限:它离开补抓队列,两道闸门都放行(壳子照原样入库)。
        c.execute("UPDATE feed_items SET fulltext_tries=?, fulltext_error=? WHERE url=?",
                  (fulltext.MAX_TRIES, '抓取失败：HTTP 403', 'https://news.example.org/post-3'))
    print(f'agg_id={source["id"]} plain_id={plain["id"]}')


if __name__ == '__main__':
    main()
