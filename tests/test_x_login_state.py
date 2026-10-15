# -*- coding: utf-8 -*-
"""判断「这一轮验证过的登录态真的保存下来了」时,**被比的到底是哪一份状态**。

从 `tests/test_x_login_persist.py` 搬出来的(那份到了 700 行上限)。那边钉的是
「范围由浏览器卡」「当场能用证不了下次还在」「账本不比 profile 旧」这三条**结构**
不变量;这里钉的是同一条链上的另一件事 —— 不变量成立与否全看**那份状态怎么投影**:

1. 比的是「验证结束那一刻浏览器里的状态」,不是我们交进去的输入(浏览器会改写它);
2. 投影要带上「哪一条 + 哪一代 + 还能用多久 + 还读得到／发得出去」,少一维就有一整
   类「局部相同」被当成「同一份状态」;
3. 完整的那一组都要在,而且盘上不许多出来;
4. 已经查明的验证失败是主结论,保存差异接在后面。

这四条各被一轮审查拦下来过一次,而且**每一次都是前一次只修了一半**:判据从比名字 →
比值 → 比有效期 → 比读写属性,每一步都是同一个「局部相同即证明完整保存」的类问题。
所以新加一维之前先问:它影不影响**下一次还用得上**。影响就必须在投影里。

摆盘共用 `ImportBase`;`FakeChrome` 的 `live`/`persisted` 两个口子分别代表「验证结束
那一刻浏览器里的状态」和「盘上的状态」,它们必须能分开说,否则这一组压根碰不到分岔。
"""

from __future__ import annotations

import json
import time
import unittest

from kb.feeds import x_login_import
from tests.x_login_fixture import ImportBase, cookie, logged_in

class CredentialGenerationTests(ImportBase):
    """不变量 ②的那一半:判据是「**这一代**在不在盘上」,不是「有没有同名的东西」。

    第 1 轮要求的不变量是「对外宣布跨重启成功的那一代凭证已持久化」。上一轮加了
    重开,但压成了名字集合 —— 于是**重导入最常见的那一档**漏掉了:cookie 过期后
    用户再点一次,目标 profile 里早就躺着同名旧凭证,一次什么都没存进去的导入照旧
    被判成 `ok`(第 2 轮审查拿真浏览器实测:盘上留的是 `synthetic-old-*`)。
    """

    def test_an_old_cookie_with_the_same_name_does_not_pass_for_the_new_one(self):
        """**报告第 1 条本体。** 名字在、值是上一次那份 → 这一轮没存上,不是成功。"""
        new = [cookie('auth_token', value='new-AUTH'), cookie('ct0', value='new-CT0')]
        old = [cookie('auth_token', value='old-AUTH'), cookie('ct0', value='old-CT0')]
        self.wire(cookies=new, persisted=old, clean=False)
        ok, detail = x_login_import.import_login_state(self.profile)
        self.assertFalse(ok)
        self.assertEqual('unsaved',
                         json.loads(self.ledger.read_text('utf-8'))['verdict'])
        self.assertNotIn('重新打开确认过', detail)

    def test_a_stale_generation_is_named_as_stale_not_as_a_session_cookie(self):
        """**三档原因的下一步不一样,所以不许折叠。** 「盘上还是旧值」要让用户知道
        他此刻用的仍是过期那份;「会话 cookie」则是再导一百次也一样。
        """
        self.wire(cookies=[cookie('auth_token', value='new'), cookie('ct0', value='new')],
                  persisted=[cookie('auth_token', value='old'), cookie('ct0', value='old')])
        _, detail = x_login_import.import_login_state(self.profile)
        self.assertIn('跟验证通过时那一份不一致', detail)
        self.assertNotIn('会话 cookie', detail)

    def test_one_stale_essential_is_enough(self):
        """`auth_token` 这一代存上了、`ct0` 还是旧的 → 照旧不算成功。"""
        self.wire(cookies=[cookie('auth_token', value='new'), cookie('ct0', value='new')],
                  persisted=[cookie('auth_token', value='new'), cookie('ct0', value='old')])
        ok, detail = x_login_import.import_login_state(self.profile)
        self.assertFalse(ok)
        self.assertIn('ct0', detail)
        self.assertNotIn('auth_token', detail)     # 它没问题,别一起点名

    def test_the_same_values_landing_again_is_a_success(self):
        """**反过来钉一边。** 同名**同值**(连着导两次、cookie 还没变)照旧算成功 ——
        指纹判的是「这一代在不在」,不是「值有没有变过」。
        """
        same = [cookie('auth_token', value='v1'), cookie('ct0', value='v2')]
        self.wire(cookies=same, persisted=[dict(c) for c in same])
        ok, detail = x_login_import.import_login_state(self.profile)
        self.assertTrue(ok)
        self.assertIn('还留在磁盘上、X 也还认这份登录态', detail)

    def test_the_fingerprint_separates_identity_from_generation(self):
        """指纹要同时分清「是哪一条」和「是哪一代」。"""
        base = cookie('auth_token', value='V')
        self.assertEqual(x_login_import.fingerprint(base),
                         x_login_import.fingerprint(dict(base)))
        for field, other in (('value', 'V2'), ('domain', 'x.com'), ('path', '/i'),
                             ('partitionKey', {'topLevelSite': 'https://x.com'})):
            with self.subTest(field=field):
                self.assertNotEqual(x_login_import.fingerprint(base),
                                    x_login_import.fingerprint(dict(base, **{field: other})))

    def test_the_fingerprint_never_carries_the_value_itself(self):
        """它会在函数之间传递,所以取摘要而不是留值 —— 万一哪天谁把它写进日志,
        漏出去的也只是个摘要。"""
        got = x_login_import.fingerprint(cookie('auth_token', value='SECRET-TOKEN'))
        self.assertNotIn('SECRET-TOKEN', got)
        self.assertNotIn('SECRET-TOKEN', json.dumps(got))

    def test_the_ledger_still_carries_no_values(self):
        """**反过来钉一边的那一半**:加了指纹判据不许把值带进明文账本。"""
        self.wire(cookies=[cookie('auth_token', value='SECRET-A'),
                           cookie('ct0', value='SECRET-B')])
        x_login_import.import_login_state(self.profile)
        raw = self.ledger.read_text('utf-8')
        for secret in ('SECRET-A', 'SECRET-B'):
            self.assertNotIn(secret, raw)


