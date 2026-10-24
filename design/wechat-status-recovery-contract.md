# 接入状态恢复与交互契约

## 范围

本任务只承接 U84ahkiKIET9 第 1 轮报告的第 1、3 条：无法解释的状态缺少恢复出口，以及 Reader 状态缺失必要交互字段。没有修改或重新启动原任务 WJ1Uxb6zVWbJ，也没有处理报告其余条目。

## 显式状态恢复

- GET 仍严格读取 UTF-8 JSON；只有文件不存在才是首次检查。损坏 JSON、过深结构、错误字段类型、旧编码及持续读取错误不会被当作空闲或触发获取。
- 状态不可用时，后端单独探测跨进程作业锁。拿不到锁或无法可靠检查锁时，禁止重置；真实占锁显示运行中。历史状态未知仍保留 running=null，通过 can_reset_status 单独表达能否尝试恢复。
- 页面增加「备份并仅重置状态」。它只提交 action=reset_status，不包含获取确认或账号选择，也不放开普通 connect/acquire 的失败关闭保护。
- 后端收到重置请求后重新取独占锁，并在锁内重新读取状态。状态已经恢复正常时直接保留，避免旧页面覆盖刚完成的结果；重复重置是无操作。
- 确认状态无法解释后，先把原文件按原字节复制到同一私有运行目录的 setup-state.json.corrupt-<随机值>.bak，独占创建、收紧文件权限、flush/fsync 成功后，才原子替换为 unchecked。
- 无法备份、无法确认空闲、无法替换状态时均拒绝继续。备份不会自动删除。重置不创建 worker、不准备 Reader、不获取访问材料，不删除材料、恢复锁或修改 Reader 配置；仍需要用户单独重新检查和再次确认获取影响。
- 复用现有 API 的本机、Host、Origin、跨站标志和操作请求头检查；容器不能执行重置。

## 统一交互契约

公共 _write 出口、状态快照和 acquire 请求都通过同一份交互校验；不在 Windows 和 Mac 的枚举转换分支分别补丁。

- account_selection_required 必须有非空列表；每个 ID 必须非空、不超过 API 的 32 字符限制、唯一，并存在于当前后端账号发现结果。显示标签从后端目录生成，而不是信任上游标签或路径。
- 缺列表、只有候选计数、未知或重复 ID、错误字段类型均转换为可重试的 needs_database_location，提示修正到本人单账号 db_storage。页面不再显示无法完成的选择步骤，重新检查入口保持可用。
- authorization_required 必须同时有合法的 URL-safe 令牌、有限且未过期的 15 分钟授权窗口、当前可接受的账号 ID、当前固定 provider revision 和匹配的平台范围。
- 缺失、过期、账号变化或版本/平台不符时，转换为可重新检查的 blocked，并移除令牌等不可用交互字段。合法授权仍必须明确确认影响；畸形或非 ASCII 确认令牌被安全拒绝。
- 已持久化的旧交互状态同样经过读取侧校验，无需先手工编辑状态文件；读取时不会为了修正文案而改写文件。

## 冻结基线与跨任务兼容

本任务从 101618c 冻结提交起步，它不含原任务的未提交 Windows 实现。因此本补丁没有整份移植原任务，也没有改动原任务的终态发布逻辑；仅补齐本次所需的严格读取、UTF-8 原子状态写入和忙锁识别基础。

额外验证使用只读复制的原任务源码快照，在独立证据目录里叠加本次模块和公共入口钩子。build_overlay.py 逐文件记录并核对原任务的 798 个源码/测试文件哈希；没有向原任务写入、提交或重新派发执行。

叠加时保留原任务的 _work、Windows 实现和终态恢复判据，增加以下接点：严格 _read；_write 发布前校验；原 snapshot 作为 _snapshot 后包裹恢复能力判断；start 的 reset_status 分支、状态读取和 acquire 契约检查。Windows 缺少对应实现的旧基线不会凭空放行 Windows 获取授权。

原任务若干旧回归夹具使用短令牌、无限远有效期、虚构 revision、未发现的账号，或只给交互状态名称；这些现在应当拒绝。inherited_contract_tests.py 只在隔离验证进程中补齐有效令牌、有效期、当前 revision 和临时账号映射，不修改产品代码或原任务测试文件，也不改断言。未适配夹具的原始失败日志一并保留，不能宣称它们原样全绿。合并两任务时应同步迁移这些夹具，而不是放宽产品契约。

## 验证与证据

证据目录：D:/ai_workspace/ash/data/runs/LKDdRwqGKHw3/scope-verification。

