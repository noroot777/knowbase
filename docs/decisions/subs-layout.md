# 订阅页布局整合记录

> 2026-10-11 最新方案见 `design/subscription-usability.md`：所有视图统一使用
> `subs-mode` 外壳，来源工具栏移出列表，管理也使用独立滚动区；以下记录保留历史方案。
> 2026-10-13 第 1 轮审查修复及当前实跑结果见文末；历史通过数不作为当前验证结论。

## 合并前方案与审查记录

以下保留 `main` 的原始决策与故障分析；工作区几何和最小高度以文末后续方案为准。


> 决策记录。本页的改动起点是一张用户截图:RSS 视图上真正能读正文的那块只剩中间
> 一条,页头占掉三行、页尾还常驻着一张「每日定时同步」大卡片。

## 定时同步:从页尾常驻卡片改成页头入口 + 弹窗

- **它是整页一份的设置,不属于任何一个来源。** 五类来源(公众号 / 大V观点 / RSS /
  邮件简报 / 网页专栏)早就并到同一页、靠顶上那排胶囊切换了,而这张卡片贴在
  `#subspanel` 的末尾 —— 于是**每切一个来源它都跟着出现一次**,看上去像"每个来源
  各有一份定时设置",实际只有一份。
- **它一天也调不了一次,却按天天要用的频率占着版面。** 实测 1440×1000 的窗口上,
  这张卡片 250px 左右,加上三行页头,整页比视口高出近 300px —— 用户要滚动才能看全
  正文区,而一滚页头就把阅读区顶出去一截。这正是那张截图里"只剩一条"的由来。
- 现在的形态:页名旁边一颗带状态的按钮(`#subs-sched-open`,
  `定时同步 · 每天 07:30` / `定时同步 · 未启用`),点开是 `#sched-modal`。
  **状态写在按钮上**是刻意的 —— 收进弹窗之后它就是这份设置在页面上唯一的露头,
  不点开也得看得见"几点跑、开没开"。读不到设置时按钮保持裸标签「定时同步」,
  **不许写死一个"未启用"**:那是在报一个没查到的结论(`renderSchedEntry(null)`)。
- 表单一个字段都没动:`#sched-fields` / `#sched-channels` / `#sched-msg` /
  `#sched-save` 原样搬进弹窗,`pipe_schedule.js` 只多了开关和那颗按钮的文案。
  `tests/pipeline_probe.js` 那一组按 id 操作的用例因此不受影响。
- `loadSched()` 仍在页面加载时跑一次(`index.html` 末尾),所以按钮上的状态不等
  用户点开就是对的;`openSchedModal()` 里再调一次,而 `loadSched` 开头那道
  `schedDirty` 闸保证**没保存的编辑不会被重新打开冲掉**。

## 页头:五类来源共用一行

- 原来 feeds 的页头是 `h2(24px 宋体) + 一句标语 + 计数` 三行,实测 132px;公众号
  那一档则完全是另一套(一张 `.pipe-source` 连接卡 + 一张带自己标题和「同步全部」
  的管理卡)。两边既不一样、又都浪费。
- 现在统一成 `.source-heading` 一行:**来源名(h2,17px)+ 事实数字 + 动作**。
  口径取自 CLAUDE.md 视觉语言那节 ——「页头就一行:标题 + 同一行上的事实数字」,
  副标题/标语是落地页的东西。`#source-description` 整个删掉(来源名在上面那排胶囊
  里已经高亮着,标语每次切来源都要再读一遍)。
- 公众号那一档改成同一个部件:`#src-label` 成了它的 h2,`#src-status` 徽章和
  `#tgt-count` 跟在后面,动作区收「检查并接入绿泡泡 / 检查连接 / 同步全部」。
  原来那张连接卡的说明文字降级成一条横幅 `#src-detail`(形状同 feeds 的
  `#source-configuration`),**有话才出现** —— 接通且采集器没话说时那句
  「来源可用，可以开始同步。」只是在复述徽章。反过来,**采集器自己给的
  `message`/`hint` 一个字都不许吞**:接通之后它照样会说要紧事。
