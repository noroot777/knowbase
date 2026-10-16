# 手动收录:给一条链接,内容抓进知识库(kb/feeds/grab.py + grab_page.py)

> **这一份的第一节就是这一块的硬约束(不许怎样)—— 动这块代码之前先读它**,后面是落点的取舍、
> 实测证据、回归清单和反向验证红数。CLAUDE.md 只留跨块的元规则和一张索引。
> 硬约束和下面的展开**说的是同一条口径,要改规则两边一起改**。

## 硬约束

- **抓不到正文一律不入库,一个字节都不写。** `registry.put_items` 是
  `ON CONFLICT(key) DO NOTHING` —— 空正文一旦落库就**再也改不回来**(重投同一条链接只会被
  `DO NOTHING` 挡住),而这一步失败的代价只是用户再点一次。所以 `grab.py` 里所有「拿不准」
  都抛 `GrabError`,`save()` 开头那道 `entry['body']` 非空检查是构造上的最后一道。口径同
  `x_article` 那条「判不准一律抛」。
- **失败要分档,一档一句话,而且**必须**说清下一步。** 登录墙 / 验证页 / 这后面不是网页 /
  推文被删 / 账号主页不是一篇内容 —— 它们的下一步完全不同(在浏览器里打开一次、换个地址、
  去「大V观点」加订阅),折叠成「抓取失败」等于把人留在原地。
- **这一类来源 `pollable=False`,crawl 阶段对它空转**(`sync.collect` 开头那一道)。
  **必须在那儿拦住,不许靠「它没有启用的订阅」** —— 收件桶是**启用着的**订阅(面板要显示它、
  `put_items` 要挂在它身上),放过去就会拿站点首页去发一次 HTTP,而 `fetching.ADAPTERS` 里
  压根没有 `manual` 这个适配器。`adapter='manual'` 是刻意的:哪条路漏了这道闸,报出来的是
  一个说得清的 KeyError,而不是拿一个假地址去抓。
- **收件桶按站点一个,`extract=1`、`fulltext=0`、`enabled=1`。** `fulltext` 必须是 0 ——
  那个开关会把条目扣在导出闸门外等「去原文补正文」,而正文此刻已经在手里了(实测后果:
  vault 里那一篇根本不出现)。`extract=1` 是因为手动收录正是「谁值得买一次提取」的肯定
  答案(口径见 `extract-optin.md`)。
- **桶的并发创建靠 `UNIQUE(kind,url)`,不靠「先查一次有没有」**(元规则:判据待在写语句
  自己的约束里)。撞上了就重查一次拿赢的那一条。
- **时间一律换算到 Asia/Shanghai**(`grab_page.as_published`、`grab._as_published`)。
  网页上的时间戳大多是 `…T10:02:37Z`,照字面取就比真实时刻**早 8 小时**,而日报按
  `published` 做字符串比较筛窗口 —— 差这 8 小时足够把一篇内容推到前一天。不带时区的写法
  按项目时区补齐,**不按宿主机**。
- **「改动时间」不许混进发布时间那张表**(`_TIME_META` / `_MODIFIED_META` 分开)。它排在
  所有发布时间来源之后,只在一个都没有时才用 —— 否则每次对方改一个错别字,这一篇的发布
  时间就跟着变一次。
- **元信息一律按纯文本处理**(`oneline(..., 'text')`)。bs4 交出来的属性值已经把 `&lt;`
  解码成 `<` 了,再过一遍 HTML 解析会把标题里的 `<T>`、`<Button>` 整段吃掉(口径同
  `fetching._is_plain` 那段注释)。
- **推文正文的换行一个都不许压掉**(`body_text(raw, 'text')`,走 `_plain_lines`),标题
  那一头反过来必须是单行(`oneline`)。分行就是作者的分段。
- **长文正文的拼接口径只有 `x_article.merge` 一份**,定时采集(`fill`)和手动收录都过它。
  各写一份的话同一篇长文从两条路进来会长出两种正文,而先到的那一份被 `DO NOTHING` 钉死。
