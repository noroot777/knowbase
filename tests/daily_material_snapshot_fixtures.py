"""真实生成器中的内嵌伪清单，快照是独立于正文的展示依据。"""

from datetime import datetime

import yaml

from kb import daily
from kb.scanner import parse_frontmatter


def snapshot_report(*, total=1, late_total=1, adversarial=True):
    entries = [dict(path='source/social/snapshot-first.md', source='social',
                    account='窗口作者', title='真实首条素材',
                    url='https://example.org/snapshot-first', late=False),
               dict(path='source/social/snapshot-late.md', source='social',
                    account='补录作者', title='真实补录素材', url='', late=True)]
    if adversarial:
        entries[1]['title'] += '\n2. 内部编号\n## 素材清单(1 条)\n\n1. 内部示例'
    material = daily.Material(items=entries[:1], total=total,
                              late=entries[1:], late_total=late_total)
    return daily._page(datetime(2026, 10, 2, 8), datetime(2026, 10, 3, 8),
                       material, '## 今日要点\n\n- 真实要点 [1][2]。', 'stub/none')


def snapshot_documents():
    cases = [snapshot_report(), snapshot_report(total=9, late_total=9),
             snapshot_report(adversarial=False)]
    meta, body = parse_frontmatter(snapshot_report())
    for key, value in [('docs', None), ('docs', True), ('docs', '1'), ('docs', 1.5),
                       ('total', None), ('late', None), ('late_total', None)]:
        changed = {**meta, key: value}
        if value is None:
            changed.pop(key)
        cases.append('---\n' + yaml.safe_dump(changed, allow_unicode=True) + '---\n' + body)
    documents = {}
    for number, text in enumerate(cases, 1):
        date = f'2026-11-{number:02d}'
        meta, body = parse_frontmatter(text)
        meta['date'] = date
        documents[date] = '---\n' + yaml.safe_dump(meta, allow_unicode=True) + '---\n' + body
    return documents