- 配套:`.pipe-source` / `.pipe-source-actions` 两套样式没有消费方了,一并删掉。

## 阅读区:锁在可用视口高度上,不再估常数

- 原来是 `.source-workbench:not(.is-managing) { height: clamp(460px, calc(100dvh -
  290px), 1000px) }`。**那个 290 是照着"没有任何横幅"的页头量出来的**,而页头并不
  稳定:`#source-progress`(同步中)、`#source-configuration`(账号待配置)、
  `#source-issues`(来源连接失败)、`#subs-error` 随时会冒出来,页头在窄屏上还会
  折行 —— 每多一条,工作台就被整体顶下去一截,整页出现滚动条。这和问答面板那条
  `calc(100dvh - N)` 是同一种缺陷(见 `frontend-feedback.md`:首次配置横幅不在
  估算里,输入区被顶出首屏)。
- 现在照 `body.chat-mode` 那套做:`body.subs-reading` 下 `#main-content` 自己变成
  flex 列、`height: calc(100dvh - var(--topbar-h))` + `min-height: 0`,
  `#subspanel` → `#subs-feeds` → `.source-workbench` 逐层 `flex:1` + `min-height:0`。
  横幅在与不在、页头折几行,都不用再改任何数字。
- **高度必须是 definite 的(`height`),`min-height` 不行 —— 这一条是第 1 轮审查
  实测打回来的。** 中途为了照顾矮窗口改成过 `min-height: calc(...)`,它只是下限,
  盒子的高度仍然按内容算;而一个高度不确定的 flex 列,`flex:1` 没有"可用空间"可分,
  它只会按子项的 **max-content 贡献**把自己撑开。空列表上完全看不出来(内容本来就
  矮,盒子"刚好"等于视口),列表里一有 30 条文章就当场发作:工作台 7598px、整页
  7823px,两栏各自那条 `overflow-y:auto` 一行都滚不动(`clientHeight ==
  scrollHeight`,`scrollTop` 写进去还是 0),只能滚整页 —— 正文一滚,列表和页头
  跟着跑掉。**这正是这次改动要治的那个毛病本身**,换了个更难发现的形态回来。
- `min-height: 0` 配套不能省:base 那条 `#main-content { min-height: 100vh }` 会盖过
  `height`(min-height 优先于 height),盒子被顶回 100vh、内容掉到首屏外。口径同
  `workspace.css` 的 `body.chat-mode`,那里也写着同一句。
- **矮窗口交给地板,不交给 `min-height`**:`height` 定死之后空间不够分时工作台会被
  flex 压到几十像素(1440×420 + 三条横幅实测只剩 10px),所以给
  `min-height: 360px` —— 压不下去就从 `#main-content` 溢出,整页照旧滚得到
  (1440×420 实测 `scrollHeight` 744、工作台 360)。
- **地板 360px,不许抬回原来那个 clamp 的 460**:820×760 这种窗口页头会折成三行,
  460 的地板反倒**自己制造出 32px 的滚动条**(实测),而不设地板时那里本来正好
  放得下。地板只管"矮到放不下"那一档。
- **只锁 feeds 的「最新内容」视图**(`sources.js` 的 `applySubsReadingLayout()`)。
  公众号视图和「订阅管理」都是自然流的长列表,锁住它们只会把列表挤进一个矮框里。
  类名在 `switchTab`、`openSubsPanel`、`updateSourceReadingView` 三处各挂一次 ——
  前两处管"离开/切到别的来源",第三处管"在 feeds 内部换视图"。
- 窄屏(≤760px)那套上下堆叠的布局不进这道锁,`@media (min-width:761px)` 圈住。

## 定时设置的「慢读取 × 用户编辑」(第 1 轮审查打回)

