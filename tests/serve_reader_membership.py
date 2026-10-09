"""Disposable full-app fixture for subscription reading membership regressions."""

import argparse
import os
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=8899)
    args = parser.parse_args()
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    with tempfile.TemporaryDirectory(prefix='kb-reader-membership-') as temp:
        os.environ['KB_DATA_DIR'] = str(Path(temp) / 'data')
        os.environ['KB_VAULT_ROOT'] = str(Path(temp) / 'vault')
        from kb import pipeline, embedder
        from kb.feeds import registry, sync
        from kb.feeds.fetching import FeedError

        pipeline.start_scheduler = lambda: None
        embedder.embed_pending_chunks = lambda *args, **kwargs: 0
        published = datetime.now() - timedelta(minutes=5)

        def entry(title, slug, newer=False):
            timestamp = published + timedelta(minutes=1) if newer else published
            return dict(title=title, body=(title + '。这是隔离验证正文。\n\n') * 35,
                        url='https://example.org/' + slug,
                        published=timestamp.strftime('%Y-%m-%d %H:%M:%S'),
                        author='边界验证作者', content_kind='content')

        for name, feed, title, slug in [
            ('边界旧订阅', 'review-old-feed', '旧网址的旧文章', 'review-old-article'),
            ('分页旧订阅', 'review-paged-old-feed', '分页旧网址的旧文章', 'review-paged-old-article'),
        ]:
            source = registry.save_subscription('rss', dict(name=name, url='https://example.org/' + feed))
            registry.put_items(source, [entry(title, slug)])

        fetches = {}

        def fixture(current, settings, **kwargs):
            if not current['url'].startswith('https://example.org/'):
                raise FeedError('隔离验证不允许真实订阅抓取')
            identity = (current['url'], current.get('feed_url', ''))
            fetches[identity] = fetches.get(identity, 0) + 1
            if current['url'].endswith('/review-new-feed'):
                articles = [entry('新网址的新文章', 'review-new-article')]
            elif current['url'].endswith('/review-paged-new-feed'):
                alternate = current.get('feed_url', '').endswith('/review-paged-other-feed')
                label = '替代 RSS 文章' if alternate else '分页新文章'
                slug = 'review-paged-other' if alternate else 'review-paged-new'
                articles = [entry(f'{label} {index:02}', f'{slug}-{index}') for index in range(1, 36)]
                if fetches[identity] > 1:
                    articles.append(entry(label + '：新增文章', slug + '-latest', newer=True))
            else:
                raise FeedError('没有为这个订阅配置隔离采集样例')
            return articles, dict(etag='membership-fixture', modified='', resolved_url=current['url'])

        sync.fetch_source = fixture
        original_status = pipeline.source_status
        pipeline.source_status = lambda channel=None: (original_status(channel)
            if channel in registry.KINDS else dict(online=False, ready=False, label='公众号',
                channel=channel or 'wechat_local', channel_label='公众号',
                message='隔离验证环境', hint='', range_hint=''))
        pipeline.pending_counts = lambda *args, **kwargs: {}
        from kb.server import app
        import uvicorn

        print('Reader membership isolated database: ' + temp, flush=True)
        uvicorn.run(app, host='127.0.0.1', port=args.port, log_level='warning')


if __name__ == '__main__':
    main()
