# 本地回归的分层门禁(`./kb.sh test`、`tests/tiers.py`、`tests/headless.py` 的浏览器闸门)

> **这一份的第一节就是这一块的硬约束(不许怎样)—— 动这块代码之前先读它**,后面是动机、
> 实测证据、回归清单和反向验证红数。CLAUDE.md 只留跨块的元规则和一张索引。

## 硬约束

- **全量不能删,只能挪。** 这套测试有一类已经实际发生过的问题:测试同进程共用
  `KB_DATA_DIR`,打桩范围没盖住后台线程/子进程时会带红**别的文件**的用例,而且
  **只在同跑时红、单跑那个文件永远绿**。分层、选测、并行,任何只跑子集的方案都
  看不见它 —— 所以 `full` 层必须留着,push 前至少跑一次。CI 在 push/PR 上也全量跑,
  但 runner 上没有 chrome-headless-shell,浏览器回归在那边自然 skip:**探针只有本机
  这一道防线,`probes`/`full` 不跑,它们就没人跑**。
- **会起真实浏览器的测试类,一律挂 `tests/headless.py` 的共享闸门**
  (`browser_gate`,或它的两个包装:.mjs 探针用 `requires_headless`、Python 直驱
  CDP 只要壳的用 `requires_shell`),**不许在测试文件里自己写 `skipUnless` 按工具
  可用性做门** —— 自己写的那份不认快速层开关,fast 层照样起浏览器;也不带
  `kb_browser` 标记,probes 层统计不到它(事故形态:七组共 90 条真实浏览器用例
  各自 `skipUnless(SHELL/_REAL_CHROME…)`,其中两组不引用 tests.headless,又漏出
  probes 名单)。工具要求按组自定(node+壳 / 只要壳 / 原生 Chrome),闸门不强加
  统一依赖。结构守卫走 AST 的 skipUnless/skipIf 条件源码(docstring 和注释不进
  射程),在 `tests/test_tiers.py`。
- **所选层对快速层开关的态度必须显式**:fast 设 `KB_SKIP_BROWSER_TESTS`,probes/full
  **清掉继承值**,都在 discover 之前(闸门是类装饰器,import 那一刻就定了)。不清
  的话,外头 export 过一次的壳里,工具齐备、显式全量照样把浏览器回归整层拦掉,
  而且一切显示正常。
- **纯逻辑邻居的通过不许充当浏览器覆盖。** 覆盖账按 `kb_browser` 标记记(账要在
  run 之前收 —— unittest 跑完会释放 suite 条目):probes 层浏览器用例**没验全就按
  失败退出**(事故形态:缺壳时浏览器用例全 skip、几百条邻居通过,命令照样报成功);
  fast 层有标记用例真跑了同样失败(闸门失效的运行时信号);full 沿用 README/CI
  那条 discover 的「缺工具自然 skip」政策,但覆盖数要如实打印。
- **覆盖账只认 `TierResult` 的正向执行事件,不许「计划减 skip」反推。** unittest
  的跳过不总按方法 id 记账:`setUpClass`/`setUpModule` 抛 `SkipTest` 记的是
  `setUpClass (模块.类)` 这种 holder、`subTest` 里的 skip 记的是带参数的子例 id ——
  反推会把一条都没开始的整组算成「全跑了」(事故形态:`Ran 0 tests` + OK +
  「真跑 2/2」,执行条数还打出负数)。「真跑」= 方法真的开始、整条没被 skip、
  子例一个都没 skip;汇总的执行条数同一口径,不许再用 `testsRun` 减 skipped 条数。
  probes 层**浏览器集合为空(一条都没发现)同样按失败** —— 名单或标记的口径坏了
  时恰好表现为 0 条,不许借「0 < 0 为假」混成成功。
- **skip 理由要在默认输出里看得到**(按原因去重打印):安装提示写在装饰器里不算
  数,verbosity=1 只显示 `s`,缺了工具的人看不到装哪个、怎么装。
- **probes 的文件名单是现算的(「源码引用 `tests.headless`」),不许改成手工维护的
  列表。** 手工列表会静默过期:新探针文件没人登记,就落进 fast 层,快速层又慢回去。
  现算口径宁可多圈(docstring 里提一句也会被圈进来,多跑几条毫秒级用例无害),
  不可少圈;能圈全靠的是上面那条 —— 起浏览器的类必须挂共享闸门,必然 import
  `tests.headless`。