- `loadSched()` 原来**只在开机时跑一次**,那会儿 `schedLoaded` 为假、fieldset 本来就
  禁用着,编辑和读取碰不到一起。搬进弹窗之后 `openSchedModal()` **每打开一次就重读
  一次**,而这时 `schedLoaded` 已经为真、字段是启用的 —— 慢一点的那次回包会把用户这
  期间敲进去的时间和来源**原样盖回去**,接着点「保存设置」存下去的还是被盖回的那份
  (审查实测:读取中改成 18:10,约两秒后变回 16:45,POST 和服务端读回的都是 16:45)。
- 入口处那道 `if (schedLoading || schedSaving || schedDirty) return` 只拦得住"**再开
  一个**请求",拦不住**已经在途**的那个回来覆盖 —— 这是两件事。
- 修法:**读取期间把 fieldset 禁掉**(`$('sched-fields').disabled = true`),读完再放开。
  读失败时按"这一趟是不是从没读成功过"分两档:从没读成功过留在禁用态(没有值可编辑),
  重读失败则放开 —— 屏幕上那份仍是上一次读到的真实设置,照旧可编辑可保存。
- 没保存的草稿照旧不被动:入口那道 `schedDirty` 早退在禁用之前,关掉弹窗再打开,
  草稿和启用态都还在。

## 回归与反向验证

回归在 `tests/subs_probe.mjs`(37 项,全绿)。几条判据的射程,如实记下:

- **空列表上问"整页滚不滚"是不敏感的 —— 这是第 1 轮审查指出来的,如实记着。**
  「整页不出现滚动条」「工作台底边落在视口里」在**没有横幅、列表也是空的**那一档,
  对 `min-height` 版实现(问题形态)和旧常数版实现**都是绿的**:内容本来就矮,盒子
  "刚好"等于视口。所以现在探针里摆了两档更硬的:①把 `#source-progress` /
  `#source-configuration` 两条横幅打开再问一遍;②**走真实渲染路径灌 30 条带长正文的
  条目**,判据钉在"整页不滚 + 列表自己滚得动 + 正文自己滚得动"。
- 定点注入「高度退回 `min-height`」(审查实测那个缺陷原样):**3 红**,全在非空列表
  那一档(page 8259 / view 1000,工作台 7959,两栏 `scrollTop` 都是 0),而空列表的
  三条**照旧全绿** —— 逐字复现了"空内容吃不住这条判据"。
- 定点注入「撤掉整个 `body.subs-reading` 规则块」:**1 红**(摆了横幅那一档,
  scroll 1010 / view 1000)。
- 定点注入「拿掉读取期间那句 `disabled = true`」:**2 红** ——「读取还没回来时字段是
  禁用的」和「刚读完的这一份不是草稿」,后者的证据正是审查描述的那个形态:
  `{"dirty":true,"msg":"已保存：每天 16:45 自动同步","saveOff":false}`(屏幕上是服务端
  那份,底下却写着有未保存的修改,保存键还亮着)。
- **那次点击必须是真实鼠标事件**(`clickAt`,口径同 `tests/x_channel_probe.mjs`):
  `el.dispatchEvent(new Event('input'))` 对**禁用**控件照样触发、照样把 `schedDirty`
  设成真,拿它去验"读取期间改不进去"等于自己把结论做没了(第一版就是这么写的,
  修好之后那条用例反而红)。
- **反向钉一边**:字段放开之后改的那一笔必须活到保存 —— 探针接着改成 18:10 再
  `saveSched()`,断言 POST body 里是 `"time":"18:10"`(别为了防覆盖把正常编辑也吃掉)。
- 「页头只剩标题 + 事实数字」有两半,各有独立射程:把 `#source-description` 加回来
  → 元素存在那一半红;只把 `.source-heading` 的样式退回旧版(24px 宋体 + 12/28 边距)
  → 高度那一半红(57 > 56)。
- 定点注入「定时设置退回页尾常驻 + 拿掉页头入口和弹窗」→ **5 红**(四条定时相关 +
  「工作台吃掉页头之外的剩余高度」,工作台被挤到 579px、掉到视口的 60% 以下)。
