/* 分类图谱前端。DATA / CFG 由构建脚本内嵌在页面里。
   DATA = {nodes, edges, arts, cats, meta};节点字段见 graph_data.py。 */
(() => {
const N = DATA.nodes, ARTS = DATA.arts;
const COLORS = CFG.colors;
const esc = s => String(s == null ? "" : s).replace(/[&<>"]/g,
  c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
// 项目主页和文章链接都是从外部内容里提出来的(LLM 读公众号正文写进 frontmatter),
// 只放行 http(s):esc 挡得住属性逃逸,挡不住 javascript: —— 那个点一下就执行
const safeUrl = u => (/^https?:\/\//i.test(u || "") ? esc(u) : "");

/* ── 类目 → 大类分组(顺序即分类表顺序) ── */
const catOf = {}, groups = [];
DATA.cats.forEach(c => {
  catOf[c.c] = c;
  let g = groups.find(x => x.g === c.g);
  if (!g) { g = { g: c.g, i: groups.length, cats: [] }; groups.push(g); }
  c.gi = g.i;
  g.cats.push(c);
});
// 配色按大类顺序发,但「其他」固定灰色、且不占一个色位——它每次都排在最后,
// 让它吃掉一个彩色只会把前面的大类挤到重复色上(skill 图谱 14 个大类时很明显)
let ci = 0;
groups.forEach(g => {
  g.col = g.g === CFG.otherGroup ? CFG.gray : COLORS[ci++ % COLORS.length];
});
const color = n => (groups[catOf[n.c] ? catOf[n.c].gi : 0] || groups[0]).col;

/* ── 邻接(共现) ── */
const adj = new Map();
DATA.edges.forEach(([a, b, w]) => {
  if (!adj.has(a)) adj.set(a, []);
  if (!adj.has(b)) adj.set(b, []);
  adj.get(a).push({ o: b, w }); adj.get(b).push({ o: a, w });
});

/* ── 建 DOM ── */
const pills = new Array(N.length);
const rows = [];          // {el, box, idxs}  一个子类一行
const sections = [];      // {el, gi, rows[]}
const nav = document.getElementById("nav");
const main = document.getElementById("main");

function pillEl(i) {
  const n = N[i];
  const el = document.createElement("span");
  el.className = "pill" + (n.n >= 8 ? " t3" : n.n >= 3 ? " t2" : "")
    + (n.rp ? " repo" : "");        // 小圆点 = 有代码仓库,和「只看有仓库」同一个判断
  el.style.color = color(n);
  el.dataset.i = i;
  el.innerHTML = esc(n.l) + (n.n > 1 ? `<span class="n">${n.n}</span>` : "");
  el.title = n.s || n.l;
  return el;
}
// 同一个节点会同时出现在「近期热议」和它所属的子类里,两处各一个 pill 元素;
// pills[i] 收的是所有副本,显隐/高亮一起作用,否则热议区永远亮着
function addPill(box, i) {
  const el = pillEl(i);
  box.appendChild(el);
  (pills[i] = pills[i] || []).push(el);
}

function makeRow(host, label, count, idxs) {
  const row = document.createElement("div");
  row.className = "sub-row";
  if (label) row.innerHTML = `<div class="st">${esc(label)}<span class="n">${count}</span></div>`;
  const box = document.createElement("div");
  box.className = "pills";
  row.appendChild(box);
  host.appendChild(row);
  const rec = { el: row, box, idxs };
  rows.push(rec);
  return rec;
}

// 近期热议:近 30 天被提到最多的,给一个"最近在发生什么"的入口
const hot = N.map((n, i) => i).filter(i => N[i].r30 > 0)
  .sort((a, b) => N[b].r30 - N[a].r30 || N[b].n - N[a].n).slice(0, CFG.hotCap);
if (hot.length) {
  const band = document.createElement("section");
  band.className = "band hot";
  band.innerHTML = `<h2><span class="dot" style="background:#e07a2f"></span>近 30 天热议
    <span class="tag">被提到最多的 ${hot.length} 个,按这 30 天的提及次数排</span></h2>`;
  main.appendChild(band);
  const rec = makeRow(band, "", 0, hot);
  rec.fixed = true;          // 这一行的顺序是「近 30 天提及」,不跟着排序切换走
  hot.forEach(i => addPill(rec.box, i));
  sections.push({ el: band, gi: -1, rows: [rec], always: true });
}

const byCat = {};
N.forEach((n, i) => (byCat[n.c] = byCat[n.c] || []).push(i));
const orderKeys = {
  n: (a, b) => N[b].n - N[a].n || (N[a].l > N[b].l ? 1 : -1),
  t: (a, b) => (N[a].t < N[b].t ? 1 : N[a].t > N[b].t ? -1 : N[b].n - N[a].n),
  l: (a, b) => N[a].l.localeCompare(N[b].l, "zh"),
};

groups.forEach(g => {
  const total = g.cats.reduce((s, c) => s + (byCat[c.c] || []).length, 0);
  if (!total) return;
  const band = document.createElement("section");
  band.className = "band";
  band.id = "g" + g.i;
  band.innerHTML = `<h2><span class="dot" style="background:${g.col}"></span>`
    + `${esc(g.g)}<span class="cnt" data-gcnt="${g.i}">${total}</span></h2>`;
  main.appendChild(band);
  const recs = [];
  g.cats.forEach(c => {
    const idxs = (byCat[c.c] || []).slice().sort(orderKeys.n);
    if (!idxs.length) return;
    const rec = makeRow(band, c.s || "", idxs.length, idxs);
    idxs.forEach(i => addPill(rec.box, i));
    recs.push(rec);
  });
  sections.push({ el: band, gi: g.i, rows: recs });

  const a = document.createElement("a");
  a.href = "#g" + g.i;
  a.innerHTML = `<span class="dot" style="background:${g.col}"></span>`
    + `${esc(g.g)}<span class="n" data-navcnt="${g.i}">${total}</span>`;
  nav.appendChild(a);
});

const emptyNote = document.createElement("div");
emptyNote.className = "empty-note";
emptyNote.textContent = "没有符合条件的条目,放宽筛选试试";
emptyNote.style.display = "none";
main.appendChild(emptyNote);

/* ── 筛选 ── */
const F = { q: "", win: 0, heat: 1, repo: false, sort: "n" };
/* built 是一个不带时区的日历日,窗口截止日必须**全程按 UTC 算**:按本地时区
   解析午夜再 toISOString(),在 UTC+8 下会退回前一天(2026-09-22 → 截止日
   2026-08-22),窗口凭空多含一天——页脚那句「近 30 天有提及 409」是后端按
   date.today()-30 数的,前端却筛出 430,同一张页面自己跟自己对不上
   (2026-09-22 第 10 轮审查发现)。所有 UTC 正偏移时区都会踩。 */
const today = new Date(DATA.meta.built + "T00:00:00Z");
function cutoff(days) {
  const d = new Date(today.getTime() - days * 864e5);
  return d.toISOString().slice(0, 10);
}
let cut = "";

function match(i) {
  const n = N[i];
  if (n.n < F.heat) return false;
  if (cut && n.t < cut) return false;
  if (F.repo && !n.rp) return false;
  if (F.q) {
    const hay = (n.l + " " + n.s + " " + (n.tp || []).join(" ")).toLowerCase();
    if (!hay.includes(F.q)) return false;
  }
  return true;
}

function apply() {
  cut = F.win ? cutoff(F.win) : "";
  const vis = new Uint8Array(N.length);
  let shown = 0;
  for (let i = 0; i < N.length; i++) if (match(i)) { vis[i] = 1; shown++; }
  pills.forEach((els, i) => els && els.forEach(el => el.classList.toggle("hide", !vis[i])));

  const gcnt = new Array(groups.length).fill(0);
  sections.forEach(sec => {
    let n = 0;
    sec.rows.forEach(r => {
      const c = r.idxs.reduce((s, i) => s + vis[i], 0);
      n += c;
      r.el.style.display = c ? "" : "none";
      const st = r.el.querySelector(".st .n");
      if (st) st.textContent = c;
    });
    sec.el.style.display = n ? "" : "none";
    if (sec.gi >= 0) gcnt[sec.gi] = n;
  });
  gcnt.forEach((c, gi) => {
    document.querySelectorAll(`[data-gcnt="${gi}"],[data-navcnt="${gi}"]`)
      .forEach(el => { el.textContent = c; });
    const a = nav.querySelector(`[data-navcnt="${gi}"]`);
    if (a) a.parentElement.classList.toggle("empty", !c);
  });
  emptyNote.style.display = shown ? "none" : "";
  document.getElementById("shown").textContent = shown;
  // 选中项被这次筛选滤掉了就整个复位:留着的话面板还展示着一个已经不在结果里的
  // 节点,而 body.hasSel 会把**所有**可见 pill 压到 opacity .2 却没有一个是亮的,
  // 整片结果区看起来像被禁用了
  if (selected >= 0 && !vis[selected]) reset();
  else if (selected >= 0) drawEdges(selected);
}

function resort() {
  const cmp = orderKeys[F.sort];
  rows.forEach(r => {
    if (r.fixed || r.idxs.length < 2) return;
    r.idxs.sort(cmp);
    const frag = document.createDocumentFragment();
    r.idxs.forEach(i => {
      const el = r.box.querySelector(`[data-i="${i}"]`);
      if (el) frag.appendChild(el);
    });
    r.box.appendChild(frag);
  });
  // pill 全都换了位置,连线必须跟着重画——否则路径还从排序前的坐标出发,
  // 指到别的节点或页面空白处
  if (selected >= 0) trackEdges();
}

/* ── 连线 ── */
const svg = document.getElementById("edges");
let selected = -1;
/* 本次交互点的是哪一份副本。同一个节点在「近 30 天热议」和它的分类区里各有一份
   pill,一律取首个可见副本的话:点分类区那份会被 scrollIntoView 拽回顶部的热议
   区,连线也从热议那份出发——用户刚点的位置直接被滚出视口
   (2026-09-22 第 1 轮审查发现)。没有来源元素的入口(面板上的关联按钮、搜索
   回车)仍退回首个可见副本。 */
let anchor = null;
function livePill(i) {
  const els = pills[i] || [];
  if (anchor && els.indexOf(anchor) >= 0 && !anchor.classList.contains("hide"))
    return anchor;
  return els.find(e => !e.classList.contains("hide"));
}
function center(el) {
  const r = el.getBoundingClientRect();
  return [r.left + r.width / 2 + scrollX, r.top + r.height / 2 + scrollY];
}
function drawEdges(i) {
  svg.innerHTML = "";
  const src = livePill(i);
  if (!src) return;
  svg.setAttribute("width", document.documentElement.scrollWidth);
  svg.setAttribute("height", document.documentElement.scrollHeight);
  const [x1, y1] = center(src);
  (adj.get(i) || []).forEach(({ o, w }) => {
    const dst = (pills[o] || []).find(e => !e.classList.contains("hide"));
    if (!dst) return;
    const [x2, y2] = center(dst);
    const dy = (y2 - y1) * 0.45, dx = (x2 - x1) * 0.12;
    const p = document.createElementNS("http://www.w3.org/2000/svg", "path");
    p.setAttribute("d", `M${x1},${y1} C${x1 + dx},${y1 + dy} ${x2 - dx},${y2 - dy} ${x2},${y2}`);
    p.setAttribute("fill", "none");
    p.setAttribute("stroke", color(N[o]));
    p.setAttribute("stroke-width", Math.min(1 + w * 0.7, 4.5));
    p.setAttribute("stroke-opacity", .45);
    p.setAttribute("stroke-linecap", "round");
    svg.appendChild(p);
  });
}
// 面板挤压布局的过渡期间 pill 位置逐帧变化,连线要跟着重画
function trackEdges(dur = 340) {
  const t0 = performance.now();
  (function tick(t) {
    if (selected >= 0) drawEdges(selected);
    if (t - t0 < dur) requestAnimationFrame(tick);
  })(t0);
}

/* ── 选中 / 详情面板 ── */
// 选中一个**当前被筛选挡着**的节点之前,先把挡它的那几项筛选放开。
// 面板里「常一起出现」那排按钮是按共现关系列出来的,跟搜索词/时间窗/热度/仓库
// 开关没有半点关系,点进去的目标十有八九不在当前结果里。直接 select 的话:
// body.hasSel 把**所有**可见 pill 压到 opacity .2,而目标 pill 还挂着 .hide
// ——面板展示着一个不在结果里的条目,结果区一片灰、没有一个亮的,连线也画不出来
// (2026-09-23 第 1 轮审查发现,实测 visibleSelected=0 / visibleDimmed=152)。
// `apply()` 里本来就有同一道保护(筛选变了、选中项被滤掉就 reset),关联跳转是从
// 旁边绕过去的,所以这道口径收在 `select()` 里,别的入口也一并管住。
// 只放开**挡路的那几项**,而且控件跟着回位——用户看得见筛选被放开了。
function setSeg(k, v) {
  F[k] = v;
  const seg = document.querySelector(`.seg[data-k="${k}"]`);
  if (seg) seg.querySelectorAll("button")
    .forEach(b => b.classList.toggle("on", +b.dataset.v === v));
}

function relax(i) {
  if (match(i)) return;                        // 常态:一次都不用重排
  const n = N[i];
  if (n.n < F.heat) setSeg("heat", 1);
  if (cut && n.t < cut) setSeg("win", 0);
  if (F.repo && !n.rp) {
    F.repo = false;
    if (repoBtn) repoBtn.classList.remove("on");
  }
  if (F.q) {
    clearTimeout(qt);                          // 刚敲下的那半个词别再回来
    F.q = "";
    qEl.value = "";
  }
  apply();                                     // 放宽只会让更多 pill 露出来
}

function select(i, from) {
  relax(i);
  selected = i;
  anchor = from || null;     // 用户点的那一份副本,滚动和连线都以它为准
  document.body.classList.add("hasSel");
  pills.forEach(els => els && els.forEach(el => el.classList.remove("sel", "lit")));
  (pills[i] || []).forEach(el => {
    el.classList.add("sel");
    el.style.setProperty("--selc", color(N[i]));
  });
  (adj.get(i) || []).forEach(({ o }) =>
    (pills[o] || []).forEach(el => el.classList.add("lit")));
  panelFor(i);
  trackEdges();
  // 滚动要**等面板挤压布局落定之后**再做:面板一开,主栏变窄、pill 整体重排,
  // 同一个 pill 的文档坐标能挪上千像素——立刻滚是按旧坐标滚,用户刚点的那一份
  // 照样被甩出视口(和 scrollIntoView 选错副本是同一个症状的两半)。
  setTimeout(() => {
    const el = livePill(i);
    if (el && selected === i) el.scrollIntoView({ block: "center", behavior: "smooth" });
  }, 360);
}

function panelFor(i) {
  const n = N[i], c = catOf[n.c] || { g: "其他", s: "" }, col = color(n);
  const path = c.s ? `${c.g} · ${c.s}` : c.g;
  let h = `<span class="badge" style="background:${col}">${esc(path)}</span>`;
  h += `<h3>${esc(n.l)}${n.promo ? '<span class="promo">软广</span>' : ""}</h3>`;
  if (n.s) h += `<div class="desc">${esc(n.s)}</div>`;
  h += `<div class="facts"><span>提及 <b>${n.n}</b> 次</span>`
    + `<span><b>${n.a}</b> 个来源号</span>`
    + `<span>近 30 天 <b>${n.r30}</b> 次</span>`
    + `<span>最近 <b>${n.t}</b></span><span>首见 ${n.f}</span></div>`;
  h += `<div class="acts">`;
  h += `<a class="primary" href="${CFG.appBase}?tool=${encodeURIComponent(n.q)}" target="_blank" rel="noopener">在图鉴里看全部记录 →</a>`;
  const link = safeUrl(n.u);
  // 文案跟着 rp 走:构建时按主机名判过的才叫仓库,官网/文档站叫「项目主页」
  if (link) {
    const label = n.rp === "github.com" ? "GitHub" : n.rp ? "仓库" : "项目主页";
    h += `<a href="${link}" target="_blank" rel="noopener">${label} ↗</a>`;
  }
  h += `</div>`;

  if ((n.tp || []).length) {
    h += `<div class="sec">主题</div><div class="chips">`
      + n.tp.map(t => `<span>${esc(t)}</span>`).join("") + `</div>`;
  }
  const rel = (adj.get(i) || []).slice().sort((a, b) => b.w - a.w).slice(0, 12);
  if (rel.length) {
    h += `<div class="sec">常一起出现(同一篇文章里被一起提到)</div><div class="chips">`
      + rel.map(({ o, w }) =>
        `<button data-go="${o}" title="一起出现 ${w} 篇">${esc(N[o].l)}`
        + `<span style="color:#aaa"> ${w}</span></button>`).join("") + `</div>`;
  }
  h += `<div class="sec">出自这些文章</div>`;
  h += (n.ar || []).map(ai => {
    const a = ARTS[ai];
    if (!a) return "";
    const t = esc(a[0] || "(无标题)");
    const href = safeUrl(a[3]);
    const link = href ? `<a href="${href}" target="_blank" rel="noopener">${t}</a>`
      : `<span>${t}</span>`;
    return `<div class="art">${link}<div class="meta">${esc(a[1])} · ${esc(a[2])}</div></div>`;
  }).join("");
  if (n.n > (n.ar || []).length) {
    h += `<div class="meta" style="color:var(--muted);font-size:12px;padding-top:8px">`
      + `…另有 ${n.n - n.ar.length} 条记录,在图鉴里看全</div>`;
  }
  document.getElementById("panelBody").innerHTML = h;
  document.getElementById("panel").classList.add("show");
  document.body.classList.add("panelOpen");
}

function reset() {
  selected = -1;
  anchor = null;
  document.body.classList.remove("hasSel", "panelOpen");
  pills.forEach(els => els && els.forEach(el => el.classList.remove("sel", "lit")));
  svg.innerHTML = "";
  document.getElementById("panel").classList.remove("show");
}
window.kbReset = reset;

/* ── 事件 ── */
document.addEventListener("click", e => {
  const go = e.target.closest("[data-go]");
  if (go) { e.stopPropagation(); select(+go.dataset.go); return; }
  const pill = e.target.closest(".pill");
  if (pill) { e.stopPropagation(); select(+pill.dataset.i, pill); return; }
  if (e.target.closest("#panel") || e.target.closest("header")) return;
  reset();
});
addEventListener("keydown", e => {
  if (e.key === "Escape") { reset(); }
  else if (e.key === "/" && document.activeElement !== qEl) {
    e.preventDefault(); qEl.focus();
  }
});
addEventListener("resize", () => { if (selected >= 0) drawEdges(selected); });

const qEl = document.getElementById("q");
let qt = null;
qEl.addEventListener("input", () => {
  clearTimeout(qt);
  qt = setTimeout(() => { F.q = qEl.value.trim().toLowerCase(); apply(); }, 120);
});
qEl.addEventListener("keydown", e => {
  if (e.key !== "Enter") return;
  const v = qEl.value.trim().toLowerCase();
  if (!v) return;
  for (let i = 0; i < N.length; i++) {
    if (match(i) && N[i].l.toLowerCase().includes(v)) { select(i); return; }
  }
});
document.querySelectorAll(".seg").forEach(seg => {
  seg.addEventListener("click", e => {
    const b = e.target.closest("button");
    if (!b) return;
    seg.querySelectorAll("button").forEach(x => x.classList.toggle("on", x === b));
    const k = seg.dataset.k, v = b.dataset.v;
    F[k] = k === "sort" ? v : +v;
    if (k === "sort") resort(); else apply();
  });
});
const repoBtn = document.getElementById("repoBtn");
if (repoBtn) repoBtn.addEventListener("click", () => {
  F.repo = !F.repo;
  repoBtn.classList.toggle("on", F.repo);
  apply();
});

apply();
})();
