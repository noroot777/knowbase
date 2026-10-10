# -*- coding: utf-8 -*-
"""往左追外层 URL 起点时那个字符集(`x_article_links._URL_RUN`)自己的**契约**。

`_nested` 回答的是"紧贴这个 `?`/`#` 左边那一段是不是一条 URL",办法是往左吃一段
URL 合法字符再找起手式。所以**那个集合少认一个字符,外层起点就追不回来**:
`_nested` 误答"不是嵌套",于是别人家查询串/片段里那条 X 文章地址被当成这条推文
自己的,`_drop` 把它删掉 —— 而 `put_items` 是 `ON CONFLICT(key) DO NOTHING`,这种
删除**永久固定**,重复同步也补不回来。

**"见一个样本补一个"栽过两次**:第 5 轮缺的是 RFC 3986 的 sub-delims(`(` `)`
`;` `$` `'` `,` `!` `*`,都在路径里),第 6 轮缺的是 gen-delims 的 `[` `]`
(IPv6 主机字面量,在 authority 里)—— 两次的末态逐字相同。所以这里不再摆样本,
而是直接钉住"它就是标准那张表":改小哪一类当场红,不必等下一个真实写法撞上来。

**按形状另拆一个文件**:`test_x_article_host.py` 那五族是真跑采集和导出的端到端
(摆盘、临时库、vault、无头浏览器),这一族一个都不要 —— 它只读那个集合本身,外加
一条测时。两边挤一个文件会越过仓库那条 700 行上限。两轮的**后果**仍然钉在那边
(`OuterPathCharTests` / `Ipv6OuterHostTests`),这边钉的是**判据**。

跑:  python -m unittest tests.test_x_article_urlrun -v
"""
import string
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from kb.feeds import x_article
from kb.feeds.x_article import needs_article
from tests.test_x_article_host import CANON, TWEET


class UrlRunContractTests(unittest.TestCase):
    """`_URL_RUN` 自己的**契约**:它就是 RFC 3986 的字符表,一个不多一个不少。

    这一族不是样本,是闸门 —— 前两轮(第 5 轮的 sub-delims、第 6 轮的 IPv6 方括号)
    栽的是同一件事:"见一个样本补一个"永远还有下一个漏掉的字符,而每漏一个都是
    别人那条参考地址被永久剪断。按标准取全集之后这张表就满了,这条用例钉住它不再
    被改小 —— 改小哪一类,下面第一条当场红,而不必等某一天某个真实写法撞上来。

    反过来也钉着:**空白和非 ASCII 一个都不许进来**。它们在正文里都是真实内容,
    收进集合就会把句读紧贴的真地址判成嵌套 —— 那是漏认 = 空正文永久固定。
    """

    # RFC 3986 的四类字符,逐类照抄标准,不引用被测模块自己那份字面量。
    UNRESERVED = string.ascii_letters + string.digits + '-._~'
    PCT = '%'
    SUB_DELIMS = "!$&'()*+,;="
    GEN_DELIMS = ':/?#[]@'

    def test_it_is_exactly_the_rfc_3986_character_table(self):
        want = set(self.UNRESERVED + self.PCT + self.SUB_DELIMS + self.GEN_DELIMS)
        got = set(x_article._URL_RUN)
        self.assertEqual(set(), want - got,
                         'URL 内部的合法字符少认一个,外层起点就追不回来')
        self.assertEqual(set(), got - want, '多认的字符都是正文里的真实内容')

    def test_no_whitespace_and_no_non_ascii_ever_gets_in(self):
        for ch in x_article._URL_RUN:
            self.assertTrue(ch.isascii(), repr(ch))
            self.assertFalse(ch.isspace(), repr(ch))

    def test_the_leftward_scan_does_not_blow_up_on_a_long_body(self):
        """报告的不拦项,顺手治掉:那一段扫描不许是二次的。

        往左取的是"末尾那段连续的 URL 字符",原来写成在左半边做一次
        `[…]*$` 的 `re.search` —— 正则引擎会从**每一个**位置各试一次,于是一条
        「长英文正文 + 空格 + 句末问号 + 文章地址」的推文越长越慢(2026-10-10 第 6
        轮审查测时:1000 字符 0.002 秒、10000 字符 0.202 秒、25000 字符 1.256 秒,
        本地复现到 1.2523 秒)。改成从右往左逐字走之后只扫实际那一段,同一条样本
        是 0.0005 秒。

        **判据给足两个数量级的余量**,所以它不是一条看机器快慢的 flaky:二次那一版
        在 25000 字符上要 1.2 秒以上,线性这一版比现在慢 200 倍也还绿。
        """
        body = 'a' * 25000 + f' ?{CANON}'
        entry = {'url': TWEET, 'body': body}
        started = time.perf_counter()
        self.assertTrue(needs_article(entry), '句末问号紧贴的真地址照旧要认出来')
        spent = time.perf_counter() - started
        self.assertLess(spent, 0.1, f'25000 字符上花了 {spent:.4f} 秒,退回二次了')


if __name__ == '__main__':
    unittest.main()