class WhatTheBrowserAcceptedTests(ImportBase):
    """不变量 ②剩下的那一半:被比的那一方必须是**验证结束那一刻浏览器里的状态**。

    前两轮都拿「我们交进去的那份输入」当被比的一方 —— 那是条假判据。三种形态都在
    2026-10-15 的真浏览器上复现过(合成 cookie、临时 profile):
    ① 交 5 年有效期进去,盘上存的是 **400 天**(Chrome 写入时削顶),而 X 的
       `auth_token` 正是多年有效期 —— 旧判据把**每一次**导入都报成「没存上」;
    ② 验证那一下导航到 `x.com/home`,站点自己换发 `ct0`(还会下 `guest_id`、
       `__cf_bm`),旧判据把一次**完整存好了**的导入报成「ct0 还是旧值」;
    ③ 同名多条只留住一条也算过(旧判据 `mine & prints` 是「任意一条对上就行」)。
    ①②是假阴性:用户照着那句话反复重导,而每一次都「失败」。③是假阳性。
    一次实测跑下来,旧判据报 `stale: [auth_token, ct0]`,新判据报空。
    """

    def test_an_expiry_the_browser_clamped_is_still_a_success(self):
        """①交 5 年、盘上 400 天 —— 这是浏览器允许的表示差异,不是没存上。"""
        far, clamped = time.time() + 5 * 365 * 86400, time.time() + 400 * 86400
        got = [cookie('auth_token', expires=clamped), cookie('ct0', expires=clamped)]
        self.wire(cookies=[cookie('auth_token', expires=far),
                           cookie('ct0', expires=far)],
                  live=got, persisted=[dict(c) for c in got])
        ok, detail = x_login_import.import_login_state(self.profile)
        self.assertTrue(ok, detail)
        self.assertIn('还留在磁盘上、X 也还认这份登录态', detail)

    def test_a_credential_the_site_rotated_during_verification_is_a_success(self):
        """②验证期间站点换发了 `ct0`,盘上是**新**那份 —— 成功。

        这一档不是假想:实测打开 `x.com/home` 时 X 自己就会下新 cookie。
        """
        got = [cookie('auth_token', value='A'), cookie('ct0', value='ROTATED')]
        self.wire(cookies=[cookie('auth_token', value='A'),
                           cookie('ct0', value='BEFORE')],
                  live=got, persisted=[dict(c) for c in got])
        ok, detail = x_login_import.import_login_state(self.profile)
        self.assertTrue(ok, detail)

    def test_refreshing_only_the_expiry_must_still_be_proven(self):
        """**反过来钉一边,而且这一档正是这个功能存在的理由。**

        「cookie 过期了再导一次」= 同名、很可能同值,**只有有效期是新的**。指纹少了
        有效期这一维,一次「续期压根没落盘」就跟成功长得一模一样:盘上那条过几十秒
        自己就没了,而面板说「以后不用再点弹窗了」。
        """
        soon, later = time.time() + 30, time.time() + 86400
        self.wire(cookies=[cookie('auth_token', expires=later),
                           cookie('ct0', expires=later)],
                  live=[cookie('auth_token', expires=later),
                        cookie('ct0', expires=later)],
                  persisted=[cookie('auth_token', expires=soon),
                             cookie('ct0', expires=later)])
        ok, detail = x_login_import.import_login_state(self.profile)
        self.assertFalse(ok)
        self.assertIn('auth_token', detail)
        self.assertIn('跟验证通过时那一份不一致', detail)
        self.assertEqual('unsaved',
                         json.loads(self.ledger.read_text('utf-8'))['verdict'])

    def test_losing_one_of_two_cookies_with_the_same_name_is_not_a_success(self):
        """③同一个名字在浏览器里有两条(`x.com` 和 `.x.com` 是两条不同的 cookie),
        盘上只留住一条 → 不算成功。站点依赖的是它验证时实际有的那一组。
        """
        both = [cookie('auth_token', domain='.x.com'),
                cookie('auth_token', domain='x.com'), cookie('ct0')]
        self.wire(cookies=both, live=[dict(c) for c in both],
                  persisted=[cookie('auth_token', domain='x.com'), cookie('ct0')])
        ok, detail = x_login_import.import_login_state(self.profile)
        self.assertFalse(ok)
        self.assertIn('auth_token', detail)

    def test_a_credential_gone_by_the_end_of_verification_is_named_as_that(self):
        """验证刚过那一刻浏览器里就没有 `ct0` 了 —— 这跟「存盘没成」是两件事,
        下一步也不一样(重导一次没用,得去看那个账号的登录态),所以单独一档。
        """
        self.wire(cookies=logged_in(), live=[cookie('auth_token')],
                  persisted=[cookie('auth_token')])
        ok, detail = x_login_import.import_login_state(self.profile)
        self.assertFalse(ok)
        self.assertIn('ct0', detail)
        self.assertIn('登录态当时正在变动', detail)
        self.assertNotIn('会话 cookie', detail)

    def test_other_cookies_may_come_and_go(self):
        """**反过来钉一边。** 关键凭证之外的 cookie 增减不影响结论 —— 浏览器和站点
        一直在动它们(实测 `guest_id`、`__cf_bm` 就是验证期间新下的),把它们也
        算进判据等于让这个功能永远「失败」。
        """
        self.wire(cookies=logged_in(),
                  live=[cookie('auth_token'), cookie('ct0'), cookie('guest_id')],
                  persisted=[cookie('auth_token'), cookie('ct0'),
                             cookie('__cf_bm', value='other')])
        ok, detail = x_login_import.import_login_state(self.profile)
        self.assertTrue(ok, detail)

    def test_the_snapshot_comes_from_the_browser_not_from_our_input(self):
        """判据的结构:`install` 在**关掉浏览器之前**又读了一次 cookie。

        上面那几条是从结论反推的;这一条直接钉住那次读在不在、在哪一边。少了它,
        把快照换回 `cookies` 入参的改动能让 ①②③ 全红,却看不出改坏了什么结构。
        """
        _, chrome = self.wire()
        x_login_import.import_login_state(self.profile)
        reads = [i for i, (m, _p) in enumerate(chrome.calls)
                 if m == 'Storage.getCookies']
        wrote = [i for i, (m, _p) in enumerate(chrome.calls)
                 if m == 'Storage.setCookies']
        # 四次读:当场验完一次、重开时读盘一次、重开验完再一次、最后只读盘一次
        self.assertEqual(4, len(reads), chrome.calls)
        self.assertLess(wrote[0], reads[0])                  # 写在前,快照在后
        self.assertEqual(3, chrome.exited)                   # 三次开机都收了尾


