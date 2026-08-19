# 交接文档:kb-web 展示系统前端重设计

> 给新会话的完整交接。目标:重写 `kb/static/index.html`,新增"工具图鉴"主视图,
> 全站换掉旧的奶油+赤陶配色。后端聚合层已完成并验证,只剩前端 + 少量收尾。
> 设计方案已经过 /frontend-design 两轮评审并获用户认可,**照方案执行,不要重新设计**。

---

## 0. 一句话现状

- ✅ 后端聚合层 `kb/projects.py` 已写完、已冒烟测试(1445 个工具,GPT-5.6 24 次提及正确合并)
- ✅ `kb/server.py` 已加 `/api/projects` 和 `/api/projects/{norm}` 两个端点
- ✅ 旧 `kb/static/index.html`(1851 行)已完整盘点,搬迁清单见 §5
- ❌ 新 index.html 还**一个字没写** —— 这是下一步的全部工作
- ❌ server.py 阅读页模板配色未换(§6)
- ❌ kb-web 未重启(server.py 改了必须重启才生效;§7 有安全重启协议)
- ❌ 浏览器验证未做(§8)

---

## 1. 需求(用户原话拆解)

- 工具/项目条目的**图鉴式浏览**:分类查找、搜索
- 点进某工具 → 展示总结信息 + 链接到原文/视频
- 详情页里带 **chatbox**,可就该工具提问
- 前端**整体重新设计**,旧的奶油白+赤陶橙风格全部换掉
- 现有全部功能(搜索/问答/收藏/管道 tab、阅读器、MCP/keys 弹窗)必须原样保留

用户约束(务必遵守):
- 回复用中文;用户阅读困难 → 汇报格式要**短行可扫读、导语+信息点+加粗锚点**,禁大段散文
- 存量 2294 篇条目化转写**不要跑**("先不跑了吧,等我有空了再跑")
- dr-youtube-dig 存量摘要:以后弄,不碰

---

## 2. 已批准的设计方案(执行,勿改)

### 2.1 色板 —— "瓷白 × 墨蓝黑 × 深松绿"

```css
--paper:  #F3F5F4;   /* 瓷白冷底(页面背景) */
--card:   #FFFFFF;
--ink:    #14202B;   /* 墨蓝黑(正文/标题) */
--muted:  #66727C;
--line:   #DFE5E5;
--brand:  #17604F;   /* 深松绿(主 accent) */
--brand-soft: #DFF0E9;
--tag-bg: #EAEEED;
--ok:     #2E7D57;
```

**kind 五色谱**(只用于卡片书脊色条和 kind 标签,别扩散到别处):

| kind | 色 |
|---|---|
| project(开源项目) | #17604F 松绿 |
| product(产品) | #3D5A80 靛蓝 |
| model(模型) | #7D4E9E 紫 |
| org(公司) | #8C6A3F 褐 |
| other | #66727C 灰 |

明确禁止:奶油白 #F4F1EA、赤陶橙 #b3541e 系(旧站正是这套 AI 陈词滥调,重设计动机之一)。

### 2.2 字体三件套(全系统字体,零下载)

- **展示体**:Songti SC / STSong,weight 900 —— 只用于大标题、图鉴卡片工具名
- **等宽体**:ui-monospace(SF Mono 系)—— 工具名、日期、计数、norm 等数据感元素
- **正文**:PingFang SC / system-ui

### 2.3 签名元素:正字计数(tally)

跨账号提及次数是这个知识库最强的信号 → 用**正字**逐笔渲染提及数,内联 SVG。

- viewBox `0 0 20 20`,五笔坐标:
  - s1 上横 `(2.5,4)→(17.5,4)`
  - s2 中竖 `(10,4)→(10,16.5)`
  - s3 右中横 `(10,10)→(16.5,10)`
  - s4 左短竖 `(4.5,8.5)→(4.5,16.5)`
  - s5 底横 `(2.5,17)→(17.5,17)`
- n 次提及 = 画 n 笔;每 5 笔一个正字;**最多画 3 个正,超出显示 `×N`**
- 出现动画:stroke-dasharray/dashoffset 逐笔 draw-in(卡片和抽屉里都有)
- `prefers-reduced-motion` 下禁用动画直接显示

### 2.4 布局