- 本分支新增定点 Python 回归 23 项；本分支 JS 12 项，原任务叠加 JS 10 项。
- 叠加环境限定回归 114 项，其中 1 项为既有 Mac 可选集成跳过；有效交互夹具下的原任务状态/接入回归 45 项；真实固定 Windows Reader 加密集成 6 项。
- reader_contract.py 校验已缓存 Reader 的固定文件 SHA256 后复制到临时目录，用真实 access-plan 重现只有候选计数、没有 accounts 的响应。真实 worker/API 状态变成可重新检查的目录错误，没有调用 provider 或写 Reader 配置。
- 两套隔离真实页面各验证 10 组：损坏状态恢复、原字节备份、GBK、结构错误、真实跨进程锁竞争、空账号降级、有效账号选择及后端接受、不完整授权拒绝、完整授权仍需确认、390px 恢复。每套均 0 个未捕获脚本异常，且没有 acquire 请求；叠加环境的 8 张截图逐张目检。
- 冻结旧基线仍保留原报告第 2 条的终态快照竞态，曾有页面轮次因此等待选择态超时。最终探针仅在独立旧基线中、且磁盘已是预期完整终态时，明确点击现有「刷新状态」继续验证，并将这个额外步骤写入 legacySnapshots；叠加环境禁止走该兜底。没有把这项旧问题在本任务里顺带修改，也不宣称旧基线在无刷新情况下完整通过。
- 定点反向注入验证：绕过交互校验触发 25 个失败子项，绕过备份触发 7 个失败子项，绕过恢复锁触发 1 个失败。
- Python AST、JS 语法、改动代码 700 行上限及 git diff --check 通过。当前环境没有 ruff，未安装新依赖，也不宣称执行过 ruff。

可直接重跑本分支回归：

    D:/ai_workspace/knowbase/.venv/Scripts/python.exe -m unittest -v tests.test_wechat_setup_status tests.test_wechat_setup_contract
    node --test tests/wechat_agent_test.mjs

证据目录保留 build_overlay.py、reader_contract.py、mutation_run.py、inherited_contract_tests.py、browser_run.py、serve_scope.py、browser_probe.mjs、原始/最终日志和截图，可独立重跑。

独立旧基线扩展回归仍有 Windows 既有兼容失败：旧锁异常类型、POSIX 0600 断言以及 os.killpg。对照冻结提交的日志在 baseline-existing-failures.log；它们没有被当成本次两项修复顺带修改，也没有据此声称整仓回归全绿。一次 Node 运行器缺少 LOCALAPPDATA 的验证失败单独保留，随后使用完整宿主环境复跑。

## 浏览器通道与清场

首选 Chrome 扩展具名后台会话实际返回 Capability is not available: visibility，未去掉后台要求重试，未复用、导航或激活任何用户标签。按规定降级到独立临时 profile 的 chrome-headless-shell，明确 --headless；仅连接自己创建的无头进程，不使用用户登录态，不启动有头浏览器。

页面写操作仅发生在本轮创建的隔离夹具中。数据目录、账号目录、状态、配置和材料占位文件均为合成；未扫描真实客户端、执行真实材料获取或改动用户服务，未重启或停止 8787。

最终两轮服务、浏览器及临时夹具均正常退出和清理，日志包含 SERVICE_STOPPED 与 TEMP_FIXTURE_REMOVED。前一轮曾因强制结束浏览器后的短暂文件占用留下 C:/Users/fanda/AppData/Local/Temp/kb-scope-ui-l1_tjzdh，内含合成 selected.json 和空白浏览器 profile 残余；核对无活跃所属浏览器后，显式删除被执行策略拒绝，未绕过策略。该残余不在工作区、不是用户 profile；后续验证改为正常关闭浏览器并等待退出，未再出现此问题。

未执行真实客户端首次获取，未在 Mac 实机上运行。本轮只承诺上述状态恢复与交互契约，不扩大到 Reader 全版本或真实客户端兼容性。

## 第 1 轮审查修复（2026-10-24）

已完整读取 V72JKyc1Ms0L/round-1/report.md。本轮认可并修复唯一阻塞项「部分损坏状态仍不能恢复」，根因按报告要求落在共享状态解释层，而不是只绕过某一次页面 map 异常。原任务、旧平台文案和冻结基线的终态竞态均未改动。

### 统一可解释性判据

