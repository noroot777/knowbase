from datetime import datetime

from kb.feeds import registry as reg


def populate():
    published = datetime.now().strftime('%Y-%m-%d %H:%M')
    for kind in ('social', 'rss', 'digest', 'web'):
        source = reg.save_subscription(kind, {
            'name': '长内容布局验证', 'url': f'https://example.org/{kind}.xml'})
        reg.put_items(source, [dict(
            url=f'https://example.org/{kind}/{index}',
            title=f'第 {index + 1} 条：知识工作流与跨设备阅读的实际使用记录',
            body='分享一个有趣的玩法：在手机上远程访问家里的电脑，'
                 '整理已订阅的内容，再通过知识库阅读完整正文。\n\n' * 80,
            author='知识工作流观察员', published=published,
            content_kind='post' if kind == 'social' else 'content',
        ) for index in range(90)])
