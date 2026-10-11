from datetime import datetime

from kb.feeds import registry as reg

THEMES = (
    ('把手机与电脑之间的阅读接起来', '工作流笔记（样例）',
     '分享一个阅读习惯：在手机上标记感兴趣的内容，回到电脑再集中阅读完整正文。'
     '重点不是把链接存得更多，而是让同一个阅读进度能在不同设备之间接续。'),
    ('先看问题，再决定要不要收藏', '阅读观察（样例）',
     '开始读一篇长文之前，先写下自己想回答的问题。读完再用自己的话总结结论，'
     '把值得继续追踪的出处保留下来，不必把每一篇文章都收进收藏夹。'),
    ('给订阅留一点空白，不必把所有更新都读完', '信息整理（样例）',
     '订阅列表应该帮助选择，而不是制造必须读完的压力。把来源分成每天关注和每周回顾，'
     '让标题、作者与摘要先完成筛选，真正有用的内容再进入详细阅读。'),
    ('同一条内容，原文和译文应该能随时切换', '产品记录（样例）',
     '译文帮助快速理解，原文保留作者自己的表达。阅读时把两种入口放在同一个位置，'
     '看到术语或者关键论述就切回原文核对，不要让语言切换打断阅读进度。'),
)


def populate():
    published = datetime.now().strftime('%Y-%m-%d %H:%M')
    for kind in ('social', 'rss', 'digest', 'web'):
        source = reg.save_subscription(kind, {
            'name': '长内容布局验证', 'url': f'https://example.org/{kind}.xml'})
        items = []
        for index in range(90):
            title, author, body = THEMES[index % len(THEMES)]
            items.append(dict(
                url=f'https://example.org/{kind}/{index}',
                title=f'第 {index + 1} 条：知识工作流实践——{title}',
                body=(body + '\n\n') * 80,
                author=author, published=published,
                content_kind='post' if kind == 'social' else 'content',
            ))
        reg.put_items(source, items)
