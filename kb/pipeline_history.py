# -*- coding: utf-8 -*-
"""给人翻的那一份:历史记录的落盘、读取与「谁顶谁」。

**硬约束在 `docs/decisions/pipeline-history.md` 第一节 —— 改这块之前先读那一份。**

**从 `kb/pipeline_state.py` 拆出来只因为行数**(那边贴着 700 行上限),整段逐符号
搬过来、一个判据都没改。这一层没有自己的可变状态:要什么都在调用那一刻问
`pipeline_state`(`_STATE_FILE` 的位置、内存里那份终态),所以**依赖方向是
`pipeline_state` → 本模块,一路单向** —— 那边在模块级 import 本模块并把这几个名字
再导出一次(`ps.load_history` / `ps._save_history` / `ps._history_file` 这几个名字
几十处在用,包括测试的打桩点),所以本模块**只许在函数体里**反过来取它。

两份落盘的分工写在 `pipeline_state` 里 `_STATE_FILE` 那段注释上:状态文件喂
「重试失败项」的范围派生,这一份只给人翻。
"""
from __future__ import annotations

from kb.pipeline_persist import (
    _dump_bytes,
    _history_runs,
    _history_shape_ok,
    _load_json,
    _load_json_strict,
    _write_atomic,
    run_record,
)
from kb.pipeline_retention import prune_history, shed_bulk


def _history_file():
    """历史文件就在 `_STATE_FILE` 旁边。

    **从它派生,不另立一个模块变量**:测试隔离落盘状态时 patch 的是 `_STATE_FILE`
    (`tests/test_pipeline_stop.py` 那一套),另立一个的话每个走到 `_end_run()` 的
    测试都得记着再 patch 一次 —— 漏掉的那个会去写**生产**那份历史,而漏没漏在
    测试输出上一个字都看不见(同 `maintenance.DATA_DIR` 那条)。派生之后隔离点
    只有一个,不可能漏。
    """
    from kb import pipeline_state as ps            # 延迟:方向单向,见模块头
    return ps._STATE_FILE.with_name(
        f"{ps._STATE_FILE.stem}_runs{ps._STATE_FILE.suffix}")


def _history_rounds_strict() -> list[dict]:
    """历史记录的**严格版**:读不出来、或者形状不对,一律抛。

    「可解析但形状不对」和「读不出来」是同一档 —— 都不能当成「这份副本没有」。
    展示那条路照旧走 `load_history()`(宽口径,少显示几行、不出声)。
    """
    raw = _load_json_strict(_history_file())
    if not _history_shape_ok(raw):
        raise ValueError(f'历史文件形状不对：{type(raw).__name__}')
    return _history_runs(raw)


def _run_record() -> dict:
    """要留进历史的那一份终态。**批次里取的是整批那份汇总,不是当前子轮。**

    **两份落盘必须是同一个业务结果的两个投影** —— 这条不变量的事故形态写在
    `_save_history` 里。字段取舍在 `pipeline_persist.run_record`。
    """
    # 延迟 import:批次层在模块级 import 本模块(**方向单向**,理由在
    # `pipeline_batch` 的模块头),反过来用它只能在函数里取。
    from kb import pipeline_state as ps            # 延迟:方向单向,见模块头
    from kb.pipeline_batch import _batch_aggregate
    return run_record(_batch_aggregate() or ps._state)


