# 旧正文补抓的来源能力约束

## 范围与取舍

只处理独立转出的两条旧路径:自动补抓资格和订阅补抓开关。不改浏览器补抓入口、扩展、回执或原任务其他审查意见。
本任务基线为 `2fb0122`,尚无 `SourceType.body_from_link`,因此补入与既有界内修复相同的共享定义及 `permits_link_body()`:
默认关闭,RSS、网页专栏、手动收录显式开放。旧补抓和订阅保存共用这个能力判据。
原任务工作区未修改,也未重新开启原任务。

- 自动补抓的 `_eligible()` 同时检查条目和归属订阅的来源能力。补抓队列、挂起列表、恢复写入、翻译闸门及存量复查共用它。
- 不适用来源即使在旧库中留着 `fulltext=1`,也不再被自动抓取,不会因历史挂起状态永久扣留英文摘要翻译。
- 订阅新建/编辑显式提交不适用的 `fulltext=true` 返回 400,不保存任何部分改动。
  不带该字段的正常编辑会把旧误开值收敛为关闭,其余选项保留;默认订阅种子同样受能力约束。
- 表单隐藏并禁用不适用的开关及说明,清掉旧勾选态,提交时再次按打开表单时的来源能力约束。
  在来源之间来回切换后,合法来源仍可正常勾选;订阅行不再显示失效的补抓标记。
- 不做批量数据迁移,不删除旧挂起记录,不猜测或恢复此前已被覆盖的正文。资格在使用时判定,不会替用户打开开关。

## 实际执行的检查

遵守项目约定,**未执行任何回归测试**,新增的 `tests/test_fulltext_capability.py` 仅写入并做语法检查,留给验收执行。
以下是静态检查、一次性缺陷冒烟和真实页面点检,不是测试套的通过记录。

```text
/Users/fjh/code/knowledge-base/.venv/bin/python -m py_compile \
    kb/source_types.py kb/feeds/fulltext.py kb/feeds/registry.py tests/test_fulltext_capability.py
node --check kb/static/js/source_forms.js
node --check kb/static/js/sources.js
git diff --check
```

上述检查均成功。修改后的代码文件均不超过 700 行。未安装 Ruff 的当前 venv 无法执行 Ruff 检查,未安装新依赖。

### 隔离接口与状态冒烟

用 `daemonize()` 和 `uv run --offline --no-sync` 启动实际应用,数据与 vault 都在本任务临时目录中;
全部订阅停用,调度器关闭,网络采集和模型调用均被拒绝。未访问生产库或启动真实管道。
一次性脚本直连实际 HTTP 路由,另在相同隔离库核对资格与挂起状态。补抓成功的正向对照仅替换 `fetch_one` 的取文结果,
队列、写回和翻译闸门使用实际代码,不证明网络提取或模型本身可用。

真实输出见 `evidence/fulltext-capability/smoke.json`:

- 简报旧开关为 1、条目带挂起原因:补抓待办为 0、请求次数为 0、挂起列表为空、恢复为 0;
  `unsettled=false`,翻译队列为真。原摘要、`summary` 类型、修订号 0、空补抓时间、尝试次数 0、旧挂起原因均未变。
- POST 新开简报补抓和 PATCH 显式开启均返回 400,原因是「简报不支持去原文链接补抓正文」。
  仅改订阅名返回 200、旧开关变 0,提取选择和停用状态保留。
- RSS 挂起时待抓为 0、正文未定、翻译不入队;重复 key 只恢复 1 条,重复恢复返回 0,恢复后待抓为 1。
  补抓实际写回后 `done=1`,正文与输入一致、类型为 `content`、修订号为 1、补抓时间非空、挂起清空,新正文可进入翻译队列。

### 浏览器通道与页面点检

第一级实际尝试了 Chrome 扩展:先命名「🔎 订阅补抓能力」会话,再请求新建 `visible:false` 标签。
扩展返回 **`Capability is not available: visibility`**,无法保证后台创建,因此停止该通道,没有改用可能激活窗口的默认新标签。
未接管用户现有标签,未连接用户 Chrome 调试端口,未激活窗口或切换标签。

