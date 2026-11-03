# YouTube 字幕直接翻译入库

## 现行边界

2026-11-03 用户确认：daily-report 的摘要/合稿是该项目的特化需求，knowledge-base 不需要，直接翻译即可。

- 发现、真实日期核实、候选排序、视频数量/时长预算和字幕完整性认证不变。
- 外语字幕使用现有 `translate` 模型逐段完整翻译，校验覆盖与输出语言；不再分段摘要、归并、生成概述或改写正文。
- 中文字幕原文直接保存，不调用模型；混合语言正文仍按现有语言判据决定是否翻译。
- 标题保留视频原标题，不为改写中文标题增加一次模型请求。
- 正式正文为字幕译文与原文对照，保留字幕来源和未分析画面的说明；不冒充大模型核验过的视频要点。
- 旧字幕与有效覆盖式译文缓存继续复用；历史摘要、合稿缓存及相应失败计数不再参与新流程。
- 原有翻译失败的有限重试、显式标记的降级原文/部分译文、安全中断和服务故障分类不变。
- 正式发布继续经过 `products.certify`/`products.valid`，不删除或重写已入库的历史笔记。
- `youtube_summary` 的旧模型设置及历史输出校验接口保留兼容，但新处理不读取、不调用该模型。

## 耗时与预期

生产 feed_runs 219 中的两个视频（`KlG0VyhIzBU`、`l1L9sezAcxc`）分别有 9 段、5 段字幕正文。
旧冷处理对应 9+9+1 与 5+5+1 个翻译/摘要/合稿环节，合计 30 个；直接翻译只需 14 个翻译环节。
这是模型调用环节的减少，不代表已实测整轮耗时减半；字幕请求、网络与模型响应仍有变化。
已有合格译文缓存命中时无需任何模型请求。未修改列表发现与普通同步的时间窗口、排名及预算配置。

## 验证记录

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