class UsableStateTests(ImportBase):
    """不变量 ②最后一块:**比的是「下一次还用得上的那一份」,不是几个字段相同。**

    前三轮把被比的一方从输入挪到了浏览器快照、把有效期和同名多条补了进去,但指纹
    仍主动排除 `httpOnly`/`secure`/`sameSite`,理由写的是「不是身份也不影响还能用
    多久」—— 那句话不等于「不影响使用」。这三个决定这条 cookie 下一次**到不到得了
    该到的地方**:`httpOnly` 决定页面脚本还读不读得到 `ct0`(X 前端靠
    `document.cookie` 取它拼 `x-csrf-token`),`sameSite` 决定跨站请求带不带它,
    `secure` 决定它只跟 HTTPS 走。而 `_PARAMS` 写入时本来就一个不少地带着它们。

    2026-10-15 第 4 轮审查在真浏览器上实测:同一个 origin 下验证时
    `document.cookie` 读得到 `ct0`,重开后是空串(盘上那条是 `httpOnly=true` 的旧
    的),而名/值/有效期/session 全同 —— 产品判 `ok` 并声称「重新打开确认过」。
    本轮自己复跑同一形态:新判据报 `stale: [ct0]`,把 `_PRINT_FIELDS` 换回上一轮
    那六个字段则报 `{}`。
    """

    def _attr(self, field, verified_value, disk_value):
        """关键凭证只有这一个属性不同时,算不算「存上了」。"""
        self.wire(cookies=[cookie('auth_token'), cookie('ct0', **{field: verified_value})],
                  live=[cookie('auth_token'), cookie('ct0', **{field: verified_value})],
                  persisted=[cookie('auth_token'), cookie('ct0', **{field: disk_value})])
        return x_login_import.import_login_state(self.profile)

    def test_a_credential_that_scripts_can_no_longer_read_is_not_saved(self):
        """**报告第 4 轮本体。** 验证时 `ct0` 脚本读得到,盘上那条是 httpOnly 的。"""
        ok, detail = self._attr('httpOnly', False, True)
        self.assertFalse(ok)
        self.assertIn('ct0', detail)
        self.assertIn('属性变了', detail)
        self.assertEqual('unsaved',
                         json.loads(self.ledger.read_text('utf-8'))['verdict'])

    def test_the_other_two_sending_attributes_count_the_same_way(self):
        """**同类实例一起钉**:跨站带不带、只不只跟 HTTPS 走,同样决定下一次能不能用。"""
        for field, verified_value, disk_value in (('sameSite', 'None', 'Strict'),
                                                 ('secure', True, False)):
            with self.subTest(field=field):
                ok, detail = self._attr(field, verified_value, disk_value)
                self.assertFalse(ok)
                self.assertIn('ct0', detail)

    def test_the_same_attributes_landing_again_is_a_success(self):
        """**反过来钉一边。** 三个属性都一样照旧算成功 —— 这一条是在挡「多比一个字段
        就多一种永远判没存上」那个风险(实测三个属性经写入→正常关→重开逐位原样
        往返,连「没指定」都原样缺席)。
        """
        ok, detail = self._attr('httpOnly', True, True)
        self.assertTrue(ok, detail)
        self.assertIn('还留在磁盘上、X 也还认这份登录态', detail)

    def test_an_unspecified_same_site_is_not_the_string_none(self):
        """`sameSite` 的合法取值里**正好有一个叫 `None`**,而「没指定」要么是键缺席、
        要么是键在值为 `null`。**后一种会跟字符串 `'None'` 撞上** —— `str(None)`
        就是 `'None'`,于是「跨站请求不带它」和「带它」两种状态指纹相同。

        实测 CDP 在没指定时是**整个键缺席**(不是回 null),所以这一道今天是备手:
        键缺席那半 `cookie.get(k, '')` 本来也分得开,会撞的是值为 `null` 那半 ——
        哪天 CDP 改成显式回 null、或者有调用方自己拼这种字典,它就是唯一那道。
        **判据落在指纹本身**,因为这两种状态在上面那组用例里长得一样。
        """
        explicit = x_login_import.fingerprint(cookie('ct0', sameSite='None'))
        self.assertNotEqual(explicit,                       # 键在、值是 null
                            x_login_import.fingerprint(cookie('ct0', sameSite=None)))
        bare = cookie('ct0')
        bare.pop('sameSite')
        self.assertNotEqual(explicit, x_login_import.fingerprint(bare))   # 键缺席
        # 反过来钉一边:两种「没指定」的写法算同一份,别把它判成换了一代
        self.assertEqual(x_login_import.fingerprint(bare),
                         x_login_import.fingerprint(cookie('ct0', sameSite=None)))

    def test_every_attribute_we_write_is_also_verified(self):
        """**口径只有一份:写进去的每个字段都要进核验。**

        这一族问题被拦下来过四轮(比名字 → 比值 → 比有效期 → 比读写属性),每一次都是
        同一句话:「我们写进去了、核验时却没看」。上面那几条用例钉的是**今天**漏的那
        几个字段,挡不住**明天**往 `_PARAMS` 加一个字段的人 —— 他没有理由想到还要去
        改另一张手写清单,而漏掉的后果是静默判「存上了」。所以判据落在**两张清单的
        结构关系**上,不在某一个字段名上。
        """
        missing = [k for k in x_login_import._PARAMS
                   if k not in x_login_import._PRINT_FIELDS]
        self.assertEqual([], missing,
                         f'这几个字段写进了 profile 却不参与核验:{missing}')
        # 持久语义不在 `_PARAMS` 里(`expires` 由 `_param` 单独处理、`session` 是
        # 浏览器自己报的),所以单独钉一次。
        for field in ('expires', 'session'):
            self.assertIn(field, x_login_import._PRINT_FIELDS)

    def test_an_extra_credential_on_disk_is_not_the_verified_state(self):
        """**子集不等于同一份状态。** 上一次导入留下的同名条目(domain/path 不同就是
        另一条 cookie,`Storage.setCookies` 按 name+domain+path 覆盖,压根不会动它)
        会跟着请求一起发出去;到底哪条被站点选中我们答不了,答不了就不许宣布成功。
        """
        both = [cookie('auth_token'), cookie('ct0')]
        self.wire(cookies=both, live=[dict(c) for c in both],
                  persisted=[*[dict(c) for c in both],
                             cookie('ct0', domain='x.com', value='上一次留下的')])
        ok, detail = x_login_import.import_login_state(self.profile)
        self.assertFalse(ok)
        self.assertIn('盘上多出了', detail)
        self.assertIn('ct0', detail)

    def test_an_extra_credential_present_at_verification_too_is_fine(self):
        """**反过来钉一边**:验证时本来就有那两条、盘上一样有 → 不算多出来。"""
        three = [cookie('auth_token'), cookie('ct0'),
                 cookie('ct0', domain='x.com', value='另一条')]
        self.wire(cookies=three, live=[dict(c) for c in three],
                  persisted=[dict(c) for c in three])
        ok, detail = x_login_import.import_login_state(self.profile)
        self.assertTrue(ok, detail)


