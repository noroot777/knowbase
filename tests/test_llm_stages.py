# -*- coding: utf-8 -*-
"""各阶段模型分配(kb.llm_stages)的回归测试。

不打网络、不碰真 key:把 secrets 文件指到 tmp、把环境变量清干净,只验解析规则。

钉住的几条:
1. **没配 = 行为不变**。这套设置加进来之前,提取/gate/拆条目/wiki 的模型是各
   文件里的字面量;注册表里的 default 必须解析成同样的 id,不然升级一次静默换模型。
2. **provider 是存下来的,不是猜的**。同一个 id 在两家中转站都可能有,面板选
   Anthropic 就得走 Anthropic,不能被「gpt- 开头归 openai」这种前缀规则改判。
3. **认不出家的 id 不能引发死循环**。旧配置里留一个谁也不认识的 default_model,
   resolve 会去问默认模型,而默认模型自己也在走这段解析 —— 用 llm.resolve 写就
   是无限递归(RecursionError),所以逐条候选只能用 parse_model。
4. agent 阶段限 Anthropic,存别家要报错而不是存进去等运行时炸。

跑:  python -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from kb import llm, llm_stages  # noqa: E402

# 这些环境变量会压过文件配置,测试期间一律清空
_ENV_VARS = ["ANTHROPIC_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_BASE_URL",
             "OPENAI_BASE_URL"] + [s.env for s in llm_stages.STAGES if s.env]

# 两家都配一个同名 id:provider 猜不出来,只能靠存下来的那个字段
_BOTH = "shared-model-x"


class _Sandbox(unittest.TestCase):
    """把 secrets 文件指到 tmp、环境变量清干净的公共夹具(本身不含用例)。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self._orig_path = llm._SECRETS_PATH
        llm._SECRETS_PATH = Path(self.tmp.name) / "secrets.json"
        self.addCleanup(lambda: setattr(llm, "_SECRETS_PATH", self._orig_path))
        for k in _ENV_VARS:
            if k in os.environ:
                v = os.environ.pop(k)
                self.addCleanup(os.environ.__setitem__, k, v)
        self.write({
            "anthropic": {"key": "sk-a", "models": ["claude-sonnet-4-5",
                                                    "claude-haiku-4-5", _BOTH]},
            "openai": {"key": "sk-o", "models": ["gpt-5.5", "gpt-5.6-luna", _BOTH]},
        })

    def write(self, data: dict) -> None:
        llm._SECRETS_PATH.write_text(json.dumps(data), encoding="utf-8")
        llm._clients.clear()

    def read(self) -> dict:
        return json.loads(llm._SECRETS_PATH.read_text(encoding="utf-8"))


