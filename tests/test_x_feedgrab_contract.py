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
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from kb.collectors import FetchScope
from kb.feeds import registry as reg, sync, x_channels, x_feedgrab
from tests.test_x_channels import ChannelBase

SHANGHAI = ZoneInfo('Asia/Shanghai')
# 样本日期跟着今天走:新订阅只收最近 `reg.HISTORY_DAYS` 天(见 `sync._within_history`),
# 写死一个日期的话过几天红的是一堆跟这条通道毫无关系的用例。
TODAY = datetime.now(SHANGHAI).date().isoformat()
YESTERDAY = (datetime.now(SHANGHAI).date() - timedelta(days=1)).isoformat()

# 假 feedgrab 的全部职责:照上游的规则把 PLAN 里那几条推文写到 OUTPUT_DIR 下,
# 并照它的样子落一份**批量记录**、打一行收尾摘要 —— 「这一批里哪几条没抓下来」
# 外部程序只在这两处说,退出码一律是 0(见 `x_feedgrab._incomplete`)。
_CLI_SRC = '''#!@@PY@@
# -*- coding: utf-8 -*-
import json
import os
import pathlib
import sys

PLAN = json.loads(pathlib.Path(@@PLAN@@).read_text(encoding='utf-8'))
# 上游 `utils/storage.py` 的 `_ILLEGAL_CHARS_RE`:这几个字符一律换成空格。
# **照抄它是这个假 CLI 的职责之一** —— `<` `>` 被它换掉,正是"推文标题带不进
# 那两个标记"这件事的依据(见 `x_feedgrab._PAGE_ABORT` 那段注释)。
BAD = set(chr(92) + '/:*?"<>|') | {chr(c) for c in range(32)}


def clean(text):
    return ''.join(' ' if ch in BAD else ch for ch in text)


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
    # 上游自己的去重索引和推文躺在同一棵树里:它是个 {item_id: url} 的字典,
    # 不是批量记录(`_batch_failures` 那道形状判据正好把它排除掉)。
    (base / 'index').mkdir(parents=True, exist_ok=True)
    (base / 'index' / 'item_id_url.json').write_text('{}', encoding='utf-8')
    # 上游在分页之前就把**作者昵称原样**打进日志,一个字都不清理
    # (`fetchers/twitter_user_tweets.py` 的 `logger.info(f"[UserTweets] 用户信息:
    # @{screen_name} ({display_name}), ID: {user_id}")`)。**照抄它是这个假 CLI
    # 的职责之一** —— 那是用户内容进到我们手里那份输出的第二条路,而它不带路径,
    # 所以"跳过回显产物目录的行"那一道救不了它(见 `x_feedgrab._PAGE_ABORT`)。
    sys.stderr.write('INFO [UserTweets] 用户: @%s\\n' % handle)
    sys.stderr.write('INFO [UserTweets] 用户信息: @%s (%s), ID: fixture-user\\n'
                     % (handle, spec['display']))
    # 紧接着那一行是过了清理的目录名(这条路和保存路径同一个清理规则)。
    sys.stderr.write('INFO [UserTweets] 保存目录: status_author/%s\\n'
                     % clean(spec['display']))
    # 上游按 `X_USER_TWEETS_SINCE` 把更早的推文整段丢掉(它那句 stop_pagination)。
    since = os.environ.get('X_USER_TWEETS_SINCE', '')
    # 翻页半途失败那一档:上游打一行 logger.error 就 break(`if not response`),
    # 这一页及其后面的条目**压根没拿到** —— 不会进 tweet_list、`failed` 一个
    # 都不加,所以批量记录和收尾摘要里一个字都看不出来。
    abort = spec.get('abort_page') or 0
    kept = [t for t in spec['tweets']
            if not (since and t['published'][:10] < since)
            and not (abort and t.get('page', 1) >= abort)]
    if abort:
        # 它走 `logger.error`,所以真输出里这一行带着级别(上游的 sink 是
        # `logger.add(sys.stderr, level=INFO)`,默认格式)。判据按**消息自己的
        # 形状**认,不按级别 —— 见 `x_feedgrab._PAGE_ABORT` 最后那两段。
        sys.stderr.write('ERROR [UserTweets] >>> 第 %d 页所有 1 个账号均失败 '
                         '<<< 已抓取 %d 条，停止分页\\n' % (abort, len(kept)))
    rows, fetched, failed = [], 0, 0
    for tweet in kept:
        bad = bool(tweet.get('fails'))
        rows.append({'url': tweet['url'], 'published': tweet['published'],
                     'status': 'failed' if bad else 'fetched',
                     'error': 'boom' if bad else ''})
        if bad:
            # 单条失败只记一笔日志就继续:整批不抛异常(上游那个 except 分支)。
            failed += 1
            sys.stderr.write('[UserTweets] [1/1] 失败: @%s - boom\\n' % handle)
            continue
        fetched += 1
        folder = base
        category = tweet.get('category', 'status_author/' + spec['display'])
        for part in [p for p in category.split('/') if p]:
            folder = folder / part
        folder.mkdir(parents=True, exist_ok=True)
        name = clean('%s_%s：%s' % (spec['display'], tweet['published'],
                                    tweet['title'])) + '.md'
        (folder / name).write_text(markdown(tweet, spec['display']), encoding='utf-8')
        # 上游每存一篇都打这一行(`storage.save_to_markdown` 的
        # `logger.info(f"Saved to Markdown: {path}")`),而路径里带着推文标题和
        # 显示名 —— 这是推文内容进到我们手里那份输出的唯一一条路。
        sys.stderr.write('INFO Saved to Markdown: %s\\n' % (folder / name))
    if spec.get('record', True):
        record = base / 'index' / ('status_%s_%s.json' % (handle, os.getpid()))
        record.write_text(json.dumps(
            {'fetched_at': '', 'screen_name': handle, 'since': since,
             'total': len(rows), 'tweets': rows}, ensure_ascii=False),
            encoding='utf-8')
    if spec.get('summary', True):
        sys.stdout.write('\\n\u2705 账号推文批量抓取完成\\n'
                         '总数: %d, 成功: %d, 跳过: 0, 失败: %d\\n'
                         '批量记录: index/status_...json\\n'
                         % (len(rows), fetched, failed))
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

    def water(self, source_id):
        return x_channels.watermark(reg.subscription('social', source_id),
                                    x_channels.FEEDGRAB)

    def start_from(self, days=4):
        """先摆一个旧水位,这样"推进了没有"才看得出来。"""
        s = self.sub()
        x_channels.save_watermark(s['id'], x_channels.FEEDGRAB, self.day(days))
        return s

    def day(self, back):
        return (datetime.now(SHANGHAI).date() - timedelta(days=back)).isoformat()


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


class PartialBatchTests(FeedgrabRound):
    """**退出码 0 不等于这一批抓全了。**

    上游逐条抓,单条失败只往批量记录里记一笔 `status: failed` 就继续
    (`fetchers/twitter_user_tweets.py` 的 except 分支),收尾把计数包成一句普通
    正文(`reader.py`),CLI 的账号主页分支 `print` 完直接 return —— 只有抛异常
    才 `sys.exit(1)`。所以"进程退出码为 0"证明不了这一批没漏。

    不认出这一档就是**静默丢数据**:水位一推到"这一轮抓到的最新那天",下一轮的
    `X_USER_TWEETS_SINCE` 就是它,上游按这个下界把更早的推文整段丢掉 —— 失败
    那几条比旧水位新、又比新水位老,从此任何一轮增量都选不到。审查实测:
    `added=1`、`errors=[]`、`partial=[]`、水位从 10-05 跳到 10-08,外部故障解除
    后再点同步 `added=0`,那条推文再也回不来(2026-10-08 第 2 轮审查)。
    """

    def plan(self, *, older_fails=True, **spec):
        """同一个号两条推文:更早那条按需失败,更新那条照旧成功。"""
        return {'example': {'display': 'Example Person', 'tweets': [
            tweet(id_='10000', title='更早那条', body='前一天的观点。',
                  published=YESTERDAY, fails=older_fails),
            tweet(id_='10001', title='更新那条', body='今天的观点。'),
        ]} | spec}

    def test_a_partial_batch_keeps_the_old_watermark_and_says_so(self):
        s = self.start_from()
        self.cli(**self.plan())
        summary = self.collect()
        # 后果先断言:水位一步都不许动 —— 动了那条失败的推文就永久选不到了。
        self.assertEqual(self.day(4), self.water(s['id']))
        # 抓到的照旧入库,这不是一次失败(不记 errors、不进退避)。
        self.assertEqual(1, summary['added'])
        self.assertEqual([], summary['errors'])
        # 但也不是一次完整的同步:面板上要看得见(「部分同步」那一档)。
        self.assertEqual(['Example'], [p['title'] for p in summary['partial']])
        self.assertIn('没抓下来', summary['partial'][0]['reason'])
        # 面板那条「部分同步」读的是订阅行上的 `last_note`(`sources.js` 的状态链
        # 在 `last_success` 之前先看它),所以这里连端点一起验 —— 只验库里有那一行
        # 的话,它发没发到浏览器还是没人管。
        page = self.client.get('/api/intake/social').json()
        rows = {row['name']: row for row in page['sources']}
        self.assertIn('没抓下来', rows['Example']['last_note'])
        self.assertEqual('', rows['Example']['last_error'])

    def test_the_tweet_that_failed_comes_back_once_the_failure_clears(self):
        """审查那份证据的反面:解除外部故障再同步一次,漏掉那条要补回来。

        靠的就是上一条留着的旧水位 —— 推进了的话下一轮的下界已经越过它。
        """
        s = self.start_from()
        self.cli(**self.plan())
        self.collect()
        self.cli(**self.plan(older_fails=False))      # 外部故障解除
        again = self.collect()
        self.assertEqual(1, again['added'])           # 补回更早那条
        self.assertEqual([], again['partial'])
        urls = sorted(r['url'] for r in self.rows(s['id'])['items'])
        self.assertEqual(['https://x.com/Example/status/10000',
                          'https://x.com/Example/status/10001'], urls)
        # 更新那条重叠重抓,但 `put_items` 按 URL 去重,一条都不会重复。
        self.assertEqual(2, self.rows(s['id'])['total'])
        # 这一轮读全了:水位跟到最新那天,提示也要清掉(否则留下过期的「部分同步」)。
        self.assertEqual(TODAY, self.water(s['id']))
        self.assertEqual('', reg.subscription('social', s['id'])['last_note'])

    def test_a_complete_batch_still_advances_the_watermark(self):
        """反过来钉一边:没有失败就该照常推进 —— 一律报「部分同步」的实现
        会把水位永久冻住,每一轮都重抓整个窗口,而面板上天天挂着假故障。"""
        s = self.start_from()
        self.cli(**self.plan(older_fails=False))
        summary = self.collect()
        self.assertEqual(TODAY, self.water(s['id']))
        self.assertEqual([], summary['partial'])
        self.assertEqual(2, summary['added'])

    def test_the_batch_record_alone_is_enough_to_see_the_gap(self):
        """摘要那行一个字都没打时,证据只剩上游落下的批量记录。

        顺带钉住另一件事:日志里那行「失败: @Example - boom」**不算**证据 ——
        收尾摘要那道判据要是写成裸 `失败:`,它会每一轮都误报,把水位永久冻住。
        """
        s = self.start_from()
        self.cli(**self.plan(summary=False))
        summary = self.collect()
        self.assertEqual(self.day(4), self.water(s['id']))
        self.assertEqual(1, len(summary['partial']))

    def test_the_summary_line_alone_is_enough_to_see_the_gap(self):
        """反过来:批量记录换了位置/改了名字时,证据只剩那行摘要。

        两道判据不是同一份实现的两种写法,所以各自单独成立。
        """
        s = self.start_from()
        self.cli(**self.plan(record=False))
        summary = self.collect()
        self.assertEqual(self.day(4), self.water(s['id']))
        self.assertEqual(1, len(summary['partial']))


class BatchRecordShapeTests(unittest.TestCase):
    """批量记录认形状不认路径,而且「跳过」不是「失败」。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)

    def write(self, name, payload):
        path = self.folder / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')

    def test_the_dedup_index_is_not_mistaken_for_a_batch_record(self):
        """`index/item_id_url.json` 是个 {item_id: url} 的字典,躺在同一棵树里。"""
        self.write('X/index/item_id_url.json', {'abc123': 'https://x.com/a/status/1'})
        self.assertEqual(0, x_feedgrab._batch_failures(self.folder))

    def test_skipped_is_not_a_failure_but_an_unknown_status_is(self):
        """`skipped` 是上游自己按去重/互动阈值跳的,那是它的决定、不是漏;
        没见过的状态一律当成漏 —— 漏报是静默丢数据,误报只多一次重叠重抓。"""
        self.write('X/index/status_a_1.json', {'tweets': [
            {'status': 'fetched'}, {'status': 'skipped'}]})
        self.assertEqual(0, x_feedgrab._batch_failures(self.folder))
        self.write('X/index/status_a_2.json', {'tweets': [{'status': 'wat'}]})
        self.assertEqual(1, x_feedgrab._batch_failures(self.folder))

    def test_both_writers_count_and_broken_files_are_ignored(self):
        """同一份记录有两个写入方,文件名前缀还不一样(`status_` / `api_status_`)
        —— 按名字取就漏掉一半。读不出来的文件不算证据,也不许把整轮带掉。"""
        self.write('X/index/status_a_1.json', {'tweets': [{'status': 'failed'}]})
        self.write('X/index/api_status_a_1.json', {'tweets': [{'status': 'failed'}]})
        (self.folder / 'X/index/broken.json').write_text('{not json', encoding='utf-8')
        self.assertEqual(2, x_feedgrab._batch_failures(self.folder))


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