按规范改用第二级独立 `chrome-headless-shell`,通过现有 `launchShell()` 管理临时 profile 和进程回收。
未安装 Playwright,没有有头浏览器,也不需要任何登录态。仅打开隔离服务并检查编辑弹窗,没有在浏览器里保存设置。

真实观察见 `evidence/fulltext-capability/browser.json`:

- 简报、大V观点:旧库开关为 1,但开关及说明隐藏、控件禁用且未勾选;订阅行不显示补抓标记,提取勾选保留。
- RSS、网页专栏、手动收录:控件可见、可用且保留已勾选状态,订阅行保留补抓标记。
- 简报切回 RSS 再打开编辑框,隐藏和禁用状态不串到合法来源。
- 390px 移动视口页面宽度与滚动宽度都是 390px,没有横向溢出;运行时 JS 异常为空。

最终代码版本重新启动隔离服务后重复上述点检,证据文件来自这次最终版本。收尾已关闭隔离服务与无头浏览器,
确认端口 63656 无监听、没有本任务服务或浏览器进程残留,并删除临时数据及 profile;扩展会话标签列表为空。

![简报编辑框不再提供补抓开关](evidence/fulltext-capability/form-digest.png)

![RSS 编辑框保留补抓开关](evidence/fulltext-capability/form-rss.png)

## 复查入口与失败场景补齐

完整读取本任务审查报告后,处理其中第 1、2 条。保持既定来源口径不变:大V观点不开放正文链接替换,
RSS / 网页专栏 / 手动收录开放;失败明细用例按 `spec.body_from_link` 挑选来源,不再用 adapter 猜测能力。

- 复查扫描改用 `fulltext._eligible('fi.key')`,不再保留第二份订阅 EXISTS。旧误开简报/大V条目不进入清理计划。
- 复查写回的 UPDATE 同时检查共享资格和原有 `body=?` CAS。计划生成后最后一条补抓归属关闭或被删时,
  正文、译文及其余条目字段全部保留;还有另一条合法归属开着时照旧放回补抓队列。跳过数量和原因如实显示。
- 增补 `tests/test_fulltext_recheck_capability.py`,覆盖能力正反两侧、撤销订阅/能力和多归属。
  只做语法检查,**未执行这些用例或任何其他回归测试**。

本轮实际检查命令:

```text
/Users/fjh/code/knowledge-base/.venv/bin/python -m py_compile \
    scripts/feed_fulltext_recheck.py kb/feeds/fulltext.py \
    tests/test_failure_sources.py tests/test_fulltext_recheck_capability.py
/Users/fjh/code/knowledge-base/.venv/bin/python /tmp/kb-fulltext-review-oYebSouTUqu6.py
git diff --check
```

命令均成功。一次性脚本未导入任何 `tests` 模块,未启动服务或管道;数据、vault 和视频路径均指向独立临时目录,
socket 连接被拒绝,真实网络调用为 0,没有模型调用。脚本及临时数据已删除。输入文件指纹和真实输出存于
`evidence/fulltext-capability/review-fix.json`,仅作为本轮缺陷复现证据,不作为可自动复用的回归记录。

- 简报和大V旧 SQL 原本都能选中,现在均选中 0 条;455 字正文、译文及整个条目记录不变。
  直接把旧计划交给写回也拒绝,各跳过 1 条,整个记录仍不变。
- RSS / 网页专栏 / 手动收录各真实降级 1 条,类型变 summary,旧译文清空,随后实际补抓队列判据均为真。
- 扫描后关闭或删除最后一条补抓订阅,各实际修改 0、跳过 1,整个条目不变;还有合法归属开启时仍实际修改 1。
- 失败明细夹具静态筛选表达式为 `not spec.body_from_link`;三个许可来源均可创建补抓订阅。
  仅对取文函数注入 HTTP 403,实际队列/失败写回给出正确的来源、条目身份和原文链接,包括新增覆盖的手动收录。

本轮改动没有可见 UI 变化,没有进行任何浏览器操作,因此没有新增浏览器通道降级或截图。
已修改代码文件均低于 700 行。

## 把判据接到剩下三个入口上

