# YouTube 字幕直接翻译入库

## 现行边界

2026-11-03 用户确认：daily-report 的摘要/合稿是该项目的特化需求，knowledge-base 不需要，直接翻译即可。

- 发现、真实日期核实、候选排序、视频数量/时长预算和字幕完整性认证不变。
- 外语字幕使用现有 `translate` 模型逐段完整翻译，校验覆盖与输出语言；不再分段摘要、归并、生成概述或改写正文。
- 中文字幕原文直接保存；标题和字幕都为中文时不调用模型，混合语言正文仍按现有语言判据决定是否翻译。
- 外语标题独立尝试翻译并缓存合格译名，输出无效时保留原题，不拖累合格正文；不生成概述或改写成稿。
- 标题和字幕翻译使用全进程共享、最多 3 席的线程池；重复字幕只调一次，按字幕原序组装结果。
  标题与同文字幕仍是不同作业，标题回退不放宽字幕覆盖或语言校验，外语原题保留在正文。
- 作业只写私有内存，协调线程合并增量缓存，保留已接收成功段与失败段内已校验的单位译文。
- 取消、超时或服务失败会停止派发并取消剩余作业，迟到回复不得写缓存或成为成品；模型 429 沿用共享熔断。
- YouTube 读取和普通同步逐视频调度仍串行，不改变数量/时长预留和正式发布事务；本轮不引入跨视频流水线。
- 正式正文为字幕译文与原文对照，保留字幕来源和未分析画面的说明；不冒充大模型核验过的视频要点。
- 旧字幕与有效覆盖式译文缓存继续复用；历史摘要、合稿缓存及相应失败计数不再参与新流程。
- 原有翻译失败的有限重试、显式标记的降级原文/部分译文、安全中断和服务故障分类不变。
- 正式发布继续经过 `products.certify`/`products.valid`，不删除或重写已入库的历史笔记。
- `youtube_summary` 的旧模型设置及历史输出校验接口保留兼容，但新处理不读取、不调用该模型。

## 耗时与预期

生产 feed_runs 219 中的两个视频（`KlG0VyhIzBU`、`l1L9sezAcxc`）分别有 9 段、5 段字幕正文。
旧冷处理对应 9+9+1 与 5+5+1 个翻译/摘要/合稿环节，合计 30 个；直接翻译正文需 14 个环节，
加上两个外语标题，当前冷处理通常共 16 个翻译环节。最多 3 个翻译作业可同时等待模型。
这是模型调用环节的减少，不代表已实测整轮耗时减半；字幕请求、网络与模型响应仍有变化。
已有合格译文缓存命中时无需任何模型请求。未修改列表发现与普通同步的时间窗口、排名及预算配置。

## 首轮直接翻译验证（87cc380 历史记录）

本轮仅登记修改/新增的四个测试模块，不执行其他回归：

```sh
PYTHONPATH="$PWD" /Users/fjh/code/knowledge-base/.venv/bin/python -m unittest tests.test_youtube_notes tests.test_youtube_output_fallback tests.test_youtube_stage_cache tests.test_youtube_translation_only -v
```

工作目录：`/Users/fjh/code/knowledge-base/.worktrees/BnEuTjdfU3Be`。
首轮登记哨兵：`v0Ys-nsld1yw`；68 项，1.114 秒，4 个失败、1 个错误，退出码 1。
失败来自旧测试仍要求模型改写标题、把 1000 字当作历史摘要硬上限，以及新测试遗漏导出步骤、
忽略内容寻址缓存对重复字幕块的去重。已按现行契约调整夹具，保留原题、专有名词/引用保真、
完整译文与正式发布认证断言；不为测试通过恢复摘要或削弱翻译安全校验。
修正后同命令重新登记哨兵 `XcOYB_Px4LKN`：68 项，1.375 秒，全部通过，退出码 0。
原始日志：`/Users/fjh/code/harness/data/runs/BnEuTjdfU3Be/regression/XcOYB_Px4LKN.log`。
本轮五个改动 Python 文件的 Ruff、AST/700 行上限与 `git diff --check` 均通过；未执行其他回归。