- 定点注入「公众号退回独立连接卡」→ **2 红**。
- **探针不许因为元素缺失就抛异常中断**:第一次做定点注入时,`openSchedModal()` 在
  弹窗被拿掉之后当场抛错,整趟探针死在那儿、一条红都报不出来。现在那几处用
  `?.` 和 `try{}catch{}` 包住,缺元素报红而不是打断。
- `tests/subs_probe.mjs` 里原来写死的 `subsCatalog.length === 3` /
  `"公众号,大V观点,RSS"` 顺手改成跟着目录走(口径同 CLAUDE.md「没有 feed 的站点」
  那节)—— 仓库早就有五类来源了,那两条在改动之前就是红的,而红的地方跟改动毫无关系。

## 后续方案：满高分栏与紧凑内容列表


## 问题

页尾常驻的每日定时同步表单、三行来源介绍占掉首屏；即使移走表单，旧双栏比例
`.36fr : 1fr` 也只给列表约 26% 的宽度。长观点卡片另有四行摘要、独占两行的
状态及操作，用户最新截图的列表因此只能完整看到一两条。

## 决定

- 每日定时同步只有一份，入口在「我的订阅」页名旁，表单放在共用弹窗。
  入口显示最后一次成功读取／保存的启用状态及时间，不用未确认的默认值冒充已保存设置。
- 五类来源共用「来源名、事实数字、动作」页头，去掉占位标语；公众号连接信息保留为
  紧凑横幅，采集器给出的故障原因与操作入口不删。
- 桌面默认列表占约 41%（1100px 以下约 44%），搜索和来源筛选并排；摘要最多两行，
  状态及原文链接同一行。整张卡片及 Enter／空格可以展开完整正文，不重复一个
  「展开阅读」操作行。社交观点保留 15px 主体文字，正文不截断。
- 提供「只看列表／显示正文」，需要浏览时可使用全部宽度；主动选一条才恢复双栏，
  自动刷新不应该替用户取消列表模式。「专注阅读」仍保留，两个模式互斥。
- 桌面阅读态用主面板固定的可用视口高度，加上逐层 `flex:1; min-height:0`，
  有真实长列表和长正文时也只能在各自面板里滚动。当前工作台至少 480px，极矮窗口
  降级整页滚动，不把列表压成一条。公众号和订阅管理态退出阅读锁；手机继续单栏切换。
- 弹窗沿用现有焦点约束、Escape、遮罩关闭和焦点返回。重读时禁用整份表单，
  避免旧设置仍可编辑而新响应覆盖修改；关闭再打开保留脏数据，保存失败可以重试。
  逐来源的同步间隔及 X 取法是不同能力，仍留在订阅管理，不合并成一个间隔。

## 验证方式

`tests.test_subs_layout` 使用独立临时 data/vault，为四种 feeds 各放入 90 条长正文。
探针验证列表占比、可见完整条数、内层滚动、双向展开、四类 feeds 页头、横幅及
首次连接提示、窄屏、极矮视口和弹窗保存／重读竞态。不是拿空列表证明高度正确。
截图写入忽略目录 `output/subs-layout/`。

第一轮 55 项布局／交互检查全部通过，原订阅页探针 17 项全部通过，相关 unittest
64 项通过。1440×1000、带一条来源配置横幅时，列表宽 544px（工作台约 41%），
一条长观点约 117px，首屏完整显示五条；有两条额外横幅及长正文时仍在首屏内。

## 第二轮：按参考阅读器改成满高工作区

用户仍认为列表区域太小，并给出 QMReader 的满高分栏截图。第一轮把问题判断成
「栏宽与卡片密度」不够准确；接着提出「改成整页单栏列表」也不是参考站的交互。
实际要保留列表和正文并排，但不能在工作区上方堆来源页头、提示、切换标签和外边距。