用户裁定不采纳上一轮的范围驳回,按报告修。那份报告点的三条是同一个类问题的三个实例 ——
「这一类来源的正文允不允许被 `url` 指向的那一页替换」在仓库里还有三处没接到 `permits_link_body()` 上。
当时的做法是让这四个入口(补抓队列、采集直取、浏览器采集、存量复查写回)各自调一次 `permits_link_body()`,
名单写在 `kb/source_types.py` 的 docstring 和一条列举式用例里。**那张名单漏了第五个**,见下一节。

- **采集时直取 Markdown**(`markdown_source.enrich`):闸门原先是订阅 `fulltext` 生值的**反面** ——
  那不是资格判据而是分工(开了补抓的让补抓阶段去取),于是简报(规定 `fulltext` 必须为 0)正好落在打开那一侧。
  生产库里一条 TLDR AI 的简讯因此被整篇 GitHub README 顶掉:9242 字、`content_kind` 变 `content`、已导出进 vault;
  同一期另外 4 条同样指向 github.com 的没被换,差别只是那一刻 `api.github.com` 放不放行。现在能力那一道排在最前面。
- **浏览器采集**(`browser_capture.target`):原先按 `kind == 'social'` 字面量挡人,只挡住大V、放过简报。
  改动与原任务分支 `23f96aa` 的同一处**逐字相同**(`git show 23f96aa:kb/browser_capture.py` 的 `target()` 与本分支
  `diff` 无输出),合并不会冲突。**没有照搬那边 `save()` 里的同一道**:那是它为「从审计表恢复 intent」那条新路径
  加的纵深,本分支没有那条路径 —— `save()` 的 intent 只能由 `create()` 产生,而 `create()` 第一件事就是 `target()`。
  把那边的 `save()` 整段搬过来会连它的回执重构一起带进来,那属于原任务。
- **存量复查**(`feed_fulltext_recheck`):射程和动作**分开**。射程不再受来源能力约束(两支并集:属于开着补抓的订阅,
  或者不许可来源手里拿着一份 `content`),否则简报 / 大V上已经被换错的正文没有任何工具够得着 —— 上一版实测三种
  射程一律 0 条、还以「没有需要复查的条目。」成功退出。动作按能力分两支:许可来源照旧换壳放回队列;不许可来源
  **只从备份装回原文**、补抓标记一个都不碰、**不拿「标题与链接」顶替**(它不会重抓,顶替就是永久毁掉),
  没有备份就报「收拾不了」并给出路。点名了不走补抓的来源却一条都没选中时,把原因说出来。

### 实际执行的检查

```text
.venv/bin/python -m py_compile kb/source_types.py kb/browser_capture.py \
    kb/feeds/markdown_source.py scripts/feed_fulltext_recheck.py \
    tests/test_fulltext_capability.py tests/test_fulltext_recheck_capability.py \
    tests/test_fulltext_recheck_race.py
git diff --check
```

遵守项目约定,**没有执行任何回归测试**(包括本轮新增和改动的用例),改为一次性缺陷冒烟 + 定点反向注入,
跑在独立临时 `KB_DATA_DIR` 上,无网络、无模型调用,收工已删除脚本与临时库。本轮没有可见 UI 变化,未操作浏览器。

正向(修复在位)实测:简报那条 `enrich` 一次请求都不发、正文与 `summary` 形态原样;**未开补抓的**许可来源照旧直取
(反向对照 —— 开着补抓的那条本来就该早退,拿它当对照会把这道闸测成假绿);四种射程都选中那条被 README 顶掉的简讯、
而许可来源上一篇正常补抓成品一条都不选;没有备份时 `stuck=1`、整条记录逐字不变、不进补抓队列;给了备份则装回原
摘要、`content_kind` 回 `summary`、`content_hash` 重算、`fulltext_at`/`fulltext_tries`/`exported_hash` 一个都没动、
译文清空、`extract_stale=1`、再扫 0 条;浏览器采集对简报抛「此来源的正文不允许被原文链接中的页面替换」、对 RSS
照旧放行;扫描与写回之间撤销来源能力时 `moved=1`、正文原样。