class StageTests(_Sandbox):

    # 1. 没配 = 沿用各阶段原来写死的那个 id
    def test_builtin_defaults_match_old_literals(self):
        was = {"extract": ("openai", "gpt-5.5"), "gate": ("openai", "gpt-5.5"),
               "transcribe": ("openai", "gpt-5.6-luna"),
               "wiki": ("openai", "gpt-5.6-luna"),
               "skill_tag": ("openai", "gpt-5.6-luna")}
        for key, (prov, mid) in was.items():
            with self.subTest(stage=key):
                m = llm_stages.stage_model(key)
                self.assertEqual((m.provider, m.model_id), (prov, mid))

    # 2. 存下来的 provider 说了算,不受 id 前缀规则影响
    def test_stored_provider_wins_over_prefix_guess(self):
        llm_stages.set_stage("extract", "anthropic", "gpt-5.5")   # 故意反着来
        m = llm_stages.stage_model("extract")
        self.assertEqual((m.provider, m.model_id), ("anthropic", "gpt-5.5"))

    def test_same_id_two_providers(self):
        for prov in ("anthropic", "openai"):
            with self.subTest(provider=prov):
                llm_stages.set_stage("wiki", prov, _BOTH)
                self.assertEqual(llm_stages.stage_model("wiki").provider, prov)

    def test_prefix_form_is_accepted(self):
        llm_stages.set_stage("wiki", "", f"anthropic:{_BOTH}")
        m = llm_stages.stage_model("wiki")
        self.assertEqual((m.provider, m.model_id), ("anthropic", _BOTH))

    # 3. 认不出家的旧配置不能把解析拖进无限递归
    def test_unknown_legacy_model_does_not_recurse(self):
        self.write({**self.read(), "default_model": "某个谁也不认识的-id"})
        # 三个 getter 回的是带家的 "provider:model_id",比 id 时统一过 resolve
        self.assertEqual(llm.resolve(llm.get_default_model()).model_id,
                         "claude-sonnet-4-5")   # 退到自动挑选

    # 面板选了哪家,真正发请求的那一步就得是哪家。三个兼容 getter 一度只回裸 id,
    # 裸 id 回到 resolve() 只能按前缀猜,而 _provider_of 按遍历顺序先命中 anthropic:
    # 面板明明选的 openai,问答/收藏却拿 Anthropic 的 key 去打(2026-09-14 审查)
    def test_getters_keep_provider(self):
        llm_stages.set_stage("ask", "openai", _BOTH)
        llm_stages.set_stage("fav", "openai", _BOTH)
        for got in (llm.get_default_model(), llm.get_fav_model()):
            with self.subTest(value=got):
                self.assertEqual(llm.resolve(got).provider, "openai")
        llm_stages.set_stage("plan", "anthropic", _BOTH)
        self.assertEqual(llm.resolve(llm.get_plan_model(_BOTH)).provider, "anthropic")

    # 一路到 chat() 的路由:面板选 openai,真正发请求的就得是 OpenAI 那条分支。
    # 上面几条验的是解析,这条验的是「解析结果真的决定了用哪个 SDK/哪把 key」
    def test_chat_routes_to_chosen_provider(self):
        import unittest.mock as mock
        llm_stages.set_stage("fav", "openai", _BOTH)
        with mock.patch.object(llm, "_chat_openai", return_value="ok") as oa, \
                mock.patch.object(llm, "_chat_anthropic", return_value="ok") as an:
            llm.chat("hi", llm.get_fav_model())
            self.assertEqual((oa.call_count, an.call_count), (1, 0))
            self.assertEqual(oa.call_args[0][0], _BOTH)   # 传给 SDK 的是裸 id
        llm_stages.set_stage("fav", "anthropic", _BOTH)
        with mock.patch.object(llm, "_chat_openai", return_value="ok") as oa, \
                mock.patch.object(llm, "_chat_anthropic", return_value="ok") as an:
            llm.chat("hi", llm.get_fav_model())
            self.assertEqual((oa.call_count, an.call_count), (0, 1))

    # 阶段模型不一定在「API Key」面板勾选的候选里(API/命令行写的、老配置迁移的、
    # 或者选完之后又被取消勾选)。下拉里必须有它:没有任何 option 匹配当前默认时,
    # 浏览器会自动选中第一个候选,而问答每次发送都带上下拉的值——面板里配好的
    # 模型就被静默顶掉了(2026-09-14 第 2 轮审查发现)
    def test_available_models_includes_unlisted_stage_model(self):
        llm_stages.set_stage("ask", "openai", "custom-o")
        rows = llm.available_models()
        keys = [r["key"] for r in rows]
        self.assertIn("openai:custom-o", keys)
        self.assertEqual([r["key"] for r in rows if r["default"]], ["openai:custom-o"])
        self.assertEqual(len(keys), len(set(keys)))

    def test_pool_covers_every_stage(self):
        llm_stages.set_stage("extract", "anthropic", "custom-a")
        pool = llm.model_pool()
        self.assertIn("custom-a", pool["anthropic"])
        # 没配的阶段也算:内置默认那个 id 同样可能不在勾选列表里
        self.write({"anthropic": {"key": "sk-a", "models": ["claude-sonnet-4-5"]},
                    "openai": {"key": "sk-o", "models": ["gpt-5.6-luna"]}})
        self.assertIn("gpt-5.5", llm.model_pool()["openai"])   # extract/gate 的内置默认

    # 下拉的 value 必须全局唯一,且只有一个是默认:两家同名 id 时,裸 id 当 value
    # 会撞成两个一模一样的选项,"哪个是默认"还会两边同时为真
    def test_available_models_keys_unique_and_single_default(self):
        llm_stages.set_stage("ask", "openai", _BOTH)
        rows = llm.available_models()
        keys = [r["key"] for r in rows]
        self.assertEqual(len(keys), len(set(keys)))
        self.assertEqual([r["key"] for r in rows if r["default"]], [f"openai:{_BOTH}"])
        both = [r["label"] for r in rows if r["model"] == _BOTH]
        self.assertTrue(all("(" in lb for lb in both), both)   # 同名时标出供应商

    # 下拉传回来的 value 原样存回阶段配置,家不能在这一圈里丢掉
    def test_roundtrip_dropdown_value(self):
        llm.set_fav_model(f"openai:{_BOTH}")
        self.assertEqual(llm_stages.configured("fav"),
                         {"provider": "openai", "model": _BOTH})
        self.assertEqual(llm.resolve(llm.get_fav_model()).provider, "openai")

    # 命令行手敲的裸 id、老会话里存的裸 id 仍要能用(猜家是兜底,不是禁止)
    def test_bare_id_still_resolves(self):
        m = llm.resolve("gpt-5.5")
        self.assertEqual((m.provider, m.model_id), ("openai", "gpt-5.5"))

    # 4. 限定供应商的阶段拒绝别家
    def test_agent_stage_rejects_other_provider(self):
        with self.assertRaises(ValueError):
            llm_stages.set_stage("agent", "openai", "gpt-5.5")
        self.assertIsNone(llm_stages.configured("agent"))

    def test_agent_none_without_anthropic_key(self):
        self.write({"openai": {"key": "sk-o", "models": ["gpt-5.5"]}})
        self.assertIsNone(llm_stages.resolve_stage("agent"))
        self.assertIsNone(llm.anthropic_model_for_tools())

    # 清除 = 回落内置默认,并且不留半条空记录
    def test_clear_falls_back(self):
        llm_stages.set_stage("transcribe", "anthropic", "claude-haiku-4-5")
        self.assertEqual(llm_stages.stage_model("transcribe").model_id, "claude-haiku-4-5")
        llm_stages.set_stage("transcribe", "", "")
        self.assertEqual(llm_stages.stage_model("transcribe").model_id, "gpt-5.6-luna")
        self.assertNotIn("transcribe", self.read().get("stages", {}))

    # 旧顶层键(面板出现之前写的)仍然读得到;一旦在面板里改过就该被清掉,
    # 不然两处配置并存,用户改了新的、读的还是旧的(优先级还看不见)
    def test_legacy_key_read_then_cleared(self):
        self.write({**self.read(), "fav_model": "claude-haiku-4-5"})
        self.assertEqual(llm.resolve(llm.get_fav_model()).model_id, "claude-haiku-4-5")
        llm_stages.set_stage("fav", "openai", "gpt-5.5")
        self.assertEqual(llm.get_fav_model(), "openai:gpt-5.5")
        self.assertNotIn("fav_model", self.read())

    # 环境变量压过内置默认,但压不过面板里显式配的那个
    def test_env_overrides_default_but_not_config(self):
        os.environ["KB_EXTRACT_MODEL"] = "claude-haiku-4-5"
        self.addCleanup(os.environ.pop, "KB_EXTRACT_MODEL", None)
        self.assertEqual(llm_stages.stage_model("extract").model_id, "claude-haiku-4-5")
        llm_stages.set_stage("extract", "openai", "gpt-5.6-luna")
        self.assertEqual(llm_stages.stage_model("extract").model_id, "gpt-5.6-luna")

    # 规划阶段:先挑便宜的,没有便宜的才用调用方当前所选(原来的语义)
    def test_plan_prefers_cheap_then_caller_model(self):
        self.assertEqual(llm.resolve(llm.get_plan_model("claude-sonnet-4-5")).model_id,
                         "claude-haiku-4-5")
        self.write({"anthropic": {"key": "sk-a", "models": ["claude-sonnet-4-5"]}})
        self.assertEqual(llm.resolve(llm.get_plan_model("claude-sonnet-4-5")).model_id,
                         "claude-sonnet-4-5")

    # 面板要能把每个阶段的当前取值和来源讲清楚
    def test_describe_covers_all_stages(self):
        llm_stages.set_stage("gate", "anthropic", "claude-haiku-4-5")
        rows = {r["key"]: r for r in llm_stages.describe()}
        self.assertEqual(len(rows), len(llm_stages.STAGES))
        self.assertEqual(rows["gate"]["source"], "config")
        self.assertEqual(rows["gate"]["provider"], "anthropic")
        self.assertTrue(rows["gate"]["ready"])
        self.assertEqual(rows["extract"]["source"], "default")
        self.assertEqual(rows["agent"]["providers"], ["anthropic"])


