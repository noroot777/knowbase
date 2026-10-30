"""Source modules are a UI/catalog concept; channels are replaceable collectors.

To add a feed-compatible category, register one SourceType and a collector using
FeedCollector. A source with different transport/storage (e.g. WeChat) implements
SourceCollector independently. Pipeline/search code does not dispatch on modules.
"""
from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class SourceType:
    id: str
    label: str
    description: str
    channels: tuple[str, ...]
    manager: str = 'feeds'
    icon: str = 'rss'
    interval_minutes: int = 360
    url_hint: str = '输入 RSS / Atom 订阅地址'
    note: str = '读取订阅中当前提供的内容；不能保证补齐订阅之前的全部历史。'
    adapter: str = 'rss'
    # 这一类来源**有没有可以轮询的源**。False = 内容由人逐条投进来(手动收录:
    # 抓取发生在用户给出链接的那一刻,见 `kb/feeds/grab.py`),于是 crawl 阶段
    # 对它空转(`kb.feeds.sync.collect` 开头那一道)—— 它参与的是后面那半条链:
    # 导出 → 索引 → 提取。**不是"这一类来源不进管道"**:积压的条目正是靠那
    # 半条链进知识库的,所以它照旧出现在同步中心的通道里。
    pollable: bool = True
    # 下面这两位问的是**两个不同的问题**,别当成一个许可位的两种说法 ——
    # 它们在 rss / web / inbox / digest 上答案碰巧一致,在 social 上分叉,
    # 混成一个的后果是一边静默覆盖、一边静默收走出路(见两个判据各自的说明)。
    #
    # ①这一类条目的正文,该不该由**通用的 HTTP + 正文提取器**那条路、
    #   去它 `url` 指向的那一页上取。判据函数 `permits_link_body` /
    #   `link_body_sql`。
    body_from_link: bool = False
    # ②这一行的 `url` 指向的**是不是它自己的原文**。判据函数
    #   `permits_origin_body` / `origin_body_sql`。默认 False:新增来源
    #   没想清楚之前不许可(漏报只是那颗「重新抓一次」不露面,误报是静默
    #   覆盖正文、不可恢复)。
    url_is_origin: bool = False


