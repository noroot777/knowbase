# -*- coding: utf-8 -*-
"""feedgrab 这条通道和那个外部程序之间的契约:产物写在哪里、调的是哪一个可执行文件。

这里一个 `harvest` / `_read_output` 都不打桩 —— 打桩成"成功"正好绕开唯一要验的
那件事。被替掉的只有最外面那个**外部程序**:一个真能跑起来的假 feedgrab,按上游
0.26.2 的保存规则落盘(`utils/storage.py` 的 `save_to_markdown`:
`OBSIDIAN_VAULT or OUTPUT_DIR` → 平台目录 `X` → category 逐段;账号推文的
category 是 `status_author/<显示名>`,见 `fetchers/twitter_user_tweets.py`)。
它之后的每一步都是真的:子进程调用、环境白名单、前言解析、归属过滤、整批预取、
`fetch_source` 分派、`sync.collect` 的存盘事务,一路到 feeds.db 和 vault 里的 md。

登录提示那一套也在这里:它问的是同一件事的另一半 —— 我们让用户去跑的,得是
采集时真会调的那一个程序。

为什么单开一个文件:`test_x_collectors.py` 已经 584 行、`test_x_channels.py` 610 行,
这一套(含那个假 CLI)塞进任何一个都越过仓库那条 700 行上限。

跑:  python -m unittest tests.test_x_feedgrab_output -v
"""
import json
import shlex
import subprocess
import sys
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from kb.collectors import FetchScope
from kb.feeds import registry as reg, sync, x_channels
from tests.test_x_channels import ChannelBase

TODAY = datetime.now(ZoneInfo('Asia/Shanghai')).date().isoformat()

# 假 feedgrab 的全部职责:照上游的规则把 PLAN 里那几条推文写到 OUTPUT_DIR 下。
_CLI_SRC = '''#!@@PY@@
# -*- coding: utf-8 -*-
import json
import os
import pathlib
import sys

PLAN = json.loads(pathlib.Path(@@PLAN@@).read_text(encoding='utf-8'))


def markdown(tweet, display):
    """照抄上游 `_format_markdown` 的 Twitter 形状(真 YAML 前言 + 正文)。"""
    lines = ['---',
             'title: "%s"' % tweet['title'],
             'source: "%s"' % tweet['url'],
             'author:',
             '  - "%s"' % display,
             'author_name: "%s"' % display,
             'published: %s' % tweet['published'],
             'created: %s' % tweet['published'],
             'tweet_type: "status"', 'tweet_count: 1', 'has_thread: false',
             'likes: 0', 'retweets: 0', 'replies: 0',
             'item_id: %s' % tweet['url'].rsplit('/', 1)[-1],
             '---', '']
    return '\\n'.join(lines) + '\\n' + tweet['body'] + '\\n'


def main():
    if len(sys.argv) < 2 or sys.argv[1] in ('login', 'doctor'):
        return 0                      # 登录/体检这两条这里不关心
    handle = sys.argv[1].rstrip('/').rsplit('/', 1)[-1]
    spec = PLAN.get(handle.lower())
    if spec is None:
        sys.stderr.write('@%s 本机没有这个账号\\n' % handle)
        return 1
    base = pathlib.Path(os.environ.get('OBSIDIAN_VAULT')
                        or os.environ['OUTPUT_DIR']) / 'X'
    # 上游自己的去重索引和推文躺在同一棵树里(不是 .md)。
    (base / 'index').mkdir(parents=True, exist_ok=True)
    (base / 'index' / 'item_id_url.json').write_text('{}', encoding='utf-8')
    for tweet in spec['tweets']:
        folder = base
        category = tweet.get('category', 'status_author/' + spec['display'])
        for part in [p for p in category.split('/') if p]:
            folder = folder / part
        folder.mkdir(parents=True, exist_ok=True)
        name = '%s_%s：%s.md' % (spec['display'], tweet['published'], tweet['title'])
        (folder / name).write_text(markdown(tweet, spec['display']), encoding='utf-8')
    return 0


sys.exit(main())
'''


def tweet(id_='10001', handle='Example', title='一条真的动态',
          body='这条推文落在 status_author 子目录里。', **extra):
    return {'url': f'https://x.com/{handle}/status/{id_}', 'title': title,
            'body': body, 'published': TODAY} | extra


