"""「我站着这一页,此刻能做什么」——  扩展开起来那一问(`POST /api/browser-capture/here`)。

服务端答五档(`fill` / `absent` / `complete` / `refused` / `undecided`)连同那句给人看
的话,扩展一个字都不自己编。硬约束见 `docs/decisions/browser-capture.md`;这一组从
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

    def test_the_item_a_person_picked_is_the_item_that_gets_written(self):
        """**人点中的那一篇就是写进去的那一篇** —— 从「改用它」一路钉到库里哪一行变了。

        两个条目填了同一个最终网址(人把网址粘错,或者两篇真共用一个跳转页)。实测过
        的事故:点了后一篇那行的「改用它」、界面也说「已改用」,可下一问只按网址发出
        去,服务端在同页那几个待办里**按创建时间代选了最早那个** —— 于是「采集」把后
        一篇的正文写进了前一篇(前者 `content_rev` 0→1、正文是后者的,后者仍然缺正文)。
        准入那几道门一道没拦住:它们证明「这一条写得了」,**证明不了「人刚才选的就是
        这一条」**。

        所以判据落在整条链上,不是某一环的标志位:`/here/adopt` 交回身份 → `/here` 带着
        它问 → 那一答里的 `intent` → `precheck` / `upload` → **库里哪一行变了**。
        """
        page = 'https://e.org/shared'
        later = self.put(url='https://e.org/post-two')
        earlier = self.intent(page_url=page)
        self.intent(page_url=page, key=later)
        untouched = self.row(self.key)
        adopted = self.here(path='/here/adopt', payload={'key': later, 'page_url': page}).json()

        state = self.here(payload={'url': page, 'intent_id': adopted['id']}).json()
        self.assertEqual('fill', state['situation'])
        # **先断后果**:拿这一答给的那个待办照常走完,然后看库里哪一行变了。代选回来
        # 时这里就是红的 —— 而先问「答的是哪一篇」的话,红会停在那一句上,落库这一步
        # 根本走不到,等于只验了过程。
        self.assertEqual(200, self.precheck(state['intent'], body=BODY, page_url=page).status_code)
        saved = self.upload(state['intent'], page_url=page)
        self.assertEqual(200, saved.status_code, saved.text)
        self.assertEqual(BODY, self.row(later)['body'], '正文要落在人点中的那一行上')
        self.assertEqual(untouched['body'], self.row(self.key)['body'], '另一条一个字都不许动')
        self.assertEqual(untouched['content_rev'], self.row(self.key)['content_rev'])
        # 诊断用:上面红了的时候这两句说清是哪一环把身份换掉的。
        self.assertEqual(later, state['item']['key'], '答的是人点中那一篇,不是最早那个待办')
        self.assertNotEqual(earlier['id'], state['intent']['id'])

    def test_the_same_page_wanted_by_two_items_is_not_decided_by_sort_order(self):
        """同一页上两个条目都在等、而人没点过:**不代选**,把候选交出去让他点。

        按创建时间挑一个是在赌,赌错那一下是正文被另一篇覆盖、**不可恢复**;而问一句
        的代价只是他在列表里点一下。**反过来钉一边**:只有一个在等时照旧直接答
        `fill` —— 这道闸不许把正常那一条也扣住。
        """
        page = 'https://e.org/shared'
        other = self.put(url='https://e.org/post-two')
        alone = self.intent(page_url=page)
        state = self.here(page).json()
        self.assertEqual('fill', state['situation'], '只有一个在等就别问')
        self.assertEqual(alone['id'], state['intent']['id'])

        second = self.intent(page_url=page, key=other)
        state = self.here(page).json()
        self.assertEqual('undecided', state['situation'])
        self.assertIsNone(state['intent'], '这一档不许带着一个代选出来的待办')
        self.assertIsNone(state['item'])
        self.assertEqual({alone['id'], second['id']},
                         {one['id'] for one in state['elsewhere']}, '两个都要摆出来,他才点得到')

    def test_a_pick_that_went_stale_does_not_quietly_hand_over_another_item(self):
        """人点过的那个失效了(过期 / 被顶掉 / 条目变了),而这一页上还有别的在等。

        让另一个顶上就是拿这一页的正文覆盖另一篇,所以这里**也不代选**。宁可多问
        一次:误问的代价是一次点击,漏判的代价是写错一篇、不可恢复。
        """
        page = 'https://e.org/shared'
        other = self.put(url='https://e.org/post-two')
        mine = self.intent(page_url=page)
        adopted = self.here(path='/here/adopt', payload={'key': other, 'page_url': page}).json()
        self.assertEqual(200, self.client.delete(
            '/api/browser-capture/intents/' + adopted['id']).status_code)

        state = self.here(payload={'url': page, 'intent_id': adopted['id']}).json()
        self.assertEqual('undecided', state['situation'])
        self.assertIsNone(state['intent'])
        self.assertEqual([mine['id']], [one['id'] for one in state['elsewhere']],
                         '剩下那个照旧摆出来,但不许自己顶上')

    def test_a_pick_that_went_stale_with_nothing_left_is_not_answered_by_url(self):
        """上一条的另一头:他点过的那个失效了,而这一页上**一个待办都不剩**。

        判据不许挂在「还剩几个」上。一个不剩时按网址查找会命中另一篇欠正文的条目、
        再给它新建一个合法待办,而 precheck、CAS、upload 验的都是「这个新待办写得了」,
        没有一道在验「它还是人刚才选的那一篇」—— 点下去正文就落在了那一篇身上。
        同一支还会把「你选的那个没了」说成另外两件事(这一页不在库里 / 正文已齐),
        前一种在下一条用例里单独钉。
        """
        page = 'https://e.org/post'
        later = self.put(url='https://e.org/post-two')
        mine = self.intent(page_url=page)
        adopted = self.here(path='/here/adopt', payload={'key': later, 'page_url': page}).json()
        before = self.row(self.key)
        for identity in (mine['id'], adopted['id']):
            self.assertEqual(200, self.client.delete(
                '/api/browser-capture/intents/' + identity).status_code)

        state = self.here(payload={'url': page, 'intent_id': adopted['id']}).json()
        # **先断后果**:它交不出任何写得了的待办 —— 拿这一答交回的东西去写,一定写不
        # 进,那一篇的正文一个字都不会变。只看形状的话,「代选了另一篇」和「老实说选
        # 不出来」在某些字段上长得很像,而真正不可恢复的是库里那一行。
        saved = self.upload(state['intent'] or {'id': adopted['id']}, page_url=page)
        self.assertNotEqual(200, saved.status_code, '不许交出一个写得了的待办')
        self.assertEqual(before['body'], self.row(self.key)['body'], '那一篇一个字都没动')
        self.assertEqual(before['content_rev'], self.row(self.key)['content_rev'])

        self.assertEqual('undecided', state['situation'])
        self.assertIsNone(state['item'], '不许把另一篇摆成主目标')
        self.assertIsNone(state['intent'], '更不许给它新建一个写得了的待办')
        self.assertEqual([], state['elsewhere'], '一个都不剩就老实说一个都不剩')

    def test_a_stale_pick_is_not_turned_into_a_brand_new_link(self):
        """同一支的另一副面目:他点过的那个失效了,而这一页在库里**压根没有对应行**。

        回落到按网址查找的那一版在这里答 `absent`,而那一档的出路是「采集」——
        只交地址让服务端自己去抓,于是库里多出第二行同一篇文章,他要补的那一篇
        仍旧缺正文。单独钉一条:上一条在第一个断言就红了,这一副面目要自己能红。
        """
        page = 'https://e.org/nowhere'
        adopted = self.here(path='/here/adopt',
                            payload={'key': self.key, 'page_url': page}).json()
        self.assertEqual(200, self.client.delete(
            '/api/browser-capture/intents/' + adopted['id']).status_code)

        state = self.here(payload={'url': page, 'intent_id': adopted['id']}).json()
        self.assertEqual('undecided', state['situation'],
                         '不许把它说成「这一页还不在知识库里」、把人引去收录新链接')
        self.assertIsNone(state['item'])
        self.assertIsNone(state['intent'])

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
