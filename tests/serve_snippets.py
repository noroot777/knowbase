# -*- coding: utf-8 -*-
"""搭一个沙箱 kb-web,给 tests/snip_probe.mjs 验「收藏此段」在四处都收得下、点得回去。

「☆ 收藏此段」原来只长在 `/doc/…` 阅读页里。日报正文、问答回答、订阅阅读器、
图鉴条目各渲染一份正文,却都收藏不了;而前三者**在 documents 表里没有行**
(日报写在 `daily/`,不在任何 `SOURCES.root` 底下;回答压根不是文件),所以这件事
不是前端加个按钮 —— `favorites.path` 原来是 `NOT NULL REFERENCES documents(path)`。

这个脚本把四类来源摆成真数据,一个模型调用都不花:

    .venv/bin/python tests/serve_snippets.py --port 8878 --manifest /tmp/m.json

- 订阅条目:真走 `reg.put_items → sync.export → kb index`,落真 md、真 documents 行
  (验 origin=doc 那一条:屏幕上那份正文在 vault 里有同一份)。
- 两份日报(今天和前一天):真写 `vault/daily/<日期>.md`,frontmatter 按
  `daily._entry` 读的那几个键(验 origin=daily:**有文件、没有 documents 行**;
  两份是为了验切日那一刻出处跟着屏幕上的正文走)。
- 两轮问答:直接往 conversations/messages 落四行(验 origin=chat:连文件都没有;
  两轮是为了验收藏「提问本身」时标题取的是它自己那一问)。
- 一条工具条目:真写 `vault/source/dr-gzh-projects/<slug>.md`(frontmatter 照
  `scripts/gzh_projects_export.py` 的形状),跟着一起建索引 —— 图鉴详情抽屉里
  那份正文走的是 origin=doc + 真 path 那条路,出处挂在每个 `.td-entry` 自己身上。

没有网络请求、没有模型调用:沙箱里没有 secrets.json,所以 `translate.available()`
为假(条目不会被翻译闸门扣着)、`favbrain.extract_action_async` 那条后台线程拿不到
模型当场返回 None,收藏本身不受影响。
"""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import sys
import time
from argparse import Namespace
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# 订阅条目。正文里那句 SNIP_FEED 是探针去选的那一段 —— 要够长(过 SNIP_MIN)、
# 在正文里只出现一次(锚点回跳按 prefix+quote+suffix 找,重复出现就说不清定位
# 到的是哪一处了)。
SNIP_FEED = '把书变成可执行技能的那条路子最关键'
# **订阅列表那张卡片上也摊着这一截正文**(`text.body` 的前 380 字,短观点往往整条
# 都在里面)—— 那是真原文,不是另生成的摘要,所以它也得能收。这一句专留给列表那条
# 判据:别的判据各自用别的句子,好让「库里那条是哪一条」说得清。
SNIP_LIST = '列表卡片上这一截正文也该收得下'
# 列表那张卡片摊的只是**正文开头那一截**,所以从它身上算不出整篇的事:
# - 这一句在那 380 字里只有一处、在整篇里有两处 —— **唯一性要按盘上那一篇重算**
#   (核得过的那一类由 `snipVerified` 重算;照切片存下去就是给裸引文那一档发了一张
#   它不配的通行证)。
SNIP_LIST_DUP = '这一句在卡片那一截里只有一处'
# - 这一句夹着一对星号:卡片按 textContent 原样摊(星号看得见),而阅读页把同一份
#   md 渲染过(星号没了)—— 核对过不去,该退回 origin=feed。那一类**不绑文档、也就
#   没有重算的机会**,唯一性只能按切片算 —— 所以一律按「说不清」存(`partial`)。
SNIP_LIST_MARKED = '列表里这一句夹着 **一对星号** 在中间'
# 同一篇里另一句:用来反过来钉住「盘上那篇就是这份正文的那一条**照旧**绑文档」。
# 另取一句而不是复用上面那句,是为了让这条判据单跑也成立(库里不许有两条同引文的)。
SNIP_FRESH = '最后一段收尾,给出两个可以直接跑的命令'
# **订阅阅读器按 textContent 原样摊开正文,`/doc/…` 阅读页把同一份 md 按
# markdown 渲染** —— 这一对星号在那一头会消失。所以"有文档路径"不等于"那段话
# 在那一头找得到",正文里得真有这么一句,才验得到存之前那道核对
# (`snip.js` 的 `snipVerified`)把它退回 origin=feed。
# **核对完、还没落库,盘上那一篇就被换了一版**的那一句。探针拿沙箱独有的
# `/__snip__/swap-after-read` 把这个时序摆成确定的:`/api/doc` 照旧回**此刻**盘上
# 那一份(核对据此通过),响应发出之后那个文件才真被改掉 —— 于是紧跟着的 POST 带
# 的版本号已经是旧的。审查实测:缺这一道的话收藏照旧落 origin=doc 返回 200,立刻
# 回看是新正文、原句不在、0 个高亮。这一句只在这条判据里收,免得和别的判据抢同
# 一条收藏。
SNIP_RACE = '这一句留给核对完到落库那一下'
# **盘上那一篇这一刻读不出来**的那一句(沙箱的 `/__snip__/unreadable` 把那个路径
# 换成一个同名目录 —— 权限、网盘没挂上、IO 错给的是同一类 OSError)。「读不出来」
# 不等于「这一篇不存在」:两种都退回站外原文,可**说给用户的话不一样** ——
# 说成「这篇还没进检索索引」,是拿一次暂时的失败冒充一个已经证明了的结论。
SNIP_UNREADABLE = '这一句留给读不出盘上那一篇的那一刻'
SNIP_MARKED_PLAIN = '收藏定位的关键文本'
SNIP_MARKED = f'**{SNIP_MARKED_PLAIN}**'
# **一字不差出现两次的整段**:连它前后各三十个字都一样。这是"整段锚点命中了"
# 证明不了位置的那种正文(重复的段落、模板说明、重复的代码块)—— 审查实测:收藏
# 第二处,回跳高亮的是第一处。前后文长到 80 字才分得出两处(更外面那一层不一样),
# 所以采集那头要加长前后文,回跳那头要求整段唯一。
AMBI_QUOTE = '完整锚点自己也可能在一篇里出现两次'
AMBI_LEAD = '下面这一句连着它前后的话一字不差地出现了两次,所以光看命中不能断定位置:'
AMBI_TAIL = ',而它前后各三十个字也完全一样,只有再往外一层才分得出这是第几处。'
AMBI_BLOCK = f'{AMBI_LEAD}{AMBI_QUOTE}{AMBI_TAIL}'
# **引文重复 + 前文跨过一个小标题**:订阅阅读器摊的是 md 原文(`## 乙一节` 带着
# 井号),`/doc/…` 把同一份 md 渲染成 `<h2>`(井号没了)—— 于是「整段」和「只用
# 前文」两档在那一头都失配,引文自己又重复两次(裸引文那一档也不唯一),**只剩
# 「只用后文」那一档活着**。这一格是「锚点只有一边被另一种渲染扰动」的定点夹具:
# 少掉「只听一侧」那一支、或者前后文最外侧的空白不剪,这条锚点在阅读页上就是一句
# 「没找到」,而库里那条、路径、HTTP 全是对的。末尾那一句**故意短**(不到三十字),
# 好让后文那一档的窗口够到正文结尾 —— 那儿的空白两头渲染得不一样。
MARK_DUP = '这一句在两个小节里各出现一次'
MARK_TAIL_A = '收尾甲。'
MARK_TAIL_B = '收尾乙,正文到这儿结束。'
# **一个 emoji,后面隔着 29 个字母才是要选的那句话。** 选中的那几个字里一个
# emoji 都没有,可三十字那一档的前文窗口正好切在它中间:emoji 占两个 UTF-16 单元,
# `slice(a-30, a)` 的第一位落在它的后一半上 —— 切出来的半个字 UTF-8 编不出来,
# sqlite3 在 INSERT 那一句当场抛,整条 POST 变成 500(审查实测:日报和独立阅读页
# 都收藏失败,而用户看不出自己选的字有什么问题)。两份实现各要有自己的红。
WIDE_LEAD = '😀' + 'x' * 29
SNIP_WIDE = '普通文字也应当能正常收藏'
WIDE_BLOCK = f'{WIDE_LEAD}{SNIP_WIDE}' + 'y' * 40
# **两边的位置证据互相矛盾**:同一句话出现两次,第一处的前文和收藏时存的那一份
# 一样、第二处的后文和收藏时存的那一份一样 —— 「只用前文」那一档唯一指向第一处,
# 「只用后文」那一档唯一指向第二处,整段那一档压根不存在。按档位顺序先采信前文
# 那一档,换来的就是一个落在错地方的高亮(审查实测:app 高亮落在偏移 509 而收藏
# 的是 606,独立阅读页 227 / 324)。探针按这个形状手搭锚点:p=卯×30、s=午×30。
CONF_Q = '这次回跳不能忽略另一侧的位置证据'
CONF_LEAD_1, CONF_TAIL_1 = '卯' * 40, '辰' * 40
CONF_LEAD_2, CONF_TAIL_2 = '巳' * 40, '午' * 40
CONF_BLOCK = (f'{CONF_LEAD_1}{CONF_Q}{CONF_TAIL_1}\n\n'
              f'{CONF_LEAD_2}{CONF_Q}{CONF_TAIL_2}')