反向(定点撤回,一次只动一处,每轮用 git 还原并核对工作区干净)六处全部变红:去掉 `enrich` 的能力判据 → 当场去取
外链那一页;射程退回共享资格 → 一条都选不中;写回不分两支 → 简报条目配上重建壳、有备份也装不回去;只放开「配什么
壳」那一道 → 正文被盖成两行标题加链接;写回时实时重判走哪一支 → 同一形态的真实损坏;浏览器采集退回 kind 字面量 →
简报重新放行。另有一处注入**红 0 条**:在写回前再加一道「能力变没变」的 CAS —— 分流已经固定在算计划那一刻,
能力撤销由许可支自带的 `_eligible()` 挡住,那一道是真冗余,已经删掉并把这个结论记进了脚本。

### 上一轮的边界请求已由用户裁定

审查第 3 条技术事实成立,但落点是原任务明确保留的「从已打开文章标签主动读取、预览并回填」链路,
不属于独立转出的旧自动补抓/订阅设置范围。当前分支仍保留该基线行为,**不声称此分支已修复浏览器覆盖问题**。
本轮未改浏览器入口、保存或前端按钮,也未重新打开原任务。

具体证据:

- `git diff 2fb0122..HEAD -- kb/browser_capture.py kb/web/browser_capture_api.py kb/static/js/source_reader.js` 无输出。
  前后对应 blob 一致:分别为 `9fb4d9d`、`067c39f`、`99255bb`。
- 基线 `2fb0122:kb/browser_capture.py:46` 已存在仅排除 social 的逻辑,不是本任务新引入的写入路径。
- 原任务已有独立修复 `23f96aa`: `kb/browser_capture.py:47` 的目标检查和 `:143` 的保存检查使用
  `permits_link_body()`,`kb/static/js/source_reader.js:48` 的入口使用相同能力字段。
  本任务没有重复合入该提交;应由原任务交付该链路,整合前本分支上的该缺陷仍存在。

用户裁定**不采纳**这条驳回。浏览器采集那一处已按上面所写在本分支接上同一份判据,与原任务分支逐字相同。

## 判据搬到写入层(第 1 轮审查的三条)

上一节那张「四个入口」的名单漏了第五个:`grab_store.replace()`,人在投递栏点「重新抓一次」那条路。
`known()` 按 **url** 查全表,命中的可能是任何一类来源的条目,而质量判据对非手动收录的条目恒真 ——
于是 `POST /api/grab {"refetch": true}` 把一条简讯的 28 字摘要换成了 1338 字的仓库 README(`content_rev` +1、
译文整组作废、后半条链被起起来)。走的是公开接口,不是内部直调。

**按报告指的层级修根因:判据搬到写入层,名单改由测试维护。**

- `kb/source_types.py` 新增 `link_body_sql(item)` —— 同一份判据的 SQL 形状。凡是要改写 `feed_items.body`、
  而内容来自这一行 `url` 的 UPDATE,都把它拼进自己的 WHERE(元规则「判据待在写语句自己的 WHERE 里」):
  `fulltext._save`、`browser_capture` 的写回、`grab_store.replace`,以及经 `_eligible()` 带上的选队列和存量复查
  许可支。判据取 `SOURCE_TYPES` 全集而不过 `feed_types()` —— 它问的是「这张表里哪些 kind 许可」,跟谁管理无关。
- `tests/test_link_body_entries.py` 是这张名单现在的住处:扫 `kb/`、`scripts/` 下每一条写 `body` 的
  `UPDATE feed_items`(排除 docstring,否则 `link_body_sql` 自己那段说明会算成一条假写入点),要求逐条声明内容
  从哪来,**声明集合不等就红**;逐函数比「带闸几条 / 不带闸几条」而不按语句顺序对号 —— 删掉某条的闸会红,
  **给备份恢复那一支加上闸同样红**(那一支要把原文原样装回不许可来源,带闸之后 rowcount 恒 0)。基线扫出 7 条
  语句、6 个函数,与声明表完全相等。残留盲区一处写在用例 docstring 里:`fulltext._fail` 的 SET 列表在 Python 里
  拼,扫描器看不见里面有什么。