SOURCE_TYPES = {
    'wechat': SourceType('wechat', '公众号', '关注的公众号文章',
                         ('wechat_oa', 'wechat_local'), manager='wechat', icon='chat',
                         url_is_origin=True),
    # 大V观点:**这两位在这一类上分叉,social 是唯一一处**。一条推文的 `url` 就是
    # 这条推文自己的永久链接(`x_timeline_js` 拼的 `x.com/<handle>/status/<id>`),所以
    # `url_is_origin=True` —— 人点「重新抓一次」走公开接口取回的是**它本人**的正文。
    # 但那条路是 X 专用的(`grab.grab()` 走 FxTwitter),通用的 HTTP + 提取器拿 x.com
    # 什么也提不出来,所以 `body_from_link=False`:它进不了补抓队列、也没有浏览器采集
    # 入口。曾经把这两个问题当成一个,于是 3356 条推文里那些空正文/短残片**唯一**的
    # 人工出路被静默收走,给出的理由还是一句反过来的话(「正文不是外链那一页」)。
    'social': SourceType('social', '大V观点', '关注的人，此刻的想法。', ('social',),
                         icon='chat', interval_minutes=1440, url_is_origin=True,
                         url_hint='输入 X / Twitter 账号主页，或其他社交媒体的 RSS 地址',
                         note='X 的取法在下面的「取法」那一块里选，每一档要准备的东西不同。'
                              '也可以为单个账号填写专用订阅链接，它优先于任何通道。',
                         adapter='social'),
    'rss': SourceType('rss', 'RSS', '让值得读的文章，自己来到这里。', ('rss',),
                      body_from_link=True, url_is_origin=True),
    # YouTube:**两位都不开,但理由不同,正好是这两个问题分开问的意义**。
    # ①`body_from_link=False`:这一类的正文是视频的转录稿,而 `url` 后面那一页是播放页 ——
    #   通用提取器从那儿拿到的是简介和推荐列表,补抓一次就把转录稿整篇换掉,**不可逆**
    #   (见 `docs/decisions/browser-capture.md`)。
    # ②`url_is_origin=False`:播放页确实是这条内容的出处,但那一页上**没有**它的正文 ——
    #   「拿这个 `url` 重抓一次」换回来的不是这篇转录稿。所以那颗「重新抓一次」也不该露面。
    'youtube': SourceType('youtube', 'YouTube', '频道更新先排名，只处理值得读的视频。',
                         ('youtube',), icon='paper', interval_minutes=1440,
                         url_hint='输入 YouTube 的 /channel/UC… 或 /@频道名 主页',
                         note='先排出一张候选榜，再按数量与时长预算处理；优先复用字幕，'
                              '需要时仅下载音频转录，英文内容翻译为中文。不分析画面。',
                         adapter='youtube'),
    # 没有 feed 的站点走这条路:解析列表页。独立成一类而不是塞进 rss,是因为
    # 它的失败形态不同 —— 读 feed 坏在对方服务,解析页面还会坏在**对方改版**,
    # 而那需要人去适配。混在一起的话 rss 那条线的状态和定时会被它带着抖。
    # 简报:RSS 只给期号,内容在每期的汇总页上并列排布。独立成一类是因为
    # 它**只能两跳**(期号从 RSS 拿、正文从落地页拿),而 rss/web 两条线都只发
    # 一次 HTTP —— 它走的是预取,见 `kb.feeds.digest`。
    'digest': SourceType('digest', '简报', '每期一个汇总页，里面是几十条带外链的简讯。',
                         ('digest',), icon='paper', interval_minutes=720,
                         # 这一行渲染在「添加订阅」弹窗里「链接」那个输入框下面
                         # (`source_forms.js` 取 `url_hint`),而**你填进去的确实是
                         # 一个 RSS 地址** —— 所以「这一类和普通 RSS 不是一条路」这句
                         # 话得待在那儿,不能只写在面板底下那段说明里。
                         url_hint='输入简报的 RSS 地址。这一类和普通 RSS 不是一条路：那种 feed '
                                  '一条就是一篇文章，而这类只给期号、不带正文，正文由我们去每期'
                                  '页面把几十条简讯逐条拆出来 —— 适合 TLDR AI 这类聚合简报；'
                                  '一条一篇的普通 feed 请加到「RSS」那一类。',
                         # **这几个字段全部经 `textContent` 落地,写 markdown 就是把星号
                         # 显示给人看**(回归 `test_feed_sources.SourceCopyTests`)。
                         note='给 TLDR 这类"一期一封"的简报用：RSS 本身不带正文，我们按它给的'
                              '期号去每期页面把每条简讯拆出来，标着「赞助」的那些不收。'
                              '正文就是简报自己写的摘要，不要再开「补抓原文」——那个'
                              '外链指向的是别人家的文章，抓回来就不是这条简讯了。',
                         adapter='digest'),
    'web': SourceType('web', '网页专栏', '没有订阅源的站点，靠解析列表页跟踪。',
                      ('web',), icon='paper', interval_minutes=720,
                      url_hint='输入站点的文章列表页地址（例如 news / blog 首页）',
                      note='给没有 RSS / Atom 的站点用：按「每条带发布时间的链接」认出'
                           '列表，正文建议打开「补抓原文」去文章页取。站点改版时这条'
                           '订阅会报错，而不是静静地变成「今天没有新文章」。',
                      adapter='page', body_from_link=True, url_is_origin=True),
    # 手动收录:看到一篇就存下来。**它没有可轮询的源** —— 内容在用户给出链接的
    # 那一刻就抓好入库了(`kb/feeds/grab.py`),所以 `pollable=False`、
    # `adapter='manual'`(**刻意不在 `fetching.ADAPTERS` 里**:哪条路漏了跳过
    # crawl 这一道,报出来的是一个说得清的 KeyError,而不是拿一个假地址去发 HTTP)。
    # 除此之外它和别的 feed 类来源逐字一样 —— vault 树、索引扫描、检索的来源
    # 筛选、提取开关、日报素材范围、同步中心的通道全部由 `feed_types()` 自动
    # 派生(见 `kb/config.py`、`kb/collectors/__init__.py`)。
    'inbox': SourceType('inbox', '手动收录', '看到就存：给一个链接，内容抓进知识库。',
                        ('inbox',), icon='box', interval_minutes=1440,
                        url_hint='粘贴文章或推文链接，回车存入',
                        note='这一类不轮询：你给一个链接，正文当场抓好写进知识库。'
                             'X 的单条推文走公开接口（长文连正文一起取），其他站点'
                             '按文章正文提取。按站点自动归到一个收件桶里，'
                             '抓不到正文的不会入库——重试一次的代价只是再抓一遍。',
                        adapter='manual', pollable=False, body_from_link=True,
                        url_is_origin=True),
}


