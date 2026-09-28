# 信息来源模块

微信同步、大V观点、RSS 是平级的信息来源。全流程运行状态仍共用「同步管理」，以一个运行锁避免同时导出、建立索引或提取知识。

本次新增两个模块：

- **大V观点**：默认 51 个 X 账号（24 个海外关注、27 个中文关注）。支持其他社交媒体提供的 RSS / Atom 地址。
- **RSS**：默认 9 个 RSS / Atom 地址，包括 HN Top 100 Blogs 聚合入口。聚合入口是一个订阅，不代表只有一位作者。

不新增 YouTube 来源，不导入之前列出的 5 个普通博客网页。独立博客类别留待后续实现。

## 使用

左侧「信息来源」打开模块。在「订阅管理」中添加、修改名称或链接、分组、停用、删除或单独同步。删除订阅停止后续采集，已经保存的内容仍可阅读和检索。默认名单只在首次初始化时导入，修改和删除不会被重启覆盖。

「最新内容」可以按来源和关键词筛选、分页、展开原文快照、打开外部原文。RSS 没有提供全文时明确显示「订阅摘要」，不会把摘要冒充全文。不会自动爬取订阅条目指向的所有网页。

手动「同步全部」和单源「同步」执行：采集 → 保存 Markdown → 建立全文与向量索引。后台执行，关闭页面不影响运行；可在同步管理中停止。知识提取和 Wiki 等耗时步骤仍由同步管理继续执行，不在每次短动态同步时自动重跑。

## 同步规则

- 自动同步初始关闭。可分别设置每 15 分钟至 7 天；观点默认 60 分钟，RSS 默认 360 分钟。
- 服务每 30 秒检查一次计划，重启后已到期的计划会补执行；服务关闭期间无法抓取。忙碌的任务保持到期状态，下一轮继续尝试，不会被标记成已完成。
- 微信每日任务优先，两个新增模块按到期时间顺序执行。没有新的并行写索引通道。
- 每个订阅保留最近尝试、最近成功、失败原因、退避期限、ETag 和 Last-Modified。
- 单个来源失败不阻塞其他来源；成功来源照常保存和索引。失败从 30 分钟开始指数退避，最大 24 小时；手动重试跳过退避。
- 首次读取订阅当前提供的条目，单源单次最多 500 条；源服务只提供近期内容时无法补齐全部历史。HTTP 响应最大 8 MB，连接/读取有超时。
- 来源类型 + 规范化原文 URL 作为身份，移除跟踪参数；X 镜像的推文链接归一到 x.com。不同 RSS 入口重复包含一篇文章时只保存一份，并记录多订阅关联。
- 原文首次收录快照保留，不会因另一聚合源摘要更短而覆盖全文。更换订阅链接会清空该订阅的缓存和关联计数，旧快照仍保留。
- 指定日期的同步按北京时间过滤，未知日期条目只在不限时间时收录；区间导入不更新缓存验证头，避免下一次普通同步漏掉区间外内容。
- 读取 RSS / Atom，以及网页明确声明的 RSS / Atom alternate 链接。不做普通博客列表页采集，不操作浏览器或获取登录凭证。

## X 的获取方式

默认沿用原配置中的 `https://nitter.net/{handle}/rss` 和 `https://xcancel.com/{handle}/rss`，按顺序尝试。两者都是外部服务，不保证可用，也不是 X 官方 API。

可以在模块的「X 订阅桥接设置」替换模板，或在某个账号的「专用订阅链接」配置其可用 feed。专用链接优先于全局模板；账号主页仍作为用户管理的链接保留。支持公开网络地址，不接受本机、私网、带用户名密码的 URL。

2026-09-28 实测：Simon Willison 订阅成功读取 30 条、HN Top 100 Blogs 成功解析 500 条；默认 X 桥接出现 nitter.net 连接失败、xcancel.com HTTP 451。因此当前 X 实际采集需要可用的桥接服务或专用 feed；错误会显示在相应订阅，不伪装成「无新内容」。

## 扩展边界

| 层 | 职责 | 入口 |
|---|---|---|
| 来源注册表 | 模块名称、导航、采集通道、能力、适配器、默认频率 | `kb/source_types.py` |
| 采集器契约 | `status / fetch / materialize / pending` | `kb/collectors/__init__.py` |
| 通用 feed 采集器 | 将注册的来源接入统一管道 | `kb/collectors/feeds.py` |
| 传输适配器 | URL → 标准条目；来源特有的地址和解析 | `kb/feeds/fetching.py` |
| 订阅与快照存储 | 一次性默认导入、增删改、去重、计划和历史 | `kb/feeds/registry.py` |
| 同步和原文导出 | 失败隔离、退避、原子写 Markdown | `kb/feeds/sync.py` |
| API 与页面 | 通用订阅编辑、阅读列表、同步设置 | `kb/web/intake_api.py`、`kb/static/js/sources.js` |
| 索引与查询 | 注册目录扫描；沿用 FTS / 向量 / 引用链 | `kb/config.py`、既有 scanner / pipeline |

新增另一种 RSS 兼容类别时，在 `SOURCE_TYPES` 注册 `manager='feeds'` 并选择 `adapter='rss'`，导航、采集器注册、扫描目录、计划和 CRUD 会自动接入。需要新的传输时增加一个适配器，实现 `endpoints(source, settings)` 和 `parse(body, url)`，产出 `url/title/body/author/published/content_kind`。不需要改统一管道。

对于未来直接抓取普通博客、播客或其他有专属认证和持久化需求的类别，可以实现独立 `SourceCollector`，像微信一样注册专属通道与管理能力；无需迁移微信或 feed 私有存储。来源类别不等同于底层传输（大V观点可以来自 X 桥接或其他社交 feed）。

## 数据与验证

订阅、设置、采集快照及最近 30 次采集结果保存在 `data/feeds.db`。Markdown 原文在 vault 的 `source/social/`、`source/rss/`，检索索引仍在 `data/kb.db`。备份需包含 feeds.db 和 vault；删除索引不会删除订阅。`KB_DATA_DIR`、`KB_VAULT_ROOT` 可用于完全隔离的测试环境。

运行后端回归：

```sh
.venv/bin/python -m unittest discover -s tests -p 'test_feed_sources.py'
.venv/bin/python -m unittest discover -s tests -p 'test_sync_channels.py'
.venv/bin/python -m unittest discover -s tests -p 'test_pipeline_stop.py'
```

浏览器验证使用 `tests/serve_sources.py` 的临时数据库和 vault，远端 feed 和向量模型为测试替身，真实运行 API、管道、Markdown 导出和 FTS 索引。不会修改生产订阅、调用真实微信或混入模拟文章。

浏览器扩展通道返回 `unsupported Codex auth method: apikey`，无法创建具名扩展会话，因此降级为独立临时 profile 的无头 Chrome / Playwright。未激活或接管用户现有 Chrome 标签，未使用有头浏览器。

本次验证结果：17 项来源回归 + 20 项微信通道回归 + 11 项管道停止回归，共 48 项通过。无头浏览器完成默认名单、添加、编辑、停用/启用、单源同步、展开阅读、独立定时配置、删除后保留内容与全文检索的端到端验证；1440px 桌面和 390px 窄屏检查通过。截图保存在本地忽略目录 `output/playwright/`。