```
┌──────────────────────────────────────────┐
│ sticky 顶栏: 知识库 | 图鉴 搜索 问答 收藏 管道 | stats 图谱↗ keys mcp │
├──────────────────────────────────────────┤
│ 图鉴筛选行: [搜索框] [kind 分段控件] [topic chips] [排序] │
├──────────────────────────────────────────┤
│ 3 列卡片网格(kind 色书脊 + 正字计数)      │
│ …                              [右滑详情抽屉]│
└──────────────────────────────────────────┘
```

- 默认视图 = **图鉴**(不再是搜索)
- 页面从旧的 860px 窄栏改为宽布局(网格区 max-width ~1200px)
- 旧 4 个面板(搜索/问答/收藏/管道)原样搬进新壳,只换 token

### 2.5 低风险移植:token 别名

新 `:root` 里**保留全部旧 token 名**并映射到新值,这样搬过来的面板 CSS 和内联样式不用逐个改:

```css
--bg: var(--paper);  --accent: var(--brand);  --accent-soft: var(--brand-soft);
/* --card/--ink/--muted/--line/--tag-bg/--ok 同名保留,值换新 */
```

### 2.6 质量底线

响应式到移动端、键盘焦点可见、reduced-motion 全局尊重、图鉴视图不用 emoji。

---

## 3. 后端已就绪的 API(勿动,直接用)

### `GET /api/projects` → 图鉴列表

```json
{
  "tools": [{ "norm", "name", "kind", "summary", "url", "n",
              "sources": ["dr-gzh-projects","dr-video-projects"],
              "accounts": 3, "latest": "2026-07-12", "first": "…",
              "topics": ["ai-coding", …] }, …],   // 按 (latest, n) 降序
  "kinds": {"project": 900, …},
  "topics": [{"value": "ai-coding", "n": 321}, …],  // top 24
  "total_entries": 2246
}
```

### `GET /api/projects/{norm}` → 详情

列表 meta + `"vault"`(Obsidian vault 名)+ `"urls"`(全部 project_url 去重)+
`"entries"`:每条含 `path/name/account/url/article_title/published/source/body`
(body 是去掉 frontmatter 和首行出处引用的 markdown,前端用 marked 渲染)。

- 实现在 `kb/projects.py`(163 行,含缓存,按文件数+mtime 失效)
- 端点在 `kb/server.py`,插在 `/api/doc` 之前
- entry 来源区分:`source` 字段 `dr-gzh-projects`(公)/ `dr-video-projects`(视)

### 问答流 `POST /api/ask/stream`(已有,抽屉 chatbox 复用)

NDJSON:`meta(plan+refs+model)` → `delta(text)`× → `done(conversation_id+refs)` | `error`。
客户端断开即中止;失败不落库。旧 index.html 的 `doSendChat` 有完整的读流实现,照抄。

---

## 4. 新 index.html 的新增部分(图鉴+抽屉)规格

### 图鉴面板 `#gallerypanel`

- 进页面 fetch `/api/projects` 一次,内存过滤
- 筛选状态 `{q, kind, topic, src, sort}`:
  - q:对 name/summary/topics 做小写包含匹配
  - kind:分段控件(全部 + 五 kind,带计数)
  - topic:chips(top 24)
  - src:全部/公众号/视频
  - sort:最近提及(默认,latest+n)/ 提及最多(n)/ 最早收录
- 卡片:左侧 kind 色书脊条、工具名(等宽体)、kind 标签、summary 两行截断、
  正字计数 SVG + `N 次 · M 个号`、latest 日期、topic 小 chips
- **增量渲染:96 张/批 + IntersectionObserver 哨兵**(1445 个工具一次全渲染会卡)
- 点卡片 → 打开详情抽屉

### 详情抽屉(右侧 slide-over)

- z-index **30/31**(必须低于阅读器 40/41,这样条目里的原文/引用链接能盖在上面打开)
- fetch `/api/projects/{norm}`
- 头部:工具名、kind、正字计数(带动画)、summary、`url`/`urls` 外链、topics
- 记录时间线:每条 entry 一个 `<details>`(最新的默认展开):
  - summary 行:`公`/`视` 来源字形 + account + published + article_title
  - 展开:marked 渲染 body + 底部 `原文↗`(entry.url)+ Obsidian 链接(obsLink(path))
  - 条目 md 本身可用 docA/openReader 在阅读器里打开
- **底部 chatbox**:
  - 首条消息发 `` 关于工具「{name}」:{q} ``,后续直接发 q
  - `toolConvId` 维持会话连续;换工具时清空
  - 走 /api/ask/stream 流式;气泡渲染**复用 renderMsg**(引用/存为笔记都白得)
  - 会话正常落库,会出现在问答 tab 的会话列表里(这是特性,不是 bug)