- 桌面只留一条紧凑的全局工具栏。列表与正文直接从工具栏下延伸到视口底部，
  去掉主面板四周的占位、工作区外框圆角及主面板原有的 1600px 宽度上限。
- 来源名、数字、添加／同步按钮、最新内容／订阅管理切换都移到列表自己的工具栏，
  不再一起把正文往下推。四类 feeds 共用这一结构。
- 待配置、同步中、连接失败保留可见状态入口；点击打开完整原因和原有操作。
  详情浮在列表上而不挤压工作区。读取失败自动展开，不能把故障藏成一个不显眼的计数。
- 本轮提高极矮窗口的地板到 480px，给移入列表的工具栏与至少一条内容留空间。
  普通窗口仍只在列表、正文内部滚动；极矮窗口才允许整页滚动。
- 回归新增「工作区顶部最多 64px、底部贴视口」「2160×1600 不受旧宽度和高度
  上限影响」「状态详情可展开且不改变列表高度」等判据。原页头判据误选到了隐藏的
  公众号页头，取到 0 高度也会绿；现在直接验证 feeds 工具栏确实住在列表内。

参考页已用独立无头浏览器打开并截图；未登录的隔离会话与用户截图分别用于核对
外壳布局和有数据时的表现。Chrome 扩展仍返回下述认证错误，web-access 的用户
Chrome CDP Proxy 会直连普通浏览器，不符合本任务的浏览器通道限制，因此未启动它。

第二轮 61 项浏览器检查、17 项原订阅页检查及 64 项相关 unittest 全部通过。
1440×1000 下工作区从 y=53 延伸到 y=1000，高度 947px（第一轮约 714px）；
列表可滚动区域约 732px，首屏完整显示六条长观点。2160×1600 下也随视口伸展，
而非停在原主面板的 1600px 总宽上限。截图仍在 `output/subs-layout/`。

浏览器通道先检查 Chrome 扩展，当前连接返回
`unsupported Codex auth method: apikey`，因此按浏览器操作规范降级为独立无头
chrome-headless-shell，并使用临时 profile。没有接管用户普通标签，也没有启动有头浏览器。

相关回归命令：

```sh
python -m unittest tests.test_subs_layout tests.test_source_post_title \
  tests.test_daily_schedule tests.test_feed_sources tests.test_retired_channel \
  tests.test_reading_literal tests.test_chat_height
```

## 2026-10-11 合并冲突整合

- 保留 `main` 的公众号统一页头、仅有说明时显示连接横幅及采集器错误提示。
- 定时弹窗统一使用 `subs-sched-open`、`subs-sched-label` 和 `openSchedModal`／`closeSchedModal`；保留读取期间禁用编辑、未保存数据保护、首次失败禁用与重读失败保留上次真实设置的行为。
- 保留满高分栏、480px 极矮窗口地板、列表模式及浮层状态详情；视口锁只由 `applySubsReadingLayout` 维护，移除重复的旧 CSS 高度规则。
- 两套浏览器探针均保留，页头断言改为验证 feeds 列表内真实可见的工具栏。
- 浏览器扩展本轮检查仍报 `unsupported Codex auth method: apikey`；按规定降级到独立无头 Chrome 与临时 profile，不接管普通标签、不启动有头浏览器。

### 合并后验证结果（2026-10-11 历史）