class ConcurrentWriteTests(_Sandbox):
    """secrets.json 是整文件读改写,并发保存不能互相吃掉。

    面板里连着改两个阶段,两个 POST 完全可能并发;一边存 key 一边改阶段也是。
    各自基于同一份旧内容写回的话,后写的那次把前一次整个盖掉——两个请求都返回
    成功,用户只看到一个生效(2026-09-14 第 3 轮审查发现)。
    下面几条都靠「把读盘拖慢」把竞争窗口撑开:没有锁的话必挂。
    """

    def _slow_read(self, delay=0.02):
        """给 _file_secrets 套一层延时,放大「读完到写回」之间的窗口。"""
        import unittest.mock as mock
        real = llm._file_secrets

        def slow():
            data = real()
            time.sleep(delay)
            return data
        return mock.patch.object(llm, "_file_secrets", side_effect=slow)

    def _run(self, fns):
        errs = []

        def wrap(f):
            try:
                f()
            except Exception as e:          # noqa: BLE001 — 线程里的异常要带回主线程
                errs.append(repr(e))
        threads = [threading.Thread(target=wrap, args=(f,)) for f in fns]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        self.assertEqual(errs, [])

    def test_concurrent_set_stage_all_survive(self):
        keys = ["extract", "gate", "transcribe", "wiki", "skill_tag"]
        with self._slow_read():
            self._run([lambda k=k: llm_stages.set_stage(k, "openai", "gpt-5.5")
                       for k in keys])
        self.assertEqual(sorted(self.read().get("stages", {})), sorted(keys))

    def test_stage_and_key_saves_dont_clobber(self):
        with self._slow_read():
            self._run([
                lambda: llm_stages.set_stage("extract", "openai", "gpt-5.5"),
                lambda: llm.set_config({"openai": {"base_url": "https://relay.example/v1"}}),
            ])
        data = self.read()
        self.assertEqual(data["stages"]["extract"]["model"], "gpt-5.5")
        self.assertEqual(data["openai"]["base_url"], "https://relay.example/v1")
        self.assertEqual(data["openai"]["key"], "sk-o")        # 原有字段没被抹掉
        self.assertIn("claude-sonnet-4-5", data["anthropic"]["models"])

    # 写盘是 tmp + rename:并发读者永远读到完整的一份,不会撞上截断了一半的文件
    def test_write_is_atomic_and_private(self):
        llm_stages.set_stage("wiki", "openai", "gpt-5.5")
        self.assertEqual(llm._SECRETS_PATH.stat().st_mode & 0o777, 0o600)
        leftovers = [p.name for p in llm._SECRETS_PATH.parent.iterdir()
                     if p.name.endswith(".tmp")]
        self.assertEqual(leftovers, [])
        json.loads(llm._SECRETS_PATH.read_text(encoding="utf-8"))   # 始终是完整 JSON


