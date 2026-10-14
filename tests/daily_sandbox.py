# -*- coding: utf-8 -*-
"""搭一个自带素材的沙箱知识库,给 tests/daily_probe.mjs 的浏览器回归用。

真跑一遍 kb-web 才能验界面,而生产那份库既动不得、也会花模型钱。这个脚本把
一套最小可用的数据摆好(vault 里几篇带发布时间的文章 + 一条英文订阅 + 指向
本地桩的模型配置),跑完打印 kb-web 该带的环境变量。

    python3 tests/daily_sandbox.py /tmp/kb-daily-sandbox 8801

素材的发布时间故意跨窗口:一篇在"上一份日报的截止时刻"之前(用来验补录),
其余在窗口内。入库时间一律是现在——现实里也是这样,两个时间不是一回事。
"""
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta
from datetime import time as dt_time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def article(path: Path, *, title, account, source, published, body):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('---\n'
                    f'source: {source}\ntitle: {title}\naccount: {account}\n'
                    f'published: {published}\nurl: https://example.org/{path.stem}\n'
                    '---\n\n'
                    f'# {title}\n\n{body}\n', encoding='utf-8')


def main():
    base = Path(sys.argv[1] if len(sys.argv) > 1 else '/tmp/kb-daily-sandbox')
    stub_port = sys.argv[2] if len(sys.argv) > 2 else '8801'
    data, vault = base / 'data', base / 'vault'
    for d in (data, vault):
        d.mkdir(parents=True, exist_ok=True)

    now = datetime.now().replace(second=0, microsecond=0)
    recent = now - timedelta(hours=3)
    older = now - timedelta(days=4)          # 落在默认窗口之外
    article(vault / 'source' / 'dr-gzh' / 'a.md', source='dr-gzh', title='本周开源工具盘点',
            account='某公众号', published=recent.strftime('%Y-%m-%d %H:%M'),
            body='本周值得一看的几个命令行工具,都能直接装来用。')
    article(vault / 'source' / 'rss' / 'b.md', source='rss', title='一个新的智能体运行时',
            account='某博客', published=(recent - timedelta(hours=1)).strftime('%Y-%m-%d %H:%M'),
            body='这个运行时为编码智能体调度工具,支持并发与重试。')
    article(vault / 'source' / 'rss' / 'old.md', source='rss', title='四天前发布的旧闻',
            account='某博客', published=older.strftime('%Y-%m-%d %H:%M'),
            body='发布得更早、这一轮才抓进来的内容,应该进补录。')

    # 一份"昨天的日报":下一份的起点就接在它的截止时刻上,是个固定值。
    # 取 min(now-20h, 今天零点前一分钟):20 小时前落在今天的话(下午之后跑),
    # 这份日报的文件名就是今天,`next_window` 会把它当成"要被覆盖的那一份"
    # 跳过,起点转而回落到 24 小时前——探针那条"起点接上一份日报"当场变成
    # 一个随时间漂的值。
    mark = min(now - timedelta(hours=20),
               datetime.combine(now.date(), dt_time()) - timedelta(minutes=1))
    daily_dir = vault / 'daily'
    daily_dir.mkdir(parents=True, exist_ok=True)
    # 这一份还故意带上"有来源没同步成功":降级拿已有内容出报是允许的,但那一份
    # 日报不能跟"全都同步到了"长得一模一样(2026-09-30 审查:三个来源全部连接
    # 失败,页面和文件里一个字都没提)。给历史那一份带上,探针才能验"翻回去还
    # 看得见当时缺了什么"。
    (daily_dir / f'{mark.date().isoformat()}.md').write_text(
        '---\ntype: daily-report\n'
        f'date: {mark.date().isoformat()}\n'
        f"window_since: {(mark - timedelta(days=1)).strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"window_until: {mark.strftime('%Y-%m-%d %H:%M:%S')}\n"
        'docs: 3\ntotal: 3\nlate: 0\nsources: {公众号: 2, RSS: 1}\nmodel: stub-model\n'
        f"generated: {mark.strftime('%Y-%m-%d %H:%M')}\n"
        "sync_failed:\n- 'RSS：无法连接来源'\n---\n\n"
        f'# 日报 · {mark.date().isoformat()}\n\n'
        '> 覆盖上一份日报之后发布的内容(按发布时间,不是抓取时间)。\n\n'
        '> **本轮有 1 项没有完成**,以下内容只来自已经入库的素材:\n'
        '> - RSS：无法连接来源\n\n'
        '## 今日要点\n\n'
        '- 上一份日报 [1]\n'
        # 带平衡圆括号的裸 url:前端要是自己写正则解析清单那几行,会在 url 里
        # 第一个 `)` 处断掉,这一条的编号永远补不出链接(2026-09-30 审查实测,
        # 生产那份日报的 107 条素材里真有这么一条)。
        '- 带圆括号网址的来源 [2] 也要能点。\n'
        # 合并编号(落盘那条路认 `[1, 2]`/`[1，2]`/`[1、2]`),前端得同一套口径;
        # `[3]` 那条素材没有 url,补不出来就该留纯文本。
        '- 合并写法 [1、2] 同样算数,而 [3] 没有原文、保持纯文本。\n'
        # 行内代码里的数字方括号不是出处,一个字都不能动。
        '- 命令里的 `items[1]` 原样保留。\n\n'
        # 素材清单**故意按老写法写**(url 不套尖括号、正文里的编号还是纯文本):
        # 这就是加链接之前那些日报的形态。它们是花过模型钱的产物,不该为了加
        # 链接重新生成一份 —— 面板得照这份清单把编号补成链接(daily.js 的
        # `enhanceDailyBody`),两种 url 写法也都要认。
        '## 素材清单(3 条)\n\n'
        '1. [上一份日报里的那篇](https://example.org/a) — 公众号·某公众号\n'
        '2. [论文 [pdf]](https://example.org/paper-(clean).pdf) — RSS·某站\n'
        '3. 没有链接的那篇 — 公众号·某公众号\n',
        encoding='utf-8')

    # 上一轮运行的落盘状态:重启 kb-web 之后进程内没有通道结果,面板要从这里
    # 读回"哪些来源没同步成功"。
    (data / 'daily_last_run.json').write_text(json.dumps({
        'date': mark.date().isoformat(), 'since': '', 'until': '', 'sync': True,
        'trigger': 'manual', 'started_at': mark.strftime('%Y-%m-%d %H:%M:%S'),
        'finished_at': mark.strftime('%Y-%m-%d %H:%M:%S'), 'error': '',
        'channels': [{'channel': 'rss', 'label': 'RSS', 'status': 'error',
                      'detail': '无法连接来源'},
                     {'channel': 'dr-gzh', 'label': '公众号', 'status': 'ok',
                      'detail': '同步完成'}],
        'result': None, 'log': [],
    }, ensure_ascii=False, indent=1), encoding='utf-8')

    env = {**os.environ, 'KB_DATA_DIR': str(data), 'KB_VAULT_ROOT': str(vault)}
    subprocess.run(['uv', 'run', 'kb', 'index', '--no-vector'], cwd=ROOT, env=env, check=True)

    # 上报账本的启用分界线:钉在所有素材之前,所以一个章都不盖。
    # 不钉的话第一次打开面板会走"启用时一次性盖章",把 `old.md`(四天前发布)
    # 当成历史存量写掉 —— 那是**迁移那一刻**该有的行为,而这个沙箱要摆的是稳态
    # (账本一直开着、`old.md` 是真·迟到的内容),否则探针那条「补录 1 条」验的
    # 是另一回事。迁移那一刻的行为在 tests/test_daily_carried.py 里测。
    import sqlite3 as _sq
    sys.path.insert(0, str(ROOT))
    os.environ.update(KB_DATA_DIR=str(data), KB_VAULT_ROOT=str(vault))
    from kb import daily_carry
    with _sq.connect(data / 'kb.db') as _c:
        _c.execute('CREATE TABLE IF NOT EXISTS daily_state(key TEXT PRIMARY KEY,'
                   ' value TEXT NOT NULL)')
        # 版本标记要一起摆上:缺了它账本会认为"这是旧算法盖的章"、整张表重算,
        # 上面那条分界线就等于没钉(口径在 daily_carry.SEED_REV)。
        _c.executemany('INSERT OR REPLACE INTO daily_state(key,value) VALUES(?,?)', (
            ('carry_epoch', (now - timedelta(days=30)).strftime('%Y-%m-%d %H:%M:%S')),
            ('carry_seed_rev', daily_carry.SEED_REV)))

    # 模型:全部指到本地桩,不连外网、不花钱。
    (data / 'secrets.json').write_text(json.dumps({
        'openai': {'key': 'stub', 'base_url': f'http://127.0.0.1:{stub_port}/v1'},
        'stages': {'daily': {'provider': 'openai', 'model': 'stub-model'},
                   'translate': {'provider': 'openai', 'model': 'stub-model'}},
    }, ensure_ascii=False, indent=1), encoding='utf-8')
    (data / 'secrets.json').chmod(0o600)

    # 一条英文订阅条目:界面上该显示译文、能切回原文。
    from kb.feeds import registry as reg
    source = reg.save_subscription('rss', {'name': 'Some Blog', 'url': 'https://example.org/f.xml'})
    reg.put_items(source, [{'url': 'https://example.org/en-1', 'title': 'A new agent runtime',
                            'body': 'The runtime schedules tools for coding agents.',
                            'published': recent.strftime('%Y-%m-%d %H:%M')}])
    with reg.db() as c:
        c.execute("UPDATE feed_items SET title_zh=?, body_zh=?, translated_at=? WHERE lang='en'",
                  ('全新的智能体运行时', '这个运行时为编码智能体调度工具。', reg.now()))
    # 再来一条英文、故意不给译文:它还排在翻译队列里,所以既不该进 vault、
    # 也不该在面板上以英文冒充库里那一份。发布时间排在上面那条之后,免得它
    # 顶到列表第一行去(那几条检查认的是第一行)。
    reg.put_items(source, [{'url': 'https://example.org/en-2', 'title': 'A second English post',
                            'body': 'This one is still waiting for its translation.',
                            'published': (recent - timedelta(hours=2)).strftime('%Y-%m-%d %H:%M')}])
    # 第三条:老解析器留下的**半成品**(正文中文、标题空、完成时间非空、已导出)。
    # 它不在翻译队列里,页脚不能写「已译为中文」——2026-09-30 第 3 轮审查就是在
    # 真实页面上看到"英文标题 + 中文正文 + 已译为中文"三样同时出现的。
    reg.put_items(source, [{'url': 'https://example.org/en-3', 'title': 'A half translated post',
                            'body': 'Only the body of this one came back in Chinese.',
                            'published': (recent - timedelta(hours=3)).strftime('%Y-%m-%d %H:%M')}])
    with reg.db() as c:
        c.execute("UPDATE feed_items SET title_zh='', body_zh=?, translated_at=?,"
                  ' exported_hash=content_hash WHERE url=?',
                  ('这一条只有正文回来了中文。', reg.now(), 'https://example.org/en-3'))
    print(json.dumps({'KB_DATA_DIR': str(data), 'KB_VAULT_ROOT': str(vault)}, ensure_ascii=False))


if __name__ == '__main__':
    main()
