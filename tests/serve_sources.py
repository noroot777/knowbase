"""Disposable full-app browser fixture. Never touches the real vault/database/network.

Run: .venv/bin/python tests/serve_sources.py --port 8879
The pipeline, export and FTS index are real; remote feed and vector model are fixtures.
"""
import argparse
import os
import sys
import tempfile
from pathlib import Path


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--port',type=int,default=8879)
    args=parser.parse_args()
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
    with tempfile.TemporaryDirectory(prefix='kb-source-qa-') as temp:
        os.environ['KB_DATA_DIR']=str(Path(temp)/'data')
        os.environ['KB_VAULT_ROOT']=str(Path(temp)/'vault')
        from kb import pipeline, embedder
        from kb.feeds import sync
        from kb.feeds.fetching import FeedError
        from kb.feeds import registry as reg
        pipeline.start_scheduler=lambda: None
        embedder.embed_pending_chunks=lambda *a,**k: 0

        def fixture(source, settings, **kwargs):
            if 'example.org' not in source['url']:
                raise FeedError('浏览器测试环境仅允许 example.org 验证源，不访问真实订阅')
            return ([dict(title='验证样例：Agent 知识工作流',body='这是一条用于验证同步与检索的样例。\n\n阅读、整理、建立 notebook 索引。',
                          url='https://example.org/qa-entry',published='2026-09-28 09:30',
                          author='测试作者',content_kind='content')],
                    dict(etag='fixture-v1',modified='',resolved_url=source['url']))
        sync.fetch_source=fixture
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