def _save_history() -> None:
    """把这一轮的终态记进历史。

    **按 run id 幂等**(身份非空时):同一轮重复落盘时替换那一条、不追加 —— 否则一次
    重复调用就在历史里留下两条一模一样的记录,而它们的区别只有写入顺序。

    **原子换文件**:面板那条读历史的请求随时在跑,而这份文件比状态文件大得多
    (带着每轮 80 行日志),截断重写的窗口里读到的是半截 JSON(同
    `docs/decisions/llm-stages.md` 那条)。写失败只是少一条历史记录,不出声 ——
    这一份是只读展示,它写不写得上不该影响这一轮同步的结论。
    """
    # 延迟 import,同上。读的是**同一本账**(都在 `_lock` 里):`_run_record()` 决定
    # "存哪一份",`_aggregate_covers()` 回答"这一份实际合并了谁"。
    from kb.pipeline_batch import _aggregate_covers
    record = _run_record()
    rid = record["run_id"]
    # **空身份一律追加,不去重。** 空串不是身份 —— 拿它当键会把两轮各自跑过的运行
    # 合成一条(没经过 `start_pipeline` 的那条路身份就是空的)。历史里少一轮是静默
    # 丢数据,多留一条只是多一行。
    # **存量必须是「确实读出来了」或者「文件确实不存在」,别的一律抛。** 这一步之后
    # 是整份原子替换,拿一个因为读失败而空着的集合去替换,就是把存量**删掉** ——
    # 原子性只保证新文件完整,不保证旧记录还在。实测形态:库里已经三轮(其中一轮
    # 带着失败原因和日志),第四轮成功、只在这一次读上注入一个 `PermissionError`,
    # 历史当场从 3 条变 1 条、面板上一个字不说(两份都写成功了,诊断是空的)。
    # 抛出去反而正好:`_end_run` 那两份是分别尝试、分别诊断的,这一份写不上只是
    # 少一条历史记录,面板会如实说一句,而**原文件一个字节都没动**。
    # **一个业务结果只占一条历史:整批那一份顶掉这一批前几轮各自留下的那条。**
    # 不顶的后果不是"多几行",而是**两份可恢复副本在同一个 run id 下存着两种含义
    # 的结果**:状态文件是整批(抓取 error),历史最新那条是末个子轮(抓取「未选择」)
    # —— 恢复按完成序号挑中历史那条,于是日报里那条 RSS 抓取失败在重启之后变成
    # 「同步已完成」,连「重试失败项」都没有(实测:只给汇总那一次写入注入
    # `OSError`,历史照常写成功,新进程起来 `last_run.stages.crawl.status=skipped`、
    # `retry_plan=[]`)。正常保存时它也看得见:面板说"失败",点开同一个身份的
    # 那条历史却写着"已完成"。
    # 顶掉**不丢内容**:整批那一份是子轮的并集(阶段按来源合并、日志接起来、失败
    # 名单并起来,见 `pipeline_merge._merge_runs`)。
    # **待删的身份只认「这一份记录实际合并了哪几轮」,不许据「内存里有没有权威结果」
    # 反推** —— 权威位上也可能躺着一轮普通运行自己的快照(落盘失败那一档,见
    # `pipeline_batch._save_own`),它一轮都没合并,按"有缓存就是整批"去删,期间那几轮
    # 各自那条唯一的历史就没了(实测形态在 `docs/decisions/pipeline-history.md`)。
    # 发布被拒那一档同理:写下的是子轮自己那份局部结果。**反过来钉住:真的汇总即使
    # 状态写失败,存进历史的也还是完整汇总**,不许退回局部结果。
    superseded = {rid} if rid else set()
    superseded |= _aggregate_covers()
    runs = [r for r in _history_rounds_strict()
            if not (r.get("run_id") and r["run_id"] in superseded)]
    runs.insert(0, record)
    # **窗口决定留哪几轮,体量只削重物、不删记录**(`shed_bulk`)—— 三天之内的每一轮
    # 都还查得到它的结果和它的红,代价是病态那一档里老轮次的日志正文和明细长清单。
    runs = shed_bulk(prune_history(runs))
    _write_atomic(_history_file(), _dump_bytes({"runs": runs}))


def load_history() -> list[dict]:
    """最近几轮的终态,**最新排在最前**。

    文件还没有、读不出来、或者内容不是预期的形状,一律空列表 —— 这一份是只读
    展示,报错不如少显示几行。
    """
    return _history_runs(_load_json(_history_file()))