class FeedgrabRound(ChannelBase):
    """真跑一轮 feedgrab 通道的摆盘:只替掉那个外部程序本身。"""

    def setUp(self):
        super().setUp()
        # 登录态的便宜判据(`readiness`):sessions 目录里有过 twitter 的文件。
        # 不摆上的话整轮会被记成"待配置"、一条订阅都不取。
        sessions = x_channels.FEEDGRAB_HOME / 'sessions'
        sessions.mkdir(parents=True, exist_ok=True)
        (sessions / 'twitter_cookies.json').write_text('{}')

    def cli(self, **plan) -> str:
        """落一个真能执行的假 feedgrab,并把它配成当前通道的可执行文件。"""
        plan_path = self.root / 'plan.json'
        plan_path.write_text(json.dumps(plan, ensure_ascii=False), encoding='utf-8')
        exe = self.root / 'fake-feedgrab'
        exe.write_text(_CLI_SRC.replace('@@PY@@', sys.executable)
                               .replace('@@PLAN@@', repr(str(plan_path))),
                       encoding='utf-8')
        exe.chmod(0o755)
        self.settings(x_channels.FEEDGRAB, x_feedgrab_bin=str(exe))
        return str(exe)

    def collect(self):
        """整轮采集,一行不打桩。"""
        return sync.collect('social', FetchScope(force=True))

    def rows(self, source_id):
        return reg.items('social', source_id)


class RealOutputTreeTests(FeedgrabRound):
    """**账号推文的子目录不许漏读。**

    原来 `_read_output` 按 `X/*.md` 只扫顶层,而上游把账号推文写在
    `X/status_author/<显示名>/` 下:抓到的推文一条也读不到,而 `harvest` 把这个
    空结果当成功存下来、随后把产物目录删掉 —— 面板上是「已同步 · 0 条」,知识库
    里什么都没有,和"这个号今天没发推"长得一模一样(2026-10-08 审查实测:
    `summary.added=0`、`item_count=0`,而产物确实落在
    `X/status_author/Example Person/` 下)。
    """

    def test_a_tweet_in_the_real_output_tree_reaches_the_knowledge_base(self):
        s = self.sub()
        self.cli(example={'display': 'Example Person', 'tweets': [tweet()]})
        summary = self.collect()
        self.assertEqual([], summary['errors'])
        self.assertEqual(1, summary['added'])
        got = self.rows(s['id'])
        self.assertEqual(1, got['total'])
        row = got['items'][0]
        self.assertEqual('https://x.com/Example/status/10001', row['url'])
        self.assertEqual('一条真的动态', row['title'])
        self.assertIn('status_author 子目录', row['body'])
        self.assertEqual('Example Person', row['author'])
        self.assertEqual(TODAY, row['published'][:10])
        # 水位推到确实见过的那一天(feedgrab 这条路只到天)。
        self.assertEqual(TODAY, x_channels.watermark(
            reg.subscription('social', s['id']), x_channels.FEEDGRAB))
        # 知识库这一侧:导出之后 vault 里真有这篇。
        sync.export('social')
        mds = list((sync.VAULT_ROOT).rglob('*.md'))
        self.assertEqual(1, len(mds), mds)
        self.assertIn('status_author 子目录', mds[0].read_text(encoding='utf-8'))

    def test_a_second_sync_of_the_same_tweet_adds_nothing(self):
        """重叠重抓是这条通道的常态(水位只到天),靠 `put_items` 按 URL 去重。"""
        s = self.sub()
        self.cli(example={'display': 'Example Person', 'tweets': [tweet()]})
        self.collect()
        again = self.collect()
        self.assertEqual([], again['errors'])
        self.assertEqual(0, again['added'])
        self.assertEqual(1, self.rows(s['id'])['total'])

    def test_a_display_name_we_could_never_have_guessed_is_read_too(self):
        """显示名只过了一遍文件名清理:中文、空格、emoji 全留着。

        所以这里只能整棵扫 —— 拼一个固定路径去取,换个号就又漏了。
        """
        s = self.sub()
        self.cli(example={'display': '量子位 · 大V 🤖', 'tweets': [tweet()]})
        self.assertEqual(1, self.collect()['added'])
        self.assertEqual(1, self.rows(s['id'])['total'])

    def test_a_top_level_artifact_is_still_read(self):
        """递归不是"改去读另一个固定路径":顶层那些产物(单条推文/线程)照旧收。"""
        s = self.sub()
        self.cli(example={'display': 'Example Person',
                          'tweets': [tweet(category='')]})
        self.assertEqual(1, self.collect()['added'])
        self.assertEqual(1, self.rows(s['id'])['total'])

    def test_recursing_does_not_loosen_the_attribution_filter(self):
        """引用/转推的产物会在树里留下别人的 status 链接,那不算这个订阅的内容。

        归属判据照旧是 `_entry` 里那句 handle 过滤,跟文件在哪一层没有关系。
        """
        s = self.sub()
        self.cli(example={'display': 'Example Person', 'tweets': [
            tweet(),
            tweet(id_='77', handle='Someone', title='别人家的推',
                  body='引用产物,不该记在这个订阅下。'),
        ]})
        self.assertEqual(1, self.collect()['added'])
        urls = [r['url'] for r in self.rows(s['id'])['items']]
        self.assertEqual(['https://x.com/Example/status/10001'], urls)

    def test_an_account_the_cli_rejects_is_reported_as_an_error(self):
        """反过来钉一边:真失败照旧是失败,不许被这条递归读法吞成"0 条成功"。"""
        s = self.sub('Missing')
        self.cli(example={'display': 'Example Person', 'tweets': [tweet()]})
        summary = self.collect()
        self.assertEqual(1, len(summary['errors']))
        self.assertEqual(0, self.rows(s['id'])['total'])


