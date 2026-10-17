// ── 日报阅读区的正文容器 ──────────────────────────────────────────────────────
// 从 daily.js 里分出来的一小块(那个文件逼近 700 行上限)。两件事必须待在一起
// 看,所以在同一个文件里:正文的唯一写入口,和「☆ 收藏此段」在这块正文上的接入
// —— 出处就写在正文容器自己的 `data-snip-ref` 上,收藏从选区所在的那份正文上
// 读它。口径在 kb/static/js/snip.js 与 docs/decisions/fav-snippets.md。

// 阅读区正文的**唯一写入口:正文和它的出处同时落下来**。
// **不读 `dailyCurrent`**:那是「列表里选中哪一天」,在请求发出之前就写好了,
// 而这期间屏幕上还是上一份正文 —— 拿它当出处,收下的就是一条正文属于 A、出处
// 记着 B 的收藏,点回去永远找不到(审查实测:卡住目标那一次读取,选中旧正文存
// 下来的那条记成了新日期)。
// 提示形态(读取失败、已删除)一律不带出处:那几个字不是日报正文,一个字都不该
// 收得下(审查实测:「读取失败」四个字被存成了一条日报片段)。
const dailyOriginalBodies = new WeakMap();
const DAILY_LEGACY_SKIP = '.daily-legacy-context';

function setDailyBody(html, ref = '') {
  const el = $('daily-reader-body');
  dailyOriginalBodies.delete(el);
  el.innerHTML = html;
  if (ref) el.dataset.snipRef = ref;
  else delete el.dataset.snipRef;
  return el;
}

function renderDailyReportBody(report) {
  const stripHeader = text => text.replace(/^\s*#\s+[^\n]*\n+(?:>\s*覆盖[^\n]*\n)?/, '');
  const view = report.material_view;
  if (!view) {
    const el = setDailyBody(mdHtml(stripHeader(report.body)), report.date);
    enhanceDailyBody(el);
    return el;
  }
  const original = stripHeader(report.body);
  const slotId = 'daily-material-' + Date.now().toString(36) + Math.random().toString(36).slice(2);
  const markdown = stripHeader(view.before) +
    `\n\n<div data-daily-material-slot="${slotId}"></div>\n\n` + view.after;
  const el = setDailyBody(mdHtml(markdown, original), report.date);
  const slot = el.querySelector(`[data-daily-material-slot="${slotId}"]`);
  if (!slot) {
    const fallback = setDailyBody(mdHtml(original), report.date);
    enhanceDailyBody(fallback);
    return fallback;
  }
  const heading = document.createElement('h2');
  heading.textContent = view.heading;
  const list = document.createElement('ol');
  list.className = 'daily-materials';
  for (const item of view.items) {
    const row = document.createElement('li');
    const entry = document.createElement('div');
    entry.className = 'daily-material-entry';
    const title = dailyMarkdownLink(item.url) || document.createElement('span');
    title.className = 'daily-material-title';
    title.textContent = item.title;
    title.title = item.full_title;
    const source = document.createElement('span');
    source.className = 'daily-material-source';
    source.textContent = ' — ' + item.source;
    entry.append(title, source);
    row.append(entry);
    list.append(row);
  }
  slot.replaceWith(heading, list);
  enhanceDailyBody(el, list);
  dailyOriginalBodies.set(el, original);
  return el;
}

function consumeDailySnipJump(pending, ref, el) {
  if (!pending || String(pending.ref) !== String(ref)) return pending;
  if (locateSnip(el, pending.spec, DAILY_LEGACY_SKIP)) return null;
  const original = dailyOriginalBodies.get(el);
  if (original) {
    const context = document.createElement('details');
    context.className = 'daily-legacy-context';
    context.open = true;
    const summary = document.createElement('summary');
    summary.textContent = '收藏所在的原版正文';
    const body = document.createElement('div');
    body.dataset.snipRef = ref;
    body.innerHTML = mdHtml(original);
    enhanceDailyBody(body);
    context.append(summary, body);
    el.append(context);
    if (locateSnip(body, pending.spec)) return null;
    context.remove();
  }
  return consumeSnipJump(pending, ref, el, DAILY_LEGACY_SKIP);
}

// 日报落在 vault 的 `daily/<日期>.md`,而那个目录**不在任何 `SOURCES.root`
// 底下**(见 kb/config.py),documents 表里没有它的行 —— 片段只能按 origin=daily
// 存,回跳靠 `openDailyReport` 回到这同一块正文里。
// 出处从选区所在的那份正文上读,于是三件事一个判据就够了:加载中选中的还是旧
// 正文 → 记旧那一天(屏幕上就是它);正文已经被换掉 → 选区离开了文档树、
// `closest` 找不到容器 → 不收;读取失败/已删除那几个字 → 容器上没有出处 → 不收。
attachSnip($('daily-reader-body'), range => {
  const host = snipHostOf(range, '[data-snip-ref]');
  const ref = host && host.dataset.snipRef;
  if (!ref || [...$('daily-reader-body').querySelectorAll(DAILY_LEGACY_SKIP)]
    .some(context => !context.contains(host) && range.intersectsNode(context))) return null;
  return { origin: 'daily', origin_ref: ref, origin_title: '日报 · ' + ref,
    root: host, skip: DAILY_LEGACY_SKIP };
});
