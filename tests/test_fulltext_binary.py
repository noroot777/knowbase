# -*- coding: utf-8 -*-
"""补抓时「原文链接后面不是网页」那一组(kb/feeds/readable.py 的判定与解码)。

**按形状单独一个文件**:它要自带一份结构有效的 PDF、一张宽编码样本表,还要
逐种写法端到端走一遍补抓 —— 和 `test_feed_fulltext.py` 挤在一起是 737 行,
越过 700 行上限(2026-10-09 第 4 轮审查指的就是这个)。摆盘(`FullTextCase`、
`PAGE`、`SHELL`)仍复用那一份,各写一份的话两边的前提会各漂各的;`raw` 和
`pdf_bytes` 只有这一组用,跟着搬过来。

被测的那件事:**可以不支持 PDF,但不能把"没读到论文"说成"已取得正文"** ——
后者不可恢复,那一条再也不会重试。放行判据(声明的类型 / 文件头 / 解一次之后
NUL 还在不在)和解码器(BOM → HTTP charset → meta)是同一份事实的两面,两边
一漂就是「错误覆盖 + 成功退出队列」。
"""
import unittest
import zlib
from unittest.mock import Mock, patch

from kb.feeds import fulltext, readable, registry as reg, sync, translate
from kb.feeds.fetching import Response
from tests.test_feed_fulltext import PAGE, SHELL, FullTextCase


def raw(body: bytes, mime: str | None = 'text/html') -> Response:
    """一份字节响应。`mime=None` = 这个站压根没发 content-type(小站常态)。"""
    return Response(200, 'https://e.org/post', body, {'content-type': mime} if mime else {})


def pdf_bytes() -> bytes:
    """一份结构有效的单页 PDF:对象、xref 表、FlateDecode 内容流都在。

    **不能是随便几个字节。** 要的正是审查实测出来那个形态 —— 解码之后看着像
    一大段文字(文件语法 + 几百个替换字符),长度轻松越过 `readable.MIN_CHARS`,
    于是提取器眼里"整篇都是正文"、补抓报成功。样本太短的话 `extract` 自己就
    返回空串了,这条用例对"有没有问过 content-type"压根不敏感。
    """
    article = 'A valid research paper with original readable content. ' + ' '.join(
        f'researchword{n}' for n in range(600))
    stream = zlib.compress(('BT /F1 12 Tf 50 750 Td (' + article + ') Tj ET').encode())
    objects = [b'<< /Type /Catalog /Pages 2 0 R >>',
               b'<< /Type /Pages /Kids [3 0 R] /Count 1 >>',
               b'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R >>',
               b'<< /Length ' + str(len(stream)).encode() + b' /Filter /FlateDecode >>\nstream\n'
               + stream + b'\nendstream']
    body, offsets = b'%PDF-1.4\n', []
    for number, obj in enumerate(objects, 1):
        offsets.append(len(body))
        body += f'{number} 0 obj\n'.encode() + obj + b'\nendobj\n'
    start = len(body)
    body += b'xref\n0 5\n0000000000 65535 f \n'
    body += b''.join(f'{at:010d} 00000 n \n'.encode() for at in offsets)
    return body + f'trailer\n<< /Root 1 0 R /Size 5 >>\nstartxref\n{start}\n%%EOF'.encode()


