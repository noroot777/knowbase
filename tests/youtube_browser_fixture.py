import json
import re
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from kb.feeds import registry as reg
from kb.youtube import discovery, media, notes, ranking, storage
from kb.collectors import FetchScope


def install():
    sources = reg.subscriptions('youtube')
    source = next(source for source in sources if source['name'] == 'AI Search')
    published = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    entries = [{'video_id': f'{number:011d}', 'url': 'https://www.youtube.com/watch?v=' + f'{number:011d}',
                'title': title, 'description': title, 'published': published, 'views': 10000 // number}
               for number, title in enumerate(('AI agent coding tutorial', 'AI model research benchmark',
                                               'AI workflow explained', 'Travel weekend'), 1)]

    def fetch(selected, *arguments):
        return entries if selected['id'] == source['id'] else []

    def reply(prompt, *arguments):
        if prompt.startswith('将以下字幕'):
            return '\n\n'.join(marker + ' 作者演示智能体编程，强调每次修改都要测试，不能仅凭生成结果认定正确。'
                                for marker in re.findall(r'\[\d{2}:\d{2}:\d{2}\]', prompt))
        if '只输出 JSON' in prompt:
            return json.dumps({'title': '智能体编程：先验证再采用',
                               'summary': '作者展示智能体编程方法，并强调测试与人工核验的重要性。',
                               'body': '## 方法与限制\n生成代码后运行测试；模型声称正确不等于结果经过验证。'}, ensure_ascii=False)
        return '作者演示智能体编程，指出必须运行测试并核验生成结果。'

    discovery.fetch = fetch
    media.probe = lambda *arguments: {'duration': 600, 'language': 'en'}
    media.obtain = lambda *arguments: {'origin': 'uploaded_captions', 'language': 'en', 'segments': [
        {'start': 0, 'end': 300, 'text': 'The AI agent generates code. Test every change.'},
        {'start': 300, 'end': 600, 'text': 'Verify the result rather than trusting model claims.'}]}
    notes.model = lambda stage: SimpleNamespace(provider='fixture', model_id='fixture')
    notes.call = reply
    storage.discover(source, entries)
    ranking.score(storage.rows(), reg.get_settings('youtube')['youtube'], FetchScope())
