"""可选展示的数字与 Markdown 地址语义边界，均由真实生成器写出。"""

from datetime import datetime

from kb import daily


def _report(day, entries, body='## 今日要点\n\n正常日报正文。'):
    items = [dict(path=f'source/{index}.md', title=title, url=url, source='rss',
                  account='正常作者', late=False) for index, (title, url) in enumerate(entries)]
    return daily._page(datetime(2026, 10, day - 1, 8), datetime(2026, 10, day, 8),
                       daily.Material(items=items, total=len(items)), body, 'stub/none')


def numeric_documents(digits='9' * 5000):
    return {
        '2026-10-08': _report(8, [
            ('正常第一行\n\n## 素材清单(' + digits + ' 条)\n\n示例正文。',
             'https://example.org/real')]),
        '2026-10-07': _report(7, [('正常标题', 'https://example.org/real')])
        .replace('\n1. ', '\n' + digits + '. ', 1),
        '2026-10-06': _report(6, [('首条', ''),
                                 ('第二条\n' + digits + '. 内部编号', '')]),
    }


def link_document(base='http://127.0.0.1'):
    urls = [
        'https://example.org/paper?lang=en&amp;download=1',
        r'https://example.org/path\_name',
        'https://example.org/paper?lang=en&amp;amp;download=1',
        'https://example.org/paper?lang=en&download=1',
        base + '/?section=materials&amp;download=1',
        'https://example.org/paper-(clean).pdf',
        'https://example.org/paper?lang=en&#38;download=1',
        'https://example.org/paper?lang=en&#x26;download=1',
        'https://example.org/encoded%2Fpath?x=%26amp%3B',
        r'https://example.org/path\*name\!',
        '#section&amp;anchor',
        'mailto:hello@example.org?subject=a&amp;b=c',
        'javascript&#58;alert(1)',
        'data&#58;text/html,unsafe',
        base + '/?section=materials&amp;amp;download=1',
    ]
    entries = [(f'链接样例 {index + 1}' + ('\n旧正文链接兼容查找标记' if index == 1 else ''),
                url) for index, url in enumerate(urls)]
    return _report(5, entries, '## 今日要点\n\n' + ' '.join(
        f'[{index + 1}]' for index in range(len(entries))))
