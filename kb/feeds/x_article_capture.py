"""人工补 X 长文:人在**自己的**浏览器里打开那篇长文、点一下采集扩展,正文交回那条还在等它的推文。

**硬约束在 `docs/decisions/x-article.md` 的「人工补长文」那一节 —— 改这里之前先读那一份。**

为什么要有这条路:长文正文没取到的推文**不入库**(空正文一落库就 `DO NOTHING` 改不回来),
它作为候选留在 X 五天队列里(`x_intake_candidates`,`state='pending'`)按退避重试。不登录那一档
只有 FxTwitter 拿得到长文 —— X 的文章页不登录是登录墙 —— 它问不到的那几篇会一直挂着,过了
五天窗口就悄悄掉出去。而人自己的浏览器是登录着的,他手动打开那一页就看得到。

**它和「从浏览器采集」(`kb.browser_capture`)是两件事,不共用那套待办**:那一套补的是**已经在
`feed_items` 里**的文章行,X 观点被它明确拒掉;这里补的是**还没入库**的推文候选。共用的只有扩展、
配对密钥和那一段读页脚本。

三条口径都只有一份,这里只转手:
- **这一页读出来的是不是这一篇、读到了什么**:`x_article.read_snapshot`(登录通道的浏览器后路也过它);
  读页脚本是同一个文件(`x_article_read.js`)。
- **长文怎么并进推文**:`x_article.merge`(自动那条路和手动收录也过它)。
- **这一条算不算可交付的完整正文**:`x_intake_store.complete`。

**这里不直接写 `feed_items`**:并好的正文经 `store.finish`(自动那条路的正文阶段也走它)把候选
从 pending 转成 ready,投递照旧由下一轮同步做(覆盖确认、存量修补、投递那一整套一道不少)。
"""

from __future__ import annotations

import json
import re
import sqlite3

from kb.feeds import registry as reg, x_article, x_post
from kb.feeds import x_intake_store as store
from kb.maintenance import LockBusy

# 人点了「采集这篇长文」在等的这一下,维护锁最多等这么久(口径同收藏那一侧的 `_DOC_LOCK_WAIT`):
# 等不到就如实说一句让人过会儿再点,不让整条 HTTP 连接陪着一轮大编码挂上几分钟。
_LOCK_WAIT = 10.0

# 人此刻站着的那一页:`/<handle>/article/<推文 id>`(长文页)或 `/<handle>/status/<推文 id>`(推文页)。
# 只认推文 id —— handle 大小写不敏感、改名后旧地址照样跳得到(口径同 `x_article._spot`)。
_PAGE = re.compile(r'^https://(?:www\.|mobile\.)?(?:x|twitter)\.com/[A-Za-z0-9_]{1,20}/(article|status)/(\d+)(?:[/?#]|$)')

# 还在等长文的候选:状态 pending、还在它那条订阅的五天窗口里(口径同 `x_intake_store.pending`,
# 只是**不看退避** —— 人手动补的这一下不该等自动重试的冷却)。这一句只看上一轮同步留下的进度,
# 「那条订阅现在还是不是这个身份」由 `_current` 再筛一道(SQL 看不到当前订阅)。
_WAITING = '''SELECT p.post_id,p.entry,p.published,p.failures,p.reason,l.source_id,l.identity
    FROM x_intake_candidates p JOIN x_intake_links l USING(post_id)
    JOIN x_intake_sources s ON s.source_id=l.source_id AND s.identity=l.identity
    WHERE p.state='pending' AND p.published>=s.floor'''


def _current() -> dict:
    """现存订阅按 (id, 身份) 索引。**列表、这一页能不能补、交回时挑谁写,都过这一份**:
    订阅改过(改指别的账号、或只是编辑过一次,身份带着 rev)之后、下一轮同步之前,旧关联和旧进度都还在,
    只看 SQL 会让扩展一直说「可以补」,交回时才被拒。"""
    return {(source['id'], store.identity(source)): source for source in reg.subscriptions('social')}


def spot(url: str) -> tuple[str, str]:
    """一个地址落在哪一篇上。返回 (`article`/`status`, 推文 id);认不出来是 ('', '')。"""
    m = _PAGE.match((url or '').strip())
    return (m.group(1), m.group(2)) if m else ('', '')


def _owes(entry: dict) -> bool:
    """这一条候选是不是在等**长文**(而不是别的正文待办,比如长推展开)。"""
    return x_post.HINT not in entry and (x_article.HINT in entry or x_article.needs_article(entry))


def _view(row) -> dict:
    entry = json.loads(row['entry'])
    return dict(post_id=row['post_id'], url=entry['url'], article_url=x_article.article_page(entry['url']),
                author=entry.get('author', ''), published=entry.get('published', ''),
                text=x_article.lead_text(entry.get('body') or '')[:200],
                source_id=row['source_id'], failures=row['failures'], reason=row['reason'])


def owed(source_id: int | None = None) -> list[dict]:
    """还在等长文正文的推文,新的在前。`source_id` 给了就只列那一条订阅的。"""
    sql, args = _WAITING, ()
    if source_id is not None:
        sql, args = sql + ' AND l.source_id=?', (source_id,)
    with store.read_db(timeout=30) as conn:
        rows = conn.execute(sql + ' ORDER BY p.published DESC', args).fetchall()
    current, seen, out = _current(), set(), []
    for row in rows:
        if (row['source_id'], row['identity']) not in current:
            continue
        if row['post_id'] not in seen and _owes(json.loads(row['entry'])):
            seen.add(row['post_id'])
            out.append(_view(row))
    return out


