from datetime import datetime

from kb.feeds import registry as reg
from kb.source_types import feed_types

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
    # **按 `feed_types()` 整批造,不写死一张清单** —— 新增一类来源时这张夹具
    # 自动把它也撑开,否则胶囊栏上会多出一个空视图,而「切到最后一类」那条用例
    # 要等的 `sourceReaderItem` 永远不出现(2026-10-16 新增「手动收录」时就是
    # 这么红的:探针按 End 落在空的那一类上,120 次轮询全部落空)。
    for kind, spec in feed_types().items():
        # 不可轮询的那一类(手动收录)的「订阅」是按站点自动建的收件桶,地址就是
        # 站点首页 —— 夹具照它的真实形态造,不要塞一个 feed 地址进去。
        url = ('https://example.org/' if not spec.pollable
               else f'https://example.org/{kind}.xml')
        source = reg.save_subscription(kind, {
            'name': '长内容布局验证' if spec.pollable else 'example.org', 'url': url})
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