def permits_link_body(kind: str) -> bool:
    """**问题①:这一类条目的正文,该不该由通用的 HTTP + 正文提取器去它外链那一页上取。**

    这一份管的是**那条通用路**:补抓队列(`fulltext`)、采集期直取
    (`markdown_source.enrich`)、浏览器采集的准入(`browser_capture.target`)、存量复查
    的分支选择(`feed_fulltext_recheck._plan`)、订阅上那颗「补抓原文」开关
    (`registry.save_subscription`、编辑表单、`fulltext._eligible`)、以及条目卡片上那个
    「从浏览器补抓」入口(`source_reader.js` 读 catalog 里这一列)、以及「问题与待办」
    详情里那两条人工出口(「从浏览器采集」和「手动补正文」,`intake_diag_api.item` 的
    `can_paste` —— **两颗共用这一道闸,不许一颗开一颗关**)。
    **真正写库的那几条 UPDATE 另有 SQL 形状的同一份判据,见 `link_body_sql`。**
    禁掉时屏幕上那句理由走 `link_body_refusal`。

    **它不是「这一行的正文许不许改」的总闸** —— 那是问题②(`permits_origin_body`)。
    两个问题在 rss / web / inbox / digest 上答案一致,在 social 上分叉:推文的正文取法是
    X 专用的公开接口,通用提取器对 x.com 无效(所以这一份是 False),但它的 `url` 指向的
    确实是它本人(所以那一份是 True)。把两者当成一个许可位,social 两边都会判错。

    **一处没接上就崩一条内容,而且不可恢复。** 简报的 `url` 指向的是别人家的文章
    (`docs/decisions/digest.md`),它那条「不开 fulltext」原先只以三种间接形式存在
    (defaults.json 不标、一句人话提示、`s.fulltext=1` 顺带挡住)。于是 `enrich` 的
    闸门方向恰好是反的、浏览器采集按 kind 字面量只挡了大V —— 生产库里一条 TLDR AI
    的简讯因此被整篇 README 顶掉。新增来源默认 `body_from_link=False`,要开就想清楚
    那一页上的内容是不是这条条目本身。
    """
    source = SOURCE_TYPES.get(kind)
    return bool(source and source.body_from_link)


#: 不许可的那几类各自**为什么**不许可 —— 一句人读得懂、而且据以换一条路走的话。
#: **判据不在这里**(只有 `permits_link_body` 一份):这张表只负责那句话,许可的一律
#: 空串。界面上两条写正文的人工出路(「从浏览器采集」和「手动补正文」)共用这一道闸,
#: 禁掉的时候得说清是为什么 —— 不说清的话人会一直点那颗没反应的按钮
#: (`kb/static/js/diag_detail.js`、`docs/decisions/browser-capture.md`)。
_LINK_BODY_REFUSALS = {
    'social': '大V观点的正文走 X 专用接口取：通用提取器对 x.com 提不出东西，'
              '拿那一页的文字替换只会把推文正文写坏',
    'youtube': 'YouTube 这一类的正文是视频转录稿，而链接后面那一页是播放页——'
               '从那儿读回来的简介和推荐列表会把整篇转录稿换掉，换不回来',
    'digest': '简报条目的链接指向被这条简讯引用的第三方文章，不是简讯本身',
    'wechat': '公众号的正文直接来自本机绿泡泡库，不从链接后面那一页取',
}


def link_body_refusal(kind: str) -> str:
    """许可就空串,不许可就给那句话(**空串 ⇔ `permits_link_body` 为真**)。

    没有登记的新来源走兜底那一句:默认不许可(见 `SourceType.body_from_link`),
    所以这里必须答得出一句话 —— 返回空串等于告诉界面「可以点」。
    """
    if permits_link_body(kind):
        return ''
    return (_LINK_BODY_REFUSALS.get(kind)
            or '这一类来源的正文不允许被原文链接后面那一页替换')


