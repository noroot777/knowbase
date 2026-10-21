"""收藏工作台的隔离验证服务：真实收藏 API、临时数据库，不读写个人收藏。"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
from datetime import datetime, timedelta
from pathlib import Path
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=18881)
    parser.add_argument('--detached', action='store_true')
    parser.add_argument('--legacy-detached', action='store_true')
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root))
    from tests.fixture_root import fixture_root

    with contextlib.ExitStack() as stack:
        temporary = Path(fixture_root(stack, 'kb-favorites-qa-'))
        os.environ['KB_DATA_DIR'] = str(temporary / 'data')
        os.environ['KB_VAULT_ROOT'] = str(temporary / 'vault')
        for name in ('OPENAI_API_KEY', 'ANTHROPIC_API_KEY'):
            os.environ.pop(name, None)
        from fastapi import FastAPI
        import uvicorn
        from kb import favbrain, store
        from kb.web import favorites_api, pages

        samples = [
            ('把网页变成干净的 Markdown', 'Defuddle', '去掉导航和广告，把文章留给你的知识库。',
             'npx defuddle https://example.org', '~5 分钟', 'try', '少数派', 'rss'),
            ('让你的 Agent 有一张地图', 'Understand Anything', '为代码仓库生成可交互的架构知识图谱。',
             'git clone https://example.org/understand-anything', '~10 分钟', 'try', '卡尔的 AI 沃茨', 'dr-gzh'),
            ('从零搭一条自己的信息管道', '个人信息管道', '从订阅、清洗到归档，搭建真正属于自己的信息流。',
             '', '~1 小时', 'read', '构建者周刊', 'rss'),
            ('好笔记，不是信息的搬运', '关于记录', '先写下你想用它解决的问题，再决定要不要收藏。',
             '', '', 'reference', '数字花园', 'dr-gzh'),
            ('用一段动画，把复杂概念讲清楚', 'Motion Canvas', '把代码变成清晰、可控的解释性动画。',
             'npm init @motion-canvas@latest', '~2 小时', 'try', 'Creative Coding', 'video'),
            ('本地优先工具的设计笔记', '', '离线可用、数据可迁移，比多一个功能更重要。',
             '', '', '', '独立开发者', 'rss'),
            ('收藏不是结束，而是下一次探索的起点', '小步实践', '降低开始的成本，每次只试一个想法。',
             '', '阅读约8分钟', 'read', '数字花园', 'dr-gzh'),
            ('给终端换一个更顺手的入口', 'Terminal Toolkit', '把每天重复的终端操作变成一个快捷指令。',
             'npx terminal-toolkit', '~3 分钟', 'try', '工具拾遗', 'rss'),
            ('一份可复用的代码审查清单', '代码审查清单', '从正确性、边界条件到可维护性的审查笔记。',
             '', '~20 分钟', 'read', '工程实践', 'rss'),
            ('轻量任务队列的另一种实现', 'Tiny Queue', '适合个人项目的小型任务队列。',
             'pip install tiny-queue', '~10 分钟', 'try', '工具拾遗', 'rss'),
        ]
        conn = store.connect()
        now = datetime.now()
        for index, (title, name, summary, cmd, eta, kind, account, source) in enumerate(samples, 1):
            path = f'source/{source}/favorite-{index}.md'
            published = (now - timedelta(days=index)).date().isoformat()
            status = 'inbox' if index <= 8 else ('done' if index == 9 else 'archived')
            created = (now - timedelta(days=index if index != 6 else 90)).isoformat(timespec='seconds')
            quote = summary if index == 4 else ''
            action = dict(name=name, what=summary, cmd=cmd, eta=eta, kind=kind) if name else None
            note = '试着接到每周的信息整理流程里。' if index == 1 else ''
            doc = temporary / 'vault' / path
            doc.parent.mkdir(parents=True, exist_ok=True)
            doc.write_text(f'---\ntitle: {title}\nsource: {source}\n---\n\n# {title}\n\n{summary}\n\n'
                           f'这是隔离验证文章，不会修改真实知识库。\n\n{cmd}\n', encoding='utf-8')
            conn.execute('INSERT INTO documents(path,source,title,account,published,url,summary)'
                         ' VALUES(?,?,?,?,?,?,?)',
                         (path, source, title, account, published, f'https://example.org/{index}', summary))
            conn.execute('INSERT INTO favorites(path,quote,note,status,action_json,created_at,updated_at)'
                         ' VALUES(?,?,?,?,?,?,?)',
                         (path, quote, note, status, json.dumps(action) if action else '', created, created))
        if args.detached:
            conn.execute("UPDATE documents SET url='' WHERE path='source/rss/favorite-6.md'")
            store.remove_documents(conn, [
                'source/rss/favorite-1.md', 'source/dr-gzh/favorite-4.md',
                'source/rss/favorite-6.md'])
        if args.legacy_detached:
            conn.execute("UPDATE favorites SET origin_source='',origin_account='',"
                         " origin_published='',origin_summary='' WHERE id=6")
        conn.commit()
        conn.close()
        triage = {'running': False, 'fresh': True, 'error': '', 'model': 'fixture:model',
                  'generated_at': now.isoformat(timespec='seconds'), 'data': {
                      'hero': {'id': 1, 'why': '只需要五分钟，就能用上你的第一份干净网页。'},
                      'quick': [{'id': 1}, {'id': 2}, {'id': 8}], 'deep': [{'id': 3}, {'id': 5}, {'id': 7}],
                      'quotes': [4], 'dup': [], 'cold': [{'id': 6, 'why': '收藏已久，可以暂存，想用时再找回来。'}]}}
        if args.legacy_detached:
            triage['data']['hero'] = {'id': 6, 'why': ''}
        favbrain.triage_status = lambda: triage
        favbrain.run_triage_async = lambda: True
        app = FastAPI()
        app.include_router(pages.router)
        app.include_router(favorites_api.router)

        @app.get('/api/{path:path}')
        def fixture(path: str):
            return {
                'stats': {'documents': 10, 'vectors': 0, 'entities': 0, 'extracted': 0},
                'facets': {'sources': [], 'accounts': [], 'topics': [], 'entities': []},
                'projects': {'tools': [], 'kinds': {}, 'total_entries': 0, 'topics': []},
                'models': {'models': [{'key': 'fixture:model', 'label': '隔离测试模型', 'enabled': True}]},
                'keys': {'keys': {}}, 'stages': {'stages': [], 'providers': []},
                'mcp/status': {'config': '{}', 'command': '', 'tools': [], 'cli_available': False},
                'conversations': {'conversations': []},
                'wechat/setup': {'state': 'unchecked', 'running': False, 'message': '隔离验证环境'},
                'daily/status': {'running': False, 'channels': []},
                'daily/reports': {'reports': []},
            }.get(path, {})

        print('Favorites QA: real API, temporary database, no external requests', flush=True)
        # 起了不关就是孤儿:什么时候自行退出,口径和理由都在 tests/fixture_life.py
        from tests import fixture_life
        uvicorn.run(fixture_life.guard(app), host='127.0.0.1', port=args.port, log_level='warning')


if __name__ == '__main__':
    main()