- 入口那一侧也拒(`grab_api._recover` 按 `seen['body_from_link']`),**连 HTTP 都不发** —— 只靠写语句那一道的话,
  人得先白等一次抓取、拿到的还是一句说不出原因的「换不成」。界面上那颗按钮据回包里的 `body_from_link` 不露面,
  **前端只拿它和 `false` 比**:字段缺失(浏览器缓存着旧脚本)时按原来那样亮,不凭「读不到」就把错存唯一的出路收走。
- **第 2 条(备份判据与扫描不同源)**:`_from_backup` 现在带上这一行的 `kind`、调 `_why` 本身验备份,
  口径只留一份。这个函数上同一处口径分家栽过两次,第二次的后果是污染备份被装回**它自己头上** ——
  `body=?` 的 CAS 比的是同一份内容、rowcount 照样是 1,于是报「已收拾 1 条」而正文一个字没变,译文却被作废、
  `content_hash` 被改写,每跑一次 `run` 重复一轮。顺带:老备份缺 `content_kind` 那一列时只有许可来源还用得上它
  (不许可那一支判不出这份备份是不是污染后做的,回落到「收拾不了」);`cmd_run` 留下「收拾不了」时**不再报 0**。
- **第 3 条(前端入口按来源名字面量渲染)**:`kb/static/js/source_reader.js:48` 换成
  `sourceModules.get(item.kind)?.body_from_link`,与原任务分支 `23f96aa` 的同一处**逐字相同**(含它那一道
  `!item.x_post_meta`),合并不会冲突。没有竞态:`sourceActive()` 本身要求 `sourceModules.has(kind)` 才渲染条目。
- **射程 ② 没有扩成「和备份比 `content_hash`」**,理由是生产只读实测(下一节),改的是**空结果那句话** ——
  它原先说「当前一条都没有」,现在如实交出射程边界(元规则:判不准不许返回「确定没有」)。

### 这一轮实际执行的检查

**没有执行任何回归套件**(项目硬约束:执行者不得执行回归测试)。做的是:

- **报告自带的两份复现**,改动后重跑:`repro-1` 的断言 `assertNotEqual(before, after)` 现在**失败**(它为复现而
  写,失败即已挡住),回包 `replaced=False`、`body_from_link=False`、`content_rev 0 -> 0`、译文未动;
  `repro-2` 从「已收拾 1 条」变成「收拾不了 1 条」、退出码 0 → 1,译文与 `translated_at` 原样保住。
- **一次性 smoke**(借 `tests/` 的夹具类写断言,不执行仓库里的用例):入口拒绝 / 写语句拒绝 / 手动收录照旧换成
  三条,污染备份 / 更早的干净备份 / 缺列老备份三条,全部通过。**如实记录一处越界**:`smoke_recheck.py` 继承了
  `RecheckCapabilityTests` 当夹具,unittest 连父类的用例一起收走了,实际跑了 33 条(全绿)—— 本应像报告那份
  `repro-2` 一样屏蔽父类方法。
- **扫描器自验**:直接调 `_statements()` 看基线输出(7 条语句 / 6 个函数 / 声明集合相等)。
- **九次定点反向注入**,每次只撤一处判据、还原走 `git checkout --` 再断言 `git diff --quiet`(改动已先提交),
  全部在**该红的那一条**上红、别的不动:
  ① 摘掉 `replace` 的闸 → 集合级用例「逐函数带闸数」红(集合仍相等,语句还在);
  ② **给备份恢复那一支补上闸 → 同样红**(反过来钉一边:这一支带闸之后 rowcount 恒 0,存量复查那条出路会失效);
  ③ 新增一条没声明的写 body 语句 → 「声明集合相等」红(带闸数那条仍绿 —— 两条分工正确);
  ④ 摘掉 `replace` 的闸 → 写语句那条行为用例红(`assertFalse(replace(...))` 变 True);
  ⑤ 摘掉 `_recover` 入口那一道 → 入口那条红(打桩的 `grab.grab` 被真调了:「不许发请求」);
  ⑥ 备份判据换回只问文字那一道 → 污染备份那条红,**顺带「更早的干净备份」那条也红**(它取了污染那份);
  ⑦ 前端入口换回 `item.kind !== 'social'` → 来源卡片探针「简报:卡片上没有从浏览器补抓」红(实际 1 个);
  ⑧ 摘掉按钮那一道能力条件 → grab 探针「不许可来源:不露面」红;
  ⑨ 把「只和 `false` 比」改成真值判断 → 「回包没给这一列时照旧露面」红(钉住那个刻意的宽松默认)。