def _stored(conn, ident: str) -> bool:
    return conn.execute("SELECT 1 FROM feed_items WHERE kind='social' AND url LIKE ? LIMIT 1",
                        (f'%/status/{ident}',)).fetchone() is not None


def here(url: str) -> dict:
    """人站在这一页上,扩展问「这一页能做什么」。一问一答,只读。

    `situation`:`fill` = 这一篇正等着,读这一页就行;`open_article` = 站在推文页上,
    要先点开那篇长文;`stored` = 这一条已经入库了;`absent` = 知识库没在等这一篇;
    `other` = 这一页不是推文也不是长文。
    """
    kind, ident = spot(url)
    if not ident:
        return dict(situation='other', detail='这一页不是 X 的推文或长文。', post=None)
    post = next((one for one in owed() if one['post_id'] == ident), None)
    if post is None:
        with reg.read_db(timeout=30) as conn:
            stored = _stored(conn, ident)
        return dict(situation='stored' if stored else 'absent', post=None,
                    detail='这一条已经在知识库里了。' if stored else '知识库没在等这一篇长文的正文。')
    if kind == 'status':
        return dict(situation='open_article', post=post,
                    detail='这条推文的长文还缺正文。点开推文里的那篇长文，在长文页上再点一次扩展。')
    return dict(situation='fill', post=post, detail='这篇长文的正文还缺着，读这一页就能补上。')


def save(post_id: str, page_url: str, snapshot) -> dict:
    """把人那一页读到的长文写回候选。拒绝一律抛 `ValueError`(文案直接给人看),什么都没写。"""
    kind, ident = spot(page_url)
    if (kind, ident) != ('article', str(post_id)):
        raise ValueError('这一页不是那篇长文的页面，没有写入。请在那篇长文的页面上点扩展。')
    try:
        got = x_article.read_snapshot(snapshot, ident)
    except x_article.ArticleUnavailable as why:
        raise ValueError(f'这一页没读成：{why}。没有写入。') from None
    if got is x_article.NOT_YET:
        raise ValueError('长文页还没显示出正文（多半还在加载，或者这个浏览器没登录 X）。等它显示出来再点一次。')
    if got is None:
        raise ValueError('X 把这一页转回了推文页：这条推文没有长文。没有写入。')
    with store.read_db(timeout=30) as conn:
        task = conn.execute("SELECT * FROM x_intake_candidates WHERE post_id=? AND state='pending'",
                            (ident,)).fetchone()
        # 关联的口径同列表(`_WAITING` + `_current`):身份是那条订阅**现在**指着的账号、推文在它的有效
        # 下界之内。订阅改指别的账号之后,旧关联要等它下一轮同步才清掉;只认 source_id 会挑中它,写不进去。
        links = [(row['source_id'], row['identity']) for row in conn.execute(
            _WAITING + ' AND p.post_id=? ORDER BY l.source_id', (ident,))]
    entry = json.loads(task['entry']) if task else None
    if not entry or not _owes(entry):
        raise ValueError('这一条已经不在等长文了（可能刚被自动补上或已入库）。没有写入。')
    merged = x_article._strip_hint(x_article.merge(entry, got))
    if not store.complete(merged):
        raise ValueError('读到的内容拼不成一条完整的正文，没有写入。')
    # **出口只走 `store.finish` 那一个**(自动那条路的正文阶段也走它):排除、存量修补、下界、
    # 订阅改过没有,全在它那一份里判;互斥待在它那条 UPDATE 自己的 WHERE 里(entry + failures 的
    # CAS)—— 这中间自动那条路刚把它补上或改过,这一下就是 0 行。外面这一次读只为拿到身份。
    #
    # **不许只把正文写回、留着 pending 等下一轮**:下一轮的时间线扫描会按时间线上那一份(空正文、
    # 带长文信号)把 pending 的候选整份重写(`store.stage`),人刚补的正文当场丢掉。
    sources = _current()
    owner = next((sources[one] for one in links if one in sources), None)
    if owner is None:
        raise ValueError('这条推文所属的订阅已经不在了（或改指了别的账号），没有写入。')
    try:
        store.finish(owner, dict(task), [merged], [], '', lock_wait=_LOCK_WAIT)
    except (LockBusy, sqlite3.OperationalError):
        raise ValueError('同步正在写库，稍后再点一次。没有写入。') from None
    with store.read_db(timeout=30) as conn:
        after = conn.execute('SELECT state, entry FROM x_intake_candidates WHERE post_id=?', (ident,)).fetchone()
    if not after or after['entry'] != json.dumps(merged, ensure_ascii=False):
        raise ValueError('这一条刚被别的操作改过，没有写入。请重新确认这一页。')
    if after['state'] == 'repair':
        detail = '长文正文已交回；库里那一条的存量正文缺少同版证明，要走版本化回填，没有覆盖旧正文。'
    elif after['state'] == 'excluded':
        detail = '长文正文已交回，但这一条已被设为不再收录。'
    else:
        detail = '长文正文已交回，下一次同步这个账号时入库。'
    return dict(post_id=ident, title=merged['title'], characters=len(got['body']), detail=detail)