- **地址规范化只有 `fetching.canonical_url` 一份**(它顺手去掉 utm_*/fbclid/gclid、把推文
  地址收成 `x.com/<handle>/status/<id>`)。`put_items` 的键是 `sha256(kind + ':' + url)`,
  两处规范化不一致时同一篇内容会变成两条记录、两篇 md、检索里两份结果。
- **X 的域判据是「等于名单里某一个」,不是 `endswith`** —— 后者会把 `notx.com` 一起圈进来,
  然后拿一个陌生站点的地址去问 FxTwitter(口径同 `x_login_import` 那条域判断)。
- **绿泡泡的文章这条路抓不到,如实说清去处,不许去绕那道关卡。** 实测见下。
- **占不到管道不是失败。** 内容已经在 feeds.db 里,写进 vault 由任何一轮 `inbox` 的 export
  兜住(面板那颗按钮、下一次收录、定时同步都算),所以端点照旧回 200、只是如实说「还没写
  进知识库」。反过来谎报成功的代价是用户以为检索里搜得到,而 vault 里还没有这一篇。
- **慢请求的回包落地前先对一次归属**(发起那一刻的 `sourceKind` + `sourceVersion`,口径同
  `frontend-feedback.md` 的代次那条)。toast 照发(那是用户自己的动作),但**一个字都不往
  面板上写** —— 切到别的来源之后那份回包说的是过去那个面板的事。

## 落点:为什么是第六类来源,而不是「往某条订阅里塞一条」

三条路都想过:

1. **塞进 `rss` / `social` 的某条订阅** —— 那条内容从此混在订阅内容里,检索按来源筛的时候
   分不出「我自己存的」;而且订阅是有身份的(水位、etag、退避、定时),一条文章地址不是一个
   可以轮询的源。
2. **自己建一张表 + 自己一条链** —— 那要重写翻译、导出 vault、索引、提取、检索、图鉴、日报
   这一整条,每一处都多一份口径。
3. **第六类来源 `inbox`(选它)** —— `kb.source_types` 里加一条 `SourceType`,于是 vault 树、
   索引扫描范围、`SOURCE_GROUPS`/`SOURCE_ALIASES`、采集器注册、「我的订阅」页那排胶囊、
   同步中心的通道、日报素材范围**全部自动派生**(`kb/config.py`、`kb/collectors/__init__.py`)。
   新来源只需要回答一件新事:内容从哪儿来。

代价只有一处:这一类**没有可以轮询的源**,所以 `SourceType` 多了一个 `pollable` 字段,
`sync.collect` 开头多一道闸。换来的是后半条链一个字都不用改。

界面上同样是复用:投递栏是「我的订阅 → 手动收录」页头下面**一行**(输入框 + 按钮 + 一行
提示),条目列表、阅读器、搜索、翻译状态全是既有部件。**刻意只有一行** —— 这一页不许再长出
整页一份的大卡片(来由见 `subs-layout.md`),而阅读区高度是锁在可用视口上的。侧栏「信息来源」
里那颗「存链接」是它的快捷入口。

## 实测证据

- **绿泡泡的文章挡在验证页后面**(2026-10-16,`https://mp.weixin.qq.com/s/<slug>`):
  302 到 `/mp/wappoc_appmsgcaptcha?poc_token=…&target_url=…`,18 KB 的验证页,og 元信息和
  `#js_content` 全都没有,正文提取 0 字。**那不是 bug,是对方的反自动化关卡** —— 我们不绕
  它,`classify` 直接拒掉并指向真正管这件事的那条通道(读本机绿泡泡数据库,
  `kb/collectors/wechat_local.py`)。
- **FxTwitter 的回包有两半**(2026-10-16 实测用户给的那条长文推文 + 一条普通推文):
  `tweet.text` / `author` / `created_timestamp` / `media` / `quote` 是手动收录要的那一半,
  `tweet.article` 是 `x_article` 已经管着的那一半。长文推文的 `text` 就是一条
  `https://x.com/i/article/<id>` —— 正好命中 `needs_article` 判据①,所以长文正文照旧由
  `x_article.fetch` 取、`merge` 拼。**手里没有浏览器**(不持有 CDP 会话,同 `bridge` 那一档),
  所以接口不可用时这一条就是抓不到。
