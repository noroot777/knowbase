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