# **三处一字不差的重复句,两侧的候选集合完全不相交。** 前文那一边(p+q)认得出
# 第一处和第二处(它们前面都是申),后文那一边(q+s)只认得出第三处(只有它后面
# 是子)—— 两个非空集合不相交。把「有两处」和「没有」压成同一个哨兵值的话,前文
# 那个集合就丢了,只剩后文那个"唯一命中",于是高亮落在一处**前文已经明确排除掉**
# 的地方(审查实测:日报三处偏移 809/912/1015,收藏的是 912、高亮落在 1015;独立
# 阅读页 223/326/429,收藏的是 326、高亮落在 429)。探针按这个形状手搭锚点
# (正文改稿之后它就是这个形状):p=申×30 配 s=子×30 不相交、配 s=酉×30 则交出
# 第一处 —— 后者是反过来那一边,**歧义的一侧不等于整条锚点作废**。
DISJ_Q = '这一处的位置还有另一侧的证据管着'
DISJ_P, DISJ_S1 = '申' * 40, '酉' * 40
DISJ_S2, DISJ_P3, DISJ_S3 = '戌' * 40, '亥' * 40, '子' * 40
DISJ_BLOCK = (f'{DISJ_P}{DISJ_Q}{DISJ_S1}\n\n'
              f'{DISJ_P}{DISJ_Q}{DISJ_S2}\n\n'
              f'{DISJ_P3}{DISJ_Q}{DISJ_S3}')
# **三档前后文全都分不出来的两处**:两头各两百多个字一模一样,采集那头长到 200
# 字还是不唯一。存照旧存(引文本身才是收藏的主要用处),但**当场就得说一句**
# 「点回去可能定位不到这一处」—— 不说的话用户要到点回去那一刻才发现。app 那一侧
# 早有这句话,独立阅读页原来一律显示普通成功(两份反馈契约要对齐)。
DEAD_Q = '这一处和那一处连两百字开外都分不出来'
DEAD_FILL = '填' * 205
DEAD_BLOCK = f'{DEAD_FILL}{DEAD_Q}{DEAD_FILL}{DEAD_Q}{DEAD_FILL}'
# **阅读页那一侧的显示分隔**:md 里两个段落渲染成两个 `<p>`,屏幕上空着一行,而
# DOM 文本节点里只有各段自己的文字。`<br>` 那一种在这一页上是**字面文字**(原始
# HTML 当文字显示,见 `pages.py` 的 `_DOC_LITERAL_JS`),所以这一头用段落边界验 ——
# 两份实现各有自己的那一格。
PAGE_PARA_1 = '阅读页这一段到这儿收尾'
PAGE_PARA_2 = '阅读页下一段打这儿起头'
PAGE_PARA_SEEN = f'{PAGE_PARA_1}。\n\n{PAGE_PARA_2}'

