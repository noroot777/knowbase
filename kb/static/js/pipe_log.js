// ── 运行日志:每行带「谁说的」,按来源筛 ──────────────────────────────────────
// 几个来源同时跑时日志交错在一起。后端给每行打了来源标记(`kb/pipeline_lanes.py`:
// `[11-03 09:00:02] <rss> 检查 9/9 个来源`),这里按同一个式样拆开,上面一排按钮筛。
// 没有标记的那几行(全库那几步、升级前的老日志)归「全库」。后端按类各留最近 40 条
// (`pipeline_lanes.trim`),所以一路刷屏挤不掉别的来源仅有的那几行。
const PIPE_LOG_LINE = /^\[([^\]]*)\] (?:<([A-Za-z0-9_.:-]+)> )?([\s\S]*)$/;
// 筛到哪一类:'*' = 全部,'' = 全库,其余是通道名
let pipeLogWho = '*', pipeLogRaw = [], pipeLogEmpty = '';

/** 拆一行:{at, who, msg}。认不出式样的整行当正文。 */
function pipeLogLine(line) {
  const hit = PIPE_LOG_LINE.exec(String(line ?? ''));
  return hit ? { at: hit[1].slice(-8), who: hit[2] || '', msg: hit[3] } : { at: '', who: '', msg: String(line ?? '') };
}

function renderPipeLog(lines, finishedAt) {
  pipeLogRaw = lines;
  pipeLogEmpty = finishedAt ? `上次结束于 ${finishedAt}，没有保留详细日志。` : '尚无运行记录。开始同步后，这里会实时显示进展。';
  paintPipeLog();
}

function paintPipeLog() {
  const log = $('pipe-log'), box = $('pipe-log-box');
  const rows = pipeLogRaw.map(pipeLogLine);
  // 筛选按钮:这一轮日志里出现过的来源(按出现先后),有全库那几行才给「全库」
  const seen = [...new Set(rows.map(r => r.who).filter(Boolean))];
  const kinds = seen.length ? ['*', ...seen, ...(rows.some(r => !r.who) ? [''] : [])] : [];
  if (!kinds.includes(pipeLogWho)) pipeLogWho = '*';
  const chips = kinds.map(k => `<button type="button" aria-pressed="${k === pipeLogWho}" data-who="${esc(k)}">${
    esc(k === '*' ? '全部' : k ? channelLabel(k) : '全库')}</button>`).join('');
  const bar = $('pipe-log-filters');
  if (bar.innerHTML !== chips) bar.innerHTML = chips;
  const shown = rows.filter(r => pipeLogWho === '*' || r.who === pipeLogWho);
  const html = shown.length ? shown.map(r => `<div><span>${esc(r.at)}</span><span class="who">${
    esc(r.who ? channelLabel(r.who) : '')}</span><span>${esc(r.msg)}</span></div>`).join('')
    : `<div class="empty">${esc(rows.length ? '这一类这一轮没有日志' : pipeLogEmpty)}</div>`;
  if (log._html === html) return;
  const follow = $('pipe-log-follow').checked, top = box.scrollTop;
  log.innerHTML = html; log._html = html;
  box.scrollTop = follow ? box.scrollHeight : top;
  $('pipe-log-latest').hidden = follow;
  $('pipe-log-note').textContent = follow ? '每类保留最近 40 条' : '已暂停跟随 · 有新进展';
}

/** 「只看这一类的日志」:展开日志、筛到这一类、滚过去。 */
function showPipeLogOf(who) {
  pipeLogWho = who;
  const panel = $('pipe-log-panel');
  panel.open = true;
  paintPipeLog();
  followPipeLog();
  panel.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
}

$('pipe-log-filters').addEventListener('click', e => {
  const btn = e.target.closest('[data-who]');
  if (!btn) return;
  pipeLogWho = btn.dataset.who;
  paintPipeLog();
  followPipeLog();
});
function followPipeLog() {
  $('pipe-log-follow').checked = true;
  $('pipe-log-box').scrollTop = $('pipe-log-box').scrollHeight;
  $('pipe-log-latest').hidden = true; $('pipe-log-note').textContent = '每类保留最近 40 条';
}
function togglePipeLogFollow() {
  if ($('pipe-log-follow').checked) followPipeLog();
  else { $('pipe-log-latest').hidden = false; $('pipe-log-note').textContent = '已暂停跟随'; }
}
$('pipe-log-box').addEventListener('scroll', () => {
  const box = $('pipe-log-box');
  if (box.scrollHeight - box.scrollTop - box.clientHeight > 32 && $('pipe-log-follow').checked) {
    $('pipe-log-follow').checked = false; togglePipeLogFollow();
  }
});
new ResizeObserver(() => {
  if ($('pipe-log-follow').checked) $('pipe-log-box').scrollTop = $('pipe-log-box').scrollHeight;
}).observe($('pipe-log-box'));
// 下载的是原样那一份(带时间和来源标记、不受筛选影响)
function downloadPipeLog() {
  const text = pipeLogRaw.length ? pipeLogRaw.join('\n') : pipeLogEmpty;
  const url = URL.createObjectURL(new Blob([text], { type: 'text/plain;charset=utf-8' }));
  const a = document.createElement('a'); a.href = url; a.download = '知识库同步日志.txt'; a.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