- **浏览器真实点检**(两份探针,各自跑完一整轮):`tests/source_post_probe.mjs` **26/26**、
  `tests/grab_probe.mjs` **39/39**,新加的断言是「简报不亮 / RSS 照旧亮 / 大V不亮」和「不许可不露面 /
  许可照旧露面 / 回包缺这一列照旧露面」。**降级说明**:按全局浏览器规范,第一档(在用户已登录的 Chrome 里开命名
  组后台标签页)这个会话没有可用的扩展通道,所以走第二档 —— 独立无头壳(playwright 缓存里的
  `chrome-headless-shell`、临时 profile、CDP 直连、不连网、始终无头)。本次验证对象是本机页面 + 自带 mock 后端,
  **不需要登录态**,所以这次降级不会产生假失败。
- **顺手修了一处假绿**:`source_post_probe.mjs` 的条目工厂不给 `kind`、modules mock 不给 `body_from_link`
  (真回包里两者都有:`registry.items` 是 `SELECT *`、`catalog()` 走 `asdict`)。缺了它们,这个入口在**所有**卡片上
  一律取不到值 —— 「该亮的那一边」会测成假绿,而面板看起来一切正常。加断言才暴露出来(第一次跑就红在 RSS 那条)。
- **生产只读实测**(2026-10-26):把 `data/feeds.db` 与 `data/backups/feeds-before-xbackfill-20261021-103633.db`
  复制到 `/tmp` 之后 `mode=ro` 打开,原库 mtime 未变、一个字节没写。结论:`digest` 231 条的 `content_rev`
  **全为 0**(`replace` 必然 +1 → 这个入口在生产库上还没造成过污染),`content_kind='content'` 的只有 1 条
  (上一节那个 `enrich` 受害者,现有射程看得见);不许可来源里 hash 与备份不同的 31 条**全是 social**,来路是
  `x_article_backfill`(空正文 → 推文自己的长文)—— 把射程 ② 换成 hash 比对会当场误伤这 31 条,而不许可那一支
  只认备份,会拿短推文盖掉回填好的长文。
  **清场**:两份数据库副本都复制到 `/tmp` 之后再 `mode=ro` 打开,用完删掉;收尾又复制了一次核对「夹具有没有泄漏
  进生产库」—— `example.org` 的条目和订阅各 0 条、`digest` 的 `content_rev` 仍是 231 条全 0。期间生产库的 mtime
  确实变过一次(15:23 → 18:31)、8787 那个进程的 pid 也换了(39425 → 70582),**不是这一轮做的**:库里最新几行是
  生产自己那一轮 rss 批次(`batch-6adc61d7…`),而这一轮一条写生产库的语句都没有。

## 留给验收

新增回归覆盖旧误开值、直接 API 开启、部分编辑保留设置、默认种子、默认关闭能力、混合归属、补抓正向对照、翻译和挂起出口;
这一轮又加了写入层那条集合级用例、手动收录两道闸的正反两边、以及备份判据的三条(污染备份 / 更早的干净备份 /
缺列老备份)。
验收阶段执行 `tests.test_link_body_entries`、`tests.test_grab_store`、`tests.test_grab_endpoint`、
`tests.test_fulltext_capability`、`tests.test_fulltext_recheck_capability`,并覆盖既有
`tests.test_markdown_source`(改了 `enrich`)、`tests.test_browser_capture`(改了 `target`)、
`tests.test_failure_sources`、`tests.test_fulltext_failures`、`tests.test_fulltext_recheck_race`、`tests.test_fulltext_admit`、
`tests.test_feed_fulltext`、`tests.test_admit_three_ways`、`tests.test_fulltext_held_exit`、`tests.test_fulltext_browser`、
`tests.test_feed_sources`,再按项目统一闸门执行全量回归。
本任务不把这些尚未执行的用例报告为通过。
