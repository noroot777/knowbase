"""带正常补录限量说明的真实生成器样本，不依赖用户资料。"""

from datetime import datetime

from kb import daily


def queue_report(*, docs=138, total=None, late=60, late_total=791,
                 title=None, account='测试作者', url='https://example.org/queue-'):
    entries = [dict(
        path=f'source/social/queue-{number}.md', source='social', account=account,
        title=title or f'Grok Bot 使用体验 {number}\n\n这是完整原文，不该平铺在清单里。\n\n'
                      '1. 先调度\n2. 再执行',
        url=f'{url}{number}' if url else '', late=number > docs,
    ) for number in range(1, docs + late + 1)]
    material = daily.Material(items=entries[:docs], total=docs if total is None else total,
                              late=entries[docs:], late_total=late_total)
    return daily._page(datetime(2026, 10, 2, 8), datetime(2026, 10, 3, 8), material,
                       f'## 今日要点\n\n- Agent 分工值得关注 [1][{len(entries)}]。', 'stub/none')
