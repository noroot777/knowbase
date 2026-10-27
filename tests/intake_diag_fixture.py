# -*- coding: utf-8 -*-
"""给「问题与待办」那块面板摆一套**每一档都占上**的样例,供浏览器探针用。

**单独一个夹具、不往别人的沙箱里插条目**(理由同 `tests/fulltext_sandbox.py` 的
那段说明):`daily_sandbox` / `subs_layout_fixture` 有好几条检查认的是「列表第一行」
「素材清单第 N 条」,往里插条目会把那些位置挪开,于是本轮的改动表现成别人的探针
变红,而红的原因跟它们要验的事情毫无关系。

摆出来的形态按 `kb.intake_diag.lanes.LANES` 一档一条,**外加三类来源级问题**:
  ① 正文:待补 / 挂起等人看 / 重试耗尽
  ② 翻译:排队 / 重试耗尽(后者已经按原文入过库)
  ③ 入库:正文已定、等着写进知识库
  ④ 已完成:正文刚补齐(落在「最近 7 天」窗口里)
  ⑤ 已搁置:暂缓一条、不再收录一条
  ⑥ 来源级:X 启动限流(`last_error`)、覆盖未完成(`last_note`)、停用的订阅连不上

**X 那两句原话从 `x_page_state` 的工厂现取**,不手抄:面板的分类也是从那里取的
(`sources._NEEDLES`),两边同源才保证「改了文案分类自动跟上」这件事真的被验到。
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta

from kb.feeds import fulltext, registry as reg, translate_scope
from kb.feeds import x_page_state as xstate
from kb.intake_diag import shelf

AGG = 'https://agg.example.org/feed.xml'


def _key(kind: str, url: str) -> str:
    return hashlib.sha256(f'{kind}:{url}'.encode()).hexdigest()[:32]


def populate() -> dict:
    now = datetime.now()

    def at(hours: float) -> str:
        return (now - timedelta(hours=hours)).strftime('%Y-%m-%d %H:%M')

    agg = reg.save_subscription('rss', {
        'name': '聚合源（样例）', 'url': AGG, 'group_name': '聚合源', 'fulltext': True})
    plain = reg.save_subscription('rss', {
        'name': '自带全文的博客（样例）', 'url': 'https://solo.example.org/feed.xml'})
    body = ('这是样例正文第 %d 段，它和别的段说的不是同一件事，整页加起来要过得了'
            '正文提取器那道最小长度的门槛，而且不能是同一段在复读。')
    full = '\n\n'.join(body % i for i in range(1, 10))
    shell = '标题壳 (\nexample.org\n)\n10-08\n108 HN Points'

    reg.put_items(agg, [
        dict(url='https://news.example.org/queued', published=at(1),
             title='还排在补抓队列里的一条', body=shell),
        dict(url='https://news.example.org/held', published=at(2),
             title='抓到了页面、但看不出哪一段是正文', body=shell),
        dict(url='https://news.example.org/exhausted', published=at(3),
             title='补抓重试预算已经用完的一条', body=shell),
        dict(url='https://news.example.org/ingest', published=at(4),
             title='正文已经定下来、等着写进知识库', body=full),
        dict(url='https://news.example.org/recent', published=at(5),
             title='正文刚补齐的一条', body=full),
        dict(url='https://news.example.org/snoozed', published=at(6),
             title='被暂缓的一条', body=shell),
        dict(url='https://news.example.org/excluded', published=at(7),
             title='被标成不再收录的一条', body=shell),
    ])
    reg.put_items(plain, [
        dict(url='https://solo.example.org/translate-queued', published=at(2),
             title='An article waiting for its translation', body=full),
        dict(url='https://solo.example.org/translate-exhausted', published=at(3),
             title='An article whose translation kept failing', body=full),
    ])

    def key(url: str) -> str:
        return _key('rss', url)

    with reg.db() as c:
        c.execute("UPDATE feed_items SET fulltext_hold=? WHERE key=?",
                  ('页面上没有识别出正文，可能需要登录或由脚本渲染；已挂起等人看一眼',
                   key('https://news.example.org/held')))
        c.execute("UPDATE feed_items SET fulltext_tries=?,fulltext_error=? WHERE key=?",
                  (fulltext.MAX_TRIES, '对方这一次把请求拦在了一道验证上（HTTP 403）',
                   key('https://news.example.org/exhausted')))
        # 入库那一档:正文已定(`fulltext_at` 落下)、还没导出。
        c.execute("UPDATE feed_items SET fulltext_at=?,content_kind='content' WHERE key=?",
                  (reg.now(), key('https://news.example.org/ingest')))
        # 已完成那一档:**补抓过**(`fulltext_tries>0`)而且落在最近 7 天窗口里。
        c.execute("UPDATE feed_items SET fulltext_at=?,fulltext_tries=1,"
                  "content_kind='content',exported_hash=content_hash WHERE key=?",
                  (reg.now(), key('https://news.example.org/recent')))
        # 翻译两档:外文、正文已定(壳子不许进翻译队列,见 fulltext.unsettled_sql)。
        for url, tries in (('https://solo.example.org/translate-queued', 0),
                           ('https://solo.example.org/translate-exhausted',
                            translate_scope.MAX_TRIES)):
            c.execute("UPDATE feed_items SET lang='en',content_kind='content',"
                      'translate_tries=?,translate_error=? WHERE key=?',
                      (tries, '翻译失败：上游返回 503' if tries else '', key(url)))
        # 重试耗尽那一条已经按原文入过库 —— 面板上那句「已按原文写进知识库」
        # 只能用真的落了盘的那一档(`translate.stuck` 的 shipped)。
        c.execute('UPDATE feed_items SET exported_hash=content_hash WHERE key=?',
                  (key('https://solo.example.org/translate-exhausted'),))
    shelf.shelve([key('https://news.example.org/snoozed')], 'rss', shelf.SNOOZE,
                 '等作者补完再说')
    shelf.shelve([key('https://news.example.org/excluded')], 'rss', shelf.EXCLUDE,
                 '这个站点的内容跟我无关')

    # ── 来源级问题三类 ───────────────────────────────────────────────────
    boot = reg.save_subscription('social', {
        'name': 'X 账号（启动限流样例）', 'url': 'https://x.com/sample_boot'})
    cover = reg.save_subscription('social', {
        'name': 'X 账号（覆盖未完成样例）', 'url': 'https://x.com/sample_cover'})
    dead = reg.save_subscription('rss', {
        'name': '连不上的订阅（样例，已停用）', 'url': 'https://down.example.org/feed.xml',
        'enabled': False})
    # **还得有一条「现在就能重试」的**:X 那两条在没配好通道的环境里一律
    # `actionable: False`(端点会 400),只摆它们的话「重试此范围」那条路在探针里
    # 压根走不到。网页专栏不用 X 通道,所以这一条是真能点的。
    page = reg.save_subscription('web', {
        'name': '网页专栏（覆盖未完成样例）', 'url': 'https://column.example.org/'})
    with reg.db() as c:
        c.execute('UPDATE subscriptions SET last_error=?,failures=?,retry_at=? WHERE id=?',
                  (str(xstate.session_throttled()), 2, 0, boot['id']))
        c.execute('UPDATE subscriptions SET last_note=?,config=? WHERE id=?',
                  ('滚动次数用完，没有读到上次的位置；已取得的内容照旧入库',
                   json.dumps({'x_watermark_browser': at(72)}), cover['id']))
        c.execute('UPDATE subscriptions SET last_error=?,failures=? WHERE id=?',
                  ('连接失败：无法解析主机名 down.example.org', 5, dead['id']))
        c.execute('UPDATE subscriptions SET last_note=? WHERE id=?',
                  ('这一轮只读到前 2 页，不在时间线底部；水位没有前移', page['id']))
    return {'agg': agg['id'], 'plain': plain['id'], 'boot': boot['id'],
            'cover': cover['id'], 'dead': dead['id'], 'page': page['id']}
