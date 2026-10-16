"""Disposable full-app browser fixture. Never touches the real vault/database/network.

Run: .venv/bin/python tests/serve_sources.py --port 8879
The pipeline, export and FTS index are real; remote feed and vector model are fixtures.
"""
import argparse
import contextlib
import os
import sys
from pathlib import Path


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--port',type=int,default=8879)
    parser.add_argument('--layout-fixture',action='store_true')
    parser.add_argument('--grab-fixture',action='store_true')
    args=parser.parse_args()
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
    from tests.fixture_root import fixture_root
    # 临时库**优先由父进程拥有**:口径和理由都在 tests/fixture_root.py
    with contextlib.ExitStack() as stack:
        temp = fixture_root(stack, 'kb-source-qa-')
        os.environ['KB_DATA_DIR']=str(Path(temp)/'data')
        os.environ['KB_VAULT_ROOT']=str(Path(temp)/'vault')
        from kb import pipeline, embedder
        from kb.feeds import sync
        from kb.feeds.fetching import FeedError
        from kb.feeds import registry as reg
        if args.layout_fixture:
            from tests.subs_layout_fixture import populate
            populate()
        pipeline.start_scheduler=lambda: None
        embedder.embed_pending_chunks=lambda *a,**k: 0

        def fixture(source, settings, **kwargs):
            if 'example.org' not in source['url']:
                raise FeedError('浏览器测试环境仅允许 example.org 验证源，不访问真实订阅')
            # 发布时间跟着今天走:新订阅只收最近 HISTORY_DAYS 天(sync._within_history),
            # 写死日期的样例过几天就被历史下界挡掉,单源同步「贯通」那条探针白等。
            from datetime import datetime
            return ([dict(title='验证样例：Agent 知识工作流',body='这是一条用于验证同步与检索的样例。\n\n阅读、整理、建立 notebook 索引。',
                          url='https://example.org/qa-entry',published=datetime.now().strftime('%Y-%m-%d %H:%M'),
                          author='测试作者',content_kind='content')],
                    dict(etag='fixture-v1',modified='',resolved_url=source['url']))
        sync.fetch_source=fixture
        if args.grab_fixture:
            # 手动收录那条路**自己发 HTTP**(不走 sync.fetch_source),所以要另外
            # 打一桩:探针不许连外网(口径同 CLAUDE.md「测试探针起无头浏览器」
            # 那一节 —— 不装 playwright、不连网)。桩只换掉「从网上取回那一页」
            # 这一步,正文提取、入库、导出、索引全是真的。
            from kb.feeds import grab
            from kb.feeds.fetching import Response
            page=('<html><head><meta charset="utf-8">'
                  '<meta property="og:title" content="验证样例：把链接存进知识库">'
                  '<meta name="author" content="样例作者">'
                  '<meta property="article:published_time" content="2026-10-15T10:02:37Z">'
                  '</head><body><nav>导航</nav><article>'
                  # 段落要够长:`readable.MIN_CHARS` 是 400,比它短的一律算
                  # 「没找到正文」—— 这条路**正是**抓不到就不入库那一档。
                  + ('<p>这是一篇用于验证手动收录的样例正文，它要足够长，'
                     '才过得了正文提取器那道最小长度的门槛，不然这一页会被'
                     '如实判成「没找到正文」。</p>') * 12
                  + '</article></body></html>')
            def grab_fixture(url, headers=None, **kwargs):
                if 'example.org' not in url:
                    raise FeedError('浏览器测试环境仅允许 example.org 验证源，不访问真实地址')
                return Response(200,url,page.encode('utf-8'),
                                {'Content-Type':'text/html; charset=utf-8'})
            grab.request=grab_fixture
        # Keep source status checks in this sandbox independent of the user's WeChat process.
        original_status=pipeline.source_status
        pipeline.source_status=lambda channel=None: (original_status(channel) if channel in reg.KINDS else
            dict(online=False,ready=False,label='公众号',channel=channel or 'wechat_local',
                 channel_label='公众号',message='隔离测试环境',hint='',range_hint=''))
        pipeline.pending_counts=lambda *a,**k: {}
        from kb.server import app
        import uvicorn
        print('Isolated source browser QA: no real data or external requests',flush=True)
        uvicorn.run(app,host='127.0.0.1',port=args.port,log_level='warning')


if __name__=='__main__':main()
