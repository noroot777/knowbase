// 历史面板那两个探针的**摆盘**:阶段常量、四轮终态、静态接口回包。
//
// 从 `pipe_history_probe.mjs` 里分出来的 —— 那边是**验证组**(摆什么局、判什么),
// 这边是**纯数据**(那四轮长什么样)。分开只为行数:判据一条都没搬过来,`let` 开关
// 和 mock server 仍在探针里,它们和验证组是一体的。
//
// 这几个对象导出的是**共享引用**:要改字段请先浅拷贝(`{ ...EVENING }`),直接改
// 属性会串到后面每一组。

const KEYS = ["crawl", "export", "index", "extract", "projects", "wiki"];
const stagesOf = over => Object.fromEntries(KEYS.map(
  k => [k, over[k] || { status: "ok", detail: "完成" }]));

// 上午那一轮:导出判了红,原因是翻译服务 503
const MORNING = {
  run_id: "run-morning", run_kind: "sync", stopped: false,
  started_at: "2026-10-14 09:00:00", finished_at: "2026-10-14 09:16:00",
  planned_stages: [...KEYS], crawl_filter: { channel: "rss" },
  stages: stagesOf({ export: { status: "error",
    detail: "3 条翻译失败（服务返回 503），库里仍是原文",
    failures: [{ title: "A new agent runtime", reason: "HTTP 503 from the model gateway" }] } }),
  log: ["[10-14 09:16:01] export: 3 条翻译失败（服务返回 503）", "[10-14 09:16:02] 管道结束"],
};
// 晚上那一轮「重试失败项」:全绿,于是面板上只剩它
const EVENING = {
  run_id: "run-evening", run_kind: "retry", stopped: false,
  started_at: "2026-10-14 21:00:00", finished_at: "2026-10-14 21:04:00",
  planned_stages: [...KEYS], crawl_filter: { channel: "rss" },
  stages: stagesOf({ export: { status: "ok", detail: "已保存 95 条到知识库" } }),
  log: ["[10-14 21:04:00] 管道结束"],
};
// 第三轮:后台刚跑完的一轮,又红了
const LATER = {
  run_id: "run-later", run_kind: "sync", stopped: false,
  started_at: "2026-10-15 09:00:00", finished_at: "2026-10-15 09:12:00",
  planned_stages: [...KEYS], crawl_filter: { channel: "rss" },
  stages: stagesOf({ export: { status: "error", detail: "第三轮又是 503" } }),
  log: ["[10-15 09:12:00] 管道结束"],
};

// 第四轮:专给待处理数那一组用 —— 「缺口」的定义是 desired 已经前移而 applied 还
// 落在旧那一轮上,所以必须真有一轮新结果,不能在「已经追平」的状态下让它失败(那种
// 失败本来就无害:屏幕上的数就是这一轮的)。
const FOURTH = {
  run_id: "run-fourth", run_kind: "sync", stopped: false,
  started_at: "2026-10-16 09:00:00", finished_at: "2026-10-16 09:08:00",
  planned_stages: [...KEYS], crawl_filter: { channel: "rss" },
  stages: stagesOf({ export: { status: "ok", detail: "已保存 12 条到知识库" } }),
  log: ["[10-16 09:08:00] 管道结束"],
};

const MIME = { html: "text/html; charset=utf-8", js: "text/javascript; charset=utf-8",
               css: "text/css; charset=utf-8", json: "application/json",
               svg: "image/svg+xml", ico: "image/x-icon", woff2: "font/woff2" };
const FIXTURES = {
  "/api/stats": { documents: 21674 },
  "/api/pipeline/channels": {
    channels: [{ id: "rss", label: "订阅源", hint: "RSS / Atom" }],
    default: "rss", labels: {}, aliases: {},
  },
  "/api/pipeline/accounts": { accounts: [] },
  "/api/facets": { tags: [], entities: [], accounts: [] },
  "/api/projects": { tools: [], kinds: {}, total_entries: 0 },
  "/api/sources/catalog": { modules: [] },
  "/api/daily/list": { days: [] },
};

// `KEYS` / `stagesOf` 只是这里拼那四轮用的,不导出
export { MORNING, EVENING, LATER, FOURTH, MIME, FIXTURES };