class VerificationFailureKeepsThePointTests(ImportBase):
    """**已经查明的验证失败是主结论,保存差异接在后面。**

    原来只要重开对不上就无条件改成 `unsaved`,而 `unsaved` 那句文案无条件写「当场
    也能用」。站点拒绝登录时它多半顺手清掉了凭证,于是盘上确实也对不上 —— 一次
    **登录被拒**就被报成一次**存盘故障**:真原因没了,还多了一句假话。两件事的下
    一步完全不同(前者该去点「打开登录窗口」,后者是再导一次)。
    """

    def test_a_rejected_login_is_not_renamed_into_a_save_failure(self):
        self.wire(verdict='login', cookies=logged_in(), live=[], persisted=[])
        ok, detail = x_login_import.import_login_state(self.profile)
        self.assertFalse(ok)
        self.assertEqual('login',
                         json.loads(self.ledger.read_text('utf-8'))['verdict'])
        self.assertIn('判成未登录', detail)
        self.assertNotIn('当场也能用', detail)

    def test_the_save_difference_is_still_said_just_not_first(self):
        """不丢信息:保存差异照旧说出来,只是不占主结论。"""
        self.wire(verdict='login', cookies=logged_in(), live=[], persisted=[])
        _, detail = x_login_import.import_login_state(self.profile)
        self.assertLess(detail.index('判成未登录'), detail.index('重新打开'))

    def test_every_unsuccessful_verdict_keeps_its_own_reason(self):
        """**同类实例一起钉**:五档验证结果都不许被 `unsaved` 顶掉。"""
        for verdict in ('login', 'limited', 'not-loaded', 'no-tweets'):
            with self.subTest(verdict=verdict):
                self.wire(verdict=verdict, cookies=logged_in(), live=[], persisted=[])
                x_login_import.import_login_state(self.profile)
                self.assertEqual(verdict,
                                 json.loads(self.ledger.read_text('utf-8'))['verdict'])

    def test_a_verified_run_that_did_not_persist_is_still_unsaved(self):
        """**反过来钉一边**:验证确实通过、只是没存上 → 照旧是 `unsaved`,而且
        「当场也能用」这句话在这一档上是有依据的。
        """
        self.wire(verdict='ok', cookies=logged_in(), live=logged_in(), persisted=[])
        ok, detail = x_login_import.import_login_state(self.profile)
        self.assertFalse(ok)
        self.assertEqual('unsaved',
                         json.loads(self.ledger.read_text('utf-8'))['verdict'])
        self.assertIn('当场也能用', detail)


