# 向量编码(bge-m3)

> 从 CLAUDE.md 外移的决策记录。CLAUDE.md 只留硬约束和一行指针,
> 这里是完整的事故形态、实测证据、回归清单和反向验证红数。原文逐字搬运。

- **模型跑在子进程里,不在 kb-web 进程里**(`kb/embed_worker.py` 是本体,
  `kb/embed_pool.py` 管起停,`kb/embedder.py` 是门面)。理由只有一个:内存。
  bge-m3 一碰 MPS 就占 ~3.7 GB,而且批量编码时每出现一种新的 batch 形状,MPS
  的显存池就再涨一截、**永不回落**——kb-web 跑几轮 index 之后常驻 8.8 GB
  (实测峰值 11 GB),`ps` 的 RSS 还看不出来(被压缩换出了,只表现为 swap 打满)。
- **这笔内存在进程内根本收不回**(2026-09-08 实测,别再试了):
  `torch.mps.empty_cache()` 只让 phys_footprint 掉 8 MB;`del model` + gc +
  empty_cache 也只从 5901 MB 降到 4157 MB;改 fp16 只把权重从 2.96 压到 1.07 GB,
  编码峰值一模一样。**进程边界是唯一可靠的回收手段。**
- 生命周期:懒起(第一次编码才 spawn,冷启动 3.2 秒)、空闲
  `KB_EMBED_IDLE_SECONDS`(默认 600)秒自杀、批量任务结束时
  `embed_pending_chunks` 显式收掉。子进程 stdin 读到 EOF 就退出,所以父进程
  怎么死都不会留孤儿;它自己 `start_new_session=True`,不会被打给父进程组的
  信号在编码半途打断。
- **批量写向量一律走 `embedder.embed_pending_chunks(conn, progress)`**,别再抄
  「取无向量 chunk → 128 条一批 → 写回 → commit」那个循环:原来 `cli.py`、
  `pipeline_stages` 的两处各有一份,改一处漏两处。
- `KB_EMBED_BATCH_BUDGET` 决定**峰值**内存(注意力张量随文本长度平方涨,显存池
  会涨到见过的最大批次那个规模)。2026-09-08 实测 1536 条真实 chunk:
  32×768²(原值)峰值 17.0 GB / 14.7 chunk·s⁻¹,8×768²(现值)8.7 GB / 9.0,
  4×768² 8.0 GB / 8.8。取 8 是拐点。**改这个值不需要 reembed**——实测新旧预算
  出来的向量余弦相似度 min 0.99999994。
- 协议是「4 字节小端长度 + 载荷」的帧。**向量帧的长度前缀必须被吃掉**:第一版
  按 `n*dim*4` 裸读,前缀被当成第一个 float,整批向量平移 4 字节(余弦相似度
  从 1.0 掉到 0),剩的 4 字节还会把下一个请求的帧同步带歪——而错位之后 read
  会**永久阻塞**,且是攥着锁阻塞的,搜索和索引一起冻住。所以父进程侧的读带
  帧长上限(`_MAX_FRAME`)和超时(`KB_EMBED_REQUEST_TIMEOUT`),把「挂住」变成
  「报错重试」。`tests/test_embed_pool.py` 拿 stub worker 把这些钉住(不加载
  真模型,1 秒跑完);注入回原 bug 会红 10 条。
- 已知遗留:`kb-mcp` 有好几个进程,各自会起自己的 worker(都命中 3.7 GB)。
  空闲超时能兜住,但同时活跃时仍是几倍开销。真要治得让它们共用一个常驻
  worker(要 socket + 守护进程),现在没做。