class ResponseKindTests(unittest.TestCase):
    """三道放行判据和解码器各自的直测 —— 端到端那几条的早期信号。"""

    def test_an_unknown_binary_format_is_caught_by_the_nul_byte(self):
        """没见过的二进制格式由 NUL 字节兜底 —— 文本里不会有它。"""
        self.assertIn('二进制', readable.html_problem(b'\x04\x03ZZ\x00\x00payload', {}))
        self.assertEqual('', readable.html_problem(b'<p>' + b'a' * 500, {}))

    def test_an_all_ascii_file_header_is_caught_by_the_magic(self):
        """头部一个 NUL 都没有的那几种 —— 这一档只有文件头那一道救得了它。

        不是边角:PDF 的首个压缩流常常在几十 KB 之后(前面是一长串 ASCII 对象
        定义和 xref),而 RTF、PostScript 整份都是 ASCII。NUL 那道兜底对它们
        完全是瞎的,而声明的类型又正好配错成 `text/html` 时(小站常态),只剩
        这一道。
        """
        head = b'%PDF-1.7\n' + b'% an ascii preamble before the first stream\n' * 120
        self.assertNotIn(b'\x00', head[:readable._SNIFF])
        self.assertIn('PDF', readable.html_problem(head, {'content-type': 'text/html'}))
        self.assertIn('RTF', readable.html_problem(br'{\rtf1\ansi ' + b'x' * 500, {}))
        self.assertIn('PostScript', readable.html_problem(b'%!PS-Adobe-3.0\n' + b'x' * 500, {}))

    def test_the_declared_type_is_believed_before_the_bytes(self):
        """站点自己说了这不是网页,就照它说的办(字节看起来像什么都不改变结论)。"""
        self.assertIn('image/png', readable.html_problem(b'<html><p>hi</p>',
                                                         {'Content-Type': 'image/png'}))
        # 大小写和参数都不该影响判定:httpx 给的键是小写的,别人构造的未必。
        self.assertEqual('', readable.html_problem(
            b'<html><p>hi</p>', {'CONTENT-TYPE': 'TEXT/HTML; charset=UTF-8'}))

    def test_decode_prefers_the_declared_charset_and_never_raises(self):
        body = '<p>café</p>'.encode('iso-8859-1')
        self.assertIn('café', readable.decode(body, {'content-type': 'text/html; charset=iso-8859-1'}))
        meta = '<meta charset="iso-8859-1">'.encode() + body
        self.assertIn('café', readable.decode(meta, {}))
        # 编码名是页面自己写的,可能根本不存在;猜错只该让几个字变成问号,
        # 抛出去就是整篇正文都没有、这一条记成抓取失败重试三轮。
        self.assertIsInstance(readable.decode(body, {'content-type': 'text/html; charset=no-such'}), str)