- wechat_setup_status.normalize 同时供持久化读取、公共发布入口和交互契约使用。公共 message/platform/deployment 必须是能编码为 UTF-8 的字符串；running/ready/能力字段必须是布尔值；updated_at 必须是非布尔、非负、有限且能被 JSON 消费的数值。错误公共字段直接进入既有的不可用恢复流程，不会透传给 API 或页面。
- status_unavailable 只允许作为实时计算的错误响应，不作为健康持久化状态。它在读取层就被判为不可解释，因此 GET 提供恢复能力与锁内 reset 是否备份使用同一判据；非字符串 message 也不会再参与拼接。
- 采用按状态投影，而非信任整份旧对象：只有选择态保留候选 ID，随后由后端发现结果生成标签；只有授权态保留必要授权字段，随后执行完整授权校验。普通状态里的 accounts、旧令牌和未知元数据均不对外暴露。无关字段含对象、字符串、null 元素或非有限数也不会再破坏页面和序列化。
- 这是有意区分两类情况：公共字段坏了就先备份、再显式重建；只有无关历史字段时，读取侧安全投影，保留原文件，reset 仍是无操作。后者已经能安全展示和重新检查，不冒充已经重写或物理修复了旧文件。
- 非法专用交互字段仍按原契约降级到可重新检查的目录错误/blocked，不将缺候选或过期授权变成空选择器。正常终态和合法交互态不会因旧恢复按钮而被覆盖。写盘另设 allow_nan=false，防止后续发布绕过有限数不变量。
- 锁内二次读取、先备份后替换、真实忙锁/未知锁拒绝，以及不自动获取、不修改配置和材料的限制保持不变。

### 页面与回归

页面仅在选择态消费结构有效的账号数组，并把首次 render 纳入请求 try/finally，意外渲染失败也释放请求闸门。POST 成功按新快照替换对象，不再继承旧 can_connect=false；覆盖报告所列的「重置后紧接 GET 失败仍灰按钮」非阻塞问题。

本轮证据目录：D:/ai_workspace/ash/data/runs/LKDdRwqGKHw3/review-fix-round-1。

| 检查 | 本轮真实结果 | 证据 |
| --- | --- | --- |
| 本分支三组 Python 定向回归 | 32 项通过；公共字段矩阵覆盖 4 类状态 × 27 种坏值，并验证真实 GET/reset、逐字节备份、重复重置、正常状态不覆盖和锁保护 | targeted-python.log、tests/test_wechat_setup_status_schema.py |
| 前端 Node 回归 | 15 项通过；新增坏账号形状、意外渲染失败及重置后刷新失败三组 | targeted-js.log |
| Windows 独立叠加回归 | 65 项无失败，6 项 Reader 集成最初因缺少专用环境而跳过；跨进程文件锁另 10 项通过 | overlay-regression.log、filelock-regression.log |
| 上述 Reader 集成补跑 | 使用既有固定 Reader/SQLCipher 环境，6 项全部通过，仅操作新建加密数据库 | encrypted-reader-regression.log |
| 两套隔离真实页面 | 每套 18 场景通过，0 未捕获脚本异常，0 acquire 请求；各 16 张截图共 32 张，以 8 张联系表逐项目检 | base-browser、overlay-browser、*-contact-*.png |
| 语法、行数和文件对照 | Python AST、JS 语法、700 行上限与 git diff --check 通过；旧叠加副本 803 文件哈希不变 | final-checks.json、static-final.log |

新叠加环境来自上一轮已存在的独立代码副本，并仅叠加本轮共享模块及入口变化；没有重新打开、启动或修改原任务。prepare_overlay.py 保留原任务 worker 与终态恢复规则；source manifest 校验的是旧验证副本，不能把它说成已经合并的产品。

旧基线页面本轮仍有 3 次既有终态误判，均在确认磁盘完整终态后显式点击原有刷新入口继续，并记录在 legacySnapshots；Windows 叠加页面没有使用此兜底。四项既有 Windows/POSIX 回归不在本轮修改范围，本轮未重跑整仓或据此宣称全仓通过。备份失败可能留下不完整 .bak 的非阻塞风险也未在此轮扩展处理。

重跑本分支回归：

    D:/ai_workspace/knowbase/.venv/Scripts/python.exe -m unittest -v tests.test_wechat_setup_status tests.test_wechat_setup_contract tests.test_wechat_setup_status_schema
    node --test tests/wechat_agent_test.mjs

browser_run.py、serve_scope.py、browser_probe.mjs 和 final_checks.py 保留于本轮证据目录。Pillow 在项目环境中不可用，首次联系表脚本因此失败，日志单独保留为 static-attempt-missing-pillow.log；随后改用 Windows 原生 System.Drawing 生成联系表，没有安装依赖，也没有把工具缺失算作产品失败。

### 本轮浏览器通道与清场

先尝试 Chrome 扩展具名后台会话「🔎 状态恢复复审修复」，visible=false，实际返回 Capability is not available: visibility；没有移除后台要求重试。随后按规范使用独立临时 profile 的 chrome-headless-shell，明确 --headless，只连接自建无头进程，未使用用户标签、窗口或登录态，未启动有头浏览器。

