"""`repair` 里「只比库里多了链接」的新版,覆盖库里那一条(`kb/feeds/x_intake_supersede.py`)。

实页形态:同一条推文两条通道抄的链接不一样 —— 页面上显示的是缩短的地址(`youtube.com/…`),
不登录通道拿到的是真实地址(`www.youtube.com/…`);末尾那条被 X 渲染成卡片的链接,页面正文里
压根没有。库里那份和这次那份对不上,候选停在 `repair`,「问题与待办」上以「正文待补」挂到超窗。
仓库主人拍板:这种按更完整的那份覆盖。

判据落在后果上:库里那一条变没变成新版、候选还欠不欠、译文有没有作废重排。反过来钉住:
新版少了链接、改了字,库里原样(库里那份能自证,这条直接算已收,见 `tests/test_x_intake_kept.py`);
覆盖没写成(别人刚改过库里那条),库里原样、候选照旧留在 `repair`。

三条集成边界各钉一次:同一条推文也被订阅源收过时,改的是大V那一条、订阅源那篇原样;
订阅在收尾前被停用/改过,旧那一轮一个字都不写;旧推文只缺完整证明时补的是证明,
正文没变、译文不作废。

跑:  uv run --offline python -m unittest tests.test_x_intake_supersede -v
"""
from unittest.mock import patch

from kb.feeds import grab_store, registry as reg, x_post_meta
from kb.feeds import x_intake_store as store
from kb.feeds import x_intake_supersede as supersede
from tests.test_x_intake_title_drift import DriftBase

SHORT = 'Subscribe to get all the episodes soon:\n\nhttps://youtube.com/@PeterYangYT?sub_confirmation=1'
CARD = 'Try it here, let your harness install it:'