def permits_origin_body(kind: str) -> bool:
    """**问题②:这一行的 `url` 指向的是不是它自己的原文。**

    是 —— 那么「拿这个 `url` 重抓一次、把正文换成抓回来的东西」换的还是这条内容本身,
    许可(rss / 网页专栏 / 手动收录 / 公众号 / 大V观点)。不是 —— 换过去就是另一篇了,
    拒(简报:它的 `url` 指向被简讯引用的第三方文章)。

    落点是**人工重抓那条路**:`grab_api._recover` 的入口拒(连 HTTP 都不发)、
    `grab_store.known` 把它交给前端(那颗「重新抓一次」据此露不露面)。写库的那几条
    UPDATE 用 SQL 形状的同一份判据,见 `origin_body_sql`。

    与问题①(`permits_link_body`)的区别见那一份的说明。默认 False:新增来源在没人想清楚
    它的 `url` 指向什么之前,不许可 —— **漏报的代价是那颗按钮不露面(人能来改这一行声明),
    误报的代价是静默把正文覆盖成另一篇、不可恢复**(元规则「拿不准就别固化终态」)。
    """
    source = SOURCE_TYPES.get(kind)
    return bool(source and source.url_is_origin)


def feed_types():
    return {k: v for k, v in SOURCE_TYPES.items() if v.manager == 'feeds'}


def _kinds_sql(item: str, flag: str) -> str:
    """把 `SourceType` 上某一位渲染成 `<表>.kind IN (...)`。

    **「怎么渲染」只该有一份**,所以两个判据共用它;**「渲染哪一位」是两个不同的问题**,
    所以对外是两个函数。全 False 时给 `kind IN (NULL)` —— WHERE 下恒不成立,即「谁都不
    许可」,这正是安全的那一边。

    判据取 `SOURCE_TYPES` 全集,不过 `feed_types()`:问的是「`feed_items` 这张表里哪些
    kind 许可」,跟那一类由谁管理无关;今天两者算出来一样,哪天某类来源换了 manager 也
    不该悄悄改变写入闸的集合。
    """
    kinds = ','.join(f"'{kind}'" for kind, spec in SOURCE_TYPES.items()
                     if getattr(spec, flag)) or 'NULL'
    return f'{item}.kind IN ({kinds})'


def link_body_sql(item: str = 'feed_items') -> str:
    """问题①的 **SQL 形状**:这一行的正文允不允许被通用提取器从它外链那一页取回来的东西替换。

    **凡是要改写 `feed_items.body`、而内容来自对这一行 `url` 发的那次通用抓取的 UPDATE,
    都把它拼进自己的 WHERE**(元规则「判据待在写语句自己的 WHERE 里」):`fulltext._save`、
    `browser_capture.save`,以及选队列和存量复查许可支经 `fulltext._eligible` 带上的那一道。
    内容来自「这一行 `url` 指向的它自己」的那几条用 `origin_body_sql`。

    **为什么是写入层的共用片段,而不是「记得在每个入口调一次 `permits_link_body()`」。**
    后者靠人记全一张入口名单,而名单只能写在注释和一条列举式用例里:前四处都接上了,
    第五处(`grab_store.replace`,人点「重新抓一次」那条路)照旧把简报里一条简讯的正文
    换成了外链那篇文章的全文 —— 走的是公开接口、不是内部直调,审查实测可达。片段化之
    后,漏接是 `tests/test_link_body_entries.py` 的红:它把 `kb/`、`scripts/` 下每一条
    写 `body` 的 `UPDATE feed_items` 都扫出来,要求逐条声明内容从哪来、并据此带对应的
    那一道闸,**声明集合不等就红**(带错了哪一道也红) —— 新增一个入口而不声明也红,所以
    这张名单不再靠人记。

    `item` 是表名或别名,只接内部写死的取值,不接外部输入(`origin_body_sql` 同)。
    """
    return _kinds_sql(item, 'body_from_link')


def origin_body_sql(item: str = 'feed_items') -> str:
    """问题②的 **SQL 形状**:这一行的 `url` 指向的是不是它自己的原文。

    **凡是要改写 `feed_items.body`、而内容是拿这一行 `url` 重抓它本人取回来的 UPDATE,
    都把它拼进自己的 WHERE**:`grab_store.replace`(人点「重新抓一次」)、
    `x_article_backfill.apply_one`(拿推文链接补回长文正文)—— 这两处是同一个来路,所以
    带同一道闸。为什么闸在写语句里、以及漏接为什么是红,见 `link_body_sql`。
    """
    return _kinds_sql(item, 'url_is_origin')


def catalog():
    return [asdict(s) for s in SOURCE_TYPES.values()]