- **`./kb.sh test` 一律把 `KB_DATA_DIR` 换成全新的临时目录,所有权在它自己的子
  shell 里,正常结束、失败、被取消(INT/TERM)都要收掉**(先收测试进程再删目录,
  反过来删的是正在被写的目录)。事故形态:一条用例在 worker 线程里跑索引,
  `mock.patch` 的作用域只盖住了主线程那次调用,替身一恢复,后台线程连到**真实**的
  `$KB_DATA_DIR/kb.db`,把夹具文档写了进去。这层隔离让同类泄漏最多弄脏一个马上
  会删掉的目录;但它只护真实库,**不**消除同轮内测试之间的互相污染 —— 后者正是
  「全量不能删」的理由。
- **TZ 与离线口径跟 CI 一致**(`TZ=Asia/Shanghai`、`HF_HUB_OFFLINE=1`、
  `TRANSFORMERS_OFFLINE=1`),理由同 `.github/workflows/ci.yml` 里那两段注释。

## 为什么分层(实测,2026-10,本机)

- 全量 3493 条跑 715 秒,其中真实浏览器用例(含当时漏网的 90 条)占了大头;单条
  用例中位数 7 毫秒、P90 也只有 59 毫秒 —— **规模不是问题,重尾才是**:最慢 30 条
  占总耗时 71%,最慢一条(`test_subs_layout` 那条摆满订阅工作区的探针)一条就 99 秒。
- 分层后 fast 实测 3362 条真跑 175 秒、147 条浏览器回归被拦;probes 另跑(约 10
  分钟);full 照旧(约 12 分钟)。
- 所以不砍测试,只把「每次提交都要等的」和「异步兜底的」分开:提交前跑 fast;
  改了 `kb/static/`、`kb/web/pages.py` 这类前端产物再跑 probes;push 前跑一次 full
  兜「只在同跑时红」的那一类。

## 三层的分工

| 层 | 跑什么 | 什么时候跑 |
|---|---|---|
| `fast`(缺省) | discover 全部,浏览器回归在共享闸门处显式拦下(同文件纯逻辑用例照常跑);有标记用例真跑了按失败算 | 每次提交前 |
| `probes` | 只跑引用 `tests.headless` 的文件;浏览器用例没验全按失败算,纯逻辑邻居的通过不作数 | 改了前端产物之后 |
| `full` | 等同 README 那条 discover,一条不拦(缺工具自然 skip,覆盖数如实打印) | push 前至少一次 |

## 回归清单

`tests/test_tiers.py`:闸门拦下且消息写清出路、不设变量不多拦(工具打桩成在,必须
放行)、空白串算没设、缺工具也要带 kb_browser 标记、requires_shell 不强加 node、
层表态(fast 设 / probes/full 清继承值)、覆盖账(skip 不算跑 / 真跑的要记上 /
类级、模块级、子例 skip 都不算跑)、层判定(probes 没验全必败 / 空集合与只有逻辑
邻居必败 / probes 全跑了放行 / full 沿用自然 skip 且夹具炸了覆盖数如实 / fast 真跑
即败 / 执行条数不出负数)、结构守卫(不许自写浏览器可用性门 + 共享闸门确有人用)、
probes 名单(重探针与两个曾漏网的文件在册 / 纯逻辑不入册 / 名单名字都真有其文件)。

## 反向验证(定点注入)

- 把共享闸门里的快速层条件注成恒假 → 红 1 条(拦下那条)。
- 把 `_PROBE_RE` 换成匹配不到的正则 → 红 1 条(在册那条)。
- 把某个浏览器类换回自家 `skipUnless(SHELL…)` → 结构守卫红 1 条。
- 把 `browser_coverage` 换回「计划减 skip」的反推 → 红 5 条(类/模块/子例三层各一,
  加 main 链路上类级 skip 的账、夹具异常的账)。
- 删掉 probes 的空集合拒绝 → 红 2 条(空 suite、只有逻辑邻居)。
- 把执行条数换回 `testsRun - len(skipped)` → 红 1 条(类级 skip 打出负数那条)。
- 把 `_apply_tier_env` 的清除注掉 → 红 1 条(层表态那条)。
- 真实 CLI:缺壳跑 `probes`(浏览器用例 145 条 skip、邻居照常通过)→ 退出码 1,
  汇总写明「真跑 2/147」,skip 分类里带安装提示;进程组 SIGTERM/SIGINT 杀掉
  `./kb.sh test` → 临时数据目录零残留。