# **采集那一刻这段引文在那一篇里有几处** —— 这个数得在采集那一刻记下来
# (`favorites.quote_unique`)。回跳那一刻两侧上下文都失配、退到裸引文那一档时按它
# 决定能不能采信:**「现在只剩一处」证明不了「这一处就是当时那一处」** —— 当时有
# 两处的话,原处完全可能正是被改稿换掉的那一处,剩下这一处是采集时就明确不是的
# 另一处(审查实测:原处删掉之后高亮落在第一处)。
# **反过来钉一边**:采集那一刻整篇只有一处的那一句,同样两侧失配之后裸引文那一档
# 照旧采信、照旧高亮得出来 —— 那一档不该被整个废掉。
# 这一组同时摊在日报和订阅那一篇里:共享层和独立阅读页各记一次、各判一次。
ONCE_Q = '这一句采集那会儿在正文里就有两处'
ONCE_LEAD_1 = '第一处前面这句话认得出它是第一处:'
ONCE_LEAD_2 = '第二处前面这句话跟上面那句完全不同:'
ONCE_TAIL_1 = '第一处后面跟着这一句收尾。'
ONCE_TAIL_2 = '第二处后面跟着另外一句收尾。'
ONCE_SOLO = '这一句采集那会儿整篇只有它自己一处'
ONCE_SOLO_LEAD = '它前面这句话是它的前文:'
ONCE_SOLO_TAIL = '它后面这句话是它的后文。'
ONCE_BLOCK = (f'{ONCE_LEAD_1}{ONCE_Q}{ONCE_TAIL_1}\n\n'
              f'{ONCE_LEAD_2}{ONCE_Q}{ONCE_TAIL_2}\n\n'
              f'{ONCE_SOLO_LEAD}{ONCE_SOLO}{ONCE_SOLO_TAIL}')

FEED_ITEM = {
    'url': 'https://example.org/skill-pipeline',
    'title': '一条把书变成技能的流水线',
    'author': '某个博客',
    'body': ('开头一段交代背景,先讲为什么要这么做。\n\n'
             f'{SNIP_LIST},在卡片上就看得见。\n\n'
             f'{SNIP_LIST_DUP},整篇里另有一处。\n\n'
             f'{SNIP_LIST_MARKED}。\n\n'
             f'{SNIP_FEED},剩下的都是工程问题。\n\n'
             f'请执行 {SNIP_MARKED} 并核对结果。\n\n'
             f'{SNIP_FRESH}。\n\n'
             f'{PAGE_PARA_1}。\n\n{PAGE_PARA_2}。\n\n'
             # 这一对**靠普通段落区分**:两头渲染出来的文字一样,所以验的是
             # 「前后文加长到唯一 + 两份实现都落在第几处」。**只有一边对得上**
             # 那一格由下面那对小标题分隔的管(`MARK_DUP`),两格各管一件事。
             # 日报那一份也是小标题分隔,但它只在同一个渲染器里回跳。
             f'先说一遍:{AMBI_BLOCK}\n\n'
             '中间插一段不一样的话把两处隔开,好让再往外一层分得出先后。\n\n'
             f'再说一遍:{AMBI_BLOCK}\n\n'
             # 这三块各管一件事,互不相干:截断切开一个字 / 两边证据矛盾 /
             # 三档都分不出来。都挂在这一篇里是因为独立阅读页那一侧要用
             # **同一篇导出稿**验同样的事(两份实现各要有自己的红)。
             # **排在小标题那一对之前**:`MARK_TAIL_B` 得留在正文最末一行,
             # 那一档「只用后文」的窗口要够到正文结尾(见上面 MARK_DUP 那段)。
             f'## 一个 emoji 隔着三十个字母\n\n{WIDE_BLOCK}\n\n'
             f'## 两边证据矛盾的两处\n\n{CONF_BLOCK}\n\n'
             f'## 两侧候选不相交的三处\n\n{DISJ_BLOCK}\n\n'
             f'{SNIP_RACE}。\n\n'
             f'{SNIP_UNREADABLE}。\n\n'
             f'## 怎么都分不出来的两处\n\n{DEAD_BLOCK}\n\n'
             # 这一组同时摊在日报和这一篇里:**采集那一刻有几处**这件事,共享层和
             # 独立阅读页各自记一次、各自按它判一次回跳
             f'## 采集那会儿有几处\n\n{ONCE_BLOCK}\n\n'
             # `SNIP_LIST_DUP` 的第二处:远在卡片那 380 字之外,所以只有按**整篇**
             # 算才看得见它
             f'{SNIP_LIST_DUP},这儿就是那另一处。\n\n'
             f'## 甲一节\n\n铺垫一句话。{MARK_DUP}{MARK_TAIL_A}\n\n'
             f'## 乙一节\n\n铺垫一句话。{MARK_DUP}{MARK_TAIL_B}\n'),
}

# **译文写回了、还没重导出**的那一条。`translate._save` 清掉 `exported_hash`
# 却保留 `path`(正文补抓那条路则是换掉 `content_hash`、特意不清 `exported_hash`)
# —— 于是屏幕上是新译文、盘上那篇还是旧的那一版。审查实测:这几分钟里收藏新
# 译文里的一句话会存成 origin=doc,点回去是旧英文、0 个高亮、报「没找到」。
SNIP_STALE = '重译完成之后这一句只在最新的订阅正文里有'
FEED_STALE = {
    'url': 'https://example.org/retranslated',
    'title': 'A post that was translated again',
    'author': '另一个博客',
    'body': 'The original English body that was exported into the vault earlier.',
}