class SupersedeTests(DriftBase):
    def parked(self, stored_text, seen_text):
        """库里一份 `stored_text`,这一轮从时间线看到 `seen_text`,候选停进 `repair`。"""
        stored = self.saved_by_detail_page(stored_text)
        self.stage([self.timeline_full_text(seen_text)])
        self.assertEqual(self.candidate()['state'], 'repair', '这一局的前提')
        return stored

    def test_the_real_address_replaces_the_display_address(self):
        fuller = SHORT.replace('https://', 'https://www.')
        stored = self.parked(SHORT, fuller)
        with reg.db() as conn:
            conn.execute("UPDATE feed_items SET title_zh='旧译', body_zh='旧译文'")

        self.assertEqual(1, supersede.run([self.source]))

        row = self.feed_row()
        self.assertEqual(row['body'], fuller)
        self.assertEqual(row['content_rev'], stored['content_rev'] + 1)
        self.assertEqual((row['title_zh'], row['body_zh']), ('', ''), '旧译文译的是旧那份,要作废重排')
        self.assertEqual(self.candidate()['state'], 'ready')
        self.assertEqual(store.delivery(self.source)[1], 0, '覆盖之后不该再算「正文待补」')

    def test_a_trailing_card_link_is_added(self):
        fuller = CARD + 'https://github.com/op7418/dsh-guizang-design'
        self.parked(CARD + '\n\nmore words after it', fuller + '\n\nmore words after it')

        self.assertEqual(1, supersede.run([self.source]))

        self.assertIn('https://github.com/op7418/dsh-guizang-design', self.feed_row()['body'])

    def test_a_sync_round_carries_it_out(self):
        """接线:一轮真的采集在收尾时就把它覆盖掉,不用另跑脚本。"""
        fuller = SHORT.replace('https://', 'https://www.')
        self.parked(SHORT, fuller)

        self.run_intake(lambda *args, **kwargs: ([self.timeline_full_text(fuller)], ''))

        self.assertEqual(self.feed_row()['body'], fuller)
        self.assertNotEqual(self.candidate()['state'], 'repair')

    def seen_again(self, stored_text, seen_text):
        """库里一份 `stored_text`,这一轮从时间线看到 `seen_text`,再跑一遍清扫。"""
        stored = self.saved_by_detail_page(stored_text)
        self.stage([self.timeline_full_text(seen_text)])
        supersede.run([self.source])
        return stored

    def test_a_copy_that_lost_a_link_never_replaces(self):
        stored = self.seen_again(SHORT, SHORT.split('https://')[0].rstrip() + '\n\nsee you')

        self.assertEqual(self.feed_row()['body'], stored['body'])
        self.assertEqual(self.candidate()['state'], 'ready', '库里那份能自证:认它,算已收')

    def test_a_changed_word_never_replaces(self):
        stored = self.seen_again(SHORT, SHORT.replace('episodes', 'shows'))

        self.assertEqual(self.feed_row()['body'], stored['body'])
        self.assertEqual(self.candidate()['state'], 'ready', '库里那份能自证:认它,算已收')

    def test_the_candidate_is_released_only_once_the_copy_is_written(self):
        """覆盖没写成(库里那条的版本 CAS 拒了)就不许放行:放行和改写同一笔、一起回滚。"""
        stored = self.parked(SHORT, SHORT.replace('https://', 'https://www.'))

        with patch.object(grab_store, 'write_version', return_value=False):
            self.assertEqual(0, supersede.run([self.source]))

        self.assertEqual(self.feed_row()['body'], stored['body'])
        self.assertEqual(self.candidate()['state'], 'repair')
        self.assertEqual(store.delivery(self.source)[1], 1)

    def test_a_post_and_an_article_never_replace_each_other(self):
        post = self.timeline_full_text(SHORT)
        fuller = self.timeline_full_text(SHORT.replace('https://', 'https://www.'))
        strip = {k: v for k, v in post.items() if k != x_post_meta.FIELD}
        article = x_post_meta.stamp(strip, 'article', '')
        self.assertTrue(x_post_meta.supersedes(post, fuller), '这一局的前提')
        self.assertFalse(x_post_meta.supersedes(article, fuller))
        self.assertFalse(x_post_meta.supersedes(
            article, x_post_meta.stamp(dict(strip, title='Another headline'), 'article', '')))

    def test_a_feed_copy_of_the_same_post_is_left_alone(self):
        """同一条推文的 url 也被订阅源收过:`feed_items` 里两行,改的必须是大V那一行。"""
        fuller = SHORT.replace('https://', 'https://www.')
        self.parked(SHORT, fuller)
        social = self.feed_row()
        mirror = dict(social, kind='rss', key=reg.item_key('rss', social['url']), path='')
        with reg.db() as conn:                 # 订阅源那篇在前(按 url 查第一行会先撞上它)
            conn.execute("UPDATE feed_items SET rowid=rowid+1000 WHERE kind='social'")
            conn.execute(f"INSERT INTO feed_items(rowid,{','.join(mirror)}) VALUES(1,{','.join('?' * len(mirror))})",
                         tuple(mirror.values()))
        self.assertEqual(grab_store.known(social['url'])['kind'], 'rss', '这一局的前提')

        self.assertEqual(1, supersede.run([self.source]))

        self.assertEqual(self.kind_row('social')['body'], fuller)
        self.assertEqual(self.kind_row('rss')['body'], SHORT, '订阅源那篇不是这条候选的那一行')
        self.assertEqual(self.candidate()['state'], 'ready')

    def test_a_revised_subscription_writes_nothing(self):
        """收尾之前订阅被停用:旧那一轮的来源快照失去交付权,库里一个字都不许改。"""
        stored = self.parked(SHORT, SHORT.replace('https://', 'https://www.'))
        reg.save_subscription('social', {'name': 'Example', 'enabled': False}, self.source['id'])
        with store.read_db() as conn:
            self.assertFalse(store.current(conn, self.source), '这一局的前提')

        self.assertEqual(0, supersede.run([self.source]))

        row = self.feed_row()
        self.assertEqual((row['body'], row['content_rev']), (stored['body'], stored['content_rev']))
        self.assertEqual(self.candidate()['state'], 'repair')

    def test_a_legacy_copy_gets_its_proof_and_keeps_its_translation(self):
        """标题、正文、作者、日期都对,只缺完整证明:补证明,正文没变、译文不作废。"""
        text = 'Read this https://example.com/demo'
        self.prepare()
        observed = self.timeline_full_text(text)
        reg.put_items(self.source, [observed])
        with reg.db() as conn:                 # 升级前入库的那一批:没有完整证明
            conn.execute("UPDATE feed_items SET x_post_meta='',body_provenance='',title_zh='旧译',extract_stale=0")
        stored = self.feed_row()
        self.assertEqual((observed['title'], observed['body']), (stored['title'], stored['body']), '这一局的前提')
        self.stage([observed])
        self.assertEqual(self.candidate()['state'], 'repair', '这一局的前提')

        self.assertEqual(1, supersede.run([self.source]))

        row = self.feed_row()
        self.assertTrue(store.complete(row), '库里那条现在能证明自己')
        self.assertEqual(row['x_post_meta'], observed['x_post_meta'])
        self.assertEqual((row['body'], row['content_hash']), (stored['body'], stored['content_hash']))
        self.assertEqual((row['title_zh'], row['extract_stale']), ('旧译', 0), '正文没变,译文和提取都还是对的')
        self.assertEqual(row['content_rev'], stored['content_rev'] + 1)
        self.assertEqual(self.candidate()['state'], 'ready')
        self.assertEqual(store.delivery(self.source)[1], 0)

    def kind_row(self, kind):
        with reg.db() as conn:
            return dict(conn.execute('SELECT * FROM feed_items WHERE kind=?', (kind,)).fetchone())