- **网页那一侧的时间戳普遍是 UTC**(2026-10-16,derekthompson.org 那一篇):
  `<time datetime>` 和 JSON-LD 的 `datePublished` 都是 `2026-10-15T10:02:37Z`(上海 18:02),
  而 `article:modified_time` 是 `19:17Z` —— 两个坑在同一页上:不换时区会早 8 小时,认
  modified 会晚 9 小时。
- **端到端(真实网络,隔离的 `KB_DATA_DIR`/`KB_VAULT_ROOT`,2026-10-16)**:那条 X 长文
  → `source/inbox/<key>.md`,frontmatter 的 `account: Sac`、`published: 2026-10-15 19:30`、
  `content_kind: post`,正文 3432 字;derekthompson 那篇 → 6743 字,`account: Derek Thompson`、
  `published: 2026-10-15 18:02`、`content_kind: content`。

## 回归

- `tests/test_grab.py`(27 条):分流与拒绝、推文四种形态、网页五种形态、收件桶、重复投递、
  跨来源查重、crawl 空转、「接进管道那一圈」、以及**整条链的后果**(收下 → 导出 → vault 里
  那一篇带着 `source: inbox`)。
- `tests/test_grab_probe.py` + `tests/grab_probe.mjs`(13 条):界面贯通 —— 侧栏入口直达并
  聚焦、这一类没有「添加订阅」、按钮文案是「写入知识库」、在途禁用、成功清空输入框、
  toast 是本项目的那一套、刚存的那篇出现在列表里、重复投递说「已经收过了」而列表不多一条、
  抓不到时把原因说出来且不清输入框。**盘上的后果在 python 这一侧查**(`base.root` 下的
  `vault/source/inbox/*.md`):界面说「正在写入知识库」而 vault 里什么都没有,是这条链最难
  看出来的一种谎报。沙箱 `tests/serve_sources.py --grab-fixture` 把 `grab.request` 打成固定
  那一页 —— **探针不连外网**,而正文提取、入库、导出、索引都是真的。
- `tests/headless.py` 的 `SandboxBase` 是为上面那次查盘加的:`fixture_server` 交出的 base
  现在是 `str` 子类,多一个 `.root`。既有调用点一个字都没改。

## 反向验证(定点注入,逐条确认用例敏感)

后端 8 处:①时间不换时区(红 3)②把改动时间混进发布时间表(红 1)③crawl 不跳过不可轮询的
来源(红 1)④长文取不到时当成「它没有长文」(红 1)⑤推文正文走 `oneline` 压掉换行(红 1)
⑥收件桶打开补抓(红 2,其中一条正是 vault 里那一篇不见了)⑦meta 按 HTML 再解析一遍(红 1)
⑧查重只看自己这一类(红 1)。

界面 4 处:⑨这一类仍然摆着「添加订阅」(红 1)⑩成功之后不清输入框(红 1)⑫让条目落到别的
来源去、导出阶段不认它(红 2)。

**⑪「去掉 `known()` 那道早检查」红 0 条,如实记着:这是真冗余,不是用例不敏感。** 去掉它之后
第二次投递照旧走到 `put_items`,`DO NOTHING` 挡住、`added=0`,端点和界面说的还是「已经收过
了」—— 两道判据指向同一个结论。`known()` 真正多答的那半件事是**跨来源**的重复(一条推文
已经被「大V观点」收过了,而 `DO NOTHING` 按 `kind:url` 算键、挡不住它),那一档由
`test_grab.py::test_known_answers_across_sources` 钉着(注入⑧即红)。顺带省掉一次 HTTP。

## 还没做的

- **公众号链接**只会被拒。要让它也能收,得去读本机绿泡泡数据库里那一篇(`wechat_local` 的
  射程),那是另一块的事。
- **查重只问 `feed_items`**,不问 kb.db 的 `documents`。前者覆盖全部订阅类来源;公众号那棵树
  在后者,而绿泡泡的地址压根走不到这一步。要跨到 vault 全库查重再说。
- **没有 CLI 入口**。面板和 `POST /api/grab` 是目前唯二的入口。