class BinaryBodyTests(FullTextCase):
    """原文链接后面不是网页时:按抓取失败处理,不许把文件语法当成正文。

    审查实测的那一档:PDF 论文。`decode` 按 `errors='replace'` 把那堆字节解成
    一段 `%PDF-1.4 1 0 obj …` 加几百个替换字符,提取器眼里"整篇都是正文"、长度
    又轻松越过 `MIN_CHARS` —— 于是补抓报成功、`fulltext_at` 落下、条目离开重试
    队列,接着拿这堆乱码去买一次翻译。**可以不支持 PDF,不能把"没读到论文"说成
    "已取得正文"。**
    """

    def test_a_pdf_paper_is_not_mistaken_for_a_body(self):
        key = self.put(url='https://e.org/paper.pdf', title='Research paper')
        with patch.object(fulltext, 'request',
                          Mock(return_value=raw(pdf_bytes(), 'application/pdf'))):
            summary = fulltext.fetch_pending('rss')
        row = self.row(key)
        # 后果先断言:壳原样留着、没拿乱码去买翻译、还排在补抓队列里。
        self.assertEqual(SHELL, row['body'])
        self.assertEqual(0, row['body'].count('\ufffd'))
        self.assertEqual((0, 1), (translate.pending('rss'), fulltext.pending('rss')))
        self.assertEqual('', row['fulltext_at'])
        # 记成失败而不是"认不出正文":面板上要说得出是哪一类。
        self.assertEqual((0, 1, 0), (summary['done'], summary['failed'], summary['empty']))
        self.assertEqual(1, row['fulltext_tries'])
        self.assertIn('不是网页', row['fulltext_error'])

    def test_a_utf16_page_is_not_mistaken_for_a_binary(self):
        """反过来钉一边:UTF-16/32 文本的 NUL 是编码的一部分,不是二进制的证据。

        它每隔一个字节就是 `\x00`,于是那道 NUL 兜底会把一篇正常网页判成
        「像是二进制文件」—— 而 `decode` 按 HTTP 头的 charset / BOM 本来就
        读得出来(审查实测:单独走解码 + 提取拿到 914 字正文,经补抓入口却
        `done=0`、`failed=1`)。
        """
        key = self.put()
        wide = PAGE.encode('utf-16')
        with patch.object(fulltext, 'request',
                          Mock(return_value=raw(wide, 'text/html; charset=utf-16'))):
            summary = fulltext.fetch_pending('rss')
        row = self.row(key)
        self.assertEqual((1, 0), (summary['done'], summary['failed']))
        self.assertIn('The runtime schedules tools for coding agents', row['body'])
        self.assertEqual('', row['fulltext_error'])

    # 六种写法,每一种都**端到端**走一遍补抓,断言正文真的解对了。
    # BOM 一律显式拼(`encode('utf-16')` 的字节序跟着本机走,而倒序那个缺口
    # 恰恰只在 LE 上踩)。
    WIDE = (
        (b'\xff\xfe' + PAGE.encode('utf-16-le'), None, 'UTF-16 LE BOM,压根没发 content-type'),
        (b'\xfe\xff' + PAGE.encode('utf-16-be'), 'text/html', 'UTF-16 BE BOM,content-type 不带 charset'),
        (b'\xff\xfe\x00\x00' + PAGE.encode('utf-32-le'), 'text/html', 'UTF-32 LE BOM'),
        (b'\x00\x00\xfe\xff' + PAGE.encode('utf-32-be'), 'text/html', 'UTF-32 BE BOM'),
        (PAGE.encode('utf-16-le'), 'text/html; charset=utf-16le', '没 BOM,声明了 charset'),
        (PAGE.encode('utf-32-be'), 'text/html; charset=utf-32be', '没 BOM,声明了 utf-32'),
    )

    def test_wide_encodings_are_decoded_by_bom_or_charset(self):
        """宽编码的六种写法:放行**而且**正文解对了,两半都要断言。

        **只断言"放行了"是不够的** —— 那正是上一版的缺口:放行判据另开了一张
        名单(BOM 或声明的 charset 命中就放过),而 `decode` 压根没有 BOM 分支。
        两张名单一漂,**只有 BOM、不声明 charset** 的那几格就从「安全失败」变成
        了「错误覆盖 + 成功退出队列」:按 UTF-8 解成乱码、NUL 留在里面,HTML
        解析认不出结构,`plain` 清掉 NUL 之后反倒把整段 HTML 源码当成了正文,
        长度轻松过 `MIN_CHARS` —— 补抓**报成功**、`fulltext_at` 落下、这一篇
        **永远**离不开壳(2026-10-09 第 3 轮审查实测)。所以判据是正文保真:
        文字在、源码不在、站点外围部件剔掉了、一个替换字符都没有。

        **UTF-32 LE 那一格另有一件事**:`\xff\xfe\x00\x00` 以 UTF-16 LE 的
        BOM 开头,`_BOM_CODECS` 把 UTF-16 排在前面的话它整篇被当成 UTF-16 解。
        """
        key = self.put()
        for body, mime, label in self.WIDE:
            with self.subTest(label):
                with reg.db() as c:
                    c.execute("UPDATE feed_items SET body=?,fulltext_at='',fulltext_tries=0"
                              ' WHERE key=?', (SHELL, key))
                with patch.object(fulltext, 'request', Mock(return_value=raw(body, mime))):
                    summary = fulltext.fetch_pending('rss')
                got = self.row(key)['body']
                self.assertEqual((1, 0), (summary['done'], summary['failed']))
                self.assertIn('The runtime schedules tools for coding agents', got)
                self.assertNotIn('<html>', got)             # 没把源码当正文
                self.assertNotIn('Home', got)               # 站点导航照旧剔掉
                self.assertNotIn('Nice post', got)          # 评论区照旧剔掉
                self.assertEqual(0, got.count('\ufffd'))
                self.assertFalse(got.startswith('\ufeff'))  # UTF-8 BOM 不许留在正文头上
                self.assertEqual('', self.row(key)['fulltext_error'])

    def test_a_utf8_bom_does_not_leak_into_the_body(self):
        """UTF-8 的 BOM 同理要在 `decode` 里吃掉:`plain` 的空白折叠不吃 `\ufeff`。"""
        key = self.put()
        with patch.object(fulltext, 'request',
                          Mock(return_value=raw(b'\xef\xbb\xbf' + PAGE.encode(), 'text/html'))):
            self.assertEqual(1, fulltext.fetch_pending('rss')['done'])
        got = self.row(key)['body']
        self.assertFalse(got.startswith('\ufeff'))
        self.assertIn('The runtime schedules tools for coding agents', got)

    def test_a_wide_page_with_neither_bom_nor_charset_fails_safe(self):
        """两样都没有的那一格:照旧安全失败,不许冒充"已取得正文"。

        猜编码不是这一轮要做的事。NUL 还在就拦下、计次重试,壳原样留着 ——
        「可以不支持某种编码,但不能把"没读对"说成"已取得正文"」。
        """
        key = self.put()
        with patch.object(fulltext, 'request',
                          Mock(return_value=raw(PAGE.encode('utf-16-le'), 'text/html'))):
            summary = fulltext.fetch_pending('rss')
        row = self.row(key)
        self.assertEqual((0, 1), (summary['done'], summary['failed']))
        self.assertEqual(SHELL, row['body'])
        self.assertEqual('', row['fulltext_at'])
        self.assertEqual(1, row['fulltext_tries'])
        self.assertEqual(1, fulltext.pending('rss'))

    def test_the_release_check_asks_the_real_decoder(self):
        """直测那道放行判据:它拿 `decode` 真解一次,不是另问一张编码名单。

        名单那种写法在上面那几条端到端用例里会红,这一条是早期信号 —— 同一份
        字节,声明了 charset 和只带 BOM 必须得到同一个结论。
        """
        for body, mime, label in self.WIDE:
            with self.subTest(label):
                head = {'content-type': mime} if mime else {}
                self.assertEqual('', readable.html_problem(body, head))

    def test_the_exhausted_pdf_link_still_leaves_the_queue(self):
        """出口还在:试满 MAX_TRIES 之后它按壳入库,不会永远挡在知识库外面。"""
        key = self.put(url='https://e.org/paper.pdf')
        with reg.db() as c:
            c.execute('UPDATE feed_items SET fulltext_tries=? WHERE key=?',
                      (fulltext.MAX_TRIES - 1, key))
        with patch.object(fulltext, 'request',
                          Mock(return_value=raw(pdf_bytes(), 'application/pdf'))):
            fulltext.fetch_pending('rss')
        self.assertEqual(0, fulltext.pending('rss'))
        sync.export('rss')
        self.assertTrue((self.root / 'vault' / 'source' / 'rss' / (key + '.md')).exists())

    def test_a_lying_content_type_does_not_get_it_through(self):
        """content-type 说 text/html、字节是 PDF —— 文件头那一道管这个。"""
        key = self.put(url='https://e.org/paper.pdf')
        with patch.object(fulltext, 'request', Mock(return_value=raw(pdf_bytes(), 'text/html'))):
            summary = fulltext.fetch_pending('rss')
        self.assertEqual((0, 1), (summary['done'], summary['failed']))
        self.assertEqual(SHELL, self.row(key)['body'])

    def test_a_pdf_whose_head_has_no_nul_byte_is_still_caught(self):
        """端到端走一遍上面那一档:撒谎的 content-type + 头部全 ASCII 的 PDF。

        这个样本解码出来是 5000 多字的 `% an ascii preamble …`,提取器眼里
        "整篇都是正文"、长度远超 `MIN_CHARS` —— 去掉文件头那一道它就被当成
        补抓成功了。
        """
        key = self.put(url='https://e.org/paper.pdf')
        head = b'%PDF-1.7\n' + b'% an ascii preamble before the first stream\n' * 120
        with patch.object(fulltext, 'request', Mock(return_value=raw(head, 'text/html'))):
            summary = fulltext.fetch_pending('rss')
        self.assertEqual((0, 1), (summary['done'], summary['failed']))
        self.assertEqual(SHELL, self.row(key)['body'])
        self.assertIn('PDF', self.row(key)['fulltext_error'])

    def test_a_real_page_is_still_fetched(self):
        """反过来钉一边:正常网页照旧抓得到(一律拒绝的实现在这里会红)。"""
        key = self.put()
        for mime in ('text/html; charset=utf-8', 'application/xhtml+xml', 'text/plain', None):
            with self.subTest(mime=mime):
                with reg.db() as c:
                    c.execute("UPDATE feed_items SET body=?,fulltext_at='',fulltext_tries=0"
                              ' WHERE key=?', (SHELL, key))
                with patch.object(fulltext, 'request',
                                  Mock(return_value=raw(PAGE.encode(), mime))):
                    summary = fulltext.fetch_pending('rss')
                self.assertEqual(1, summary['done'])
                self.assertIn('The runtime schedules tools', self.row(key)['body'])