两套服务 PID 17220/12804、端口 9385/2555，浏览器 PID 22812/7164 均已退出。每轮日志均有 SERVICE_STOPPED 与 TEMP_FIXTURE_REMOVED；cleanup-status.json 记录本轮验证进程与监听端口为 0，主工作区仍干净。未重启 8787、扫描真实客户端或执行首次材料获取；之前记录的策略拒删残余未尝试换通道清理。没有提交或合并。

## 验收合并冲突处理（2026-10-24）

以上冻结基线、叠加验证及未提交说明是各历史回合的记录。本轮按用户要求，仅在任务分支 ash/LKDdRwqG 的工作区执行 git merge main 并提交合并；没有切换、修改或合并到 main，没有删除分支或 worktree。合并前任务提交为 fec04189e61ba3716537841f30d63c0fe5cf0b77，合入的 main 为 27d5d3f787f3db88958f30fdb53d2a92c1130731。

### 双方改动的保留

- 两个冲突文件为 kb/wechat_setup.py 与 kb/static/js/wechat_setup.js。保留 main 的 Windows 接入、平台/版本授权限制、完整终态一次原子发布、worker 收尾只释放锁、仅恢复活动阶段和锁内二次读取；同时保留本任务的严格状态契约、显式备份重置、失效交互降级及前端请求恢复。
- 作业锁沿用 main 的错误边界：只有文件锁层翻译出的 BlockingIOError 才是忙锁；原始 Windows error 33 不在接入层另行吞成忙碌。读取/锁失败仍失败关闭，不暴露原始系统错误、不获取材料。
- 合并回归发现严格状态投影需兼容 main 的终态字段：保留合法非负整数 unresolved 和持久化账号标签，补上非法值矩阵与完整终态读回一致性回归。交互校验仍使用后端账号目录生成标签，不信任旧标签。
- main 的终态/授权回归夹具补齐真实契约需要的临时账号目录、有效令牌、有效期和固定版本；原有断言未删减，版本拒绝测试仍故意使用错误版本。
- 对比 main 的产品差异仅限本任务的 12 个文件。AST 检查确认 account_roots、_state_file、_lock、_reader_ready、_config、_recovery_lock、_ready、_backup、_connect、_work 与 main 完全一致，活动阶段恢复主体、平台返回、main 既有状态文案与活动枚举也保持一致。

### 实际合并工作树验证

本轮不再用叠加副本代替产品；以下成功结果均针对实际合并工作树。证据目录：D:/ai_workspace/ash/data/runs/LKDdRwqGKHw3/merge-main-verification。

| 检查 | 真实结果 | 证据 |
| --- | --- | --- |
| 接入、状态契约、终态恢复、Windows、文件锁与适用 Docker 定向回归 | 124 项全部通过，无跳过 | final-regression.log |
| 固定 Reader/SQLCipher 集成 | 6 项全部通过，只操作新建加密数据库 | encrypted-reader.log |
| Node 前端回归 | 18 项全部通过 | initial-js.log |
| 隔离真实页面 | 18 场景通过，0 脚本异常、0 acquire 请求；16 张截图逐张目检；未使用历史终态刷新兜底 | browser/browser-results.json、contact-1.png 至 contact-4.png |
| 静态与保留性检查 | Python AST、700 行上限、JS 语法、无冲突标记/未合并索引、双份 diff --check、main AST 保留性对照通过 | final-static.json |
| 验证清场 | 自建服务/浏览器进程及验证端口均为 0；main 引用未变，主工作区干净 | cleanup-status.json、before-merge.json |

初次合并回归的真实失败保留在 initial-regression.log，包括上述已修兼容缺口。另有两项 Windows/POSIX Docker 基线失败：test_entrypoint_initializes_new_volume_and_preserves_it_on_restart、test_exports_wiki_and_pipeline_use_the_same_mounted_paths；用 git archive 导出的未修改 main 独立副本重跑，两项同样失败，见 main-baseline-docker.log。因此只报告定向回归通过，不宣称整仓全绿，也未扩大范围去修基线 Docker 问题。

### 本轮浏览器通道与清场

先尝试 Chrome 扩展具名后台会话「🔎 接入合并验证」，visible=false，返回 Capability is not available: visibility；没有放宽后台要求重试。按规范降级到独立临时 profile 的 chrome-headless-shell，明确 --headless，仅连接自建无头浏览器；没有接管用户标签、激活窗口、使用用户登录态或启动有头浏览器。

浏览器 PID 26084、隔离服务 PID 27072 及端口 14832 均已关闭；browser-run.log 有 SERVICE_STOPPED、TEMP_FIXTURE_REMOVED，cleanup-status.json 记录进程与监听均为 0。未重启 8787，未扫描真实客户端或执行首次材料获取，未绕过此前临时残余的删除策略。