生产缓存冒烟只读取上述两个视频的真实字幕、候选身份和缓存，所有正式发布与导出写入隔离目录；
模型调用被显式禁止，用于核实旧译文可直接生成新成品，不冒充冷处理或生产全轮性能验证。
实际结果：两条视频的 9 段与 5 段旧译文均成功复用，模型请求 0 次；隔离正式发布 2 篇、导出 2 篇，
两篇均通过成品认证，导出文件存在且导出哈希与正式内容一致。各条翻译/认证/发布区间分别为 0.060 秒与 0.044 秒，
只是缓存命中时间，不能用于推断首次翻译的耗时。

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$PWD" /Users/fjh/code/knowledge-base/.venv/bin/python data/diagnostics/youtube-translation-only/cached_smoke.py
```

输出：`data/diagnostics/youtube-translation-only/cached-smoke-result.json`；退出码 0。
临时数据库、缓存副本和 vault 全在当前任务 `data/diagnostics/youtube-translation-only/sandbox-w31m3thr`；
生产数据库仅以 `mode=ro` 打开，未写生产数据或调用上游模型。
无界面改动，不操作浏览器，无浏览器通道降级。

## 首轮审查修复与有限并发（ad37c63 历史记录）

已完整读取第 1 轮报告 `free-review/lQKnG-fTEKdC/round-1/report.md`。
标题不翻译的问题成立：此前保留英文原题会因正文已经是中文而永久绕开通用翻译队列。
当前在 YouTube 处理边界翻译标题，并保留原题，避免修改通用翻译或其它来源。
标题使用同一个覆盖校验和缓存签名；旧正文缓存不失效，但若缺标题译文，仍需补一次标题请求。
降级出口优先使用已校验的标题缓存，没有可用译名时仍明确标记待校验，不能冒充完成的中文成品。
同时修正正常正文中“未经逐段完整性核验”的误导说法，区分自动覆盖/语言校验与人工事实核查。

并发只针对翻译作业，不改变排名、视频预算或 YouTube 抓取并发。
线程池全进程共用，多个构建调用也共用 3 席；每个构建只滚动提交最多 3 个未接收任务，
不一次排队整份长字幕。模型请求子进程的停止/超时保护继续生效。
缓存只合并作业实际修改的键，不用工作线程持有的旧快照覆盖别的成功结果。
视频内所有必要段和标题通过校验后才可返回成品；先前成功的缓存保留，不能因并发先后改变正文顺序。

本轮只登记修改/新增的五个测试模块，不重跑未修改模块，
不把上轮 68 项通过结果当作本轮并发验证。原始测试命令：

```sh
PYTHONPATH="$PWD" /Users/fjh/code/knowledge-base/.venv/bin/python -m unittest tests.test_youtube_notes tests.test_youtube_output_fallback tests.test_youtube_stage_cache tests.test_youtube_translation_only tests.test_youtube_translation_parallel -v
```

首轮哨兵 `u1PkSGj02Bg8`：80 项，4.503 秒，1 个失败，退出码 1。
新增并发/标题用例全部通过；失败是原有残缺译文用例断言恰好 2 次请求，未计并发时已在途的其它块。
保留“不缓存、不发布残缺译文、不调成稿”的断言，将请求数限定为实际作业数及 3 席额度内的有限尝试。
同时将乱序用例的同步信号放在完成记录之后，避免测试线程自身的信号先后竞争。

修正后哨兵 `7O53pXEyjh7R`：80 项，4.700 秒，全部通过，退出码 0。
工作目录仍为 `/Users/fjh/code/knowledge-base/.worktrees/BnEuTjdfU3Be`；
原始日志 `/Users/fjh/code/harness/data/runs/BnEuTjdfU3Be/regression/7O53pXEyjh7R.log`。
本次未过滤的命令在运行测试前先用 `shasum -a 256` 输出两个实现模块与五个测试模块的哈希，
日志前七行即受测内容清单；提交前逐一核对一致，避免仅凭可变工作区的 tree 推断提交代码已受测。
受测代码在通过后未修改，仅补充本文的验证记录；七个文件的 Ruff、AST/700 行上限和 diff 检查通过。

### 标题入库与导出冒烟

使用生产两个视频的真实字幕与已合格的正文译文缓存，所有写入隔离库和 vault。
标题输出采用生产已有中文标题作为**明确的本地模型夹具**，不是调用上游重新翻译或冷模型性能实测。
实际执行新 `notes.build` → `products.certify/valid/publish` → `translate.display` → `sync.export`。
两篇成品标题均为中文，正文保留英文原题，落库语言为 `zh`，两篇均正式发布并导出，导出哈希一致。
每篇仅缺标题，因此各调用 1 次本地标题夹具；再次构建两篇均命中完整缓存，0 次请求。
上游模型请求 0 次、生产写入 0 次。该冒烟不证明真实模型延迟或整轮加速比例。

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$PWD" /Users/fjh/code/knowledge-base/.venv/bin/python data/diagnostics/youtube-translation-only/parallel_title_smoke.py
```

