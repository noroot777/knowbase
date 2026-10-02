"""原文不可用的页面判定:哪几种页面算「再也读不回来」,哪几种只是这次没抓到。

判错的代价不对称,两个方向都钉住:
- 漏判(不可用被当临时失败)→ 每天重试一次、永远停在面板的「正文待重试」;
- 误判(临时故障被当不可用)→ 退避拉到一周,一篇本可抓到的文章一周才试一次。

四段 HTML 是 2026-09-29 从真实页面上取的骨架:删除页的提示在
div.weui-msg__title.warn 里,违规/侵权页在 h3.weui-msg__title 里(原来
只认前者,后两种一直落在 request_failed 上),隐私页则一句提示都没有、
只能认入口 bundle。
"""

from __future__ import annotations

import unittest
from unittest.mock import Mock, patch

from src.utils.helpers import message_unavailable
from src.utils.wechat_body import (
    FETCH_ERROR,
    SHARE_ONLY_HINT,
    body_unavailable,
    fetch_article_body_full,
)

DELETED = """<html><body><div class="weui-msg">
  <div class="weui-msg__text-area">
    <div class="weui-msg__title warn">该内容已被发布者删除</div>
  </div></div></body></html>"""

VIOLATION = """<html><body><div class="weui-msg">
  <div class="weui-msg__text-area">
    <h3 class="weui-msg__title">
                        此内容因违规无法查看
                    </h3>
  </div></div></body></html>"""

COMPLAINT = """<html><body><div class="weui-msg">
  <div class="weui-msg__text-area">
    <h3 class="weui-msg__title">此内容被投诉且经审核涉嫌侵权，无法查看。</h3>
    <div class="weui-msg__custom-desc"><p class="weui-form-preview__value">标题</p></div>
  </div></div></body></html>"""

# 抓不到正文但页面没给任何提示(实测 GitHubStore 那批失效链接就是这个形态):
# 空壳页、反爬页、半截响应都归这里,只能当临时失败。
BLANK = """<html><head><style>.weui-msg__title{ word-break: break-word; }</style></head>
  <body><div id="js_pc_qr_code"></div></body></html>"""

# 隐私可见:服务端只回空壳,「根据作者隐私设置,无法查看该内容」是前端渲染的,
# HTML 里没有这句话,只有入口 bundle 从 appmsg.* 换成了 private.*。
PRIVATE = """<html><body><div id="app"></div>
  <script type="module" crossorigin
    src="//res.wx.qq.com/mmbizappmsg/zh_CN/htmledition/js/assets/private.muko7m9qfafd29b1.js"></script>
  <script nomodule crossorigin id="vite-legacy-entry"
    data-src="//res.wx.qq.com/mmbizappmsg/zh_CN/htmledition/js/assets/private-legacy.muko7m9q526de422.js"></script>
  </body></html>"""

# 正常文章页:入口是 appmsg.*,正文在 rich_media_content 里,不能判成隐私
NORMAL = """<html><body><div id="js_article">
  <div class="rich_media_content" id="js_content"><p>正文段落</p></div>
  <script type="module" crossorigin
    src="//res.wx.qq.com/mmbizappmsg/zh_CN/htmledition/js/assets/appmsg.muko7m9q62bcebb0.js"></script>
  </body></html>"""


class PageHintTests(unittest.TestCase):
    def test_deleted_violation_and_complaint_are_unavailable(self):
        for html, expected in ((DELETED, "该内容已被发布者删除"),
                               (VIOLATION, "此内容因违规无法查看"),
                               (COMPLAINT, "此内容被投诉且经审核涉嫌侵权，无法查看。")):
            with self.subTest(expected=expected):
                self.assertEqual(expected, message_unavailable(response=html))

    def test_private_shell_is_unavailable_although_page_says_nothing(self):
        self.assertEqual("根据作者隐私设置，无法查看该内容",
                         message_unavailable(response=PRIVATE))

    def test_page_without_hint_is_not_unavailable(self):
        # 样式表里也有 .weui-msg__title 这个串,按文本匹配不能把它算成提示;
        # 正常文章页的入口是 appmsg.*,也不能被 private 那条规则蹭到
        for html in (BLANK, NORMAL, "", "<html></html>", "not html at all"):
            with self.subTest(html=html[:20]):
                self.assertEqual("", message_unavailable(response=html))

    def test_the_word_private_in_body_text_is_not_a_verdict(self):
        # 判据是入口资源路径,不是正文里出现 private 这个词
        html = NORMAL.replace("<p>正文段落</p>",
                              "<p>private repo 的 assets/private.js 怎么配</p>")
        self.assertEqual("", message_unavailable(response=html))