class ThePathBlindSpotTests(ImportBase):
    """投影那一族的第五档:**漏掉的不是某一维,是整整一条 cookie**。

    前四档漏的都是「同一条的某一维」(值、有效期、读写属性)。这一档漏的是**条目
    本身**:按 URL 读只交得出「对那几个地址适用的」,`ct0@/i/api` 整条不在里面 ——
    **两次读同时漏掉它**,于是「集合相等」成立,而验证页真正用的正是它。审查实测:
    第二次加载把它换发成会话 cookie,正常关闭之后盘上没有,三次读一次都看不见,
    产品报 `ok` 并承诺「以后不用点弹窗」,下一次打开同一个 profile 分类器判 `login`。

    出路不是再补一个 URL(路径不可枚举),而是**目标 profile 那一侧整杯读**
    (`x_login_import.read_all`;用户那个浏览器那一侧的射程一点都不许放宽)。
    所以这一组的判据落在**结果**上:路径受限的那条没留下 → 不许报成功。
    """

    def deep(self, **extra):
        """一份「验证页真正用的是路径受限那条」的登录态。"""
        return [cookie('auth_token'), cookie('ct0'),
                cookie('ct0', path='/i/api', value='path-ct0'), *extra.get('more', [])]

    def test_a_path_scoped_credential_lost_at_the_last_close_is_not_ok(self):
        """报告那条反例:最后那次验证用的 `ct0@/i/api` 关掉之后没留在盘上。"""
        self.wire(cookies=logged_in(), live=self.deep(), persisted=self.deep(),
                  live2=self.deep(), settled=logged_in())
        ok, detail = x_login_import.import_login_state(self.profile)
        self.assertFalse(ok, detail)
        self.assertEqual('unsaved',
                         json.loads(self.ledger.read_text('utf-8'))['verdict'])
        # 说的必须是「最后那次验证用的那一份」,不是「这一轮写进去的那一份」——
        # 后者好好的,指错方向会让人反复重导(口径见 `x_login_words._detail`)。
        self.assertIn('最后那次验证', detail)

    def test_the_same_credential_still_on_disk_is_ok(self):
        """**反过来钉一边**:同一条路径受限的凭证确实留在盘上 → 照旧 `ok`。

        少了这一条,上面那条用「整杯读之后一律判失败」也能绿,而那是把功能判死。
        """
        self.wire(cookies=logged_in(), live=self.deep(), persisted=self.deep(),
                  live2=self.deep(), settled=self.deep())
        ok, detail = x_login_import.import_login_state(self.profile)
        self.assertTrue(ok, detail)
        self.assertEqual('ok', json.loads(self.ledger.read_text('utf-8'))['verdict'])

    def test_the_first_close_is_covered_by_the_same_read(self):
        """**第一道闸也要没有盲区**:当场验用的是 `ct0@/i/api`,第一次关掉就没了。

        两道闸各有自己的回归(元规则「两份实现各有自己的回归」)—— 这一条不走到
        第三次开机,红的是「写进去了但重开之后不在」那一档。
        """
        self.wire(cookies=logged_in(), live=self.deep(), persisted=logged_in(),
                  live2=logged_in(), settled=logged_in())
        ok, detail = x_login_import.import_login_state(self.profile)
        self.assertFalse(ok, detail)
        self.assertEqual('unsaved',
                         json.loads(self.ledger.read_text('utf-8'))['verdict'])
        self.assertNotIn('最后那次验证', detail)

    def test_the_target_side_read_hands_the_browser_no_url_filter(self):
        """判据的结构:目标侧那几次读**一个 `urls` 都不许带**。

        带上就等于路径盲区又回来了,而它静默的形态正是「两侧同时漏掉」——
        后果先断言(上面三条),这一条钉的是「为什么它们能成立」。
        """
        _browser, chrome = self.wire(cookies=logged_in())
        x_login_import.import_login_state(self.profile)
        jar = [params for method, params in chrome.calls
               if method == 'Storage.getCookies']
        self.assertEqual(4, len(jar), chrome.calls)
        for params in jar:
            self.assertEqual({}, params)

if __name__ == '__main__':
    unittest.main()