- `.venv/bin/python -m unittest tests.test_subs_layout tests.test_source_post_title tests.test_daily_schedule tests.test_feed_sources tests.test_retired_channel tests.test_reading_literal tests.test_chat_height`：64 项通过；其中有数据布局探针 62／62 项通过。
- 通过 `tests.headless.run_probe` 在隔离数据服务运行 `tests/subs_probe.mjs`：当时 40／40 项通过。页头移动后的选择器修复与当前树复跑结果见下方 2026-10-13 记录。
- `.venv/bin/python -m unittest tests.test_wechat_setup tests.test_wechat_discovery_integration tests.test_pipeline_stop`：30 项通过，1 项因未配置 `KB_WECHAT_TEST_RUNTIME` 的隔离 Reader 运行时跳过；未读取用户真实微信数据库。
- 修改过的 JavaScript 与探针均通过 `node --check`，HTML 的 329 个 ID 无重复，`git diff --check` 通过，代码文件均少于 700 行。
- 截图：`output/subs-layout/social-1440.png`、`schedule-1440.png`、`source-notices-1440.png`、`large-2160-1600.png`。1440×1000 工作区仍从 y=53 铺到 y=1000；列表与正文均可独立滚动，弹窗可关闭并返回入口焦点。
- 在任务分支 `ash/waO-34BV` 内合入 `main`；没有检出、提交或修改目标分支，也没有删除分支或 worktree。

## 2026-10-13 第 1 轮审查修复

本轮只修复审查指出的两条阻塞项，不改动非阻塞观察项或已确认的 X 通道基线问题。

- `sourceError()` 仅在非空错误文案变化时自动展开详情；同一错误的后续轮询保留用户的收起／展开选择。恢复后再发生同类错误仍会重新提醒。
- `updateSourceNotices()` 在所有状态消失、整条提示被隐藏时同时复位 `open`，防止后续同步或待配置提示继承旧展开态、遮住搜索框。
- `tests/subs_probe.mjs` 三处页头选择器统一为 `#subs-feeds > header.source-heading`，检查真实可见的工作区工具栏，不再访问已移除的列表内页头。
- `tests/subs_notices_checks.mjs` 接入布局探针，增加 15 项定向浏览器检查：真实概览请求失败后的相同错误重试、错误文案变化、隐藏／重新出现、手动展开及搜索框命中和鼠标聚焦。

### 复现与当前验证结果

- 只加入回归、尚未修复状态逻辑时，布局探针为 **109／118 项通过、9 项失败**，可复现反复展开及搜索被遮挡；修复后为 **118／118 项通过**，浏览器无未捕获异常。
- 在隔离 `tests/serve_sources.py --layout-fixture` 服务上，通过 `tests.headless.run_probe` 实跑修复后的 `tests/subs_probe.mjs`，当前结果为 **40／40 项通过**，进程正常结束，不再抛页头为 null 的 TypeError。
- 相关来源、定时同步、阅读和聊天布局 unittest **63 项全部通过**；加上布局测试，共 **64 项 unittest 通过**。
- 本轮修改的 JavaScript 和探针均通过 `node --check`；`git diff --check` 通过；修改过的代码文件均少于 700 行。
- 截图已检查：`output/subs-layout/source-notices-repeated-error-1440.png` 和 `output/subs-layout/source-notices-recovered-1440.png`。相同错误重试后及恢复后开始同步时，详情保持收起，搜索框未被遮挡；检查结果见 `output/subs-layout/results.json`，旧探针输出见 `output/subs-layout/subs-probe-results.log`（均为忽略的验证产物）。

复跑命令（使用项目已有虚拟环境）：

```sh
python -m unittest tests.test_subs_layout tests.test_source_post_title \
  tests.test_daily_schedule tests.test_feed_sources tests.test_retired_channel \
  tests.test_reading_literal tests.test_chat_height -v
```

旧探针使用隔离服务地址及 `tests.headless.probe_env()`，通过
`tests.headless.run_probe(['node', 'tests/subs_probe.mjs', base], ...)` 执行，
服务与浏览器均在结束后清理。

### 本轮浏览器通道

先重新检查 Chrome 扩展，`cua.getState()` 返回
`Browser 1: Error: unsupported Codex auth method: apikey`，无法建立具名后台标签会话。
因此按规定降级为独立无头 `chrome-headless-shell`，每次使用临时 profile；
CDP 仅连接本次启动的隔离无头实例。未接管或直连用户普通标签，未激活 Chrome，
未切换用户标签，也未启动有头浏览器。所有被测数据和服务均为本地隔离 fixture。
