# -*- coding: utf-8 -*-
"""**可撤销的搁置**:「暂缓」和「不再收录」各一档,都只影响被点名的那一条。

**硬约束在 `docs/decisions/intake-diagnostics.md` 第一节 —— 改这块之前先读那一份。**

**它不是 `kb/feeds/exclusion.py`。** 那一份是**永久移除**:把 key 写进
`feed_item_exclusions`、删掉 `feed_items` 那一行、删掉导出的 md、从 kb.db 里收掉
文档,还装了一个 BEFORE INSERT 触发器让这条链接**再也收不回来**。它答的是「这篇
我永远不要」,没有撤销路径,所以不能拿它冒充面板上那颗「可恢复的不再收录」——
一颗写着「可在已搁置里恢复」的按钮背后是一次不可逆删除,是最糟的一种谎。

**这一份一个字节的正文、一行条目、一个文件都不碰**:只在一张旁表上记一句「这一条
先别动」。恢复 = 删掉那一行,条目回到搁置前**逐字相同**的状态(搁置不改任何
`fulltext_*` / `translate_*` 列,所以没有「恢复成什么」的二义)。

**两种身份,两个问题,各有自己那一列。**
- **「哪一世的内容」= `feed_items.life`**(不可复用,见 `kb/feeds/schema.py`)。
  `item_key` 按网址算:存量清理删掉一条、下一轮又从同一个链接收回来,key 一个字都
  没变,而库里那是另一行、另一份正文。上一版认这件事靠「`fetched_at` 只在 INSERT
  时写」加「`content_rev` 一生只增」两道组合 —— 而那两列**都是可以重复的本地状态**
  (`fetched_at` 只记到秒;新插进来的 `content_rev` 从 0 起,上一世本来就是 0 时
  `>=` 照样成立)。**本地可重复的状态不是身份**,所以判据换成那一列的相等。
- **「这一世的哪一次决定」= `decision_id`**(每次 `shelve` 新发一个,改写也新发)。
  恢复要绑它:同一条内容先暂缓、恢复、再「不再收录」,处理记录里第一条那颗「恢复」
  若只按 key 删,会把**后来**那次决定一起取消(审查第 1 轮 F08)。`created_at` 顶
  不了这个用 —— 它只记到秒,三步都落在同一秒里是常事。

**认不出来就当「没有搁置」**(`item_life<>''` 那一道):新收回来的那一篇是一条全新
的待办,继承上一世的「不再收录」等于拿一个针对别的内容做过的决定静默挡住它,而面板
上什么都看不见。上一世留下的那几行留在表里不碍事(三处读都认不出它们),下一次对同
一个 key 搁置时由 `ON CONFLICT` 原地改写成这一世。
"""
from __future__ import annotations

import secrets

SCHEMA = '''
CREATE TABLE IF NOT EXISTS feed_item_shelf (
 item_key TEXT PRIMARY KEY, kind TEXT NOT NULL, decision TEXT NOT NULL,
 reason TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL,
 -- 排在最后,和老库 ALTER 补出来的列序一致(见 `kb/feeds/schema.py`)。
 -- 前两列是上一版的「哪一世」判据,现在**只留着不读**(元规则:迁移只增不减),
 -- 身份改认 `item_life`;它们仍然照写,人工排查时能看出这一世是什么时候收的。
 item_fetched_at TEXT NOT NULL DEFAULT '', content_rev INTEGER NOT NULL DEFAULT 0,
 item_life TEXT NOT NULL DEFAULT '', decision_id TEXT NOT NULL DEFAULT ''
);
'''

#: 两档搁置。**它们在面板上分开显示,绝不合并成一句「已处理」**(演示文档的状态
#: 契约:已搁置里暂缓与不再收录分别显示,都不计为解决)。
SNOOZE = 'snooze'
EXCLUDE = 'exclude'
DECISIONS = (SNOOZE, EXCLUDE)


