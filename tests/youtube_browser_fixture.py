import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from kb.feeds import registry as reg
from kb.youtube import collector, discovery, fulltext, login, media, notes, ranking, storage, toolchain, transcripts
from kb.collectors import FetchScope
from tests.youtube_translation_fixture import source_text


def translation_reply(prompt):
    plain = fulltext.SOURCE_START in prompt
    segments = [source_text(prompt)] if plain else json.loads(prompt.split('\n\n', 1)[1])['segments']
    for original, translated in (
            ('The AI agent generates code.', '作者演示智能体编程。'),
            ('Test every change.', '强调每次修改都要测试，不能仅凭生成结果认定正确。'),
            ('Verify the result rather than trusting model claims.', '作者要求核验结果，而不是相信模型的声称。'),
            ('The API budget is 64.', '接口预算为六十四。'),
            ('The planner checks every later step before using its result.', '规划器逐步核验后续结果。'),
            # 标题和字幕走同一个直译提示(`notes.build_translation`),没有这一条它就保留英文原题。
            ('AI agent coding tutorial', '智能体编程实操教程')):
        segments = [segment.replace(original, translated) for segment in segments]
    return (segments[0] if plain else
            '好的，以下是译文：' + json.dumps({'translations': segments}, ensure_ascii=False) + '（以上为译文）')


def install():
    sources = reg.subscriptions('youtube')
    source = next(source for source in sources if source['name'] == 'AI Search')
    status_sources = {}
    for selected, label in zip((item for item in sources if item['enabled'] and item['id'] != source['id']),
                               ('跳过一条测试频道', '跳过两条测试频道', '部分同步测试频道', '干净测试频道',
                                '零新增积压测试频道', '内容失败测试频道', '连接失败说明测试频道')):
        reg.save_subscription('youtube', {'name': label, 'group_name': '状态验证'}, selected['id'])
        status_sources[label] = selected['id']
    published = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    with reg.db() as connection:
        connection.executemany("UPDATE subscriptions SET last_error='上一轮连接失败',failures=3,retry_at=1 WHERE id=?",
                               [(status_sources[label],) for label in
                                ('部分同步测试频道', '零新增积压测试频道', '内容失败测试频道')] + [(source['id'],)])
        connection.execute("UPDATE subscriptions SET last_note='上轮只列到 3 条，覆盖未完成',"
                           "last_notice='上轮有 1 条视频无法读取' WHERE id=?",
                           (status_sources['连接失败说明测试频道'],))
    entries = [{'video_id': f'{number:011d}', 'url': 'https://www.youtube.com/watch?v=' + f'{number:011d}',
                'title': title, 'description': title, 'published': published, 'views': 10000 // number}
               for number, title in enumerate(('AI agent coding tutorial', 'AI model research benchmark',
                                               'AI workflow explained', 'Travel weekend'), 1)]

    def fetch(selected, *arguments):
        if selected['id'] == status_sources['连接失败说明测试频道']:
            raise RuntimeError('本轮频道列表无法读取')
        for number, label in enumerate(('零新增积压测试频道', '内容失败测试频道'), 991):
            if selected['id'] == status_sources[label]:
                entry = {**entries[0], 'video_id': f'{number:011d}', 'url': f'https://www.youtube.com/watch?v={number:011d}',
                         'views': 1}
                storage.discover(selected, [entry])
                storage.update(entry['video_id'], state='failed' if number == 992 else 'deferred',
                               last_error='该视频内容仍待重试' if number == 992 else '', retry_at=10**12)
                return []
        if selected['id'] == source['id']:
            selected['discovery_unavailable'] = [
                {'video_id': f'{number:011d}', 'reason': '离线夹具：单片无法读取'} for number in (981, 982)]
        for count, label in enumerate(('跳过一条测试频道', '跳过两条测试频道'), 1):
            if selected['id'] == status_sources[label]:
                selected['discovery_unavailable'] = [
                    {'video_id': str(number).zfill(11), 'reason': '离线夹具：单片无法读取'}
                    for number in range(count)]
        if selected['id'] == status_sources['部分同步测试频道']:
            arguments[-1].append('只列到 7 条就中断了：频道预算用尽')
            return [{**entries[0], 'published': (datetime.now(timezone.utc) - timedelta(days=40)).isoformat()}]
        return entries if selected['id'] == source['id'] else []

    def reply(prompt, *arguments):
        if prompt.startswith('将以下字幕'):
            return translation_reply(prompt)
        if '只输出 JSON' in prompt:
            return '以下是笔记：' + json.dumps({'title': '智能体编程：先验证再采用',
                               'summary': '作者展示智能体编程方法，并强调测试与人工核验的重要性。',
                               'body': '## 方法与限制\n生成代码后运行测试；模型声称正确不等于结果经过验证。'}, ensure_ascii=False) + '（以上为笔记）'
        return '作者演示智能体编程，指出必须运行测试并核验生成结果。'

    # ── 登录态与工具那一块:钉成一个「配好了、但那个浏览器里没登录」的确定局面 ──
    # 真去读本机 cookie、连一次 YouTube、问一次 PyPI,这三件事在测试里既慢又不确定。
    # 打桩的是**那三道边界**(命令在不在、真连一次的结论、本地/上游版本),面板自己
    # 的渲染和分档照旧跑真代码。两步分工本身由 tests/test_youtube_login.py 钉。
    toolchain.command = lambda: ['/fixture/bin/yt-dlp']
    toolchain.local = lambda: {'installed': '2026.06.09', 'origin': 'bundled'}
    toolchain.status = lambda deadline=None: {'installed': '2026.06.09', 'origin': 'bundled',
                                              'latest': '2026.10.26', 'outdated': True,
                                              'latest_error': ''}
    login.installed = lambda spec: login.browser_of(spec) in ('chrome', 'firefox')
    login.check = lambda settings, deadline=None: {
        'ready': False, 'code': 'not_logged_in', 'browser': 'chrome', 'checked': True,
        'message': login.describe('not_logged_in', 'chrome'),
        'detail': 'ERROR: Sign in to confirm you are not a bot. Use --cookies-from-browser'}

    discovery.fetch = fetch
    collector.DISCOVERY_SPACING = 0
    # 第 2 条报首映未开始:准入那一道把它延后重查,AI Search 因此留着一条真实的处理积压
    # (条数预算拆掉之后,「本轮做不完的队尾」得靠这类延后来造)。
    media.probe = lambda row, *arguments, **keywords: {
        'duration': 600, 'language': 'en',
        **({'live_status': 'is_upcoming'} if row['video_id'] == entries[1]['video_id'] else {})}
    narrative = ('The AI agent generates code. Test every change. ' * 6 +
                 'Verify the result rather than trusting model claims. The API budget is 64.')
    commands = '. '.join(['npm install --save-dev vitest && npx vitest run --coverage'] * 12)
    caption_text = narrative + '\n\n' + commands + '.\n' + (
        'The planner checks every later step before using its result. ' * 6)
    segments = transcripts.parse(json.dumps({'segments': [{'start': 0, 'end': 600, 'text': caption_text}]}), '.json')
    media.obtain = lambda *arguments: {'origin': 'uploaded_captions', 'language': 'en', 'segments': segments}
    notes.model = lambda stage: SimpleNamespace(provider='fixture', model_id='fixture')
    notes.call = reply
    storage.discover(source, entries)
    ranking.score(storage.rows(), reg.get_settings('youtube')['youtube'], FetchScope())
