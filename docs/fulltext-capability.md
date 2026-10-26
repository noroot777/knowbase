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

### 浏览器覆盖意见的边界待裁定

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

就第 3 条提交范围裁定请求;第 1、2 条已修复,不将有效事实写成驳回理由。

## 留给验收

新增回归覆盖旧误开值、直接 API 开启、部分编辑保留设置、默认种子、默认关闭能力、混合归属、补抓正向对照、翻译和挂起出口。
验收阶段执行 `tests.test_fulltext_capability`、`tests.test_fulltext_recheck_capability`,并覆盖既有
`tests.test_failure_sources`、`tests.test_fulltext_failures`、`tests.test_fulltext_recheck_race`、`tests.test_fulltext_admit`、
`tests.test_feed_fulltext`、`tests.test_admit_three_ways`、`tests.test_fulltext_held_exit`、`tests.test_fulltext_browser`、
`tests.test_feed_sources`,再按项目统一闸门执行全量回归。
本任务不把这些尚未执行的用例报告为通过。
