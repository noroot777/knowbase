# -*- coding: utf-8 -*-
"""往 daily_sandbox 摆好的那个沙箱里再加「出处是译文」的那一档,
给 tests/daily_zh_probe.mjs 的浏览器回归用。

    python3 tests/daily_sandbox.py /tmp/kb-daily-zh-sandbox 8801
    python3 tests/daily_zh_sandbox.py /tmp/kb-daily-zh-sandbox

**单独一个脚本、不往 daily_sandbox.py 里加东西**(理由同 fulltext_sandbox):
那一份是 daily_probe / subs_probe 的共同依赖,它们有好几条检查认的是"素材清单
第 N 条""条目列表第一行",往里插东西会让别人的探针变红,而红的原因跟它们要验的
事情毫无关系。这里改的是**这个沙箱自己**那份历史日报的副本。

摆的是一对对照:
  ① `zh-of-en.md` —— frontmatter 带 `translated_from: en`,也就是入库前译过的
     那种。它的出处编号后面该多一个「译」。
  ② 已有的 `a.md` —— 中文公众号文章,库里有、但不是译文。它的编号后面
     **一个字都不该多**(反过来钉一边:有入口的那半对了,没入口的那半也得对)。
两条素材都写成**老写法**(裸 url、正文里编号是纯文本):那是加链接之前那些日报的
形态,编号由面板照清单补(`daily_refs.js`),顺带验了"补出来的链接也拿得到入口"。
"""
import os
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# 这一篇的原文地址(探针按它核对阅读器头上那个「原文」入口)。
ZH_URL = 'https://example.org/zh-of-en'
# 正文里那句暗号:探针 fetch 一次 /doc/ 页面,确认抽屉里要读的确实是中文那一份。
ZH_MARK = '这一篇在写进知识库之前就已经译好了'


def main():
    base = Path(sys.argv[1] if len(sys.argv) > 1 else '/tmp/kb-daily-zh-sandbox')
    data, vault = base / 'data', base / 'vault'
    published = (datetime.now().replace(second=0, microsecond=0)
                 - timedelta(hours=4)).strftime('%Y-%m-%d %H:%M')

    # ① 入库前译过的那一篇。`translated_from` 是 kb.feeds.sync.export 落盘时写的,
    #    也是 kb/daily_trans.py 唯一的判据;`original_title` 只在标题也跟着译了
    #    的时候才有,探针按它核对提示语。
    art = vault / 'source' / 'rss' / 'zh-of-en.md'
    art.parent.mkdir(parents=True, exist_ok=True)
    art.write_text(
        '---\n'
        'source: rss\n'
        'title: 一个从英文译过来的运行时发布\n'
        'account: 某英文站\n'
        f'published: {published}\n'
        f'url: {ZH_URL}\n'
        'lang: zh\n'
        'translated_from: en\n'
        'original_title: A runtime release, in English\n'
        '---\n\n'
        '# 一个从英文译过来的运行时发布\n\n'
        f'{ZH_MARK},所以 vault 里躺着的正是中文那一份。\n',
        encoding='utf-8')

    # 历史日报里加一条出处指向它。**定点替换,不整份重写**:那一份的别的形态
    # (带圆括号的裸 url、没有 url 的素材、行内代码里的数字方括号)是 daily_probe
    # 在验的东西,照搬一遍就会有两份会各自漂的副本。
    reports = sorted((vault / 'daily').glob('*.md'))
    if len(reports) != 1:
        raise SystemExit(f'预期沙箱里只有一份历史日报,实际 {len(reports)} 份')
    report = reports[0]
    text = report.read_text(encoding='utf-8')
    pairs = [
        ('- 命令里的 `items[1]` 原样保留。\n',
         '- 命令里的 `items[1]` 原样保留。\n'
         '- 本地有中文译文的那一条 [4],而 [1] 是库里的中文原文。\n'),
        ('## 素材清单(3 条)', '## 素材清单(4 条)'),
        ('3. 没有链接的那篇 — 公众号·某公众号\n',
         '3. 没有链接的那篇 — 公众号·某公众号\n'
         f'4. [一个从英文译过来的运行时发布]({ZH_URL}) — RSS·某英文站\n'),
    ]
    for old, new in pairs:
        if text.count(old) != 1:
            raise SystemExit(f'daily_sandbox 那份日报的写法变了,对不上:{old!r}')
        text = text.replace(old, new)
    report.write_text(text, encoding='utf-8')

    # 新加的那一篇要进 documents 才查得到(daily_trans 按 url 问的就是这张表)。
    env = {**os.environ, 'KB_DATA_DIR': str(data), 'KB_VAULT_ROOT': str(vault)}
    subprocess.run(['uv', 'run', '--offline', 'kb', 'index', '--no-vector'],
                   cwd=ROOT, env=env, check=True)
    print(f'report={report.stem} zh_url={ZH_URL}')


if __name__ == '__main__':
    main()