# **两个哈希相等,盘上那篇却已经是另一份译文。** `exported_hash=content_hash`
# 是**原文**的哈希(`kb/feeds/sync.py` 导出成功那一句写的就是它),同一篇原文
# 重译一遍、重导出一遍它一个字节都不变 —— 旧判据(`exported_hash ===
# content_hash`)于是照旧说「盘上那篇就是屏幕上这份」,而那两份译文可以是完全
# 不同的两句话(审查实测:保留旧阅读器不刷新、后台重译重导出,收藏存成 doc 返回
# 200,点回去是另一份译文、0 个高亮、报「没找到」)。这里照那条链的末态摆:
# 译文换掉、**两个哈希保持相等**、盘上那篇还是导出时那一份。
SNIP_SWAP = '这句旧译文只留在已经打开的那一页订阅正文里'
FEED_SWAP = {
    'url': 'https://example.org/retranslated-twice',
    'title': 'A post whose translation got swapped',
    'author': '第三个博客',
    'body': 'The exported body on disk stays exactly this English paragraph.',
}

# **原文链接超过约定上限的那一条。** 半截 URL 点过去是另一个地方,所以超长就整条
# 不送(`snip.js` 的 `SNIP_MAX_URL`);可这一类**没有 path**,链接是回原文的唯一
# 一条路 —— 丢了只能回到来源面板,这件事收藏那一刻就得说。译文照 FEED_SWAP 那样
# 换掉,好让它落到 feed 那一类(绑了文档的那一类回跳本来走阅读页,不必说)。
SNIP_LONGURL = '这一条的原文链接长得存不下来'
FEED_LONGURL = {
    'url': 'https://example.org/long?x=' + 'q' * 5000,
    'title': 'A post with an absurdly long url',
    'author': '第四个博客',
    'body': 'The exported body on disk is this English paragraph only.',
}

# 日报正文里探针去选的那一段。日报是模型写的 markdown,所以这里也按那个形状写。
SNIP_DAILY = '今天最值得动手的是那条本地索引的方案'
# **带换行和缩进的四行代码**。收藏它得到的必须还是这四行 —— 压平过的那一份是
# 匹配表示,不是收藏原文(审查实测:存进库里的是接成一行的
# `if ready: run_task() else: stop_task()`,换行和缩进再也找不回来)。
SNIP_CODE = 'if ready:\n    run_task()\nelse:\n    stop_task()'
# **同一句话在正文里出现两次**,前文不同。这是前后文锚点唯一存在的理由:
# 光靠 quote 全文搜只会命中第一处,于是用户点回去看到的是另一段上下文里
# 那句一模一样的话。探针选的是**第二处**,断言落回去的也是第二处。
SNIP_DUP = '这件事要先把索引建好'
DUP_LEAD_FIRST = '第一次提到:'
DUP_LEAD_SECOND = '换个上下文再提一次:'
# 换日那一刻去收的那一段。**单独一句**,免得和上面几条判据抢同一条收藏
# (`clickJump` 按 origin + 引文找按钮)。
SNIP_DAILY_SLOW = '这一段留给换日那一刻的那次收藏'
# **前一天还有一份日报**:切日那一刻屏幕上还是旧那一份,出处必须跟着屏幕上
# 这份正文走,而不是跟着"正在请求谁"(`daily_body.js` 的 `setDailyBody`)。
SNIP_DAILY_OLD = '前一天那份日报里写的是另一件事'
# 图鉴详情抽屉里探针去选的那一段(条目正文)。
SNIP_TOOL = '装上之后它会在本地建一棵条目树'
TOOL_NAME = 'Snipkit'
# 版本契约那几条判据**各用一句话**:同一句存两次的话,「库里有没有这一条」就说
# 不清了(收藏表没有去重约束,同引文可以并存两行)。
TOOL_V_FRESH = '这一句用来验新开的页面照旧收得下'
TOOL_V_STALE = '这一句用来验盘上那篇换了版之后不许落库'
TOOL_V_AGAIN = '这一句用来验重新打开之后又收得下'
# 盘上那一篇**补了一段**(原句还在)之后去收的那一句:面板上这份视图是旧的,可那
# 段话在新那一版里照旧定位得到 —— 该照存、绑新那一版,不该因为"版本号不等"就拒。
TOOL_V_KEPT = '这一句用来验补过一段之后照旧绑得上'
# **条目正文里真带着一段原始 HTML 的那一句。** 图鉴抽屉按 `mdHtml` 渲染(标签成了
# 元素、本身看不见),而阅读页把原始 HTML 当**字面文字**显示 —— 同一个文件版本,
# 两头的文本不是同一份。跨过这一段的选区在抽屉里是连着的一句,在阅读页那一头中间
# 插着四个尖括号字:核对该判"定位不到",落库该照落、但当场说清。
TOOL_HTML_MD = '图鉴里这句跨过<strong>加粗</strong>那几个字,收藏它要如实说一句'
TOOL_HTML_SEEN = TOOL_HTML_MD.replace('<strong>', '').replace('</strong>', '')
# **带删除线的那一句:搜索卡那一头的形状。** 检索语料剥掉了 `*`、却留着 `~`
# (`kb/chunker.py` 的 `strip_markup`),所以这一句在搜索卡上带着两道波浪号,而
# 阅读页把它渲染成 `<del>`(波浪号没了)—— 跨过波浪号的选区在那一页上压根不存在,
# 核对必然不过。而搜索卡这一类**既不自带版本号、也没有一档不说谎的退路**:它只能
# 记下刚读到的那一版、照旧绑那一篇,并当场说清点回去可能定位不到。
# **这一句要排在条目正文靠前的位置**:搜索卡只摊 `best_chunk` 的前 160 字
# (`core.js` 的 `.snippet`),排到后面去的话卡片上压根看不到它,这一节会因为
# 「一处都没取到」而看着是绿的。
TOOL_DEL_TEXT = '删除线这一句在搜索卡上带着波浪号'
TOOL_DEL_MD = f'~~{TOOL_DEL_TEXT}~~'
# **同一句话在条目正文里出现两处**,引子各不相同(采集那头据此分得出是第几处)。
# 探针把选中那一处连引子一起从盘上删掉(`/__snip__/drop` 认整串),盘上于是只剩
# 另一处 —— 核对那一步若按裸引文命中它就算"核过了",点回去高亮的是没选过的那一处。
TOOL_TWIN, TOOL_TWIN_LEAD = '这一句用来验原处删掉之后不许冒认别处那一句', '甲节里先说一遍:'
# 条目正文的**最后一句**:它后面紧跟着的是抽屉里那三颗链接按钮和小节标题 ——
# 前后文锚点必须只从正文里采,不许把界面文字当成目标文档的上下文。
TOOL_TAIL = '交代它和别的工具怎么搭'
# 问答回答里探针去选的那一段。
SNIP_CHAT = '按这个顺序装三个依赖就能跑起来'
# 问答那一轮的提问 —— 收藏列表里用它指代那一段(`chatQuestionOf`)。
CHAT_QUESTION = '知识库里有哪些能本地跑的检索方案?'
# **第二轮**。收藏「提问本身」时标题该取它自己,不该往前取到上一轮那个问题。
CHAT_QUESTION_2 = '那套本地检索方案要装哪几个依赖?'
SNIP_CHAT_2 = '第二轮这段回答里另有一句可以收的话'
# **第三轮:提问的第 60 个字是个 emoji。** 收藏列表里那条片段的标题取的是这一问
# (裁到 60 字),裁口正好落在它中间 —— 切出来的半个字后端存不下,整条 POST 被拒,
# 而用户选的是回答正文里一句普通的话(`chat.js` 的 `chatQuestionOf` + `snipClip`)。
CHAT_QUESTION_3 = '问' * 59 + '😀 这一问的第六十个字是个 emoji,后面还有几个字'
SNIP_CHAT_3 = '第三轮这段回答是用来验标题裁口的'
# **第四轮:屏幕上分了行、DOM 文本节点里却一个分隔字符都没有的那两种。**
# `<br>` 是一处(markdown 里的硬换行、模型自己写的 HTML 都会出现),段落边界是另
# 一处。拿文本节点串当原文存的话,这两行当场接成一行 —— 收藏内容本身就丢了,
# 事后谁也补不回来(审查实测:`Selection.toString()` 是 `第一行\n第二行`,
# `Range.toString()` 是 `第一行第二行`,落库的是后者)。
CHAT_QUESTION_4 = '那套方案装完之后第一步该做什么?'
SNIP_BR_1 = '显示分隔第一行这句要连着下一行一起收'
SNIP_BR_2 = '显示分隔第二行跟上一行之间只隔一个换行'
SNIP_PARA_1 = '上一段这句话在屏幕上自成一段'
SNIP_PARA_2 = '下一段这句跟它之间空着一行'
# 屏幕上那份文字:`<br>` 一个换行,段落边界两个(同 `Selection.toString()`)
SNIP_BR_SEEN = f'{SNIP_BR_1}\n{SNIP_BR_2}'
SNIP_PARA_SEEN = f'{SNIP_PARA_1}。\n\n{SNIP_PARA_2}'

