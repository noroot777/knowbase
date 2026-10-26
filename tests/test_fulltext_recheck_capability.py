"""存量复查:**射程不受来源能力约束,动作受**。仅在验收阶段执行。

被测的那件事分两半,而它们曾经被合成同一个判据:
- **扫描看得见** —— 不许 `body_from_link` 的来源(简报 / 大V)上「正文来自原文链接
  那一页」的条目,全库扫、`--kind`、`--source` 三种射程都要选中它。把 `_suspect`
  接到 `fulltext._eligible()` 上的后果实测过:三种射程一律 0 条、还以「没有需要复查
  的条目。」成功退出,而生产库里当时就躺着一条(TLDR AI 那条简讯,正文是别人家仓库
  的 README)—— **这一类的既有损坏从此没有任何工具够得着**。
- **写回不放回队列** —— 那一支只把备份里那份原文装回去,`fulltext_at` /
  `fulltext_error` / `fulltext_tries` 一个都不碰,也不拿「标题与链接」顶替。

**两条红各钉一边**:许可来源那边照旧换壳、放回队列(`..._return_to_a_real_refill_
queue`);许可来源上一篇正常的补抓成品不许被这一轮新射程选中(`..._is_left_alone`)。
"""
import os
import sqlite3
from dataclasses import replace
from unittest.mock import patch

from kb.feeds import fulltext, readable, registry as reg
from kb.source_types import SOURCE_TYPES, feed_types
from scripts import feed_fulltext_recheck as recheck
from tests.test_fulltext_capability import CapabilityCase
from tests.test_fulltext_failures import LEGAL_TEXT

#: 简报那条简讯**本来**那段摘要(形态取自生产库里 TLDR AI 的其余 230 条)。
ORIGINAL = ('OpenAI shipped a terminal client for its models. It reads a prompt from '
            'stdin, streams the answer back, and keeps the session in a local file.')
#: 一篇**逐字正常**的文章正文。生产库里那条受害简讯装的就是这个形态(别人家仓库的
#: README,9242 字)—— `boilerplate_reason` 对它返回空串,所以光靠文字判据永远选不中。
FOREIGN_ARTICLE = (
    'The client talks to the responses endpoint and keeps a transcript on disk. '
    'Install it with one command, then point it at any model you have access to. '
    'Every run appends to the same log, so a crash resumes where it stopped.\n'
    'Configuration lives in a single file. The defaults are good enough for most '
    'sessions, and each flag has a matching environment variable.')