# 子进程里干的活:等主进程发令 → 存一个阶段。读盘被拖慢,把「读完到写回」的
# 窗口撑开到进程调度抖动之上;没有跨进程锁的话,几个子进程都基于同一份旧文件
# 写回,最后只剩一个阶段。
_CHILD = r"""
import os, sys, time
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from kb import llm, llm_stages
llm._SECRETS_PATH = Path(sys.argv[2])
_real = llm._file_secrets
def _slow():
    d = _real()
    time.sleep(0.4)
    return d
llm._file_secrets = _slow
Path(sys.argv[3]).write_text("1")          # 告诉主进程:我已就位(还没进临界区)
while not os.path.exists(sys.argv[4]):     # 等发令,让几个子进程几乎同时开抢
    time.sleep(0.005)
llm_stages.set_stage(sys.argv[5], sys.argv[6], sys.argv[7])
"""


class CrossProcessWriteTests(_Sandbox):
    """**跨进程**并发保存同样不能丢更新(2026-09-14 第 4 轮审查发现)。

    进程内的 RLock 只挡得住一个 kb-web 里的并发。两个 kb-web 实例、或者服务进程
    和一条命令行脚本同时存盘时,各自读到同一份旧 JSON 再整文件写回,后写的照样
    把前一次吃掉,两边都返回成功。所以临界区里另有一把 lockfile 上的 flock。
    """

    def test_separate_processes_dont_clobber(self):
        repo = str(Path(__file__).resolve().parent.parent)
        secrets = str(llm._SECRETS_PATH)
        go = Path(self.tmp.name) / "go"
        jobs = [("extract", "openai", "gpt-5.5"),
                ("gate", "anthropic", "claude-haiku-4-5"),
                ("wiki", "openai", "gpt-5.6-luna")]
        env = {k: v for k, v in os.environ.items() if k not in _ENV_VARS}
        procs, readies = [], []
        for i, (key, prov, model) in enumerate(jobs):
            ready = Path(self.tmp.name) / f"ready-{i}"
            readies.append(ready)
            procs.append(subprocess.Popen(
                [sys.executable, "-c", _CHILD, repo, secrets, str(ready), str(go),
                 key, prov, model],
                env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True))
        self.addCleanup(lambda: [p.kill() for p in procs if p.poll() is None])

        deadline = time.time() + 60
        while not all(r.exists() for r in readies):
            self.assertLess(time.time(), deadline, "子进程没能就位")
            time.sleep(0.02)
        go.write_text("1")
        for p in procs:
            out, err = p.communicate(timeout=60)
            self.assertEqual(p.returncode, 0, err or out)

        stages = self.read().get("stages", {})
        self.assertEqual(sorted(stages), sorted(k for k, _, _ in jobs))
        for key, prov, model in jobs:
            self.assertEqual(stages[key], {"provider": prov, "model": model})
        data = self.read()                       # key / 候选列表也没被顺手抹掉
        self.assertEqual(data["openai"]["key"], "sk-o")
        self.assertIn("claude-sonnet-4-5", data["anthropic"]["models"])


if __name__ == "__main__":
    unittest.main()
