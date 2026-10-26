# -*- coding: utf-8 -*-
"""存量复查:把「正文不是这一条自己的内容」的条目收拾干净。

**两条路留下的存量都在射程里**(`_covered`):①补抓报了成功、而抓回来的其实不是
文章正文;②这一类来源压根不许「拿原文链接那一页替换正文」,而它手里拿着一份
`content` —— 那只能来自 `markdown_source.enrich` 或 `kb.browser_capture`,这两条
**不看订阅开关**。**而「怎么收拾」按来源能力分两支**(`_downgrade`):许可来源换壳
放回补抓队列,不许可来源只从备份装回原文、补抓标记一个都不碰。

这几道判据都是事后才加的(样板区那道在 2026-10-09 审查之后、准入那几道更晚),
而它要挡的东西**已经在库里了** —— 补抓一旦报成功,`fulltext_at` 落下、条目
离开重试队列,后面任何一轮同步都不会再碰它(见 `fulltext.queued_sql`)。所以
光修判据只管未来,存量得单独走一趟。

**判据只有一份,而且只取 `readable.boilerplate_reason`(版权区、作者简介)** ——
写库准入那八道**一道都不借**。那几道是给「刚抓回来的一页」用的,误拒在那条路上
代价是一次重抓、用户当场还看得见;**在这里代价是把正常正文换成壳、译文作废、
重导出一轮,不可逆**。同一个启发式担不起两种后果。两次借过来都当场借出了事故
(都在审查里实测到,数字和夹具见 `_not_article`):借 ⑧「不重复的字数不够」判坏了
正常的 Markdown 原文(它本来就允许重复),借 ⑥「通篇就是那几句提示」判坏了正常的
英文教程。**而最该借的那一道压根搬不过来**:① 问的是这一次响应自己带不带关卡标记,
而库里存着的只有那份正文,头和状态码早就不在了。

**所以「关卡页被写成正文」这一类存量,这个脚本收拾不了**(能力边界,不是漏掉的
一道):库里只剩那份纯文字,认出它要的 DOM、最终地址和响应头当时就没存。出路是
人点「重新抓一次」(`grab.replace`),而以后 `admit` 会先把它们拦住。
**而「存量里有没有」这一问,这个脚本答不了,别拿它的数字去指导清理。** 生产只读
实测说的是**那些文字里找不出一条可识别的关卡提示**(份额最高 0.0,数字在
`_suspect` 头上)—— 那推不出「存量里一条关卡页都没有」:只靠 DOM 组件认出来的
那一类,正文和普通文章逐字相同、份额同样是 0。口径同 `kb/feeds/fulltext.py` 模块
说明和 `docs/decisions/fulltext.md` 那条,三处说的是同一件事。

**那一份标准抽成 `_not_article`,两处消费:挑哪几行要收拾(`_suspect`)、以及从
备份里挑哪一份内容当壳(`_from_backup`)。** 两处各写一遍的后果实测过(审查第 1
轮):扫描那一侧认得出、恢复那一侧还按旧标准放行,于是一份同样不合格的较新备份
被当成有效原摘要装回来,`_downgrade` 紧接着清掉 `fulltext_at` —— 这一条从此连扫描
都选不中,而正确的较旧备份明明还在。

**「放回队列」不够,必须连那份 body 一起换掉。** 第 1 轮只清了三个标记
(`fulltext_at` / `fulltext_error` / `fulltext_tries`)和译文,body 原样留着 ——
于是重抓三轮都失败之后它**离开补抓队列**,而 `translate.queued_sql` 这时不再
排除它:那段版权声明被**重新翻译一遍、重新导出**,阶段还报「已保存 1 条到
知识库」,`_suspect` 也再也选不中它(它只认 `fulltext_at<>''`)。换句话说清
标记只是让已知的错误内容又走了一遍正文消费链,还多花一次翻译钱
(2026-10-09 第 2 轮审查三轮实测)。

**换完之后它就是一条「从没补抓过」的普通条目**:body 是壳、
`content_kind='summary'`、`content_hash` 跟着重算。于是三轮重抓失败后按壳
入库 —— 那和一条压根没开过补抓的条目待遇完全一样,**是正确的降级**,不需要
任何新列、新标记或新的排除条件。判据自己也不会再选中它(壳过得了
`boilerplate_reason`)。

**壳从哪儿来,两条路,各有判据**:
①**备份库**(`--backup`,或自动扫 `<data>/backups/*/feeds.db` 按新旧倒序)。
  第 1 轮我断言"原始那份壳救不回来",**那是错的** —— 回填前自己做的那份备份
  里就有(审查实测:Stripe 68 字、Fortune 118 字)。恢复回来的壳要**再过一遍
  判据**:备份可能是在某一轮错误补抓之后做的,里面那份同样是样板文字,
  那就不算有效壳。
②**按标题和链接重建**。备份里没有(或者里面那份也过不了判据)时的兜底。
  形态和 feed 原本那份壳一致(标题 + 链接),是从库里现成的字段拼的,不编造
  任何内容。比留着 455 字版权声明严格地好:检索、提取打标、日报素材都不再
  被它污染。
  **这一条只对许可来源成立** —— 它的前提是「下一轮同步会重抓一次」。不许可来源
  不进补抓队列,在那儿拿两行标题链接盖掉已经在库里的正文就是**把内容毁掉**,
  而且没有任何一轮同步会来补,所以那一支只认 ①,没有备份就报出来让人处理
  (`_plan` / `cmd_scan` 的「收拾不了」那一栏)。

**译文一并作废**:那份译文译的是这段样板文字,body 一换它就无效了(口径同
`fulltext._save`)。`extract_stale=1` 留一个待办,新 body 落盘之后由
`fulltext.retire_stale` 让按旧内容算出来的摘要、主题和实体作废。
`exported_hash` 不动 —— body 一换 `content_hash` 就不一样了,导出闸门
(`exported_hash<>content_hash`)自然成立,下一轮同步会把 vault 里那篇
覆盖掉。

**不取维护锁,也不问管道忙不忙 —— 换来的那道保护在 `_downgrade` 的
WHERE 里。** 那两道闸是给不可逆删除用的(见 `scripts/feed_trim_history.py`),
而且对这件事压根不适用:补抓那一侧的 `fulltext._save` 自己就不持维护锁,
这边取了也挡不住它。
**原来这儿写着「最坏的交错后果是某一条被多抓一次」,那句话是错的。**
第 1 轮版本只改几个标记(那些确实幂等、谁盖谁都一样),第 2 轮加上 `body=?`
之后这条写回就变成了「拿一份旧快照覆盖正文」—— 而 `_suspect` 读完到真写回
之间还隔着一段备份库 I/O:同步在这期间把这一条补抓成功、译文和 vault 都写
好了,随后这份旧计划照样按 `key` 盖回去,914 字完整正文连同中文译文和成功
标记一起变回 29 字的标题加链接,后面三轮再抓不到就永久停在壳上
(2026-10-09 第 3 轮审查双线程实测,独立复现三次)。
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from kb.feeds import fulltext, readable, registry as reg       # noqa: E402
from kb.source_types import SOURCE_TYPES, feed_types, permits_link_body   # noqa: E402


def _not_article(body: str) -> str:
    """这段文字**自己**有没有把「这不是文章正文」写在脸上。空串 = 没有。

    **本脚本的那份标准只有这一处,两边消费**:扫描挑哪几行要收拾(`_suspect`)、
    以及从备份里挑哪一份内容当壳(`_from_backup`)。只在前一处更新标准的后果见
    模块说明那一段 —— 不合格的备份被当成有效原摘要装回来,而 `_downgrade` 紧接着
    清掉 `fulltext_at`,这一条从此连扫描都选不中(元规则:口径只有一份)。

    **就是 `boilerplate_reason` 一道,写库准入那八道一道都不借。** 三条路的误判代价
    各不相同:手动收录那边用户当场看得见、能点「重新抓一次」;补抓那边没人在看、
    三次额度用完就永久退回摘要(审查实测);而**这条路是主动把已经在库里的那份正文
    换成壳、译文作废、重导出**。前两条是「这一次没拿到」,这一条是「把拿到的毁掉」。
    两次借过来都当场实测到误删正常内容:
    - 借 ⑧(`body_confirmed`,不重复的字数要过 `MIN_CHARS`):一份 646 字、按行去重
      254 字的正常 quick-start README 被判坏(几个平台各一段同样的安装命令)。它本来
      就不走 `admit` —— `fetch_one` 有两条出口交出 **Markdown 原文**(`.md`/GitHub
      地址、`text/markdown` 响应),Markdown 允许重复。
    - 借 ⑥(`_gate_share`,关卡句的份额):**那一侧有一半的证据已经不成立了,结论
      却一个字没变。** 当时那篇被判坏的英文设置教程,根在句界不在这里(`_SENTENCE`
      原先不切 `. `,一整段英文只算一句,提一次「enable JavaScript」就是 100%);句界
      修好之后它是 **0.143**,早就落到线下。但 **Markdown 那份教程照旧被判坏**:标题
      带正文两行都提到它,按 `\n` 分句就是 2/2 = **100%**,与句界那一版无关。连 feed
      自己给的、**还没补抓过**的 158 字摘要都会被选中。

    这两样都不是「再调一调就能用」,而且这一版多了一条更硬的理由:**⑥⑦ 现在交出的
    结论是「证据不足」而不是「确定关卡」**(`grab_page.Admission.unsure`)—— 在准入那
    条路上它只换来一次重抓加一条待人看的记录,而在这条路上拿它去办的事是**把已经在库
    里的正文换成壳**。拿「我没能确认」去执行一个不可逆动作,方向是反的。库里那份文字
    答得出的只有「它提到过那几个词」,所以这里**宁可认不出**。
    """
    return readable.boilerplate_reason(body)


def _covered() -> str:
    """射程:**这一条的正文有可能是「`url` 指向的那一页」写进来的**,两支并集。

    ①**属于某条开着补抓的订阅**(`s.fulltext=1`)—— 补抓那条路,含旧库误开的。
    ②**这一类来源不许 `body_from_link`、而 `content_kind='content'`** —— 另外两条
      **不看订阅开关**的路(`markdown_source.enrich`、`kb.browser_capture`)留下的
      痕迹。这几类来源自己写的形态是 `summary`(简报,`digest.py`)或 `post`
      (大V,`fetching`),`content` 只可能来自外链那一页。

    **射程不受 `permits_link_body` 约束,受它约束的是写回时放不放回补抓队列**
    (`_downgrade` 的两支)。两件事合成一个判据的后果实测过(审查第 1 轮):
    `_suspect` 改用 `fulltext._eligible()` 之后,简报 / 大V上已经被换错的正文
    **从此没有任何工具够得着** —— 全库扫、`--kind digest`、`--source <那条订阅>`
    三种射程一律 0 条,还以「没有需要复查的条目。」成功退出。而生产库里当时就
    躺着一条(TLDR AI 的一条简讯,正文是别人家仓库的 README,9242 字、已导出)。

    ② 这一支是**结构事实**,不是启发式:它问的是「这条的正文形态本该是什么」,
    答案由采集通道自己写死,不靠任何「这段文字像不像正文」的猜。所以它和
    `_not_article` 并列成立(见 `_why`)—— 那条 README 逐字是一篇正常文章,
    `boilerplate_reason` 对它返回空串(生产实测),光靠文字判据永远选不中它。

    **② 认的是 `content_kind` 这个结构信号,它只覆盖「会改 content_kind 的那几条
    路」。** 曾经漏过的是 `grab_store.replace`(人点「重新抓一次」):它刻意不动
    `content_kind`(一条推文换了正文还是推文),于是简报那条被换掉正文的简讯在这儿
    **一条都选不中**。根治在写入层 —— 那几条 UPDATE 现在自己的 WHERE 里就带着能力
    判据(`kb.source_types.link_body_sql`),不许可来源的正文**不会再**被外链那一页
    换掉,所以 ② 看得见的那一类就是今后唯一还会出现的一类。

    **把 ② 换成「和备份比 `content_hash`」试过,不可取**:生产只读实测
    (2026-10-26,`data/feeds.db` 对 20261021 那份全库备份)不许可来源里 hash 与备份
    不同的 31 条**全是 social**,来路是 X 那一侧的合法长文回填(`x_article_backfill`,
    空正文→推文自己的长文);把它们选中之后不许可那一支只认备份,会拿备份里那条短推
    文盖掉回填好的长文 —— 这是制造破坏,不是「顶多重抓一次」。同一次实测里 `digest`
    的 231 条 `content_rev` 全为 0(`replace` 必然 +1),说明那个入口在生产库上还没
    造成过污染,存量不需要一道会误伤的射程。
    **扫不中不等于「确定没有」,所以空结果那句话要把这层边界说出来**(见 `_nothing`)。
    """
    kinds = ','.join(f"'{kind}'" for kind in feed_types()
                     if not permits_link_body(kind)) or 'NULL'
    return ('(EXISTS(SELECT 1 FROM subscription_items x JOIN subscriptions s'
            ' ON s.id=x.subscription_id WHERE x.item_key=fi.key AND s.fulltext=1)'
            f" OR (fi.kind IN ({kinds}) AND fi.content_kind='content'))")


def _why(row: dict) -> str:
    """这一条**为什么**要收拾。空串 = 不收拾。

    两道并列,先问结构、再问文字:不许 `body_from_link` 的来源手里拿着一份
    `content`,这件事本身就说明那不是它自己的内容;其余的交给 `_not_article`。
    """
    if not permits_link_body(row['kind']) and row['content_kind'] == 'content':
        return '正文是外链那一页的内容，不是这一条自己的'
    return _not_article(row['body'])


def _suspect(kind: str | None = None, source: int | None = None) -> list[dict]:
    """射程(`_covered`)之内,`_why` 答得出理由的那些。

    **射程不是"报过补抓成功"**(`fulltext_at<>''`)。后者
    漏掉的恰恰是这个脚本自己制造出来的中间态:第 1 轮版本只清标记不换 body,
    于是那两条生产条目的 `fulltext_at` 和 `fulltext_tries` 都归了零、body 还是
    455 字版权声明 —— 和「从没补抓过」长得一模一样,再跑一次复查**一条都选不中**
    (2026-10-09 第 2 轮审查点名的就是这个:「不再被复查选中」)。

    **在射程 ① 里"body 是样板文字"就是充分判据**:HN Buzzing 这类聚合源的
    feed 原本给的是跳转壳(标题 + 域名 + HN 得分),过得了判据;真正文同理。
    一条 body 是版权声明或作者简介的,只能是补抓这条路写进去的。**既没开过补抓、
    正文形态也正常的订阅一个都不碰** —— 它们的正文是 feed 自己给的,压根不在
    `_covered()` 里。

    **写库准入那八道一道都不借**,理由和两次借出来的事故在模块说明那一段。
    生产只读实测(2026-10-21,`data/feeds.db`)是这个决定的底:开了补抓的订阅名下
    1021 条,其中 352 条还没补抓过(壳,`substance` 全落在 37~133)、669 条报过成功。
    那 669 条里**关卡句份额最高的是 0.0** —— 存量的**文字里**找不出一条可识别的关卡
    提示,借 ⑥ 过来一条也收拾不到、只会误伤。(这个 0.0 是按旧句界量的,换成新句界只会
    更低或持平:旧句界分段更粗、同样的命中摊在更少的句子上,份额只会偏高。)**这推不出「存量里没有关卡页」**:身份
    证据多半在 DOM 上,而库里只剩文字 —— `COMPONENT_ONLY` 那张只靠编排脚本认出来的
    关卡页,正文和一篇普通文章逐字相同、份额同样是 0。丢掉的身份追认不回来;「内容质量」那一道当时挑出 2 条(Meta 产品落地页 934 字 /
    不重复 393、英国地方报的面包屑 473 / 392),它判坏正常 Markdown 的那次实测就在
    同一轮里。**认不出来的代价是库里留着两条瘦正文,认错的代价是把正常正文写死** ——
    后者不可逆地更贵。
    """
    where = [_covered()]
    args: list = []
    if kind:
        where.append('fi.kind=?')
        args.append(kind)
    join = ''
    if source is not None:
        join = ' JOIN subscription_items si ON si.item_key=fi.key'
        where.append('si.subscription_id=?')
        args.append(source)
    sql = (f'SELECT DISTINCT fi.key,fi.kind,fi.title,fi.url,fi.body,fi.path,fi.content_kind,'
           f"fi.exported_hash<>'' AS shipped FROM feed_items fi{join}"
           f' WHERE {" AND ".join(where)}')
    with reg.db() as c:
        rows = [dict(r) for r in c.execute(sql, args).fetchall()]
    return [{**row, 'why': why} for row in rows if (why := _why(row))]


def _backups(explicit: str | None) -> list[Path]:
    """备份库,新的在前。显式给了就只认那一个 —— 指错了要当场看出来。"""
    if explicit:
        path = Path(explicit)
        if path.is_dir():
            path = path / 'feeds.db'
        return [path] if path.exists() else []
    found = sorted((reg.DB_PATH.parent / 'backups').glob('*/feeds.db'),
                   key=lambda p: p.stat().st_mtime, reverse=True)
    return found


def _from_backup(paths: list[Path], key: str, kind: str) -> tuple[str, str, str]:
    """备份里那份原始 body。返回 `(壳, 它的 content_kind, 来自哪个备份)`,没有就三个空串。

    **`content_kind` 一起取回来是给不许可来源那一支用的**:简报原生 `summary`、
    大V原生 `post`,而降级那条路对许可来源一律写 `summary`(「从没补抓过」的形态)。
    与其在这儿按 kind 写死两个字面量,不如恢复备份里那条**原本的样子** —— 新增
    一类不许可来源时这儿不用跟着改。

    **恢复回来的要再过一遍判据,而且过的是和扫描同一道那份** —— 所以这里调的是
    `_why` 本身,不是它两道中的某一道。备份可能是在某一轮错误覆盖之后做的,里面那份
    同样不合格:那就不算有效壳,继续往更早的备份找。`_backups()` 按 mtime **倒序**,
    所以「污染之后做的那一份」是默认命中项,不是边角情形。

    **同一处口径分家,这个函数上栽过两次,形态一模一样。** 第一次是扫描换成
    `_not_article` 而这里还在用旧判据:一份同样印着「请完成验证」的较新备份被当成
    有效原摘要装回来,`_downgrade` 紧接着清掉 `fulltext_at`,这一条从此连扫描都选不
    中,而正确的较旧备份明明还在 —— 三轮同步之后那段验证提示仍旧导出到了 vault。
    第二次是扫描加了结构那一道(`_why` 的第一道)而这里只留着文字那一道:一份
    `content_kind='content'` 的污染备份过了 `boilerplate_reason`,被装回它自己头上
    —— `body=?` 的 CAS 比的是同一份内容,**rowcount 照样是 1**,于是报「已收拾 1 条、
    从备份装回了原文」而那条正文一个字都没变,译文却被作废、`content_hash` 被改写,
    下一轮重新买一次翻译、vault 里那篇重写一遍,每跑一次 `run` 重复一轮(审查实测)。

    **老备份缺 `content_kind` 这一列时,只有许可来源还用得上它**:它那一支按「从没
    补抓过」的形态写 `summary`,不读备份的 content_kind;不许可来源那一支判不出这份
    备份是不是在某一轮错误覆盖之后做的,**拿不准就别固化终态** —— 回落到「收拾不了」
    由人处理,比赌一份判不出来的备份便宜。

    **宁可多拒一份备份。** 误拒的代价是回落到「标题与链接」那份重建壳、下一轮重抓
    一次(`_downgrade` 会把它放回队列、额度给满);漏放的代价是把已经认出来的坏正文
    重新当成这一篇的摘要,而清掉成功时间之后再也没人会来问它(元规则:漏报是静默
    丢数据,误报顶多重抓一次)。
    """
    for path in paths:
        try:
            conn = sqlite3.connect(f'file:{path}?mode=ro', uri=True)
            conn.row_factory = sqlite3.Row
            with conn:
                # `SELECT *` 是为了老备份缺列时不整份跳过(那样连许可来源也救不
                # 回来);读的仍只有下面这两项。
                row = conn.execute('SELECT * FROM feed_items WHERE key=?',
                                   (key,)).fetchone()
        except sqlite3.Error:
            continue                      # 老备份的 schema 不同、或者文件坏了
        finally:
            try:
                conn.close()
            except (sqlite3.Error, NameError, UnboundLocalError):
                pass
        body = (row['body'] if row else '') or ''
        if not body.strip():
            continue
        columns = row.keys()
        content_kind = (row['content_kind'] if 'content_kind' in columns else '') or ''
        if not content_kind and not permits_link_body(kind):
            continue                      # 老备份判不出结构那一道,不拿它赌
        if _why({'kind': kind, 'content_kind': content_kind, 'body': body}):
            continue                      # 和扫描同一道判据,往更早的备份找
        return body, content_kind or 'summary', path.parent.name
    return '', '', ''


def _rebuild(row: dict) -> str:
    """按标题和链接拼一份壳。形态同 feed 原本那份,不编造任何内容。"""
    return '\n'.join(part for part in (row['title'], row['url']) if part)


def _plan(rows: list[dict], backups: list[Path]) -> list[dict]:
    """给每一条配一份要写回去的内容。`shell` 空串 = **收拾不了,一个字都不许动**。

    **「标题与链接」那份重建壳只给许可来源**:它成立的前提是「下一轮同步会重抓一
    次」(`_downgrade` 把它放回队列),拿不到才永久停在壳上。不许可来源压根不进补抓
    队列 —— 在那儿拿两行标题链接盖掉已经在库里的正文,是**把内容毁掉**,而且没有
    任何一轮同步会来补。所以那一支只认备份,没有就报出来让人处理(元规则:拿不准
    就别固化终态)。
    """
    for row in rows:
        shell, kind, origin = _from_backup(backups, row['key'], row['kind'])
        row['requeue'] = permits_link_body(row['kind'])
        if row['requeue']:
            row['shell'] = shell or _rebuild(row)
            row['shell_kind'] = 'summary'
            row['origin'] = origin or '标题与链接'
        else:
            row['shell'], row['shell_kind'], row['origin'] = shell, kind, origin
    return rows


def _nothing(kind: str | None, source: int | None) -> str:
    """一条都没选中时那句话。**点名过不走补抓的来源,就把这件事说出来。**

    光回一句「没有需要复查的条目。」读起来像「这一类来源的损坏不在这个脚本的射程
    里」—— 而射程改对之后恰好相反(`_covered` ②)。那句误解实测过:简报上有一条
    正文被外链文章换掉、人点名 `--kind digest` 来收拾,拿到的就是这句话加退出码 0。
    """
    base = '没有需要复查的条目。'
    named = {kind} if kind else set()
    if source is not None:
        with reg.db() as c:
            row = c.execute('SELECT kind FROM subscriptions WHERE id=?', (source,)).fetchone()
        if not row:
            return base + f'\n（订阅 {source} 不存在 —— 核对一下 subscriptions.id。）'
        named.add(row['kind'])
    closed = [k for k in sorted(named) if k in SOURCE_TYPES and not permits_link_body(k)]
    if not closed:
        return base
    labels = '、'.join(SOURCE_TYPES[k].label for k in closed)
    # **不许把「扫不中」说成「确定没有」**(元规则:判不准一律如实交出,不许返回
    # 「确定没有」)。射程 ② 认的是 `content_kind`,而换正文的入口里曾有一个刻意
    # 不动它(`_covered` 那一段);那句「当前一条都没有」实测过被当成结论用。
    return (base + f'\n（{labels}不走正文补抓:正文就是这一类来源自己给的那一份。'
            '这里按两道判据选:正文形态不像文章、或者这一类来源手里拿着一份网页'
            '正文 —— 两道都没选中。\n这不等于「它的正文一定没被换过」:若有过一次'
            '连 content_kind 都没改的覆盖,这里看不出来,要拿备份逐条比 content_hash '
            '才查得到。改正文那几条语句现在自己的 WHERE 里就拦着不许可的来源,'
            '所以新的不会再有。）')


def _groups(rows: list[dict]) -> tuple[list[dict], list[dict], list[dict]]:
    """按「该怎么收拾」分三组:换壳回队列 / 只恢复原文 / 收拾不了。"""
    refill = [row for row in rows if permits_link_body(row['kind'])]
    rest = [row for row in rows if not permits_link_body(row['kind'])]
    return refill, [r for r in rest if r['shell']], [r for r in rest if not r['shell']]


def _lines(rows: list[dict], *, shell: bool = True) -> None:
    for row in rows:
        print(f"  [{'已入库' if row['shipped'] else '未入库'}] {row['title'][:48]}")
        print(f"      {row['why']}（{len(row['body'])} 字）")
        if shell:
            print(f"      正文换成 → {row['origin']}（{len(row['shell'])} 字）")
        print(f"      {row['url']}")


def cmd_scan(args) -> int:
    rows = _plan(_suspect(args.kind, args.source), _backups(getattr(args, 'backup', None)))
    if not rows:
        print(_nothing(args.kind, args.source))
        return 0
    refill, restore, stuck = _groups(rows)
    print(f'{len(rows)} 条正文要收拾:')
    if refill:
        restored = sum(1 for r in refill if r['origin'] != '标题与链接')
        print('\n补抓写进来的,不是文章正文（换成壳、放回补抓队列、译文一并作废）:')
        _lines(refill)
        print(f'  {restored} 条从备份里恢复原始内容，{len(refill) - restored} 条按标题与链接重建。')
        print(f'  下一轮同步重抓一次:拿到真正文就补上;仍然拿不到 → 试满'
              f' {fulltext.MAX_TRIES} 次之后按壳入库（和没开过补抓的条目一样）。')
    if restore:
        print('\n正文来自原文链接那一页（这一类来源不走补抓:只从备份装回原文、不放回队列）:')
        _lines(restore)
    if stuck:
        print('\n收拾不了,要你自己处理（备份里没有这一条的原始内容）:')
        _lines(stuck, shell=False)
        print('  这一类来源不会重抓,所以不拿「标题与链接」顶替已经在库里的那份正文。')
        print('  出路:换一份更早的备份（--backup），或者到那一期的落地页把这条摘要抄回来。')
    # **只数真会动的那些** —— 「收拾不了」那一栏一个字都不改,它那篇 md 不会被覆盖。
    shipped = sum(1 for r in refill + restore if r['shipped'])
    if shipped:
        print(f'\n其中 {shipped} 条已经写进过 vault，那篇 md 下一轮导出就会被覆盖。')
    print('\n真要改:feed_fulltext_recheck.py run --yes')
    return 1


def _downgrade(rows: list[dict]) -> tuple[list[dict], list[dict], list[dict]]:
    """把计划里的每一条写回去。返回 `(真改了的, 这期间变过的, 收拾不了的)`。

    **动作按来源能力分两支,这才是 `permits_link_body` 在这个脚本里的落点**:
    - **许可来源**(rss / web / inbox):换成壳、清掉补抓那三个标记**放回队列**、
      译文作废。写入时再核一次 `fulltext._eligible()` —— 计划生成后关掉最后一条
      补抓归属或删掉订阅,不得把已无机会补回的正文换成壳。
    - **不许可来源**(简报 / 大V):**只把备份里那份原文装回去**,`fulltext_at` /
      `fulltext_error` / `fulltext_tries` 一个都不碰 —— 它压根不该进补抓队列,清
      掉标记只会让它看起来「在等全文」而永远没人来抓。这一支不带 `_eligible()`:
      那个判据对它恒假,带上就是 0 行。它的 CAS 是 `body=?` 加 `content_kind=?`
      (扫到那一刻的形态),同样待在 UPDATE 自己的 WHERE 里。

    两支都让译文作废、`extract_stale=1`、不动 `exported_hash`(口径同下)。

    **走哪一支读的是 `row['requeue']`,也就是算计划那一刻的能力,不是此刻重新问一
    遍。** 这一道要紧:计划里那份壳是按某一支算出来的(许可支可能配的是「标题与
    链接」重建壳,它成立的前提是紧接着回补抓队列),拿它去走另一支就是把内容毁掉。
    定点注入过改成实时重判的后果:扫描时 rss 还是许可来源、配了重建壳,写回前能力
    被撤销,于是那份重建壳走了**不许可**那一支 —— 那条 SQL 里没有 `_eligible()`,
    `body` 和 `content_kind` 又都还是扫到那一版,`rowcount=1`,一条正文被盖成两行
    标题加链接,而且永远不会有哪一轮同步来补。
    **固定住之后不需要再加一道「能力变没变」的 CAS**:那一道定点注入下来红 0 条 ——
    能力撤销时这一条照旧走许可支,而许可支自带的 `_eligible()` 当场判假、0 行、落进
    `moved`(元规则:红 0 条先怀疑用例,想清楚是真冗余)。

    **「不许可来源不拿『标题与链接』顶替」这道保护只在 `_plan` 里,这儿没有第二
    道。** 想过在这条 UPDATE 前面再核一次「这份内容是不是从备份来的」,但写入口
    能读到的只有 `_plan` 交过来的那几个字段 —— 核的还是同一个结论,不是独立证据
    (元规则:口径只有一份,别在两处各判一遍)。定点注入过它的后果:把 `_plan` 里
    不许可那一支换成「没备份就用 `_rebuild`」,这条 UPDATE 当场把一条简讯的正文
    盖成两行标题加链接、`rowcount=1`、还报「已收拾 1 条」。所以钉住它的是回归
    (`tests/test_fulltext_recheck_capability.py` 里那条「没有备份就整条留着」),
    不是这儿的第二道 if。

    **判据是「这一条还是我扫到的那份 body 吗」,而且待在 UPDATE 自己的 WHERE
    里(`AND body=?`),不许在 Python 这边先 SELECT 一次比对。** 先查后写在两
    个进程之间压根不成立(kb-web 的补抓 worker 是另一个写入方,`fulltext._save`
    也不持任何跨进程锁),而 SQLite 把写入串行化 —— 判据待在写语句里才和那次
    写入同生共死,放行不了就是 0 行。口径同 collector 那条「判据就在 UPDATE
    自己的 WHERE 里」和 `sync._claim_write`。

    **比的是旧 body 本身,不是 `content_hash`。** 那一列在这张表上有两个写入
    方、两套口径(`registry.put_items` 写的是整个 entry 的 JSON 摘要,
    `fulltext.body_hash` 写的是 `sha256(body)`),而这里要回答的恰恰是"我读到
    的那份正文还是当前那一份吗" —— 直接比正文没有第二份口径可漂。

    **0 行不是失败,是我们想要的末态**:同步在这期间把它补抓成功了,那份正文
    (以及译文、`fulltext_at`、已经导出的 md)一个字都不许动。**也不在这里就地
    重判**:重判会变成一个没有终点的循环,而第 2 轮放宽 `_suspect` 射程之后
    「body 是样板文字」就是充分判据、与 `fulltext_at` 无关 —— 新内容要是又一份
    样板文字,下一次 scan 自己就会再选中它。所以如实报出来、让人再跑一次。

    整批在**一个**写事务里(`reg.db()` 退出时才 commit):第一条 UPDATE 就把写
    锁攥住,后面每一条的判据都在锁内求值。
    """
    cut, moved, stuck = [], [], []
    with reg.db() as c:
        for row in rows:
            if not row['shell']:
                stuck.append(row)         # 没有可恢复的原文(见 `_plan`),不许写空
                continue
            shell, key, body = row['shell'], row['key'], row['body']
            digest = fulltext.body_hash(shell)
            lang = reg.text_language(row['title'], shell)
            if row['requeue']:
                cur = c.execute(
                    f"""UPDATE feed_items SET body=?,content_kind='summary',content_hash=?,lang=?,
                       fulltext_at='',fulltext_error='',fulltext_tries=0,extract_stale=1,
                       title_zh='',body_zh='',translated_at='',translate_error='',translate_tries=0
                       WHERE key=? AND body=? AND {fulltext._eligible()}""",
                    (shell, digest, lang, key, body))
            else:
                cur = c.execute(
                    """UPDATE feed_items SET body=?,content_kind=?,content_hash=?,lang=?,
                       extract_stale=1,title_zh='',body_zh='',translated_at='',
                       translate_error='',translate_tries=0
                       WHERE key=? AND body=? AND content_kind=?""",
                    (shell, row['shell_kind'], digest, lang, key, body, row['content_kind']))
            (cut if cur.rowcount else moved).append(row)
    return cut, moved, stuck


def cmd_run(args) -> int:
    rows = _plan(_suspect(args.kind, args.source), _backups(getattr(args, 'backup', None)))
    if not rows:
        print(_nothing(args.kind, args.source))
        return 0
    if not args.yes:
        print(f'{len(rows)} 条待收拾,加 --yes 才会真改。')
        return 1
    cut, moved, stuck = _downgrade(rows)
    queued, restored_rows, _ = _groups(cut)
    restored = sum(1 for r in queued if r['origin'] != '标题与链接')
    print(f'已收拾 {len(cut)} 条。')
    if queued:
        print(f'  {len(queued)} 条正文换成壳（{restored} 条取自备份，'
              f'{len(queued) - restored} 条按标题与链接重建）、放回补抓队列、译文一并作废。')
    if restored_rows:
        print(f'  {len(restored_rows)} 条从备份装回了原文、译文一并作废;'
              '这一类来源不走补抓,补抓标记一个都没动、也没放回队列。')
    if stuck:
        print(f'\n另有 {len(stuck)} 条备份里没有原始内容，一个字都没动:')
        for row in stuck:
            print(f"  {row['title'][:48]}")
        print('  （这一类来源不会重抓,不拿两行标题链接顶替。见 scan 里那一栏。）')
    if moved:
        print(f'\n另有 {len(moved)} 条在这一趟里内容已经换过或补抓资格已变化，原样留着没动:')
        for row in moved:
            print(f"  {row['title'][:48]}")
        print('  （正文或订阅设置已变化。再跑一次 scan 看它现在是什么。）')
    # **还有收拾不了的就不报 0**:那几条的坏正文还在库里、还会导出,脚本化调用
    # 据退出码当「全清了」的话,这件事就再没人来问它(统计按实际写入结果报)。
    return 1 if stuck else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest='cmd', required=True)
    for name in ('scan', 'run'):
        p = sub.add_parser(name)
        p.add_argument('--kind', help='只看这一类来源（rss / social …）')
        p.add_argument('--source', type=int, help='只看这一条订阅（subscriptions.id）')
        p.add_argument('--backup', help='从这个备份里恢复原始正文（目录或 feeds.db）；'
                                        '不给则自动扫 <data>/backups/*/feeds.db')
        if name == 'run':
            p.add_argument('--yes', action='store_true')
    args = ap.parse_args()
    return cmd_scan(args) if args.cmd == 'scan' else cmd_run(args)


if __name__ == '__main__':
    raise SystemExit(main())