# **第五轮:屏幕上分了行,而分行是 CSS 说的,标签名上看不出来。** 这两句各装在一个
# `<span>` 里(标签名是行内元素),样式把它们摊成了两行 —— 浏览器照 CSS 算,给的是
# 两行;照标签名列表那份退路算,给的是接成一行的那一份。谁是权威这件事只有这一格
# 验得到:`<br>` 和段落那两格在两套算法下答案一样。审查实测:按标签名判的那一版
# 把两行存成一行,而屏幕上、剪贴板里都是两行。
# **样式由探针现注入**(页面上本来没有这条规则):要的就是「产品代码一个字不改、
# 只有 CSS 变了」这个形状 —— 用户样式、主题、将来某次 CSS 改动都是这个形状。
CHAT_QUESTION_5 = '那套方案还有哪两条注意事项?'
SNIP_CSS_CLASS = 'snip-css-probe'
SNIP_CSS_1 = 'CSS 把这一句单独摊成了一行'
SNIP_CSS_2 = '它下面这一句是另一行,标签名上看不出来'
# **第六、七轮:同一个问题问两遍,两条回答一字不差。** 这个对话里于是有两处
# 完全一样的文字,**靠文字谁也分不出是哪一条** —— 分得出它们的只有消息编号
# (`favorites.origin_msg`,后端核验过的那一个)。采集和回跳都该收到那一条消息
# 上:在整个消息区里搜的话,这两处都对得上 → 按「位置说不清」一个高亮也不给,
# 而要找的那一条明明记着(审查复现的那一条);更坏的是两条回答只有一半相同时,
# 锚点在另一条上先命中、高亮落在别人身上。
CHAT_QUESTION_6 = '同一个问题问两遍,你会给出一字不差的回答吗?'
SNIP_CHAT_TWICE = '会:这一句在两条回答里一字不差,靠文字分不出是哪一条'

# frontmatter 的键按 scripts/gzh_projects_export.py 来:`project` 缺了
# `kb/projects.py` 的 `_load_entry` 直接判成"不是条目"、整个图鉴里看不到它。
_TOOL_MD = """---
title: {name}
source: dr-gzh-projects
account: 某个公众号
published: {published}
url: https://example.org/tool-post
article_title: 一篇讲这个工具的文章
project: {name}
project_url: https://github.com/example/snipkit
kind: project
summary: 本地把文章拆成条目的小工具
tags:
  - entity/snipkit
  - topic/skill工具
---

> 整理自公众号文章 [一篇讲这个工具的文章](https://example.org/tool-post) · 某个公众号 · {day}

{snip},不连任何外部服务。

{vdel}。

{vfresh}。{vstale}。{vagain}。{vkept}。

{vhtml}。

{twinlead}{vtwin}。中间隔着一句话。乙节里又说一遍:{vtwin}。

后面还有一段,交代它和别的工具怎么搭。
"""