---

## 5. 旧功能搬迁清单(必须逐项核对)

> **做法**:先完整读旧 `kb/static/index.html`(1851 行),搜索/问答/收藏/管道/弹窗/阅读器
> 的 HTML 和 JS **原样搬**(保留全部元素 id 和函数名),只做两件事:
> ① 删掉旧 :root,套新 token;② 按 §5.5 清单扫掉写死的暖色值。

### 5.1 必须保留的元素 id(JS 全靠 getElementById)

- 顶栏:`#stats`、图谱↗ 链接(/graph)、`openKeys()`/`openMcp()` 按钮、`#keys-dot`/`#mcp-dot`
- tab:`#tab-search/#tab-ask/#tab-fav/#tab-pipe` + 新增 `#tab-gallery`;`switchTab(t)` 加 gallery 分支
  (注意 switchTab 里有 `body.chat-mode`、`refreshPipe(true)`/`initChat()`/`loadFavPanel()` 调用)
- 搜索:`#q #f-source #f-account #f-topic #f-entity #chips #results`
- 问答:`#chat-new #chat-convs #chat-msgs #chat-input #ask-model #chat-send`
- 收藏:`#fav-t-inbox/#fav-t-done/#fav-t-archived #fav-model #fav-triage-btn #fav-mode-toggle #fav-stale #fav-list`
- 管道:`#wo-status #wo-auth #pipe-stages #pipe-run #pipe-retry #pipe-hint #sched-on #sched-time #sched-msg #pipe-log`
- 管道弹窗:`#pipe-modal #pipe-range-btn/-label/-pop/-presets #pipe-custom-from/-to #pipe-range-hint
  #pipe-acct-block/-count/-search/-list #pipe-est #pipe-start`
- keys 弹窗:每 provider p∈{anthropic,openai}:`#key-{p} #url-{p} #key-{p}-st #models-{p}-msg/-box/-save`;`#keys-save #keys-msg`
- mcp 弹窗:`#mcp-status #mcp-config #mcp-cmd #mcp-reg #mcp-unreg #mcp-msg`
- 阅读器:`#reader-mask`(z40)`#reader`(z41)`#reader-title #reader-fav #reader-open #reader-obs #reader-src #reader-frame`

### 5.2 必须保留的 JS 函数(原样搬,不重写)

- 基础:`$` 、`loadMeta / renderChips / addTopic / rmTopic / addEntity / rmEntity / obsLink / docLink / esc / docA`
  + 两个全局 click handler(a[data-doc]→openReader;点外面关 pipe-range-pop)+ Escape keydown
- 搜索:`doSearch`(含 favbtn、matched_by 徽章、entities chips)
- 收藏全家桶:`loadFavSet setFavBtn toggleFav switchFavView toggleFavMode initFavTools saveFavModel
  renderFavEmpty favDocA favOps favGroupCard favListHtml favCmdHtml copyFavCmd swapHero favHeroHtml
  favAcardHtml favDupHtml favColdRow favQuoteRow favFreshRow favDrawer renderTriage runTriage pollTriage
  loadFavPanel favMark favMarkMany favDel archiveStale`
- 问答全家桶:`pendingHtml paintPending setSendMode stopChat initChat autoGrowChat timeAgo loadConvs
  renderConvList appendInflight renderChatError openConv newChat delConv renderMsg scrollChat suggestChat
  sendChat doSendChat retryChat`(doSendChat 的 NDJSON 读流循环照抄,抽屉 chat 复用它或抽公共函数)
- MCP/keys/models:`refreshMcp openMcp closeMcp mcpRegister mcpUnregister copyCmd copyMcpConfig
  loadModels fmtKeyState refreshKeys openKeys closeKeys saveKeys fetchModels saveModels`
- 管道:`STAGE_META STATUS_ICON isoDay pipePresets openPipeModal togglePipeRangePop renderPipePresets
  applyPipePreset applyPipeCustomRange renderPipeRange renderPipeAcctList togglePipeAcct pipeSelectAll
  pipeSelectNone renderPipeEst refreshPipe retryFailed loadSched saveSched startPipeFromModal`
  (STAGE_META 已含 6 阶段:crawl/export/index/extract/projects/wiki)
- 底部初始化:`loadMeta(); loadFavSet(); loadModels(); refreshKeys(); refreshMcp(); loadSched();`
  + 新增 `loadGallery()`

### 5.3 外部依赖

