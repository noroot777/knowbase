"""简洁素材清单的隔离 kb-web，不启动调度，不读取生产资料。"""

import argparse
import contextlib
import os
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests.fixture_root import fixture_root


def build(vault):
    from kb import daily, store

    store.connect().close()
    entries = [
        ('Frontier Slides，超快 ;)', 'social', 'Amjad Masad'),
        ('Grok Bot 套餐不够用怎么办？\n\n让 Grok Bot 安装 Pi Agent。\n\n'
         '1. 负责调度\n2. 把执行交给其他模型', 'social', '范凯说AI'),
        ('让多个 AI 一起查资料，这个开源项目有点东西', 'dr-gzh', 'GitHub科技'),
        ('用持久记忆延续开发上下文\n\n这里是很长的技术说明。', 'rss', 'speckx'),
        ('节目幕后：把零散素材整合成一次讨论\n\n幕后正文和人物提及。', 'social', 'swyx'),
        ('关于 OpenAI 模型定价与使用量的观点\n\n这里是讨论的具体背景。', 'social', 'Bindu Reddy'),
        ('远程面试中识别 AI 换脸的一则经验', 'social', '宝玉'),
        ('Compute Trading：工程与算力交易', 'social', 'swyx'),
        ('Anthropic 的 AI 工作方式与公众预期报告\n\n长段落不该铺在清单里。', 'social', '宝玉'),
        ('Claude Code 提速：更短的首字等待\n\n1. 本地会话\n2. 云端会话', 'social', '耳朵'),
        ('从收集到整理：搭建 AI 协作流程', 'dr-gzh', '开源观察'),
        ('fx.sh 与持久性：工具中的状态管理', 'rss', '工程笔记'),
    ]
    items = [dict(path=f'source/{number}.md', title=title, source=source, account=author,
                  url='' if number == 3 else f'https://example.org/material-{number}', late=False)
             for number, (title, source, author) in enumerate(entries)]
    text = daily._page(datetime(2026, 10, 16, 8), datetime(2026, 10, 17, 8),
                       daily.Material(items=items, total=15),
                       '## 今日要点\n\n- **Agent 分工**：调度与执行可以分别承担 [2]。\n'
                       '- **开发体验**：持久记忆与响应速度值得关注 [4][10]。', 'stub/none')
    root = vault / 'daily'
    root.mkdir(parents=True)
    (root / '2026-10-17.md').write_text(text, encoding='utf-8')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=8876)
    args = parser.parse_args()
    with contextlib.ExitStack() as stack:
        root = Path(fixture_root(stack, 'kb-material-qa-'))
        vault = root / 'vault'
        os.environ['KB_DATA_DIR'] = str(root / 'data')
        os.environ['KB_VAULT_ROOT'] = str(vault)
        from kb import pipeline
        pipeline.start_scheduler = lambda: None
        build(vault)
        import uvicorn
        from kb.server import app
        uvicorn.run(app, host='127.0.0.1', port=args.port, log_level='warning')


if __name__ == '__main__':
    main()