def _match(alias: str) -> str:
    """「这一行搁置记录说的就是这一世的这一条」—— **搁置的身份判据只有这一份**。

    三处读都过它:`live_sql`(算哪一档)、`free_sql`(四族队列的准入)、`row_sql`
    (列表和详情上那枚标记)。各写一遍的后果是面板上标着「暂缓」而队列照旧去抓它,
    或者反过来 —— 而两种都是「按钮只在屏幕上改了个字」。

    `alias` 是外层那张 `feed_items` 在调用方语句里的名字(`feed_items` 或 `fi`)。
    **它不接外部输入**,取值全写死在本仓库的调用点上 —— 口径同
    `fulltext._eligible` 的 `ref`:拼进别人 WHERE 的那几段用全名,带别名的 SELECT
    用别名,否则列名在子查询里压根解析不出来。
    """
    return (f"sh.item_key={alias}.key AND sh.item_life<>''"
            f" AND sh.item_life={alias}.life")


def live_sql(alias: str = 'feed_items') -> str:
    """「这一条此刻真的被搁置着」。"""
    return f'EXISTS(SELECT 1 FROM feed_item_shelf sh WHERE {_match(alias)})'


def free_sql(alias: str = 'feed_items') -> str:
    """「这一条没有被搁置」。补抓、翻译、待办、**以及导出**四族判据都要 AND 上它。

    **导出那一道是承重处,而且它和前三道的方向是反的。** 交付层问的是「这一条可以
    写进 vault 了吗」,而它的判据是**队列的反集**(`sync._export` 的
    `AND NOT (fulltext.queued_sql())` / `AND NOT (translate.queued_sql())`)——
    于是往队列里加一道正向筛选,在交付那头会**取反成放行**:人点完「不再收录」,
    这一条当场「不在补抓队列里」、「不在翻译队列里」,导出照旧把它写进知识库
    (审查第 1 轮 F01 实测:三条未导出的内容全部标成「不再收录」,真跑一次
    `sync._export` 之后 vault 里出现 3 篇 md)。**离开某个队列不等于获准进入下一
    个环节**,所以交付层要自己明说一句「这一世没有被搁置」。

    **`fulltext.unsettled_sql` 是例外,那一道刻意不带它** —— 它答的是「这一条的
    正文还没定下来」(翻译闸门据此**排除**它)。带上之后,一条被暂缓、正文还是
    feed 给的跳转壳的条目当场变成「正文已定」、排进翻译队列 —— 同一条内容按壳
    买一次模型调用,而人刚刚说的是「这条先别动」。理由与事故形态见那个函数自己
    的说明和 `docs/decisions/intake-diagnostics.md`。
    """
    return 'NOT ' + live_sql(alias)


def admitted(key: str) -> bool:
    """**派发前的最后一道:这一条此刻还获准为它做事吗。**

    判据只有 `free_sql` 那一份(元规则:口径只有一份)。给补抓和翻译两个 worker
    用 —— 它们**取样一批、再逐条去发请求**,而两件事之间隔着前面几条的网络等待和
    模型调用,几分钟是常事。只认取样那一刻的结果就是把「取样时获准」当成「整批
    一直获准」:审查第 2 轮 R1 实测,第一条正在调模型的时候把第二条「不再收录」,
    第二条随后照旧被派发出去 —— 而那是一笔**还没花的钱**,人刚刚说的是「这条
    我不要了」。

    **它和写回那道判据不是一回事,两道都要。** 这一道管「别开始」(拒了就一次
    请求都不发);写回那道管「这次结果算不算」(`fulltext._save` / `_fail` 的
    WHERE,以及交付层 `sync._export` 那一句)。只有写回那道的话,钱已经花了、
    网络已经走了;只有这一道的话,它和真正发请求之间仍有一瞬。

    **它关不掉的那一瞬,如实说在这里:** 这一问返回之后、系统调用发出之前,搁置
    仍然可能落下。要连那一瞬也关掉,就得让 `shelve` 看得见「有一笔请求正在路
    上」—— 那是一列新的在途标记加一句新的按钮承诺(「已暂缓,但有一笔请求已经
    发出去了」),是另一份契约,不在这一轮的射程里。眼下的末态是:那一笔回来时
    补抓那一侧照旧被写回判据拒掉(一个字都不动),翻译那一侧按**已经付过钱**
    收下(理由在 `fulltext._save` 末尾那段),而交付层那一句保证它进不了知识库。
    """
    from kb.feeds import registry as reg

    with reg.db() as c:
        return bool(c.execute(
            f'SELECT 1 FROM feed_items WHERE key=? AND {free_sql()}',
            (key,)).fetchone())


