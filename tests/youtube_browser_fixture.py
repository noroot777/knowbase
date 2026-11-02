import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from kb.feeds import registry as reg
from kb.youtube import collector, discovery, login, media, notes, ranking, storage, toolchain
from kb.collectors import FetchScope


def install():
    sources = reg.subscriptions('youtube')
    source = next(source for source in sources if source['name'] == 'AI Search')
    status_sources = {}
    for selected, label in zip((item for item in sources if item['enabled'] and item['id'] != source['id']),
                               ('跳过一条测试频道', '跳过两条测试频道', '部分同步测试频道', '干净测试频道')):
        reg.save_subscription('youtube', {'name': label, 'group_name': '状态验证'}, selected['id'])
        status_sources[label] = selected['id']
    published = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    with reg.db() as connection:
        connection.execute("UPDATE subscriptions SET last_error='上一轮连接失败',failures=3,retry_at=1 WHERE id=?",
                           (status_sources['部分同步测试频道'],))
    entries = [{'video_id': f'{number:011d}', 'url': 'https://www.youtube.com/watch?v=' + f'{number:011d}',
                'title': title, 'description': title, 'published': published, 'views': 10000 // number}
               for number, title in enumerate(('AI agent coding tutorial', 'AI model research benchmark',
                                               'AI workflow explained', 'Travel weekend'), 1)]

    def fetch(selected, *arguments):
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
            segments = json.loads(prompt.split('\n\n', 1)[1])['segments']
            return '好的，以下是译文：' + json.dumps({'translations': [
                '作者演示智能体编程，强调每次修改都要测试，不能仅凭生成结果认定正确。接口预算为六十四。'
                if 'The AI agent' in segment or 'Verify the result' in segment else segment
                for segment in segments]}, ensure_ascii=False) + '（以上为译文）'
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
    media.probe = lambda *arguments, **keywords: {'duration': 600, 'language': 'en'}
    narrative = ('The AI agent generates code. Test every change. ' * 6 +
                 'Verify the result rather than trusting model claims. The API budget is 64.')
    commands = '\n'.join(['npm install --save-dev vitest && npx vitest run --coverage'] * 12)
    media.obtain = lambda *arguments: {'origin': 'uploaded_captions', 'language': 'en', 'segments': [
        {'start': 0, 'end': 600, 'text': narrative + '\n\n' + commands}]}
    notes.model = lambda stage: SimpleNamespace(provider='fixture', model_id='fixture')
    notes.call = reply
    storage.discover(source, entries)
    ranking.score(storage.rows(), reg.get_settings('youtube')['youtube'], FetchScope())
