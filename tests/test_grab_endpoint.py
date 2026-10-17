# -*- coding: utf-8 -*-
"""`/api/grab` 那一层:**重复投递时按真实进度回答**,别把暂存说成已完成。

入库那一半在 `tests/test_grab_store.py`、抓取那一半在 `tests/test_grab.py`,夹具
共用 `tests/grab_fixtures.py`。判据一律先断言后果(见夹具模块的说明):回答里那句
话之外,还要钉「库里那一行动没动」「后半条链被起没起」「外部取页面发了几次」。
"""
from __future__ import annotations

import unittest
from unittest.mock import patch

from kb import config, pipeline
from kb.feeds import grab, registry as reg
from tests.grab_fixtures import (BODY, SOFT_WALL, WALL, GrabCase, PAGE, TWEET,
                                 html_resp, page_html)


class EndpointTests(GrabCase):
    """收录端点:**重复投递时按真实进度回答**,别把暂存说成已完成。"""

    def setUp(self):
        super().setUp()
        from fastapi.testclient import TestClient

        from kb.server import app
        self.client = self.enterContext(TestClient(app))
        self.runs: list[list[str]] = []

    def pipeline_patch(self, started=True, reason='其他同步正在运行'):
        """**打桩也要还原那条契约**:`start_pipeline` 返回的是**这一轮的 id**,
        不是 True —— 前端要拿它核对那一轮 export/index 的结果。回 True 的打桩会
        让「端点把 run 原样交出去」这件事测不出来。"""
        def fake(stages, refused=None, crawl_filter=None, **kwargs):
            self.runs.append((list(stages), (crawl_filter or {}).get('channel')))
            if not started and refused is not None:
                refused.append(reason)
            return 'run-7' if started else ''
        return patch.object(pipeline, 'start_pipeline', fake)

    def post(self, url=PAGE, **body):
        return self.client.post('/api/grab', json={'url': url, **body})

    def restore(self, url=PAGE):
        """「只把欠的那一步补上」那条**独立路径**。

        它不是 `/api/grab` 上的一个字段:字段会被**更新了文件却没重启**的那个
        kb-web 静默忽略(Pydantic 默认丢掉不认识的键),于是降级成「不在就抓一篇
        新的」。独立路径在那种服务上回 404,客户端据此失败关闭 —— 口径见
        `kb.web.grab_api.RestoreRequest`。
        """
        return self.client.post('/api/grab/restore', json={'url': url})

    def test_a_staged_duplicate_gets_the_rest_of_the_chain_run_again(self):
        grab.save({'url': PAGE, 'title': 'A', 'body': BODY, 'author': 'x',
                   'published': '', 'content_kind': 'content'})
        with self.pipeline_patch():
            r = self.post()
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertEqual((data['duplicate'], data['done']), (True, False))
        self.assertEqual(data['pipeline'], 'started')
        # **那一轮的身份要原样交给前端**:没有它,面板只能按「run_id 变了」猜 ——
        # 别人插在中间的一轮、以及我们这一轮红了,都会被写成「已写入知识库」。
        self.assertEqual(data['run'], 'run-7')
        # **欠哪一步要说出来**(`grab.owing` 那一份口径):笼统一句「还没写进知识
        # 库」把「还没导出」和「文件不见了」说成同一件事,而后者补跑一轮撞不到它。
        self.assertIn('还没写成 vault 里那一篇', data['detail'])
        self.assertEqual(data['owing'], '还没写成 vault 里那一篇')
        # **后果断言**:后半条链真被跑起来了 —— 只改一句文案的话这里红。
        self.assertEqual(self.runs, [(['export', 'index', 'extract'], 'inbox')])

    def test_restore_can_never_create_anything(self):
        """**恢复那条路在构造上就没有「抓一篇新的」这个动作,不是一次时序赌博。**

        `/api/grab` 的公开契约是「在就恢复、不在就抓一篇新的」。命令行自动补后半条
        链时手里只有一张旧快照(「刚才查到它还在」),而第 2 轮审查实测:等待期间
        历史清理把那一行删掉之后,那一次重投**原地重新抓了一遍** —— 一条清理掉的
        `rss` 文章被重建成 `inbox`、正文换成这次抓到的,外部取页面实发一次,而命令行
        照旧退出 0。

        所以约束放在**写入端**:那一行不在了就 409 到此为止,抓取 + 新建那条路径
        压根走不到 —— 不靠「再查一次 state」去堵检查和写入之间的窗口。
        反过来钉一边:**`/api/grab` 的初次收录照旧抓取并创建**(不然把这个接口整个
        关掉也能全绿)。
        """
        with patch.object(grab, 'request', return_value=html_resp(page_html())) as got, \
             self.pipeline_patch():
            r = self.restore()
        self.assertEqual(r.status_code, 409, r.text)
        self.assertIn('没有可恢复的条目', r.json()['detail'])
        # **后果断言**:一次外部取页面都没发,库里一行没多,管道也没被占。
        self.assertEqual(got.call_count, 0)
        self.assertEqual(self.items(), [])
        self.assertEqual(self.runs, [])
        # 反过来钉一边:同一条地址走 `/api/grab` 就是正常收录。
        with patch.object(grab, 'request', return_value=html_resp(page_html())) as got, \
             self.pipeline_patch():
            data = self.post().json()
        self.assertEqual((got.call_count, data['added']), (1, 1))
        self.assertEqual(len(self.items()), 1)

    def test_restore_still_finishes_the_chain_for_an_item_that_is_there(self):
        """**反过来钉一边:那一行还在、内容也没问题时,「只恢复」照旧把后半条链
        补起来。** 少了它,把这道约束写成「一律 409」也全绿,而自动恢复就再也补不了
        任何一条 —— 批量投递时第二条起全都停在「还没写进知识库」。
        """
        grab.save({'url': PAGE, 'title': 'A', 'body': BODY, 'author': 'x',
                   'published': '', 'content_kind': 'content'})
        with patch.object(grab, 'request',
                          return_value=html_resp(page_html())) as got, self.pipeline_patch():
            data = self.restore().json()
        self.assertEqual(got.call_count, 0, '恢复不许发任何对外抓取')
        self.assertEqual((data['duplicate'], data['done']), (True, False))
        self.assertEqual(data['pipeline'], 'started')
        self.assertEqual(self.runs, [(['export', 'index', 'extract'], 'inbox')])
        # **多余的键当场 400,不许被忽略。** 「静默丢掉不认识的键」正是跨版本那个
        # 故障的机制:哪天又有人想拿一个字段表达「只恢复」/「顺便重抓」,他要立刻
        # 拿到拒绝,而不是一个会动内容的 200。
        with patch.object(grab, 'request',
                          return_value=html_resp(page_html())) as got, self.pipeline_patch():
            r = self.client.post('/api/grab/restore', json={'url': PAGE, 'refetch': True})
        self.assertEqual(r.status_code, 422, r.text)
        self.assertEqual(got.call_count, 0, '恢复那条路不许被另一个字段撬开')
        self.assertEqual(self.items()[0]['body'], BODY, '一个字节都不许动')
        # 同一道边界也挡住「拿字段表达只恢复」那种写法(它曾经被旧服务静默忽略)。
        r = self.client.post('/api/grab', json={'url': PAGE, 'restore_only': True})
        self.assertEqual(r.status_code, 422, r.text)

    def test_restore_never_replaces_the_body(self):
        """**「只恢复」不替任何人做「换掉它」这个决定。**

        库里那一份看不出是正文时,不带约束的那条路会自动走进 `_recover`:真发
        HTTP、真条件替换。那是人明确要求时才做的事(`refetch`)—— 自动补链撞上
        这一档,只该如实说它欠在哪儿。
        """
        grab.save({'url': PAGE, 'title': 'Sign in to read', 'body': WALL,
                   'author': 'x', 'published': '', 'content_kind': 'content'})
        with patch.object(grab, 'request', return_value=html_resp(page_html())) as got, \
             self.pipeline_patch():
            data = self.restore().json()
        self.assertEqual(got.call_count, 0, '恢复不许替人重抓')
        self.assertEqual(self.items()[0]['body'], WALL, '一个字节都不许动')
        self.assertEqual((data['duplicate'], data['content_ok']), (True, False))
        self.assertNotIn('replaced', data)
        self.assertEqual(data['owing'], '收下的那一份看不出是正文')
        self.assertEqual(self.runs, [], '后面几步全做完也只是把一份坏内容写进知识库')
        # 反过来钉一边:`/api/grab` 上明确要求重抓时照旧换得成。
        with patch.object(grab, 'request', return_value=html_resp(page_html())) as got, \
             self.pipeline_patch():
            data = self.post(refetch=True).json()
        self.assertEqual((got.call_count, data['replaced']), (1, True))
        self.assertIn('这是正文第一段', self.items()[0]['body'])

    def test_a_finished_duplicate_is_idempotent_and_costs_no_run(self):
        grab.save({'url': PAGE, 'title': 'A', 'body': BODY, 'author': 'x',
                   'published': '', 'content_kind': 'content'})
        self.mark_exported(PAGE, 'source/inbox/abc.md')
        self.index_doc('source/inbox/abc.md')
        with self.pipeline_patch():
            data = self.post().json()
        self.assertEqual((data['duplicate'], data['done']), (True, True))
        self.assertIn('已经在知识库里了', data['detail'])
        self.assertNotIn('pipeline', data)
        # 绝大多数重复投递都落在这一档:不许顺手占一轮管道。
        self.assertEqual(self.runs, [])

    def test_a_cross_source_item_is_written_through_under_its_own_source(self):
        """一条推文已经被「大V观点」收过、但还没导出:起一轮 `inbox` 的 export
        一个字都不会动它(export 按通道筛),而回答里却写着「正在写入知识库」。"""
        social = reg.save_subscription('social', {'name': 'E', 'url': 'https://x.com/example'})
        reg.put_items(social, [{'url': grab.normalize(TWEET), 'title': '旧标题',
                                'body': BODY, 'published': ''}])
        with self.pipeline_patch():
            data = self.post(TWEET).json()
        self.assertEqual((data['duplicate'], data['done'], data['kind']),
                         (True, False, 'social'))
        self.assertEqual(self.runs, [(['export', 'index', 'extract'], 'social')])
        self.assertIn('大V观点', data['detail'])

    def test_a_busy_pipeline_names_the_button_of_the_owning_source(self):
        """顺延那一句里的**按钮名按归属来源给**:来源页工具栏那颗按钮在可轮询的
        来源里写「同步全部」,只有手动收录写「写入知识库」(判据就是 `pollable`,
        和 `sources.js` 的 `manual` 分支同一个)。一条推文被「大V观点」收过、这一轮
        又没占到管道时,照手动收录那套说就是指着一颗那一页上不存在的按钮。"""
        social = reg.save_subscription('social', {'name': 'E', 'url': 'https://x.com/example'})
        reg.put_items(social, [{'url': grab.normalize(TWEET), 'title': '旧标题',
                                'body': BODY, 'published': ''}])
        with self.pipeline_patch(started=False):
            data = self.post(TWEET).json()
        self.assertEqual(data['pipeline'], 'busy')
        self.assertIn('在「大V观点」里点一次「同步全部」', data['detail'])
        self.assertNotIn('写入知识库', data['detail'])

    def test_a_fresh_link_that_cannot_get_the_pipeline_says_what_to_do(self):
        with patch.object(grab, 'request', return_value=html_resp(page_html())), \
             self.pipeline_patch(started=False):
            data = self.post().json()
        self.assertEqual((data['added'], data['pipeline']), (1, 'busy'))
        self.assertEqual(data['run'], '')          # 没起跑就没有可核对的那一轮
        self.assertIn('其他同步正在运行', data['detail'])
        self.assertIn('写入知识库', data['detail'])
        # 占不到管道不是失败:内容已经在库里了,下一轮 export 会兜住它。
        self.assertEqual(len(self.items()), 1)

    def test_a_refusal_comes_back_as_a_readable_400(self):
        """**一个真实请求都不许发。** 这一条原先拿一条公众号地址当素材,而
        `classify` 不再按域名拒它了(R2-F3)—— 于是用例会**真的去连
        mp.weixin.qq.com**:结论跟着本机网络走,不可重放。地址那一档照旧不发
        HTTP(X 的账号页),响应那一档把 `request` 打桩成那张验证页。
        """
        r = self.client.post('/api/grab', json={'url': 'https://x.com/example'})
        self.assertEqual(r.status_code, 400)
        self.assertIn('大V观点', r.json()['detail'])
        gate = ('https://mp.weixin.qq.com/mp/wappoc_appmsgcaptcha?poc_token=x'
                '&target_url=https%3A%2F%2Fmp.weixin.qq.com%2Fs%2Fabc')
        with patch.object(grab, 'request', return_value=html_resp(
                '<html><body><p>环境异常</p></body></html>', url=gate)):
            r = self.client.post('/api/grab', json={'url': 'https://mp.weixin.qq.com/s/abc'})
        self.assertEqual(r.status_code, 400)
        self.assertIn('公众号', r.json()['detail'])
        self.assertEqual(self.items(), [])
    def test_a_vanished_file_is_queued_for_rewrite_before_the_run(self):
        """**补跑一轮之前先让它变成待办。** 文件被删掉而两个 hash 还相等时,
        `export` 一个字都不会写 —— 端点要先把水位清掉(`grab.reclaim_export`)
        再起这一轮,否则这颗按钮永远只是空转。
        """
        path = 'source/inbox/abc.md'
        grab.save({'url': PAGE, 'title': 'A', 'body': BODY, 'author': 'x',
                   'published': '', 'content_kind': 'content'})
        self.mark_exported(PAGE, path)
        self.index_doc(path)
        (config.VAULT_ROOT / path).unlink()
        with self.pipeline_patch():
            data = self.post().json()
        self.assertEqual((data['duplicate'], data['done']), (True, False))
        self.assertEqual(data['owing'], 'vault 里那一篇不见了')
        self.assertTrue(data['reclaimed'])
        # **后果断言**:这一轮 export 真会选中它(只回一个标志位的话这里红)。
        self.assertEqual(self.pending_export(), 1)
        self.assertEqual(self.runs, [(['export', 'index', 'extract'], 'inbox')])

    def test_the_resume_hint_names_the_button_of_the_owning_source(self):
        """**「等不到就自己点一次」这句里的按钮也按归属来源给。** 前端轮询超时
        后要给一句出路;写死「写入知识库」会让一条被「大V观点」收过的推文指着
        一颗那一页上没有的按钮(和 busy 那一句同一份口径)。"""
        with patch.object(grab, 'request', return_value=html_resp(page_html())), \
             self.pipeline_patch():
            self.assertIn('写入知识库', self.post().json()['resume'])
        social = reg.save_subscription('social', {'name': 'E', 'url': 'https://x.com/example'})
        reg.put_items(social, [{'url': grab.normalize(TWEET), 'title': '旧标题',
                                'body': BODY, 'published': ''}])
        with self.pipeline_patch():
            resume = self.post(TWEET).json()['resume']
        self.assertIn('大V观点', resume)
        self.assertIn('同步全部', resume)
        self.assertNotIn('写入知识库', resume)

    def test_the_state_endpoint_reads_this_item_and_starts_nothing(self):
        """前端收尾问的是**这一篇的事实**,不是那一轮的阶段结果:export/index
        双 ok 也可能一个字都没写到它身上(队列暂扣、别人的一轮、文件后来没了)。
        所以这个口子必须按条目回答,而且**只读** —— 轮询一次占一轮管道的话,
        面板每隔一秒就抢一次同步。
        """
        path = 'source/inbox/abc.md'
        with self.pipeline_patch():
            got = self.client.get('/api/grab/state', params={'url': PAGE}).json()
        self.assertEqual((got['known'], got['done'], got['owing']),
                         (False, False, '这条还没收进来'))
        grab.save({'url': PAGE, 'title': 'A', 'body': BODY, 'author': 'x',
                   'published': '', 'content_kind': 'content'})
        with self.pipeline_patch():
            got = self.client.get('/api/grab/state', params={'url': PAGE}).json()
        self.assertEqual((got['known'], got['done'], got['owing']),
                         (True, False, '还没写成 vault 里那一篇'))
        self.mark_exported(PAGE, path)
        self.index_doc(path)
        with self.pipeline_patch():
            got = self.client.get('/api/grab/state', params={'url': PAGE}).json()
        self.assertEqual((got['known'], got['done'], got['owing']), (True, True, ''))
        self.assertEqual((got['exported'], got['stored'], got['indexed']),
                         (True, True, True))
        # **第四档是这个口子存在的理由**:两个 hash 还相等(`exported` 为真)而
        # vault 里那一篇没了 —— 按库里那两个 hash 算就是「已完成」,而它搜不到。
        # 少了这一档,把 `done` 换成 `exported` 的改法整组全绿(实测红 0 条)。
        (config.VAULT_ROOT / path).unlink()
        with self.pipeline_patch():
            got = self.client.get('/api/grab/state', params={'url': PAGE}).json()
        self.assertEqual((got['done'], got['owing']), (False, 'vault 里那一篇不见了'))
        self.assertEqual((got['exported'], got['stored']), (True, False))
        # **只读**:这一问不许顺手把水位清掉(那是投递那颗按钮的事)。
        self.assertEqual(self.pending_export(), 0)
        self.assertEqual(self.runs, [])       # 四次轮询都没占管道

    def test_a_bad_duplicate_is_refetched_and_replaced(self):
        """**查重命中坏内容时要受控重抓一次 —— 这是它唯一的出路。**

        `put_items` 是 `DO NOTHING`:再投一次同一条链接一个字节都不会动那一行,
        于是一份错存的内容(以前那一版判据漏放的登录提示页)永远留在知识库里,
        而用户每次只得到同一句「已经收过了」。**这一档不看 `done`** —— 走完整条
        链的坏内容更该换。
        """
        path = 'source/inbox/abc.md'
        grab.save({'url': PAGE, 'title': 'Sign in to read', 'body': WALL,
                   'author': 'x', 'published': '', 'content_kind': 'content'})
        self.mark_exported(PAGE, path)
        self.index_doc(path)
        self.assertTrue(grab.known(PAGE)['done'])        # 后半条链全齐
        with patch.object(grab, 'request', return_value=html_resp(page_html())) as got, \
             self.pipeline_patch():
            data = self.post().json()
        self.assertEqual(got.call_count, 1)              # 真去重抓了一次
        self.assertEqual((data['duplicate'], data['content_ok'], data['replaced']),
                         (True, False, True))
        self.assertEqual(data['done'], False)            # 换完要重新走后半条链
        self.assertEqual(data['owing'], '收下的那一份看不出是正文')
        # **后果断言**:库里那一份真换了,而且后半条链被起了起来。
        self.assertIn('这是正文第一段', self.items()[0]['body'])
        self.assertTrue(self.pending_export())
        self.assertEqual(self.runs, [(['export', 'index', 'extract'], 'inbox')])

    def test_a_refetch_that_still_cannot_be_confirmed_changes_nothing(self):
        """重抓到的还是那张墙:**库里原样留着,不占管道,也不谎报换好了**。
        (元规则:0 行不是失败;拿不准别固化终态。)"""
        grab.save({'url': PAGE, 'title': 'Sign in to read', 'body': WALL,
                   'author': 'x', 'published': '', 'content_kind': 'content'})
        wall_page = page_html(title='Sign in', body=[
            'Sign in to continue reading. Subscribe to unlock this story. ' * 3] * 7)
        with patch.object(grab, 'request', return_value=html_resp(wall_page)), \
             self.pipeline_patch():
            data = self.post().json()
        # **「没换成」要如实交出来**(`replaced=False`):界面据它落可恢复的那一档,
        # 而不是照「没有 pipeline 字段」把这一次当成「早就走完整条链了」。
        self.assertEqual((data['duplicate'], data['replaced']), (True, False))
        self.assertIn('看不出是正文', data['detail'])
        # **这不是终态**:`content_ok` 为假要一路交到前端(它据此落可恢复的那一档,
        # 而不是照「没有 pipeline 字段」当成「早就走完整条链了」)。
        self.assertFalse(data['content_ok'])
        self.assertEqual(self.items()[0]['body'], WALL)   # 一个字节都没动
        self.assertEqual(self.runs, [])                             # 没什么可写,不占管道

    def test_an_explicit_refetch_recovers_what_the_gate_cannot_see(self):
        """**恢复出口不许架在那道会误判的质量判据上。**

        这里种进去的是一张**六道全过**的墙(`SOFT_WALL`:五段各不相同的会员说明,
        一个表单都没有),它从此 `content_ok` 恒真、`done` 也为真 —— 只开给
        「看不出是正文」那一档的恢复出口认不出它,于是原站后来恢复可读、用户一遍
        遍重投,得到的始终是同一句「已经在知识库里了」,HTTP 发出 0 次
        (第 3 轮审查实测)。所以人说「重新抓一次」就重抓,**不先问 content_ok**。
        """
        grab.save({'url': PAGE, 'title': 'Membership required', 'body': SOFT_WALL,
                   'author': 'Example', 'published': '', 'content_kind': 'content'})
        self.mark_exported(PAGE, 'source/inbox/a.md')
        self.index_doc('source/inbox/a.md')
        seen = grab.known(PAGE)
        # 先钉住前提:这一份在质量那一道眼里**没毛病**,整条链也走完了。
        self.assertEqual((seen['content_ok'], seen['done']), (True, True))
        # ① 反过来钉一边:不带 refetch 的重复投递照旧连 HTTP 都不发、照旧幂等。
        with patch.object(grab, 'request', side_effect=AssertionError('不许发请求')), \
             self.pipeline_patch():
            plain = self.post().json()
        self.assertEqual((plain['duplicate'], plain['done']), (True, True))
        self.assertIn('已经在知识库里了', plain['detail'])
        # ② 人明确要求重抓:真去抓一次,确认得了就把库里那一份换掉。
        with patch.object(grab, 'request',
                          return_value=html_resp(page_html())) as got, \
             self.pipeline_patch():
            data = self.post(refetch=True).json()
        self.assertEqual(got.call_count, 1)
        self.assertEqual((data['duplicate'], data['replaced']), (True, True))
        self.assertEqual(data['done'], False)        # 换完要重新走后半条链
        self.assertIn('你要求重新抓一次', data['detail'])
        # **后果断言**:库里那一份真换了,导出欠账跟着回来,后半条链被起了起来。
        self.assertIn('这是正文第一段', self.items()[0]['body'])
        self.assertNotIn('Sign in to continue', self.items()[0]['body'])
        self.assertEqual(self.pending_export(), 1)
        self.assertEqual(self.runs, [(['export', 'index', 'extract'], 'inbox')])

    def test_an_explicit_refetch_that_hits_a_wall_leaves_the_row_alone(self):
        """重抓撞上关卡:**库里原样留着,不占管道,也不说「已完成」。**

        (元规则:拿不准别固化终态。`replaced=False` 如实交到前端,它据此落
        可恢复的那一档 —— 那颗按钮还在,再点一次照旧会重抓。)"""
        grab.save({'url': PAGE, 'title': 'Membership required', 'body': SOFT_WALL,
                   'author': 'Example', 'published': '', 'content_kind': 'content'})
        wall = page_html(title='Just a moment...', body=[
            'Checking your browser before accessing this site. '
            'Please enable JavaScript and cookies to continue. ' * 4])
        with patch.object(grab, 'request', return_value=html_resp(wall)) as got, \
             self.pipeline_patch():
            data = self.post(refetch=True).json()
        self.assertEqual(got.call_count, 1)
        self.assertEqual((data['duplicate'], data['replaced']), (True, False))
        self.assertTrue(data['content_ok'])          # 这一份在质量那道眼里没毛病
        self.assertIn('你要求重新抓一次', data['detail'])
        self.assertEqual(self.items()[0]['body'], SOFT_WALL)   # 一个字节都没动
        self.assertEqual(self.runs, [])                        # 没什么可写,不占管道

    def test_a_good_duplicate_still_costs_no_request(self):
        """**反过来钉一边:内容没问题的重复投递照旧连 HTTP 都不发。** 受控重抓
        只开给「看不出是正文」那一档 —— 不然每次重复投递都要去对方站点跑一趟。"""
        grab.save({'url': PAGE, 'title': 'A', 'body': BODY, 'author': 'x',
                   'published': '', 'content_kind': 'content'})
        with patch.object(grab, 'request', side_effect=AssertionError('不许发请求')), \
             self.pipeline_patch():
            data = self.post().json()
        self.assertEqual((data['duplicate'], data['content_ok']), (True, True))
        self.assertEqual(self.runs, [(['export', 'index', 'extract'], 'inbox')])

    def test_the_state_endpoint_tells_bad_content_from_a_missing_step(self):
        """`/api/grab/state` 也要把这件事实交出去:界面据此说「收下的内容看不出
        是正文,再投一次会重抓替换」,而不是「已写入知识库,搜得到了」。"""
        path = 'source/inbox/abc.md'
        grab.save({'url': PAGE, 'title': 'Sign in to read', 'body': WALL,
                   'author': 'x', 'published': '', 'content_kind': 'content'})
        self.mark_exported(PAGE, path)
        self.index_doc(path)
        got = self.client.get('/api/grab/state', params={'url': PAGE}).json()
        self.assertEqual((got['done'], got['content_ok']), (True, False))
        self.assertEqual(got['owing'], '收下的那一份看不出是正文')


if __name__ == '__main__':
    unittest.main()


if __name__ == '__main__':
    unittest.main()
