# YouTube 低成本接入验证

## 范围与隔离

- 实现工作区：`/Users/fjh/code/knowledge-base/.worktrees/wnYGhulNzmEH`。
- 频道快照沿用已提交的 52 个频道，默认启用 48 个；验证新库幂等补种、用户删除后不复活和权重保留。
- 测试使用临时数据库、vault、转录与模型替身，没有修改生产库，也没有调用真实媒体下载、ASR 或付费 LLM。
- 浏览器夹具只替换远端发现、媒体获取与模型回复；真实执行排名准入、全文分块翻译编排、笔记构建、数据库提交、导出、索引、API 和 UI。

## 定向回归

哨兵 `NDa6Lm4KsGJA`，退出码 0，35 项通过，无跳过。命令：

```sh
PATH=/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin TMPDIR=/private/tmp KB_DATA_DIR=/private/tmp/kb-youtube-targeted-v5 TZ=Asia/Shanghai HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 /Users/fjh/code/knowledge-base/.venv/bin/python -m unittest discover -s tests -p 'test_youtube*.py' -v
```

日志：`/Users/fjh/code/harness/data/runs/wnYGhulNzmEH/regression/NDa6Lm4KsGJA.log`。

覆盖频道管理和独立种子版本、RSS 与 handle 解析、AI 相关性和双榜排名、数量/累计时长/单条时长门槛、重试退避、旧订阅回包拒收与改址重发现。
字幕链路覆盖缓存、既有原文 SRT、作者字幕、原语言自动字幕、坏字幕回退和音频后备；验证不选择自动翻译轨道、不下载画面、ASR 线程上限与媒体清理。
中文链路覆盖长字幕分块、结尾内容进入模型输入、时间标记完整性、模型/输入缓存身份、失败不发布、完整中文转录导出以及显式日期范围。

## 浏览器实测

首选 Chrome 扩展已命名会话 `🔎 YouTube 中文订阅验证`，但创建后台标签的 `visible:false` 被拒绝：`Capability is not available: visibility`。
这次调用没有创建标签；因无法保证后台操作，降级到独立无头浏览器和临时 profile，没有连接用户 Chrome 的 CDP、激活窗口或操作用户已有标签。

浏览器探针的 14 项断言全部通过，包括：

- 初始 52 个频道出现在订阅管理，热度与价值榜可切换，非 AI 候选显示过滤原因。
- 将每轮数量改成 1 后，服务端保存；新增频道的权重 1.5 持久化。
- 通过真实同步弹窗只运行抓取、导出和索引，准入一个视频，另外两个因预算保留，非 AI 视频不处理。
- 阅读区显示中文标题、笔记、完整中文转录、画面未分析提示，以及跳转原视频的时间戳；临时 vault 中恰好导出一篇中文 Markdown。
- 切换 RSS 后清除视频排名，恢复通用输入框；390 像素窄屏没有页面横向溢出，也没有浏览器脚本异常。

已人工查看以下截图，桌面排名、中文正文和窄屏内容未见遮挡或横向溢出。截图与断言 JSON 是工作区内忽略的运行产物：

- `output/youtube/subscriptions-desktop.png`
- `output/youtube/chinese-reader-desktop.png`
- `output/youtube/subscriptions-mobile.png`
- `output/youtube/checks.json`

## 全仓快速回归与基线对照

哨兵 `7jS2LIbf0rsL` 运行快速层，退出码 1：4125 项，2 个失败、4 个错误、242 个跳过；快速层按设计不运行 151 项浏览器用例，本功能的真实浏览器验证已在定向回归单独执行。命令：

```sh
PATH=/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin TMPDIR=/private/tmp KB_DATA_DIR=/private/tmp/kb-youtube-fast-v4 TZ=Asia/Shanghai HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 /Users/fjh/code/knowledge-base/.venv/bin/python -m tests.tiers fast
```

日志：`/Users/fjh/code/harness/data/runs/wnYGhulNzmEH/regression/7jS2LIbf0rsL.log`。

在本轮实现前的 `3bbc0c5` 归档目录 `/private/tmp/kb-yt-baseline-sg3RHH`，用相同环境运行这五个失败相关模块，哨兵 `FR0uaV4hHl27` 实际执行 67 项，同样得到 2 个失败、4 个错误：

```sh
PATH=/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin TMPDIR=/private/tmp KB_DATA_DIR=/private/tmp/kb-youtube-baseline-v4 TZ=Asia/Shanghai HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 /Users/fjh/code/knowledge-base/.venv/bin/python -m unittest tests.test_filelock tests.test_wechat_setup tests.test_fulltext_held_exit tests.test_kb_grab_cmd tests.test_kb_web_start -v
```

日志：`/Users/fjh/code/harness/data/runs/wnYGhulNzmEH/regression/FR0uaV4hHl27.log`。残余失败与基线逐项一致：

- `test_fulltext_held_exit` 的两个 shell 入口用例在其隔离命令环境中找不到 `uv`。
- `test_filelock` 负文件描述符返回 `ValueError`，而用例预期其他异常。
- `test_wechat_setup` 在 macOS 上模拟 Windows 路径时缺少 `subprocess.CREATE_NO_WINDOW`，产生三个错误结果。

未修改这些与本任务无关的实现，不把全仓快速层报告为通过。归档目录不含 `.git`，哨兵输入诊断会提示不是 Git 仓库，但完整日志证明上述测试实际执行；本记录不是可自动复用的独立验收证据。

## 静态检查和未验证边界

修改与新增代码的 Python 编译、Node 语法、`git diff --check` 和单文件不超过 700 行检查通过。
尚未验证真实 YouTube 当前可访问性、字幕限流、本机模型的识别质量、真实模型翻译质量与真实长视频耗时。工具或模型缺失会明确报错，不伪造成功。
时间戳和尾部覆盖检查只能拒绝明显坏字幕，不能证明语义无损；系统不读取画面，画面专属信息仍会缺失。
