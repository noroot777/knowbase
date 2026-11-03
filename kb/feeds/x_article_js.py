# -*- coding: utf-8 -*-
"""读一次 X 文章页的那段浏览器脚本(`x_article.browser_fetch` 的后路用它)。

**脚本本身住在采集扩展目录里**(`kb/static/browser-extension/knowbase-capture/x_article_read.js`),
这里只把那个文件原样读进来。理由:人工补长文那条路(扩展在人自己打开的长文页上读一次,
见 `docs/decisions/x-article.md`)要跑**同一段**脚本,而扩展装在浏览器里,只拿得到它自己
目录下的文件。两边各抄一份的话,「这一页读出来的正文是什么」就有两份口径。每一段判据
为什么那样写,注释都跟着脚本搬过去了。

**单独一个文件起初只有一个理由:行数**(160 多行 JS 会把 `x_article.py` 顶出 700 行
上限)。它是**纯数据**(一个字符串常量),没有被任何测试打桩;被测试改写的模块变量
(`API` / `PAGE_WAIT` / `BROWSER_FALLBACK` / `MAX_CHARS`)一个都没搬,仍然待在 `x_article` 里。

`x_article` 把它绑回自己的模块全局(`_READ_JS`),所以外面照旧按老名字读它。
"""

from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1] / 'static/browser-extension/knowbase-capture/x_article_read.js'
READ_JS = SOURCE.read_text(encoding='utf-8')