class RecheckCapabilityTests(CapabilityCase):
    def wrong_article(self, kind, body=LEGAL_TEXT):
        """一条「正文被原文链接那一页换掉了」的条目:已补抓、已翻译、已导出。"""
        source, key = self.legacy_item(kind)
        with reg.db() as conn:
            conn.execute(
                "UPDATE feed_items SET body=?,content_kind='content',content_hash=?,"
                "fulltext_at='2026-10-26T09:00:00+08:00',title_zh='原标题译文',"
                "body_zh='原正文译文',translated_at='2026-10-26T09:01:00+08:00',"
                'exported_hash=content_hash WHERE key=?',
                (body, fulltext.body_hash(body), key),
            )
        return source, key

    def backup(self, key, body=ORIGINAL, content_kind='summary', tag=''):
        """一份只够 `_from_backup` 读的备份库。返回它的路径列表。

        `content_kind=None` 建一份**没有这一列**的老备份(schema 变过那种)。
        """
        path = self.root / f'backup-{key[:8]}{tag}' / 'feeds.db'
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path)
        with conn:
            if content_kind is None:
                conn.execute('CREATE TABLE feed_items(key TEXT PRIMARY KEY,body TEXT)')
                conn.execute('INSERT INTO feed_items VALUES(?,?)', (key, body))
            else:
                conn.execute('CREATE TABLE feed_items(key TEXT PRIMARY KEY,body TEXT,'
                             'content_kind TEXT)')
                conn.execute('INSERT INTO feed_items VALUES(?,?,?)',
                             (key, body, content_kind))
        conn.close()
        return [path]

    def row(self, key):
        with reg.db() as conn:
            return dict(conn.execute('SELECT * FROM feed_items WHERE key=?',
                                     (key,)).fetchone())

    def plan(self, backups=(), **scope):
        return recheck._plan(recheck._suspect(**scope), list(backups))

    def native(self, kind):
        return 'post' if kind == 'social' else 'summary'

    def forget(self, kind, source, key):
        """连条目一起清掉 —— **删订阅是刻意保留已收录条目的**(`registry`),而射程 ②
        不问订阅在不在,留着它下一个 subTest 的全库扫就多选中一条。"""
        reg.delete_subscription(kind, source['id'])
        with reg.db() as conn:
            conn.execute('DELETE FROM feed_items WHERE key=?', (key,))

    def test_disallowed_sources_are_visible_in_every_scope(self):
        for kind, spec in feed_types().items():
            if spec.body_from_link:
                continue
            with self.subTest(kind=kind):
                source, key = self.wrong_article(kind)
                for scope in ({}, {'kind': kind}, {'source': source['id']},
                              {'kind': kind, 'source': source['id']}):
                    self.assertEqual([key], [row['key'] for row in self.plan(**scope)], scope)
                self.forget(kind, source, key)

    def test_a_normal_foreign_article_is_caught_by_shape_not_by_wording(self):
        """那条 README 读起来就是一篇正常文章 —— 判据是「这一类来源不该拿着 content」。"""
        self.assertEqual('', readable.boilerplate_reason(FOREIGN_ARTICLE))
        source, key = self.wrong_article('digest', FOREIGN_ARTICLE)
        planned = self.plan(kind='digest', source=source['id'])
        self.assertEqual([key], [row['key'] for row in planned])
        self.assertIn('不是这一条自己的', planned[0]['why'])

    def test_a_backup_made_after_the_damage_is_not_restored_onto_itself(self):
        """**备份判据和扫描判据必须同源。** 这一条栽过两次,形态一模一样。

        `_backups()` 自动扫按 mtime **倒序**,所以「污染之后做的那一份」是默认命中
        项:里面那份同样是外链那一页的全文、`content_kind` 同样是 `content`。只问
        文字那一道(`boilerplate_reason` 对一篇逐字正常的文章返回空串)的后果实测
        过 —— 它被当成有效原文装回**它自己头上**,`body=?` 的 CAS 比的是同一份内容、
        **rowcount 照样是 1**,于是报「已收拾 1 条、从备份装回了原文」而正文一个字
        都没变;代价全落在旁边几列:译文作废(下一轮按这份别人家的文章再买一次翻译,
        翻译是这条链上最贵的一步)、`content_hash` 改写(vault 里那篇跟着重写),而且
        每跑一次 `run` 重复一轮、永不收敛。
        """
        source, key = self.wrong_article('digest', FOREIGN_ARTICLE)
        before = self.row(key)
        rows = self.plan(backups=self.backup(key, FOREIGN_ARTICLE, 'content'),
                         kind='digest', source=source['id'])
        self.assertEqual([''], [row['shell'] for row in rows])   # 这份不算有效原文
        cut, moved, stuck = recheck._downgrade(rows)
        self.assertEqual((len(cut), len(moved), len(stuck)), (0, 0, 1))
        # **后果断言**:报「收拾不了」就真的一列都没动 —— 尤其译文还在。
        self.assertEqual(self.row(key), before)

    def test_an_older_clean_backup_wins_over_a_polluted_newer_one(self):
        """拒掉一份不等于放弃:继续往更早的备份找,找到了照旧装回去。"""
        source, key = self.wrong_article('digest', FOREIGN_ARTICLE)
        backups = (self.backup(key, FOREIGN_ARTICLE, 'content', tag='-new')
                   + self.backup(key, ORIGINAL, 'summary', tag='-old'))
        rows = self.plan(backups=backups, kind='digest', source=source['id'])
        self.assertEqual([ORIGINAL], [row['shell'] for row in rows])
        self.assertEqual(['summary'], [row['shell_kind'] for row in rows])
        cut, moved, stuck = recheck._downgrade(rows)
        self.assertEqual((len(cut), len(moved), len(stuck)), (1, 0, 0))
        self.assertEqual(self.row(key)['body'], ORIGINAL)

    def test_a_legacy_backup_without_content_kind_is_only_trusted_where_it_is_safe(self):
        """老备份缺 `content_kind` 这一列:**不许可来源那一支不拿它赌**。

        那一支要把备份里那份原文**原样**装回去、而且不放回补抓队列,所以判不出这份
        备份是不是在某一轮错误覆盖之后做的时候,回落到「收拾不了」由人处理比赌一把
        便宜(拿不准就别固化终态)。**反过来钉一边**:许可来源那一支照旧用得上它 ——
        它按「从没补抓过」的形态写 `summary`、而且会把条目放回队列,下一轮自己重抓。
        """
        source, key = self.wrong_article('digest', FOREIGN_ARTICLE)
        rows = self.plan(backups=self.backup(key, ORIGINAL, None),
                         kind='digest', source=source['id'])
        self.assertEqual([''], [row['shell'] for row in rows])
        self.forget('digest', source, key)
        source, key = self.wrong_article('rss')
        rows = self.plan(backups=self.backup(key, ORIGINAL, None),
                         kind='rss', source=source['id'])
        self.assertEqual([ORIGINAL], [row['shell'] for row in rows])
        self.assertEqual(['summary'], [row['shell_kind'] for row in rows])

    def test_a_normal_fulltext_article_on_a_capable_source_is_left_alone(self):
        """反过来钉一边:许可来源上一篇正常的补抓成品不在射程里,一个字都不许动。"""
        source, key = self.wrong_article('rss', FOREIGN_ARTICLE)
        self.assertEqual([], self.plan(kind='rss', source=source['id']))
        self.assertEqual([], self.plan())
        self.assertEqual(FOREIGN_ARTICLE, self.row(key)['body'])

    def test_an_untouched_summary_on_a_legacy_flag_is_left_alone(self):
        """旧库误开了 fulltext、而正文还是来源自己那份摘要 —— 同样不在射程里。"""
        source, _ = self.legacy_item('digest')
        self.assertEqual([], self.plan(kind='digest', source=source['id']))

    def test_without_a_backup_the_whole_item_is_kept_and_reported(self):
        """没有可恢复的原文就一个字都不动:这一类来源不会重抓,不拿标题链接顶替。"""
        for kind, spec in feed_types().items():
            if spec.body_from_link:
                continue
            with self.subTest(kind=kind):
                source, key = self.wrong_article(kind)
                before = self.row(key)
                planned = self.plan(kind=kind, source=source['id'])
                self.assertEqual([''], [row['shell'] for row in planned])
                cut, moved, stuck = recheck._downgrade(planned)
                self.assertEqual(([], []), (cut, moved))
                self.assertEqual(planned, stuck)
                self.assertEqual(before, self.row(key))
                self.assertFalse(self.matches(key, fulltext.queued_sql))
                self.forget(kind, source, key)

    def test_a_backup_restores_the_original_body_without_requeueing(self):
        for kind, spec in feed_types().items():
            if spec.body_from_link:
                continue
            with self.subTest(kind=kind):
                source, key = self.wrong_article(kind)
                before = self.row(key)
                planned = self.plan(self.backup(key, content_kind=self.native(kind)),
                                    kind=kind, source=source['id'])
                cut, moved, stuck = recheck._downgrade(planned)
                self.assertEqual(planned, cut)
                self.assertEqual(([], []), (moved, stuck))
                stored = self.row(key)
                self.assertEqual(ORIGINAL, stored['body'])
                self.assertEqual(self.native(kind), stored['content_kind'])
                self.assertEqual(fulltext.body_hash(ORIGINAL), stored['content_hash'])
                # 补抓那三个标记一个都没动 —— 它压根不该进队列,清掉只会让它看
                # 起来「在等全文」而永远没人来抓。
                for field in ('fulltext_at', 'fulltext_tries', 'fulltext_error'):
                    self.assertEqual(before[field], stored[field], field)
                self.assertFalse(self.matches(key, fulltext.queued_sql))
                self.assertFalse(self.matches(key, fulltext.unsettled_sql))
                # 译文作废、留一个重提取的待办;导出闸门靠 content_hash 自己成立。
                for field in ('title_zh', 'body_zh', 'translated_at'):
                    self.assertEqual('', stored[field], field)
                self.assertEqual(1, stored['extract_stale'])
                self.assertEqual(before['exported_hash'], stored['exported_hash'])
                self.assertEqual([], self.plan(kind=kind, source=source['id']))
                self.assertEqual([], self.plan())
                self.forget(kind, source, key)

    def test_a_changed_body_is_skipped_on_the_disallowed_branch_too(self):
        """那一支的内容 CAS 同样待在 UPDATE 自己的 WHERE 里。"""
        source, key = self.wrong_article('digest')
        planned = self.plan(self.backup(key), kind='digest', source=source['id'])
        with reg.db() as conn:
            conn.execute('UPDATE feed_items SET body=? WHERE key=?', ('换过了', key))
        cut, moved, stuck = recheck._downgrade(planned)
        self.assertEqual(([], []), (cut, stuck))
        self.assertEqual(planned, moved)
        self.assertEqual('换过了', self.row(key)['body'])

    def test_capable_sources_return_to_a_real_refill_queue(self):
        for kind, spec in feed_types().items():
            if not spec.body_from_link:
                continue
            with self.subTest(kind=kind):
                source, key = self.wrong_article(kind)
                planned = self.plan(kind=kind, source=source['id'])
                self.assertEqual([key], [row['key'] for row in planned])
                cut, moved, stuck = recheck._downgrade(planned)
                self.assertEqual(planned, cut)
                self.assertEqual(([], []), (moved, stuck))
                stored = self.row(key)
                self.assertEqual(planned[0]['shell'], stored['body'])
                self.assertEqual(fulltext.body_hash(stored['body']), stored['content_hash'])
                self.assertEqual('summary', stored['content_kind'])
                for field in ('fulltext_at', 'body_zh', 'title_zh', 'translated_at'):
                    self.assertEqual('', stored[field], field)
                self.assertTrue(self.matches(key, fulltext.queued_sql))

    def test_revoking_the_last_membership_after_scan_preserves_the_whole_item(self):
        for revoke in ('disable', 'delete'):
            with self.subTest(revoke=revoke):
                source, key = self.wrong_article('rss')
                planned = self.plan(kind='rss', source=source['id'])
                self.assertEqual([key], [row['key'] for row in planned])
                if revoke == 'disable':
                    reg.save_subscription('rss', {'fulltext': False}, source['id'])
                else:
                    reg.delete_subscription('rss', source['id'])
                before = self.row(key)
                cut, moved, stuck = recheck._downgrade(planned)
                self.assertEqual(([], []), (cut, stuck))
                self.assertEqual(planned, moved)
                self.assertEqual(before, self.row(key))
                self.assertEqual([], self.plan(kind='rss', source=source['id']))

    def test_revoking_the_source_capability_after_scan_preserves_the_whole_item(self):
        """扫描到写回之间能力本身被撤销:许可那一支的 `_eligible()` 当场拦住。

        **这时它落进 `moved` 而不是改走不许可那一支** —— 计划里那份壳是按「下一轮
        会重抓」算出来的(可能是标题与链接那份重建壳),拿它去办「只恢复原文」那件
        事就是把内容毁掉。如实报出来、让人再跑一次 scan。
        """
        source, key = self.wrong_article('rss')
        planned = self.plan(kind='rss', source=source['id'])
        self.assertEqual([key], [row['key'] for row in planned])
        before = self.row(key)
        disabled = replace(SOURCE_TYPES['rss'], body_from_link=False)
        with patch.dict(SOURCE_TYPES, {'rss': disabled}):
            cut, moved, stuck = recheck._downgrade(planned)
            self.assertEqual(([], []), (cut, stuck))
            self.assertEqual(planned, moved)
            self.assertEqual([key], [row['key'] for row in self.plan(kind='rss')])
        self.assertEqual(before, self.row(key))

    def test_another_enabled_membership_keeps_the_recheck_eligible(self):
        source, key = self.wrong_article('rss')
        other = reg.save_subscription('rss', {
            'name': '仍要补抓', 'url': 'https://other.example.org/feed', 'fulltext': True,
        })
        with reg.db() as conn:
            conn.execute('INSERT INTO subscription_items VALUES(?,?)', (other['id'], key))
        planned = self.plan(kind='rss', source=source['id'])
        self.assertEqual([key], [row['key'] for row in planned])
        reg.save_subscription('rss', {'fulltext': False}, source['id'])
        cut, moved, stuck = recheck._downgrade(planned)
        self.assertEqual(planned, cut)
        self.assertEqual(([], []), (moved, stuck))
        self.assertTrue(self.matches(key, fulltext.queued_sql))

    def test_naming_a_disallowed_source_explains_an_empty_result(self):
        """点名了不走补抓的来源却一条都没选中时,把「为什么」说出来。"""
        source, _ = self.legacy_item('digest')
        for scope in ((None, None), ('digest', None), (None, source['id']),
                      ('digest', source['id'])):
            with self.subTest(scope=scope):
                note = recheck._nothing(*scope)
                self.assertTrue(note.startswith('没有需要复查的条目。'))
                self.assertEqual(scope != (None, None), '简报不走正文补抓' in note)
        self.assertEqual('没有需要复查的条目。', recheck._nothing('rss', None))
        self.assertIn('不存在', recheck._nothing(None, 9999))

    def test_naming_a_source_that_does_not_live_here_says_so(self):
        """**公众号点名时要说「这里没有这一类」,不是「这一类按两道判据查过了」。**

        公众号条目由它自己那条采集链存(`collector/`、`wechat_local`),压根不进
        feeds.db。给它那段「不走正文补抓、这里按两道判据选」的解释,是把「这个脚本
        管不着」说成了「这个脚本查过了」—— 而人正是拿这句话当结论用的。
        """
        note = recheck._nothing('wechat', None)
        self.assertIn('不在这张表里', note)
        self.assertIn('不是「这一类没问题」', note)
        self.assertNotIn('按两道判据选', note)

    def test_the_regrab_path_is_not_claimed_to_be_gated_for_tweets(self):
        """空结果那句话里「新的不会再有」要说准到**哪一条路**。

        通用那条路(补抓队列、浏览器采集)每条写语句自己的 WHERE 里都拦着不许可的
        来源;人点「重新抓一次」问的是另一个问题(`url_is_origin`),**大V观点在那一问
        上是放行的** —— 换回来的是推文本人的正文,不属于这里要找的损坏,但也不该被
        那句话顺带说成「拦着」。反过来钉一边:简报两问都关,那一句不该出现。
        """
        self.assertIn('「重新抓一次」那条路对大V观点是放行的',
                      recheck._nothing('social', None))
        self.assertNotIn('是放行的', recheck._nothing('digest', None))

    def test_the_automatic_scan_finds_a_flat_backup_file_too(self):
        """**自动模式认两种摆法** —— 平铺的那一个曾经整份看不见。

        生产上那份全库备份是 `backups/<名字>.db`(手工 `.backup` 出来的一个文件),
        而自动模式只扫 `backups/*/feeds.db`;于是脚本说「备份里没有原始内容」,
        而唯一那份有用的备份就躺在同一个目录里。名字也要分得开:平铺式再取
        `parent.name` 会把几份备份一起叫成「backups」。
        """
        root = reg.DB_PATH.parent / 'backups'
        (root / '2026-10-09').mkdir(parents=True, exist_ok=True)
        nested, flat = root / '2026-10-09' / 'feeds.db', root / 'feeds-20261021.db'
        for path, when in ((nested, 1760000000), (flat, 1761000000)):
            sqlite3.connect(path).close()
            os.utime(path, (when, when))
        self.assertEqual([flat, nested], recheck._backups(None))   # 新的在前
        self.assertEqual(['feeds-20261021', '2026-10-09'],
                         [recheck._backup_tag(p) for p in recheck._backups(None)])

    def test_a_backup_without_a_body_column_is_skipped_not_raised(self):
        """**老备份缺 `body` 这一列时跳过它,不许抛。**

        `sqlite3.Row` 对不存在的列抛的是 `IndexError`,**不在 `sqlite3.Error` 的射程
        里** —— 那会一路穿出去把整趟 run 打断,而这个函数的全部设计就是「老备份缺列
        时别整份跳过」。反过来钉一边:同一批里那份完好的备份照旧用得上。
        """
        source, key = self.wrong_article('digest')
        broken = self.root / 'backup-no-body' / 'feeds.db'
        broken.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(broken)
        with conn:
            conn.execute('CREATE TABLE feed_items(key TEXT PRIMARY KEY,content_kind TEXT)')
            conn.execute("INSERT INTO feed_items VALUES(?,'summary')", (key,))
        conn.close()
        self.assertEqual(('', '', ''), recheck._from_backup([broken], key, 'digest'))
        good = self.backup(key)
        self.assertEqual((ORIGINAL, 'summary'),
                         recheck._from_backup([broken, *good], key, 'digest')[:2])
