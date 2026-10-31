"""「我站着这一页,此刻能做什么」——  扩展开起来那一问(`POST /api/browser-capture/here`)。

服务端答四档(`fill` / `absent` / `complete` / `refused`)连同那句给人看的话,扩展
一个字都不自己编。硬约束见 `docs/decisions/browser-capture.md`;这一组从
`test_browser_capture.py` 拆出来,共用的脚手架在 `browser_capture_case.py`。
"""

from kb import browser_capture as capture
from kb.feeds import registry as reg
from tests.browser_capture_case import BODY, CaptureCase


class CaptureHereTests(CaptureCase):
    def test_standing_on_the_article_is_enough_to_fill_it(self):
        """**站在那篇文章上就够了** —— 不必先去补抓页发起一个待办。

        原先补一篇正文要跨三个界面走七步,其中「补回哪个待办」那一下是让人把
        服务端已经知道的事再答一遍:地址栏里那一行就是答案。判据落在
        「待办是服务端自己建的」「正文真的写进了原条目」两件事上,**而且全程
        一次 `/intents` 都没调**。
        """
        answer = self.here()
        self.assertEqual(200, answer.status_code, answer.text)
        state = answer.json()
        self.assertEqual('fill', state['situation'])
        self.assertEqual(self.key, state['item']['key'])
        self.assertTrue(state['intent']['id'])
        self.assertEqual('https://e.org/post', state['intent']['page_url'])
        # 反复开扩展不该堆出一串待办:`create` 幂等,同一(条目, 页面)复用那一个
        self.assertEqual(state['intent']['id'], self.here().json()['intent']['id'])
        self.assertEqual(1, len(capture.pending()))
        # 拿它直接提交,补抓页一次都没经过
        saved = self.upload(state['intent'])
        self.assertEqual(200, saved.status_code, saved.text)
        self.assertEqual(BODY, self.row(self.key)['body'])

    def test_the_page_is_found_under_either_canonical_form(self):
        """库里那一行的 `url` 是**收它的那条路**存下来的,所以要按两份规范形问。

        订阅类来源原样存 feed 给的地址,手动收录存的是 `grab.normalize`(它去掉
        `utm_*` 这类跟踪参数)。只按一份问,另一条路收的那一半永远答「不在库里」
        —— 而那一句的下一步是**收录**,于是同一篇在库里变成两行。
        反过来钉一边:真正另一篇文章照旧答「不在库里」,不许靠模糊匹配认亲。
        """
        self.assertEqual('fill', self.here('https://e.org/post?utm_source=newsletter')
                         .json()['situation'], '带跟踪参数的同一页要认出来')
        self.assertEqual('absent', self.here('https://e.org/post-two').json()['situation'])
        self.assertEqual('absent', self.here('https://e.org/zh-Hans-CN/post').json()['situation'],
                         '语言前缀是站点自己的约定,不许在这里剥掉认成同一页')

    def test_a_page_outside_the_library_creates_nothing(self):
        """不在库里的那一页:只回一句话,**一个待办都不建**。

        建了的话额度(`MAX_PENDING`)会被随手逛过的页面占满,而它们手上没有任何
        产物 —— 真要补正文的那一条反倒被顶掉。
        """
        state = self.here('https://elsewhere.example.org/x').json()
        self.assertEqual('absent', state['situation'])
        self.assertIsNone(state['item'])
        self.assertIsNone(state['intent'])
        self.assertEqual([], capture.pending())

    def test_a_settled_body_is_not_quietly_offered_for_refilling(self):
        """正文已经定下来的那一条:答「齐了」,**不自动武装一条写入路径**。

        「把缺的那份补上」和「我手上这份是错的、重新采一次」是两件事:后者在补抓
        页上有自己的流程、自己的警示和回执,挤成同一颗按钮会让人分不出点的是哪件。
        反过来钉一边:正文欠着的那一条照旧给(上面那条)。
        """
        with reg.db() as conn:
            conn.execute("UPDATE feed_items SET fulltext_at=? WHERE key=?", (reg.now(), self.key))
        state = self.here().json()
        self.assertEqual('complete', state['situation'])
        self.assertIsNone(state['intent'])
        self.assertEqual([], capture.pending())

    def test_an_item_no_subscription_wants_fulltext_for_is_not_owed(self):
        """`_OWED` 的两半各钉一次 —— 这一条钉的是**资格**那一半。

        「正文还欠着」不等于「`fulltext_at` 是空的」:一条内容归属的订阅都没开补抓
        (或者最后一条开着的被关掉、被删掉)时,它压根不该等全文 —— 自动补抓队列
        (`fulltext.queued_sql`)也正是这么判的。只看 `fulltext_at` 的话,这一页会被
        说成「正文还缺着」并自动武装一条写入路径,而面板上它一个待办都没有。
        """
        plain = self.put(self.plain, url='https://e.org/plain')
        state = self.here('https://e.org/plain').json()
        self.assertEqual(plain, state['item']['key'])
        self.assertEqual('complete', state['situation'], '没有订阅要它的全文 = 不欠')
        self.assertEqual([], capture.pending())

    def test_a_request_pointing_at_another_address_is_handed_to_the_person(self):
        """等着的待办指着另一个地址时,**把它摆在人眼前**,而不是只说一句「不在库里」。

        事故形态:站点按浏览器语言跳转(`openai.com/index/x` 在中文浏览器里落到
        `/zh-Hans-CN/index/x`)。按网址一问,这一页确实「不在库里」—— 只凭这一句
        请人收录,库里立刻多出**第二行同一篇文章**,而两行各有各的 key、各导出
        一篇 md。摆出那个待办并给一颗「改用这一页的地址」,他一眼就看出是同一篇。

        反过来钉一边:站在待办指定的那一页上时,这一项是空的(不然每次都挂着一句
        与此刻无关的提示)。
        """
        self.intent()                                  # 指着 https://e.org/post
        landed = self.here('https://e.org/zh-Hans-CN/post').json()
        self.assertEqual('absent', landed['situation'])
        self.assertEqual(['https://e.org/post'], [one['page_url'] for one in landed['elsewhere']])
        self.assertEqual([], self.here().json()['elsewhere'], '就站在它指定的那一页上时不提')

        adopted = self.here(path='/here/adopt',
                            payload={'key': self.key, 'page_url': 'https://e.org/zh-Hans-CN/post'})
        self.assertEqual(200, adopted.status_code, adopted.text)
        self.assertEqual('https://e.org/zh-Hans-CN/post', adopted.json()['page_url'])
        # 改用**不是放行**:正文照旧要过那一整套准入,这里只验「它现在认这一页」
        self.assertEqual(200, self.precheck(adopted.json(), body=BODY,
                                            page_url='https://e.org/zh-Hans-CN/post').status_code)

    def test_elsewhere_never_lists_a_request_that_was_just_superseded(self):
        """`elsewhere` 要在建完待办**之后**算。

        `here()` 自己那次 `create` 会把同一条目上指着别处的旧待办标成
        `superseded`。先算就会把刚被顶掉的那个摆出来、请人「改用这一页的地址」,
        而它已经作废 —— 点下去只会被拒,屏幕上却看不出为什么。
        """
        stale = self.intent()                          # 指着 https://e.org/post
        landed = self.here('https://e.org/post?utm_source=x').json()
        self.assertEqual('fill', landed['situation'])
        self.assertNotEqual(stale['id'], landed['intent']['id'], '指着另一个地址,该另建一个')
        self.assertEqual([], landed['elsewhere'], '刚被顶掉的那个不许摆出来')

    def test_a_request_bound_to_this_very_page_wins_over_what_the_url_says(self):
        """人明确指过的最终网址**排在库里那一行前面**,否则改址那条路整条是死的。

        事故形态(实测):库里那一行是 `https://e.org/post`,人在采集抽屉里按原样
        发起、但把最终网址填成站点按语言跳转后的那一个。扩展站在那一页上一问,
        `here()` 只按 `feed_items.url` 查 —— 答「这一页还不在知识库里」,而主按钮
        在这一档走的是 `/api/grab`:**同一篇文章在库里变成第二行**。更糟的是这个
        待办的 `page_url` 正好等于当前页,连 `elsewhere` 那条出路都把它过滤掉了,
        屏幕上一点痕迹都没有。adopt 之后扩展立刻再问一次,走的也是这一条路。

        判据落在**后果**上:答出来的那个 intent 要真能拿去写(precheck 200),
        而不只是「situation 变成 fill 了」。
        """
        landed = 'https://e.org/zh-Hans-CN/post'
        mine = self.intent(page_url=landed)
        state = self.here(landed).json()
        self.assertEqual('fill', state['situation'])
        self.assertEqual(mine['id'], state['intent']['id'], '要兑现人发起的那一个,不另建')
        self.assertEqual(self.key, state['item']['key'])
        self.assertEqual([], state['elsewhere'], '这一答用掉的那个不该再摆成「别处」')
        self.assertEqual(200, self.precheck(state['intent'], body=BODY,
                                            page_url=landed).status_code)
        # 反过来钉一边:**没人发起过**的另一个地址照旧答「不在库里」,
        # 不许因为上面这条就对所有地址模糊认亲。
        self.assertEqual('absent', self.here('https://e.org/fr/post').json()['situation'])

    def test_an_explicit_recapture_still_has_a_way_in_after_the_body_is_settled(self):
        """「我手上这份是错的」那条路:正文已经齐了,**但人明确发起过重新采集**。

        撤掉读完之后那道人工核对以后,登录墙、摘要页只要过得了去重门槛就会被写
        进去,而事后纠错的出口只剩这一个。`here()` 按 `_OWED` 一刀切的话,这一条
        答的是 `complete` / `intent=null`,弹窗一颗按钮都不给 —— 抽屉那边却还写着
        「等待浏览器采集」,两块屏幕互相打脸,而那条出路实际上已经堵死。

        反过来钉一边:**没发起过**的那一条照旧答「齐了」
        (`test_a_settled_body_is_not_quietly_offered_for_refilling`),这里再钉一次
        顺序 —— 先确认它是 complete,发起之后才变。
        """
        with reg.db() as conn:
            conn.execute("UPDATE feed_items SET fulltext_at=? WHERE key=?", (reg.now(), self.key))
        self.assertEqual('complete', self.here().json()['situation'], '没发起过就还是「齐了」')
        mine = self.intent()
        state = self.here().json()
        self.assertEqual('fill', state['situation'])
        self.assertEqual(mine['id'], state['intent']['id'])
        self.assertIn('重新采集', state['detail'], '话要说清这是哪一件事,别写成「正文还缺着」')
        self.assertEqual(200, self.precheck(state['intent'], body=BODY).status_code)

    def test_two_requests_on_the_same_page_leave_the_second_one_reachable(self):
        """两个不同条目的待办同时指着这一页:认最早那个,**另一个必须还摆得出来**。

        人把最终网址粘错时就是这个形态。只认一个而把另一个连同 `elsewhere` 一起
        吞掉的话,这一页到底算哪一篇在界面上**无从改** —— 而写进去是不可逆的。
        """
        other = self.put(url='https://e.org/post-two')
        first = self.intent(page_url='https://e.org/shared')
        second = self.intent(page_url='https://e.org/shared', key=other)
        state = self.here('https://e.org/shared').json()
        self.assertEqual(first['id'], state['intent']['id'], '认最早发起的那一个')
        self.assertEqual([second['id']], [one['id'] for one in state['elsewhere']],
                         '另一个要摆在人眼前,他才改得了')

    def test_a_bound_request_does_not_bypass_a_single_admission_check(self):
        """**先兑现**不等于**放行**:待办指着这一页,但条目这期间被排除了。

        这一条钉的是「兑现」走的仍是 `_pending()` 那一份(来源许可、排除、CAS 都在
        它的 `_refresh_status` 里)。自己另写一遍挑待办的判据,就会把一个已经作废的
        待办当成「人明确发起过」兑现,而点下去只会在服务端被拒。
        """
        landed = 'https://e.org/zh-Hans-CN/post'
        self.intent(page_url=landed)
        with reg.db() as conn:
            conn.execute('INSERT INTO feed_item_exclusions(key,kind,url,created_at) VALUES (?,?,?,?)',
                         (self.key, 'rss', 'https://e.org/post', reg.now()))
        state = self.here(landed).json()
        self.assertEqual('absent', state['situation'], '作废的待办不许被当成人发起过的那个')
        self.assertIsNone(state['intent'])

    def test_the_page_question_needs_the_pairing_key(self):
        """两个新端点都由调用方指定地址,所以鉴权和 `/upload` 同一档。

        反过来钉一边:带着密钥的那一次照旧放行(否则「一律拒」也能让上面几条绿)。
        """
        for path in ('/here', '/here/adopt'):
            with self.subTest(path=path):
                payload = ({'url': 'https://e.org/post'} if path == '/here'
                           else {'key': self.key, 'page_url': 'https://e.org/post'})
                self.assertEqual(401, self.here(auth=False, path=path, payload=payload).status_code)
                self.assertEqual(200, self.here(path=path, payload=payload).status_code)