class LoginCommandTests(ChannelBase):
    """登录提示里的可执行文件,必须就是采集时会调的那一个。

    原来 `login_command` 只收通道名、写死裸 `feedgrab`:用户把它装在独立虚拟
    环境里、只在面板上填了绝对路径(`harvest`/`doctor` 都认这个设置)时,照页面
    抄下来那条命令在终端里是 `feedgrab: command not found` —— 首次配置从此走不
    通,而同一个页面上明明显示着那条可用的路径(2026-10-08 审查实测 `exit 127`)。
    保存设置不会往用户终端的 PATH 里加任何东西,所以这条提示得自己把路径带上。
    """

    def stub(self, home=None):
        """一个真能跑起来的假 feedgrab,**装在带空格的目录里**:它把拿到的
        `FEEDGRAB_DATA_DIR` 原样打出来,所以引号对不对一跑就知道。"""
        folder = (home or self.root) / 'my tools' / 'venv bin'
        folder.mkdir(parents=True, exist_ok=True)
        exe = folder / 'feedgrab'
        exe.write_text('#!/bin/sh\nprintf %s "$FEEDGRAB_DATA_DIR"\n')
        exe.chmod(0o755)
        return exe

    def command(self, **extra):
        self.settings(x_channels.FEEDGRAB, **extra)
        return self.client.get('/api/intake/social/x-channel').json()['login_command']

    def shell(self, command):
        """在一个**没有裸 feedgrab**的 shell 里真跑一遍 —— 审查那份证据就是
        `/bin/sh: feedgrab: command not found` + `exit 127`。"""
        return subprocess.run(['/bin/sh', '-c', command], env={'PATH': '/usr/bin:/bin'},
                              capture_output=True, text=True)

    def test_it_uses_the_configured_binary_when_the_bare_name_is_not_on_path(self):
        exe = self.stub()
        with patch('shutil.which', return_value=None):
            cmd = self.command(x_feedgrab_bin=str(exe))
        # 后果先断言:审查量到的就是这个退出码(127 / command not found)。
        proc = self.shell(cmd)
        self.assertEqual(0, proc.returncode, proc.stderr)
        self.assertIn(f'{shlex.quote(str(exe))} login twitter', cmd)

    def test_the_data_dir_survives_the_shell_even_with_spaces_in_it(self):
        home = self.root / 'kb data' / 'feedgrab home'
        with patch.object(x_channels, 'FEEDGRAB_HOME', home), \
                patch('shutil.which', return_value=None):
            cmd = self.command(x_feedgrab_bin=str(self.stub(home)))
        proc = self.shell(cmd)
        # 不 quote 的话 shell 会把带空格的路径拆成两段:这里比的是假 CLI
        # 真正收到的那个值,不是命令字符串长什么样。
        self.assertEqual(str(home / 'sessions'), proc.stdout)

    def test_nothing_configured_falls_back_to_path_and_then_to_the_bare_name(self):
        """没填路径时用 PATH 上解析出来的那一份(和 `find_binary` 同一个口径);
        两头都没有才退回裸命令 —— 那一档面板写的是"装好之后填它的完整路径"。"""
        exe = self.stub()
        with patch('shutil.which', return_value=str(exe)):
            self.assertIn(f'{shlex.quote(str(exe))} login twitter', self.command())
        with patch('shutil.which', return_value=None):
            self.assertTrue(self.command().endswith('feedgrab login twitter'))

    def test_a_configured_path_that_cannot_run_is_still_the_one_shown(self):
        """填错了要看得见自己填的是什么。「这个路径不可执行」由就绪判断另说一句,
        这里换成裸 `feedgrab` 只会让人以为自己没填。"""
        bad = self.root / 'not installed' / 'feedgrab'
        bad.parent.mkdir(parents=True)
        with patch('shutil.which', return_value=None):
            cmd = self.command(x_feedgrab_bin=str(bad))
        self.assertIn(shlex.quote(str(bad)), cmd)

    def test_the_readiness_hint_carries_the_same_command(self):
        """面板那条「还没有登录态」的横幅读的是 `readiness` 的第三个返回值,
        和端点那一份是两条路 —— 两处都得带上配好的可执行文件。"""
        exe = self.stub()
        settings = self.settings(x_channels.FEEDGRAB, x_feedgrab_bin=str(exe))
        with patch('shutil.which', return_value=None):
            ready, why, hint = x_channels.readiness(settings)
        self.assertFalse(ready)
        self.assertIn(shlex.quote(str(exe)), hint)


if __name__ == '__main__':
    unittest.main()