_DAILY_MD = """---
date: {day}
docs: 1
total: 1
late: 0
model: stub/none
generated: {generated}
sources:
  rss: 1
window_since: {since}
window_until: {until}
---

# 日报 · {day}

> 覆盖 {since} → {until} 发布的内容

## 今天的重点

{snip},它不依赖任何外部服务。

## 两处一样的话

{lead1}{dup}。

{lead2}

{dup}

## 换日那一刻

{slow}。

## 一段可以直接跑的代码

```python
{code}
```

## 一字不差的两段

{ambi}

## 一个 emoji 隔着三十个字母

{wide}

## 两边证据矛盾的两处

{conf}

## 两侧候选不相交的三处

{disj}

## 又说了一遍

{ambi}

## 采集那会儿有几处

{once}

## 素材清单

1. [一条把书变成技能的流水线](https://example.org/skill-pipeline) — 某个博客
"""


# ── 假的问答流:一个模型调用都不花,也不连网 ──────────────────────────────────
# 「这个会话在库里」不等于「这一轮已经落库」:后端只在 NDJSON 收完之后才
# `_save_qa`(见 `kb/web/chat_api.py`),生成中、点了停止、出错全都没落库,刷新
# 就什么都没了 —— 而追问时会话号早就有了。所以"生成中的气泡能不能收藏"这件事
# 必须有一条**真的流**才验得到:把 `run_ask_stream` 换成下面这个发生器,帧序和
# 真的那条一样(meta → delta× → 返回),收完照旧走真的 `_save_qa`。
HANG_MARK = '挂住别结束'
STREAM_TEXT = '这一段是正在生成的半截回答,它还没有落库'
STREAM_TAIL = '这一句只有正常收完的那一轮才有,它和提问一起落了库'


def _stub_stream(conn, q, model='', history=None):
    yield {'type': 'meta', 'refs': [], 'plan': None}
    yield {'type': 'delta', 'text': STREAM_TEXT}
    if HANG_MARK in q:
        # 挂着不结束(探针在这期间验"生成中的气泡收不下")。客户端点停止 =
        # fetch abort,前端当场落到"已停止(未保存)",后端这边什么都不写。
        for _ in range(200):
            time.sleep(0.1)
        return
    yield {'type': 'delta', 'text': STREAM_TAIL}


# 沙箱独有的那几条路由(`touch` / `drop` / `swap-after-read` / `unreadable`)搬去了
# `tests/snip_sandbox_routes.py` —— 这一份到了行数上限。它们一个响应都不伪造,
# 只是把「盘上那一篇真的换了一版」摆成确定的时序,理由写在那一头。