def row_sql(alias: str = 'feed_items') -> str:
    """列表和详情要显示的那几列(逗号起头,直接拼在 SELECT 列表后面)。

    **和 `live_sql` 认的是同一世**(`_match`):只按 key 取的话,一条删掉重收的新
    内容会带着上一世的「暂缓 / 不再收录」标记出现在列表上,而它压根没有被搁置。
    `shelf_decision_id` 带出去是为了让恢复那一下绑得住「这一次决定」(见 `release`)。
    """
    def pick(column: str, name: str) -> str:
        return (f' (SELECT {column} FROM feed_item_shelf sh'
                f' WHERE {_match(alias)}) AS {name}')
    return ',\n' + ',\n'.join([
        pick('decision', 'shelf_decision'),
        pick('reason', 'shelf_reason'),
        pick('created_at', 'shelf_at'),
        pick('decision_id', 'shelf_decision_id'),
    ])


def shelve(keys, kind: str, decision: str, reason: str = '', lives=None) -> dict:
    """把这几条搁置起来。返回**真写进去了哪几条**和各自那次决定的编号。

    **判据待在写语句自己的 WHERE 里**(元规则):`kind` 约束写入本身,不靠「路由名
    对了」推断 —— 路由上那个 kind 过了合法性校验,不等于请求体里每个 key 都属于
    它(`fulltext.release` 那一侧实测过:网页专栏的 key 提给 RSS 的端点照样被改)。
    **这一世的身份也在同一条语句里现取**(`SELECT ... FROM feed_items`),不在
    Python 那边先 SELECT 一次再写:legacy `isolation_level` 下 SELECT 不开事务、
    不持锁,中间那一条可能已经被清理删掉又重新收回来。

    条目不存在、或者不属于这个来源时写不进去,`rowcount==0` 就是拒绝信号
    (元规则:0 行不是失败,统计按实际写入结果报)。**返回的是成功那一批的 key**,
    不是整份请求 —— 处理记录里那颗「恢复」只该作用于真搁置成了的那几条。

    **`lives` 是人勾选那一刻这几条各在第几世**(`{key: life}`,口径在
    `warrant.life_sql`)。「现取这一世」防的是并发写入方,防不了**旧表单**:列表在
    那儿开着的时候这条内容被清理删掉又从同一个链接收回来,这一下的「不再收录」会
    按在**新一世**上 —— 而人决定的是另一条内容。带了就 AND 进同一条语句,
    `rowcount==0` 即拒绝;没带照旧(存量调用方)。
    """
    if decision not in DECISIONS:
        raise ValueError('未知的搁置方式')
    from kb.feeds import registry as reg
    from kb.intake_diag import warrant

    done, ids = [], []
    with reg.db() as c:
        for key in keys:
            rid = secrets.token_hex(16)
            life_cond, life_args = warrant.life_sql(warrant.life_of(lives, key))
            if c.execute(
                'INSERT INTO feed_item_shelf'
                ' (item_key,kind,decision,reason,created_at,item_fetched_at,content_rev,'
                '  item_life,decision_id)'
                ' SELECT key,kind,?,?,?,fetched_at,content_rev,life,? FROM feed_items'
                f' WHERE key=? AND kind=? AND ({life_cond})'
                ' ON CONFLICT(item_key) DO UPDATE SET decision=excluded.decision,'
                ' reason=excluded.reason,created_at=excluded.created_at,'
                ' item_fetched_at=excluded.item_fetched_at,content_rev=excluded.content_rev,'
                ' item_life=excluded.item_life,decision_id=excluded.decision_id',
                    (decision, str(reason)[:300], reg.now(), rid, key, kind,
                     *life_args)).rowcount:
                done.append(key)
                ids.append(rid)
    return {'shelved': len(done), 'keys': done, 'decisions': ids}