- marked@12 CDN(旧文件头部有,保留)
- WeChatOA 配置链接 `http://localhost:5173/#/config`(管道面板里,保留)

### 5.4 z-index 秩序

阅读器 mask/panel = 40/41(最高)> 详情抽屉 30/31 > 各弹窗(keys/mcp/pipe-modal,沿用旧值)。

### 5.5 写死的暖色扫除清单(搬 CSS 时逐个替换)

- `#6b3413`(fav-stale、fav-note、fav-hero .why、.msg.user .bubble 文字、fav-dup .verdict)→ 墨蓝或 brand 深色
- `.fav-hero` 渐变 `#fdf3ec` + 边 `#eeddd0` → brand-soft 系
- `.conv-item.on` 边框 `#eeddd0` → 新 line/brand-soft
- `.opt.win` `#cfe3d5/#f7fbf8`、`.badge.vec` `#e4eee7` → 新绿色系微调
- `code.fav-cmd` `#1a1a1a/#7fd88f` → 墨蓝黑底可保留,绿字可保留
- models 盒子内联 border `#e8e4dc` → var(--line)
- `#c33/#b33` 错误红:**可以不动**

---

## 6. server.py 阅读页配色(小改)

`kb/server.py` 里 `_DOC_TMPL` 和 `_DOC_SNIP_JS`(阅读器 iframe 加载的文档页模板)还是旧配色,直接字符串替换:

| 旧 | 新 |
|---|---|
| `#b3541e` | `#17604F` |
| `#faf9f7` | `#F2F4F3` |
| `#f0ede6` | `#EAEEED` |
| `#e8e4dc` | `#DFE5E5` |
| `#f6e8de` | `#DFF0E9` |
| `#8a8578` | `#66727C` |

---

## 7. kb-web 重启协议(server.py 改动后必须做;index.html 不用)

> index.html 每次请求从磁盘读 → 静态改动**免重启**。server.py/projects.py 改动才要重启。

1. `curl -s http://127.0.0.1:8787/api/pipeline/status` 确认 `"running": false`(管道跑在 kb-web 进程里,跑着时**严禁杀进程**)
2. 杀 8787 旧进程
3. **env -i 干净环境**重启(继承环境会带上嵌套 claude 的变量导致 Not logged in 类问题),形如:
   ```bash
   cd /Users/fjh/code/knowledge-base && env -i HOME="$HOME" PATH="/usr/local/bin:/usr/bin:/bin:$HOME/.local/bin" nohup uv run kb web >/tmp/kb-web.log 2>&1 &
   ```
   (以实际曾用命令为准,可查 `/tmp/kb-web.log` 或进程历史)
4. `curl -s http://127.0.0.1:8787/api/projects | head -c 300` 验证新端点活着
5. **绝对不要用 preview_start 托管 kb-web**(它会接管生命周期,管道会被杀)

浏览器预览接入技巧:`.claude/launch.json` 里放一个 dummy 配置
(`"runtimeExecutable": "sh", "runtimeArgs": ["-c","sleep 999999"], "port": 8787`),
preview_start 它,preview 工具就能附着到已运行的 8787 而不管理它。

---

## 8. 验证清单(浏览器)

- [ ] 图鉴:卡片网格渲染、正字计数动画、四类筛选 + 排序、增量加载(滚到底继续出卡)
- [ ] 抽屉:打开某个多来源工具(如 GPT-5.6,24 条记录)、时间线展开、原文↗/Obsidian 链接
- [ ] 抽屉 chatbox:提问 → 流式回答 → 引用可点开阅读器 → 问答 tab 里能看到该会话
- [ ] 搜索 tab:搜索、筛选、收藏星标、打开阅读器(iframe 配色应已是新绿)
- [ ] 问答 tab:新会话、发送、停止、重试、存为笔记
- [ ] 收藏 tab:三个子视图、triage 面板
- [ ] 管道 tab:状态显示正常(**只看不跑**)
- [ ] keys/mcp 弹窗打开正常
- [ ] 响应式:mobile 375 宽单列;reduced-motion 下无 draw-in 动画
- [ ] 截图:图鉴网格 + 详情抽屉,发给用户看

---

## 9. 其它注意

- 每次 Bash 调用后 shell cwd 会重置(原会话在 /Users/fjh/code/gongzhonghao),命令都带
  `cd /Users/fjh/code/knowledge-base &&`
- 任务 #6(跑存量管道)是**暂停**状态,别顺手恢复
- 用户偏好:汇报短行可扫读;完成后给截图
