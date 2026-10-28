"""「问题与待办」那块面板的一次性全栈沙箱。不碰真实 vault / 库 / 网络。

    .venv/bin/python tests/serve_intake_diag.py --port 8893

管道、导出、索引都是真的,**但调度线程不起、向量不跑、订阅取数被打桩** ——
这块面板一个字节都不该靠联网才看得出样子。样例在 `tests/intake_diag_fixture.py`。
"""
import argparse
import contextlib
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
# 起了不关就是孤儿:什么时候自行退出,口径和理由都在 tests/fixture_life.py。
# **必须在模块加载时就 import** —— 所有权看门狗装在它的模块级,而摆盘那几秒
# 正是调用方最可能被取消的窗口。
from tests import fixture_life  # noqa: E402


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=8893)
    args = parser.parse_args()
    from tests.fixture_root import fixture_root
    # 临时库**优先由父进程拥有**:口径和理由都在 tests/fixture_root.py
    with contextlib.ExitStack() as stack:
        temp = fixture_root(stack, 'kb-intake-diag-qa-')
        os.environ['KB_DATA_DIR'] = str(Path(temp) / 'data')
        os.environ['KB_VAULT_ROOT'] = str(Path(temp) / 'vault')
        from kb import embedder, pipeline
        from kb.feeds import sync
        from kb.feeds.fetching import FeedError
        from kb.feeds import registry as reg
        from tests.intake_diag_fixture import populate

        # **在 import kb.server 之前**打掉调度线程:它一跳就可能真起一轮同步。
        pipeline.start_scheduler = lambda: None
        embedder.embed_pending_chunks = lambda *a, **k: 0

        def fixture(source, settings, **kwargs):
            raise FeedError('浏览器测试环境不访问真实订阅')
        sync.fetch_source = fixture
        original_status = pipeline.source_status
        pipeline.source_status = lambda channel=None: (
            original_status(channel) if channel in reg.KINDS else
            dict(online=False, ready=False, label='公众号', channel=channel or 'wechat_local',
                 channel_label='公众号', message='隔离测试环境', hint='', range_hint=''))
        pipeline.pending_counts = lambda *a, **k: {}
        populate()

        from kb.server import app
        import uvicorn
        fixture_life.announce('Isolated intake-care browser QA: no real data or external requests')
        uvicorn.run(fixture_life.guard(app), host='127.0.0.1', port=args.port,
                    log_level='warning')


if __name__ == '__main__':
    main()