def release(keys, kind: str, decisions=()) -> int:
    """恢复这几条:删掉搁置记录。返回真删掉了几行。

    **`decisions` 和 `keys` 一一对应,而且判据待在 DELETE 自己的 WHERE 里。**
    它说的是「我要取消的是**那一次**决定」:同一条内容先暂缓、恢复、再「不再收录」,
    处理记录里第一条那颗「恢复」若只按 key 删,会把后来那次「不再收录」一起取消
    (审查第 1 轮 F08)。已搁置列表里那颗「恢复」同理 —— 页面停留期间别处改过决定
    时,它手上那一份也是过时的。`rowcount==0` 就是拒绝信号:这一条的决定在那之后
    变过了,这颗按钮已经失效(元规则:0 行不是失败)。

    **缺编号时一律拒绝,不退化成按 key 删**:退化那一下正是上面那次事故,而「少删
    一行」的代价只是人去「已搁置」里再点一次。

    **`item_life` 也在同一条 WHERE 里,否则返回的那个数是句谎话。** 上一世留下的
    那几行还躺在表上(三处读都认不出它们),按 key + 决定编号删得掉 —— 于是一条
    删掉重收、**从来没有被搁置过**的内容,处理记录里那颗旧「恢复」点下去会报
    「已恢复 1 条」(审查第 2 轮 B7)。元规则:统计按**实际写入结果**报。删不掉
    那几行不碍事,下一次对同一个 key 搁置时由 `ON CONFLICT` 原地改写成这一世。

    `kind` 同样约束写入本身,理由见 `shelve`。
    """
    from kb.feeds import registry as reg

    wanted = list(keys)
    ids = list(decisions)
    if len(ids) != len(wanted):
        raise ValueError('恢复请求里缺少对应那次决定的编号，没有恢复任何内容；'
                         '请刷新再看一次')
    done = 0
    with reg.db() as c:
        for key, rid in zip(wanted, ids):
            done += c.execute(
                'DELETE FROM feed_item_shelf WHERE item_key=? AND kind=? AND decision_id=?'
                " AND item_life<>'' AND item_life=(SELECT life FROM feed_items"
                ' WHERE key=feed_item_shelf.item_key)',
                (key, kind, str(rid))).rowcount
    return done


def live_decisions(ids) -> list[str]:
    """这几个决定编号里,哪几个此刻还在表上。

    给处理记录那颗「恢复」用:后来又改过决定的那几笔,按钮要**明确失效**而不是点
    下去才拿到一句拒绝(原型的操作契约)。

    **「还在表上」不够,得「还按在这一世上」**(`_match`,和三处读同源):一条删掉
    重收的内容身上没有任何搁置,而上一世那一行还躺在旁表里 —— 只查这张表的话那颗
    旧「恢复」会显示为**可点**,点下去 `release` 还会报「已恢复 1 条」(审查第 2 轮
    B7)。两头一起钉:按钮的可点性和那一下真能删掉的行,问的必须是同一句话。
    """
    wanted = [str(one) for one in dict.fromkeys(ids) if one]
    if not wanted:
        return []
    from kb.feeds import registry as reg

    slots = ','.join('?' for _ in wanted)
    with reg.db() as c:
        rows = c.execute(
            'SELECT sh.decision_id FROM feed_item_shelf sh JOIN feed_items'
            f' ON {_match("feed_items")}'
            f' WHERE sh.decision_id IN ({slots})', wanted).fetchall()
    return [row[0] for row in rows]