class SentinelTests(unittest.TestCase):
    def test_only_non_fetch_error_strings_mean_unavailable(self):
        self.assertEqual("该内容已被发布者删除", body_unavailable("该内容已被发布者删除"))
        self.assertEqual("此内容因违规无法查看", body_unavailable("此内容因违规无法查看"))
        self.assertEqual("", body_unavailable(FETCH_ERROR))

    def test_real_body_is_never_unavailable(self):
        self.assertEqual("", body_unavailable(["正文段落"]))
        self.assertEqual("", body_unavailable([]))


# 分享型图文:本体的正文容器是空的 original_panel_content,真内容在卡片
# 指向的原文里。抓这一篇拿到的就是下面这张页面,正文一个字都没有。
SHARE_CARD = """<html><body><div id="js_article" class="rich_media">
  <div class="share_media">
    <div class="original_panel appmsg_card_context">
      <div class="original_panel_content" id="js_content" aria-hidden="true"></div>
      <div class="original_panel_tool" aria-hidden="true">
        <span data-url="https://mp.weixin.qq.com/s?__biz=OTHER&amp;mid=2&amp;idx=1&amp;sn=x"
              class="weui-link" id="js_share_source">阅读全文</span>
      </div>
    </div>
  </div></div></body></html>"""

# 卡片原文这次读不到:风控验证页。页面里没有任何 weui 提示,也没有正文。
CAPTCHA = """<html><body><div class="weui-msg"><div id="tips"></div></div>
  <script>window.cgiData = { poc_token : "HKhSu2qj", cap_appid : "2003810213" };
  seajs.use('secitptpage/template/verify.js')</script></body></html>"""

ORIGIN_BODY = """<html><body><div class="rich_media_content" id="js_content">
  <p>原文正文</p></div></body></html>"""


class ShareCardTests(unittest.TestCase):
    """本体没正文、只有一张分享卡片的图文,顺着卡片抓原文的那条路。"""

    def fetch(self, *pages):
        # _fetch_page 里两次 requests.get:先本体,再卡片指向的原文
        with patch("src.utils.wechat_body.requests.get",
                   side_effect=[Mock(text=p) for p in pages]) as get, \
             patch("src.utils.wechat_body._request_headers", return_value={}):
            text, html, _ = fetch_article_body_full("https://mp.weixin.qq.com/s?x=1")
        return text, html, get.call_count

    def test_unreadable_origin_is_unavailable_not_endless_retry(self):
        # 原文被风控挡住 → 这篇没有正文可抓,而且下次来还是同一个空节点:
        # 落「不可用」按周复查,不要当临时故障每天重试(也不要重试 3 次才罢休)
        text, html, calls = self.fetch(SHARE_CARD, CAPTCHA)
        self.assertEqual(SHARE_ONLY_HINT, text)
        self.assertEqual(SHARE_ONLY_HINT, body_unavailable(text))
        self.assertIsNone(html)
        self.assertEqual(2, calls)

    def test_readable_origin_still_yields_its_body(self):
        # 原文抓得到时照常入库 —— 这条路不能被上面那条判定顺手掐掉
        text, html, calls = self.fetch(SHARE_CARD, ORIGIN_BODY)
        self.assertEqual(["原文正文"], text)
        self.assertIn("原文正文", html or "")
        self.assertEqual(2, calls)

    def test_deleted_origin_reports_the_real_reason(self):
        # 原文明确被删时按原文的原因记,比笼统的「分享读不到」准
        text, _html, _calls = self.fetch(SHARE_CARD, DELETED)
        self.assertEqual("该内容已被发布者删除", text)

    def test_plain_page_without_card_keeps_retrying(self):
        # 没有卡片的普通页面抓不到正文,仍然是临时失败(重试到上限)
        with patch("src.utils.wechat_body.requests.get",
                   return_value=Mock(text=BLANK)), \
             patch("src.utils.wechat_body._request_headers", return_value={}), \
             patch("src.utils.wechat_body.time.sleep"):
            text, _html, _ = fetch_article_body_full("https://mp.weixin.qq.com/s?x=1")
        self.assertEqual(FETCH_ERROR, text)


if __name__ == "__main__":
    unittest.main()
