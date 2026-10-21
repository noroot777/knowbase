# -*- coding: utf-8 -*-
"""搭一个沙箱 kb-web,给 tests/reading_probe.mjs 验「同一篇文章两个阅读入口一致」。

订阅正文按契约已经是纯文本(标签都剥掉了),所以正文里剩下的 `List<T>`、
`<script>…</script>` 全是文章作者写的示例文字。订阅面板的展开阅读用 textContent
显示,看得见;知识库阅读页(`/doc/…`)把 md 当 markdown 渲染,原来会把这些字当原始
HTML 吃掉 —— 前者少一个 `<T>`、后者整段正文消失(2026-10-08 第 3 轮审查实测)。
这个脚本把两边读的是同一篇这件事摆成真数据:

    .venv/bin/python tests/serve_reading.py --port 8879 --manifest /tmp/m.json

真走 `reg.put_items → sync.export → kb index` 一条链,落的是真 vault md、真
documents 行;没有网络请求、没有模型调用(沙箱里没有 secrets.json,
`translate.available()` 为假,所以条目不会被翻译闸门扣着、展开阅读照旧给出来)。
"""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import sys
from argparse import Namespace
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# 两条正文带字面标签的条目 + 一篇正文是真 markdown 的文章。后者是反向保险:
# 阅读页把原始 HTML 转义成文字之后,**正常的 markdown 必须一个字不差照旧渲染**。
LITERAL_ITEMS = [
    {'url': 'https://example.org/generics',
     'title': '泛型示例',
     'body': '新 API 使用 List<T>。\n\n第二段保持独立。'},
    # 整段只有一个脚本示例:净化之后正文一片空白,是审查报告里最严重的那一种。
    {'url': 'https://example.org/script',
     'title': '脚本字面示例',
     'body': '<script>alert(1)</script>'},
    # 作者只展示「脚本开头怎么写」:`<!--` 后面再出现 `<script` 会把页面内联脚本推进
    # HTML 的「双重转义」状态,于是页面末尾那个 `</script>` 不再结束脚本 —— 整段脚本
    # 一行都没跑,阅读页只剩标题,连后面那个普通段落一起没了(第 4 轮审查实测)。
    {'url': 'https://example.org/script-comment',
     'title': '脚本开头示例',
     'body': '示例开头写 <!-- <script>。\n\n这里是普通的第二段。'},
]

MARKDOWN_ARTICLE = """---
source: dr-gzh
title: 正常 markdown 的文章
account: 某公众号
published: {published}
url: https://example.org/markdown-doc
---

# 正常 markdown 的文章

一段正文,带一个[外链](https://example.org/link)和一处 `行内代码`。

- 第一项
- 第二项

```html
<script>alert('在围栏里')</script>
```

| 列一 | 列二 |
| --- | --- |
| 1 | 2 |
"""


# 同一个开头放进正常的 markdown 代码围栏里也照样复现 —— 故障发生在 markdown 解析
# **之前**(HTML 文档的内联 JSON 那一层),围栏保护不了它。
FENCED_ARTICLE = """---
source: dr-gzh
title: 围栏里的脚本开头
account: 某公众号
published: {published}
url: https://example.org/fenced-comment
---

示例展示开始部分:

```html
<!--
<script>
```

这是代码块之后的普通段落。
"""


def build(data: Path, vault: Path) -> dict:
    from kb import cli
    from kb.feeds import registry as reg
    from kb.feeds import sync

    published = (datetime.now() - timedelta(hours=1)).strftime('%Y-%m-%d %H:%M')
    source = reg.save_subscription('rss', {'name': 'Literal Markup Blog',
                                           'url': 'https://example.org/f.xml'})
    reg.put_items(source, [{**it, 'published': published} for it in LITERAL_ITEMS])
    # 这几条正文里一个汉字都没有的会被判成外文(registry.text_language),沙箱里没有
    # key、翻译整个跳过,所以这里按原文导出 —— 两个阅读入口读的是同一份原文。
    sync.export('rss', hold_untranslated=False)

    md = vault / 'source' / 'dr-gzh' / 'markdown-doc.md'
    md.parent.mkdir(parents=True, exist_ok=True)
    md.write_text(MARKDOWN_ARTICLE.format(published=published), encoding='utf-8')
    fenced = vault / 'source' / 'dr-gzh' / 'fenced-comment.md'
    fenced.write_text(FENCED_ARTICLE.format(published=published), encoding='utf-8')

    cli._index_pass(Namespace(reembed=False, no_vector=True))

    with reg.db() as c:
        rows = c.execute('SELECT url, path FROM feed_items ORDER BY url').fetchall()
    docs = {r['url']: r['path'] for r in rows}
    return {'generics': docs['https://example.org/generics'],
            'script': docs['https://example.org/script'],
            'scriptComment': docs['https://example.org/script-comment'],
            'markdown': md.relative_to(vault).as_posix(),
            'fenced': fenced.relative_to(vault).as_posix()}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=8879)
    parser.add_argument('--manifest', default='')
    args = parser.parse_args()
    sys.path.insert(0, str(ROOT))
    from tests.fixture_root import fixture_root
    # 临时库**优先由父进程拥有**:口径和理由都在 tests/fixture_root.py
    with contextlib.ExitStack() as stack:
        temp = fixture_root(stack, 'kb-reading-qa-')
        data, vault = Path(temp) / 'data', Path(temp) / 'vault'
        os.environ['KB_DATA_DIR'] = str(data)
        os.environ['KB_VAULT_ROOT'] = str(vault)
        vault.mkdir(parents=True, exist_ok=True)
        from kb import pipeline
        pipeline.start_scheduler = lambda: None
        manifest = build(data, vault)
        if args.manifest:
            Path(args.manifest).write_text(json.dumps(manifest, ensure_ascii=False),
                                           encoding='utf-8')
        import uvicorn
        from kb.server import app
        print('Isolated reading-page QA: no real data or external requests', flush=True)
        # 起了不关就是孤儿:什么时候自行退出,口径和理由都在 tests/fixture_life.py
        from tests import fixture_life
        uvicorn.run(fixture_life.guard(app), host='127.0.0.1', port=args.port, log_level='warning')


if __name__ == '__main__':
    main()
