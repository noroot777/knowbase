# collector — Knowbase 的公众号采集器

从**本机微信（Mac 客户端）已同步的推送**里发现公众号文章,按链接抓正文,导出成
Obsidian markdown 交给 Knowbase 索引。

它是一次性 CLI,不是常驻服务:

```bash
cd collector && ./.venv/bin/python -m cli <命令>
```

日常不必直接碰它 —— 仓库根目录的 `./kb.sh sync` 会把它当作一个采集通道调起来
(契约在 `kb/collectors/wechat_local.py`)。stdout 只出 NDJSON 给程序读,
人看的提示走 stderr。

## 装

```bash
./setup.sh
```

把两样不进版本库的东西链到仓库外:`data/`(文章库)和 `.venv/`。两者默认落在
`$XDG_DATA_HOME/knowbase/collector/`,可用 `KB_COLLECTOR_DATA_PATH` /
`KB_COLLECTOR_VENV_PATH` 指到别处。venv 不在就用 [uv](https://docs.astral.sh/uv/)
建一个。脚本幂等,新开 git worktree 跑一次即可 —— 多个工作树共用同一份库才对,
采集是有状态的。

依赖只有三个:`lxml`、`requests`、`zstandard`(后者解本机推送里的压缩消息)。

## 用

```bash
python -m cli status                      # 健康状况 + 订阅列表(JSON)
python -m cli collect [--since 2026-09-01 --until 2026-09-30] [--targets a,b]
python -m cli export [--no-images]        # 导出成 obsidian md
python -m cli accounts list|discover|add|remove|enable|disable [名字] [--json]
```

`accounts discover` 从本机微信的会话列表里找候选:**不登录、不走网络**。裸列时
归属从发现索引里查(一条推送都不翻),给了关键词才现翻索引外那几个会话。
加号走 `accounts add <名字>`,同名会话要补 `--fakeid`。

## 它是怎么拿到文章的

```
本机微信数据库  --(只读 SQL,经 rion-wechat-cli)-->  发现:哪些推送、属于哪个号
      |
      v
local_wechat_articles  --(按 __biz + mid + idx 认身份)-->  articles
      |
      v
按文章链接抓正文(保留 chksm)  -->  article_bodies  -->  export 成 vault md
```

两件事分开:**发现**只读本机库,**正文**照旧按 `mp.weixin.qq.com` 的文章链接抓。
本机库只提供「有哪些推送、各属于谁」。

几条硬约束(详见仓库根目录 `CLAUDE.md` 的「collector 采集器」一节):

- 只能通过已配置的 `rion-wechat-cli` 读,须确认 `live_database_read_ok`;不自动
  获取密钥、不操作微信、不用通知预览替代。
- 归属最终按推送 URL 里的 `__biz` 核实,不以同名自动匹配。
- 文章身份是 `__biz + mid + idx`;用户删除的文章记进链接身份黑名单,换一种发现
  方式也不复活。
- 公众号改名只改 `accounts.display_name`,永不动主键 `name`(`articles` 有外键
  指着它,vault 目录也按它归档)。新名字从本机会话列表上读。

## 数据落在哪

| 路径 | 用途 |
|------|------|
| `data/wechatoa.db` | 文章、公众号、正文、本机发现索引(SQLite) |
| `data/covers/`、`data/backups/`、`data/*.json` | 旧形态留下的存量,代码已经不读了 |

库里有正文全文和订阅列表 —— **别提交,别外传**。`.gitignore` 里对 `data` 有
不带斜杠的显式规则(它是符号链接,带斜杠那条挡不住),别删。

## 来历

这个采集器源自 [ZejunCao/WeChatOA_Aggregation](https://github.com/ZejunCao/WeChatOA_Aggregation),
搬进本仓库之后形态变了很多:原来的 Vue 前端、跑在 :8000 的 FastAPI 后端、Docker
部署、公众平台后台抓列表、扫码登录、封面下载、MinHash 相似检测都已删除,只留下
「发现 → 抓正文 → 导出」这条链和它的库。另一条采集方式(逐号文章凭证,靠 mitm
从 Mac 微信里截)在 `wechat-oa` 分支上,**两个分支始终并行、不打算合并**。

当前实现里仍有一部分代码逐字沿用上游(主要在 `src/db/repository.py`、
`src/db/migrations.py`、`src/utils/wechat_body.py`、`src/db/connection.py`)。
**上游没有 LICENSE**,所以这部分的许可状态未定 —— 本仓库对外发布前要先解决它。