退出码 0；结果 `data/diagnostics/youtube-translation-only/parallel-title-smoke-result.json`；
沙箱 `data/diagnostics/youtube-translation-only/parallel-title-6c6ls8xv`。
已断言工作树导入和所有数据/vault 路径隔离，生产数据库以 `mode=ro` 和 `query_only` 打开。
没有界面改动、浏览器操作或通道降级。

## 第二轮审查：标题输出与字幕失败隔离（2026-11-03）

完整读取第 2 轮报告 `free-review/lQKnG-fTEKdC/round-2/report.md`，接受唯一阻断意见。
根因是把可能只含产品名的标题当作必须出现中文的字幕段，并把标题输出失败计入整条视频预算。
本轮在标题层统一修复，不添加品牌白名单，也不放宽字幕段的覆盖、语言或成品认证：

- 标题只请求一次；输出原名、非中文、格式不完整或模型明确的 `OutputError` 时仅回退原题，
  正文照常成品入库，不增加字幕失败次数，不把坏标题输出缓存成合格字幕。
- 新标题使用独立 `translate-title-v1` 作业/缓存身份，标题与同文字幕也不会共用可选回退路径。
  仍兼容读取旧签名下已通过严格校验的标题译文；普通出口和正文降级出口共用标题缓存读取规则。
- 完整正文成功时按原契约清掉当前失败计数；真实字幕失败仍保留原有三次预算与明确降级。
  不整体清空历史计数或译文缓存，不重写已发布笔记。
- 服务未交付、本地存储故障、429 熔断、用户停止和超时不伪装成标题输出失败，仍按原分类中断。
  全局 3 席额度、普通逐视频调度、列表发现、排名和视频处理预算保持不变。

### 本轮验证

仅修改/新增 `test_youtube_translation_only` 与 `test_youtube_title_fallback` 两个模块；
其余回归留给验收，不复用历史 80 项通过记录作为本轮验证。
首轮哨兵 `eYE9HmS5XyXh` 实跑 24 项、0.174 秒、退出码 1，4 个中文字幕子用例报错。
错误均发生在测试的缓存断言：未创建缓存文件时返回 `None`，不能做字典成员判断。
改为明确断言中文字幕路径不创建空缓存文件，外语字幕路径不缓存坏标题；生产代码未改。
修正后哨兵 `cfTMMATdrr_V`：24 项，0.164 秒，全部通过，退出码 0。
实际工作目录 `/Users/fjh/code/knowledge-base/.worktrees/BnEuTjdfU3Be`；原始完整日志
`/Users/fjh/code/harness/data/runs/BnEuTjdfU3Be/regression/cfTMMATdrr_V.log`。
两次登记均执行以下未过滤的命令，前四行记录实现、共享池及两个测试模块的 SHA256：

```sh
shasum -a 256 kb/youtube/notes.py kb/youtube/translation_pool.py tests/test_youtube_translation_only.py tests/test_youtube_title_fallback.py && PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$PWD" /Users/fjh/code/knowledge-base/.venv/bin/python -m unittest tests.test_youtube_translation_only tests.test_youtube_title_fallback -v
```

测试通过后只补本文记录；四个 Python 文件的哈希与原始日志逐项一致。
本轮三个修改/新增 Python 文件的 Ruff、AST/700 行检查与 `git diff --check` 均通过。
未运行其它回归；collector 全流程与通用翻译/导出的回归覆盖仍留给验收前置命令。

另执行真实数据层隔离冒烟，输入及模型均为明确的本地夹具：四种产品名标题各配英文、中文完整字幕，
调用实际 `notes.build` → `products.certify/valid/publish` → `translate.display` → `sync.export`。
8 篇均首轮成品发布并在开启 `hold_untranslated` 时成功导出，原题保留，字幕失败计数为 0，
导出哈希与正式内容一致。12 次本地夹具请求，上游模型调用 0 次；未打开或写入生产数据库。
这证明标题回退不会阻断正式交付，不证明上游模型翻译质量或性能。

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$PWD" /Users/fjh/code/knowledge-base/.venv/bin/python data/diagnostics/youtube-translation-only/title_fallback_smoke.py
```

冒烟退出码 0，日志及结果分别为 `data/diagnostics/youtube-translation-only/title-fallback-smoke.log`、
`data/diagnostics/youtube-translation-only/title-fallback-smoke-result.json`，
沙箱 `data/diagnostics/youtube-translation-only/title-fallback-kotjdkud`。
未改前端或其它来源；没有浏览器操作、通道降级、真实网络请求或生产写入。