def build(vault: Path) -> dict:
    from kb import cli, store
    from kb.feeds import registry as reg
    from kb.feeds import sync

    now = datetime.now()
    published = (now - timedelta(hours=2)).strftime('%Y-%m-%d %H:%M')
    source = reg.save_subscription('rss', {'name': 'Snippet Blog',
                                           'url': 'https://example.org/f.xml'})
    reg.put_items(source, [{**FEED_ITEM, 'published': published},
                           {**FEED_STALE, 'published': published},
                           {**FEED_SWAP, 'published': published},
                           {**FEED_LONGURL, 'published': published}])
    sync.export('rss', hold_untranslated=False)
    # 两条都导出过了,再把第二条**重译一遍而不重导出** —— 照 `translate._save`
    # 的样子写:译文落库、`exported_hash` 清空、`path` 留着。这就是"屏幕上是新
    # 正文、盘上那篇还是旧的"那个窗口,收藏绑文档必须认得出来。
    with reg.db() as c:
        c.execute("UPDATE feed_items SET title_zh=?, body_zh=?, exported_hash=''"
                  " WHERE url=?",
                  ('重译过的那一篇', f'{SNIP_STALE}。后面还有一段收尾的话。',
                   FEED_STALE['url']))
        # 第三条:译文换掉,**`exported_hash` 原样留着**(导出那一步刚把它写成
        # content_hash)—— 于是两个哈希相等、盘上那篇还是导出时那份英文原文。
        c.execute("UPDATE feed_items SET title_zh=?, body_zh=? WHERE url=?",
                  ('换过译文的那一篇', f'{SNIP_SWAP}。后面还有一段收尾的话。',
                   FEED_SWAP['url']))
        # 第四条:同样换掉译文(于是落到 feed 那一类),它的原文链接超长
        c.execute("UPDATE feed_items SET title_zh=?, body_zh=? WHERE url=?",
                  ('链接超长的那一篇', f'{SNIP_LONGURL}。后面还有一段收尾的话。',
                   FEED_LONGURL['url']))

    # 工具条目:**要在建索引之前落盘**,不然 documents 里没有它的行,
    # 图鉴那条 origin=doc 的收藏会被后端判成 404(它不替调用方降级)。
    day = now.strftime('%Y-%m-%d')
    tool_dir = vault / 'source' / 'dr-gzh-projects'
    tool_dir.mkdir(parents=True, exist_ok=True)
    tool_path = tool_dir / 'snipkit.md'
    tool_path.write_text(_TOOL_MD.format(
        name=TOOL_NAME, snip=SNIP_TOOL, day=day, published=published,
        vfresh=TOOL_V_FRESH, vstale=TOOL_V_STALE, vagain=TOOL_V_AGAIN,
        vkept=TOOL_V_KEPT, vhtml=TOOL_HTML_MD, vdel=TOOL_DEL_MD,
        vtwin=TOOL_TWIN, twinlead=TOOL_TWIN_LEAD),
        encoding='utf-8')

    cli._index_pass(Namespace(reembed=False, no_vector=True))

    # 日报:**文件落在 vault/daily/ 下,那个目录不在任何来源的 root 底下** ——
    # 所以索引跑完它照旧不在 documents 里,正是 origin=daily 要面对的局面。
    stamp = '%Y-%m-%d %H:%M'
    daily_dir = vault / 'daily'
    daily_dir.mkdir(parents=True, exist_ok=True)
    (daily_dir / f'{day}.md').write_text(_DAILY_MD.format(
        day=day, snip=SNIP_DAILY, dup=SNIP_DUP, slow=SNIP_DAILY_SLOW,
        code=SNIP_CODE, ambi=AMBI_BLOCK, wide=WIDE_BLOCK, conf=CONF_BLOCK,
        disj=DISJ_BLOCK, once=ONCE_BLOCK,
        lead1=DUP_LEAD_FIRST, lead2=DUP_LEAD_SECOND,
        since=(now - timedelta(days=1)).strftime(stamp),
        until=now.strftime(stamp),
        generated=now.strftime(stamp)), encoding='utf-8')
    # 前一天那一份:正文里那句话和今天这份不重复,这样"屏幕上是哪一份"看得出来
    old_day = (now - timedelta(days=1)).strftime('%Y-%m-%d')
    (daily_dir / f'{old_day}.md').write_text(_DAILY_MD.format(
        day=old_day, snip=SNIP_DAILY_OLD, dup=SNIP_DUP, slow=SNIP_DAILY_SLOW,
        code=SNIP_CODE, ambi=AMBI_BLOCK, wide=WIDE_BLOCK, conf=CONF_BLOCK,
        disj=DISJ_BLOCK, once=ONCE_BLOCK,
        lead1=DUP_LEAD_FIRST, lead2=DUP_LEAD_SECOND,
        since=(now - timedelta(days=2)).strftime(stamp),
        until=(now - timedelta(days=1)).strftime(stamp),
        generated=(now - timedelta(days=1)).strftime(stamp)), encoding='utf-8')

    # 一轮问答。
    iso = now.isoformat(timespec='seconds')
    with reg.db() as c:
        doc_path = c.execute('SELECT path FROM feed_items WHERE url=?',
                             (FEED_ITEM['url'],)).fetchone()['path']
    conn = store.connect()
    try:
        cid = conn.execute(
            'INSERT INTO conversations (title, created_at, updated_at) VALUES (?,?,?)',
            (CHAT_QUESTION, iso, iso)).lastrowid
        conn.execute(
            "INSERT INTO messages (conversation_id, role, content, created_at)"
            " VALUES (?, 'user', ?, ?)", (cid, CHAT_QUESTION, iso))
        refs = json.dumps([{'n': 1, 'title': FEED_ITEM['title'], 'path': doc_path,
                            'account': FEED_ITEM['author'], 'vault': 'kb',
                            'url': FEED_ITEM['url']}], ensure_ascii=False)
        conn.execute(
            "INSERT INTO messages (conversation_id, role, content, model,"
            " refs_json, created_at) VALUES (?, 'assistant', ?, 'stub/none', ?, ?)",
            (cid, f'可以。{SNIP_CHAT},全部都是本地依赖。', refs, iso))
        conn.execute(
            "INSERT INTO messages (conversation_id, role, content, created_at)"
            " VALUES (?, 'user', ?, ?)", (cid, CHAT_QUESTION_2, iso))
        conn.execute(
            "INSERT INTO messages (conversation_id, role, content, model, created_at)"
            " VALUES (?, 'assistant', ?, 'stub/none', ?)",
            (cid, f'{SNIP_CHAT_2},装完直接跑。', iso))
        conn.execute(
            "INSERT INTO messages (conversation_id, role, content, created_at)"
            " VALUES (?, 'user', ?, ?)", (cid, CHAT_QUESTION_3, iso))
        conn.execute(
            "INSERT INTO messages (conversation_id, role, content, model, created_at)"
            " VALUES (?, 'assistant', ?, 'stub/none', ?)",
            (cid, f'{SNIP_CHAT_3},就这一句。', iso))
        conn.execute(
            "INSERT INTO messages (conversation_id, role, content, created_at)"
            " VALUES (?, 'user', ?, ?)", (cid, CHAT_QUESTION_4, iso))
        conn.execute(
            "INSERT INTO messages (conversation_id, role, content, model, created_at)"
            " VALUES (?, 'assistant', ?, 'stub/none', ?)",
            (cid, f'{SNIP_BR_1}<br>{SNIP_BR_2}。\n\n'
                  f'{SNIP_PARA_1}。\n\n{SNIP_PARA_2}。', iso))
        conn.execute(
            "INSERT INTO messages (conversation_id, role, content, created_at)"
            " VALUES (?, 'user', ?, ?)", (cid, CHAT_QUESTION_5, iso))
        conn.execute(
            "INSERT INTO messages (conversation_id, role, content, model, created_at)"
            " VALUES (?, 'assistant', ?, 'stub/none', ?)",
            (cid, f'<span class="{SNIP_CSS_CLASS}">{SNIP_CSS_1}</span>'
                  f'<span class="{SNIP_CSS_CLASS}">{SNIP_CSS_2}</span>', iso))
        # 第六、七轮:同一个问题、一字不差的两条回答(见 CHAT_QUESTION_6)。
        # **排在最后两轮**:前面几节按文字找消息,多两轮一模一样的话会把它们
        # 认到的那一条换掉 —— 而这两轮的文字和前面任何一轮都不重复。
        for _ in range(2):
            conn.execute(
                "INSERT INTO messages (conversation_id, role, content, created_at)"
                " VALUES (?, 'user', ?, ?)", (cid, CHAT_QUESTION_6, iso))
            conn.execute(
                "INSERT INTO messages (conversation_id, role, content, model,"
                " created_at) VALUES (?, 'assistant', ?, 'stub/none', ?)",
                (cid, f'{SNIP_CHAT_TWICE}。', iso))
        conn.commit()
    finally:
        conn.close()

    with reg.db() as c:
        row = c.execute('SELECT key, path FROM feed_items WHERE url=?',
                        (FEED_ITEM['url'],)).fetchone()
        stale = c.execute('SELECT key, path FROM feed_items WHERE url=?',
                          (FEED_STALE['url'],)).fetchone()
        swap = c.execute('SELECT key, path FROM feed_items WHERE url=?',
                         (FEED_SWAP['url'],)).fetchone()
        longurl = c.execute('SELECT key, path FROM feed_items WHERE url=?',
                            (FEED_LONGURL['url'],)).fetchone()
    from kb.norm import entity_norm
    return {'docPath': row['path'], 'itemKey': row['key'], 'dailyDate': day,
            'dailyDateOld': old_day, 'snipDailyOld': SNIP_DAILY_OLD,
            'convId': cid, 'snipFeed': SNIP_FEED, 'snipDaily': SNIP_DAILY,
            'snipChat': SNIP_CHAT, 'snipDup': SNIP_DUP,
            'snipMarked': SNIP_MARKED, 'snipMarkedPlain': SNIP_MARKED_PLAIN,
            'chatQuestion2': CHAT_QUESTION_2, 'snipChat2': SNIP_CHAT_2,
            'snipBr1': SNIP_BR_1, 'snipBr2': SNIP_BR_2,
            'snipBrSeen': SNIP_BR_SEEN, 'snipParaSeen': SNIP_PARA_SEEN,
            'snipPara1': SNIP_PARA_1, 'snipPara2': SNIP_PARA_2,
            'pagePara1': PAGE_PARA_1, 'pagePara2': PAGE_PARA_2,
            'pageParaSeen': PAGE_PARA_SEEN,
            'dupLeadSecond': DUP_LEAD_SECOND, 'snipDailySlow': SNIP_DAILY_SLOW,
            'snipTool': SNIP_TOOL, 'toolNorm': entity_norm(TOOL_NAME),
            'snipCode': SNIP_CODE, 'ambiQuote': AMBI_QUOTE,
            # 手搭「三十字那一档」的歧义锚点要用(阅读页那一侧的唯一性判据)
            'ambiLead': AMBI_LEAD, 'ambiTail': AMBI_TAIL,
            'markDup': MARK_DUP, 'markTailB': MARK_TAIL_B,
            'snipFresh': SNIP_FRESH,
            'staleKey': stale['key'], 'stalePath': stale['path'],
            'snipStale': SNIP_STALE, 'hangMark': HANG_MARK,
            'widePrefix': 'x' * 29, 'refTitle': FEED_ITEM['title'],
            'longUrlKey': longurl['key'], 'snipLongUrl': SNIP_LONGURL,
            'snipChat3': SNIP_CHAT_3, 'snipList': SNIP_LIST,
            'snipListDup': SNIP_LIST_DUP, 'snipListMarked': SNIP_LIST_MARKED,
            'cssClass': SNIP_CSS_CLASS, 'snipCss1': SNIP_CSS_1, 'snipCss2': SNIP_CSS_2,
            'chatQuestion6': CHAT_QUESTION_6, 'snipChatTwice': SNIP_CHAT_TWICE,
            'onceQ': ONCE_Q, 'onceSolo': ONCE_SOLO,
            # 两处各自的后文:判「一侧有证据、在这份文本里一处也对不上」那一档用
            # —— 后文指得出其中一处,前文给一句正文里压根没有的话
            'onceTail1': ONCE_TAIL_1, 'onceTail2': ONCE_TAIL_2,
            'swapKey': swap['key'], 'swapUrl': FEED_SWAP['url'],
            'snipSwap': SNIP_SWAP, 'snipWide': SNIP_WIDE,
            'confQ': CONF_Q, 'confP': CONF_LEAD_1[:30], 'confS': CONF_TAIL_2[:30],
            'deadQ': DEAD_Q,
            # 两侧候选不相交那一组:同一个 p 配两个 s —— 一个不相交(该拒)、
            # 一个交出第一处(该放行)
            'disjQ': DISJ_Q, 'disjP': DISJ_P[:30],
            'disjSFar': DISJ_S3[:30], 'disjSNear': DISJ_S1[:30],
            'snipRace': SNIP_RACE, 'snipUnreadable': SNIP_UNREADABLE,
            'toolVFresh': TOOL_V_FRESH,
            'toolVStale': TOOL_V_STALE, 'toolVAgain': TOOL_V_AGAIN,
            'toolVKept': TOOL_V_KEPT, 'toolHtmlSeen': TOOL_HTML_SEEN,
            'toolDelText': TOOL_DEL_TEXT, 'toolDelMd': TOOL_DEL_MD,
            'toolTwin': TOOL_TWIN, 'toolTwinLead': TOOL_TWIN_LEAD,
            'toolTail': TOOL_TAIL,
            'streamText': STREAM_TEXT, 'streamTail': STREAM_TAIL,
            'toolPath': str(tool_path.relative_to(vault))}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=8878)
    parser.add_argument('--manifest', default='')
    args = parser.parse_args()
    sys.path.insert(0, str(ROOT))
    from tests.fixture_root import fixture_root
    from tests.snip_sandbox_routes import install_test_routes
    # 临时库优先由父进程拥有:口径和理由都在 tests/fixture_root.py
    with contextlib.ExitStack() as stack:
        temp = fixture_root(stack, 'kb-snip-qa-')
        data, vault = Path(temp) / 'data', Path(temp) / 'vault'
        os.environ['KB_DATA_DIR'] = str(data)
        os.environ['KB_VAULT_ROOT'] = str(vault)
        vault.mkdir(parents=True, exist_ok=True)
        from kb import pipeline
        pipeline.start_scheduler = lambda: None
        manifest = build(vault)
        if args.manifest:
            Path(args.manifest).write_text(json.dumps(manifest, ensure_ascii=False),
                                           encoding='utf-8')
        import uvicorn
        from kb.server import app
        from kb.web import chat_api
        chat_api.run_ask_stream = _stub_stream   # 见 `_stub_stream` 上面那段
        # **得在 uvicorn 起之前挂**:Starlette 的中间件栈一启动就定型了
        install_test_routes(app, vault)
        print('Isolated snippet-favorite QA: no real data or external requests', flush=True)
        uvicorn.run(app, host='127.0.0.1', port=args.port, log_level='warning')


if __name__ == '__main__':
    main()
