// ── 历史运行:翻到某一轮,整屏按那一轮渲染 ──────────────────────────────────────
// 面板那份当前状态只留最后一轮,而一轮的红很容易在被人看见之前就被下一轮覆盖掉:
// 上午那轮翻译报了 503、导出阶段判了红,晚上一次「重试失败项」跑完,面板上就只剩
// 那一轮的结论,那个红没有任何地方查得到。后端为此单独落了一份历史。
let pipeHistory = [], pipeViewing = null, pipeHistoryRequest = null;
// 后端报的保留天数(`/api/pipeline/runs` 的 `keep_days`);0 = 这份回包没说
let pipeKeepDays = 0;
// 这一列上次是**按什么渲染的**:`在跑没跑|屏幕上那一轮的版本`。null = 还没渲染过。
// 一翻就要重渲染整列 —— 因为「最新」那一枚的判据正是这两样。
// **版本必须在里面。** 只记「跑没跑」的那一版漏掉了「空闲 → 空闲、但又跑完了一轮」:
// 后台那一轮跑完、状态端点报的已经是它,而这一列那次历史查询恰好失败(503),于是整列
// 一个字没重渲染 —— 上午那个红**还挂着上一次渲染留下的「最新」和 `exitPipeHistory()`**,
// 点它屏幕不换、那一轮的 503 读不到(第 2 轮审查在页面上实点出来的)。
let pipeHistoryRunState = null;
function pipeHistorySig() {
  return `${pipeRunning() ? 1 : 0}|${pipeVersion(typeof pipeSnapshot === 'undefined' ? null : pipeSnapshot)}`;
}
// 「还欠一次历史刷新」。在途那次请求只能回答它开跑那一刻的事实,而「又跑完一轮」
// 这件事**一轮只说一次**(`refreshPipe` 的 wasRunning / newResult 两个分支)——
// 在途时直接复用旧 promise 的话那一次机会就没了:旧回包把列表设成它取到的那两轮,
// 而 `pipeDoneRun` 已经更新成第三轮,后续轮询再也不进那个分支,列表自己追不平,
// 得切走再回来才补齐。所以记一笔欠账,等在途那次收尾了补发。
// 口径同 `pipe.js` 的 `pipePendingWanted`。
let pipeHistoryWanted = false;

// 「这一轮结果怎么一句话说」。**判据复用阶段卡片那两个**(`pipePlannedStages` /
// `pipeHasFailure`)—— 自己再数一遍的话,列表上写「已完成」而点进去是两个红。
function pipeRunSummary(run) {
  const planned = pipePlannedStages(run), stages = run.stages || {};
  const failed = planned.filter(k => pipeHasFailure(stages[k])).length;
  if (failed) return { status: 'error', text: `${failed} 个步骤需处理` };
  if (run.stopped) return { status: 'stopped', text: '已停止' };
  const skipped = planned.filter(k => stages[k]?.status === 'skipped').length;
  if (skipped) return { status: 'skipped', text: `${skipped} 个步骤已跳过` };
  return { status: 'ok', text: '已完成' };
}

// 「10-14 09:16」。落盘的是「YYYY-MM-DD HH:MM:SS」本地时刻,**按字符切不去解析它**
// —— `new Date('2026-10-14 09:16:00')` 在各家浏览器上行为不一致,而这里只是显示。
function pipeRunWhen(run) {
  const stamp = run.finished_at || run.started_at || '';
  return stamp.slice(5, 16) || '时间未知';
}

// 「这一轮作用在哪」。**判据递的是整份快照、不是裸 filter**(`pipeRunScopes` /
// `pipeLibraryOnly`,口径在 `pipe_channels.js`,主视图和订阅页走的是同一份)。
// 递裸 filter 会落进那条留给存量记录的兼容分支 —— 而 `start_pipeline` **一律**往
// filter 里盖一个默认采集器名,于是日报那个只跑 index/extract 的子轮在列表上被说成
// 「公众号」,点进去主视图写的却是「全库(不按来源)」:同一轮两种说法。
// 全库那一档要**自己有话说**,不许回落成「未记录来源」—— 它不是没记,是不按来源。
function pipeRunWhere(run) {
  if (pipeLibraryOnly(run)) return '全库';
  const scopes = pipeRunScopes(run);
  return scopes.length ? scopes.map(s => channelLabel(s.channel)).join(' + ') : '未记录来源';
}

// 「这一列上面那句话」。**保留口径由后端给**(`/api/pipeline/runs` 的 `keep_days`),
// 不在这里写死一个「3 天」—— 两处各写一份的话,改了后端而面板照旧念旧数字,而那句话
// 正是用户判断「我还查不查得到前天那一轮」的唯一依据。**后端没给就不编**:老后端、
// 探针摆的桩回包里都没有这个字段,那时只报轮数。
function pipeHistoryNote(n) {
  return pipeKeepDays > 0 ? `保留最近 ${pipeKeepDays} 天 · ${n} 轮` : `共 ${n} 轮`;
}

function renderPipeHistory() {
  const list = $('pipe-history-list'), note = $('pipe-history-note');
  renderPipeHistoryLive();
  if (!pipeHistory.length) {
    list.innerHTML = '<p class="pipe-history-empty">还没有跑完过一轮。同步结束后这里会按轮次留档。</p>';
    note.textContent = '';
    pipeHistoryRunState = pipeHistorySig();
    if (typeof renderPipeHeadFacts === 'function') renderPipeHeadFacts();
    return;
  }
  note.textContent = pipeHistoryNote(pipeHistory.length);
  // 这一列有自己的滚动条(侧栏定高),而「有没有一轮在跑」一翻就要重渲染整列 ——
  // 不留位置的话那一下会把正在看的那几行顶回最上面。
  const scroll = list.scrollTop;
  // **一行渲染不出来只能坏那一行。** 这些字段全来自落盘文件,形状没有任何保证
  // (`finished_at` 落成数字时 `slice` 当场抛 TypeError),而原来是一次 map 渲染整
  // 表 —— 一条坏记录就把整屏历史换成一句「没能读到历史记录」,连那些好的一起,
  // 而文件其实读到了。
  list.innerHTML = pipeHistory.map(run => {
    try {
      return pipeHistoryRow(run);
    } catch {
      return '<p class="pipe-history-empty">有一轮记录读不出来，已跳过。</p>';
    }
  }).join('');
  pipeHistoryRunState = pipeHistorySig();
  list.scrollTop = scroll;
  // 页头那半句「N 轮留档」只有这一侧知道(轮数来自这条请求)。**两边都要调** ——
  // 理由在 `pipe_view.js` 的 `renderPipeHeadFacts` 注释里。
  if (typeof renderPipeHeadFacts === 'function') renderPipeHeadFacts();
}

function pipeHistoryRow(run) {
  {
    // **后端标了无效就走坏行那个出口**(抛给上面那个 try)。它不抛、字段也齐,照
    // 常渲染的话就是一行「时间未知 · 已完成」—— 一轮根本没跑过的「成功」。
    // 「渲染不出来」和「内容不算一轮运行」是同一件事的两种形态,所以一个出口。
    if (run.invalid) throw new Error('这一行不算一轮运行');
    // 选择键认后端给的 `key`(**逐行唯一**),不认 `run_id` 也不认 `version`:
    // 前者升级前那几条没有(属性里是空字符串,点下去打不开),后者在「两条都缺身份、
    // 收尾时间又相同」时会撞(点较早那行打开较新那行)。渲染、点击、定位、高亮共用它。
    const sum = pipeRunSummary(run), on = pipeViewing && pipeViewing.key === run.key;
    // 「这一行就是此刻屏幕上那份结果」的那一枚:标「最新」、高亮、点它回到最新,
    // **三处共用这一个判据**(各判一次的话标签说是最新、点下去打开的是另一轮)。
    // **判据不许是「排第一」** —— 清单和当前状态来自两条独立的请求、两份独立可失败的
    // 落盘,允许不同步:留档失败那一轮没写进历史文件,首行于是是上午那个红,而屏幕上
    // 是下午那一轮的成功。按位置认的那一版点上午那个红会退出只读、屏幕换成下午的
    // 「同步已完成」,那一轮的失败明细再也打不开(第 1 轮审查实点)。
    // 所以证明由**后端**给(`is_current`,过 `same_event` 只认可信身份),这边再和手上
    // 这份快照的版本对一次 —— 后端那次证明只对它生成回包那一刻成立,而这份清单可能是
    // 一个迟到的旧回包。两道都过不去就不标:那一行照旧打开它自己那一轮,「回到最新」
    // 在概况卡上另有一颗按钮,代价只是多走一步。
    // 跑着的时候最新那一档是正在跑的这一轮,它不在留档里,由 `renderPipeHistoryLive`
    // 单独出一行 —— 所以这里一律不标。
    const latest = pipeIsCurrent(run);
    const chosen = latest ? !pipeViewing : on;
    // 身份走 `data-run` 读、**不拼进 onclick**:`esc` 把引号转成实体,而属性值里的
    // 实体会在解析时还原 —— 拼进去的话一个带引号的身份就把那句 JS 拆了。这一行
    // 其余几个字段也都来自落盘文件,一处放松就是一个注入口。
    return `<button type="button" class="pipe-history-row${chosen ? ' on' : ''}${latest ? ' latest' : ''}"
      data-status="${esc(sum.status)}" data-run="${esc(run.key)}" aria-current="${chosen ? 'true' : 'false'}"
      onclick="pickPipeRun(this.dataset.run)">
      <span class="pipe-history-r1"><span class="pipe-history-when">${esc(pipeRunWhen(run))}</span>
      <span class="pipe-history-result">${esc(sum.text)}</span></span>
      <span class="pipe-history-r2"><span class="pipe-history-kind">${esc(pipeRunLabel(run))}</span>
      · <span class="pipe-history-where">${esc(pipeRunWhere(run))}</span></span></button>`;
  }
}

/** 这一刻是不是真有一轮在跑。拿不到快照时按「没在跑」—— 保守那一边只是少出一行。 */
function pipeRunning() { return !!(pipeSnapshot && pipeSnapshot.running); }

/** 「这一行就是此刻屏幕上那份结果」吗 —— **标签、高亮、点击动作共用的那一个判据**。
 *
 *  各判一次的话标签说是最新、点下去打开的是另一轮。**判据不许是「排第一」**:清单和
 *  当前状态来自两条独立的请求、两份独立可失败的落盘,允许不同步 —— 留档失败那一轮没写
 *  进历史文件,首行于是是上午那个红,而屏幕上是下午那一轮的成功。按位置认的那一版点
 *  上午那个红会退出只读、屏幕换成下午的「同步已完成」,那一轮的失败明细再也打不开。
 *
 *  两道:证明由**后端**给(`is_current`,过 `same_event` 只认可信身份),这边再和手上
 *  这份快照的版本对一次 —— 后端那次证明只对它生成回包那一刻成立,而这份清单可能是一个
 *  迟到的旧回包。两道都过不去就不是:那一行照旧打开它自己那一轮,「回到最新」在概况卡
 *  上另有一颗按钮,代价只是多走一步。
 *
 *  跑着的时候最新那一档是正在跑的这一轮,它不在留档里(由 `renderPipeHistoryLive` 单独
 *  出一行),所以这里一律为假。
 */
function pipeIsCurrent(run) {
  return !pipeRunning() && !!(run && run.is_current)
         && run.version === pipeVersion(typeof pipeSnapshot === 'undefined' ? null : pipeSnapshot);
}

/** 点一行:**点的那一刻再核验一次**,不认上一次渲染留下的动作。
 *
 *  渲染那一刻的证明只在那一刻成立,而这一行会在 DOM 里待很久:状态变了而这一列没能
 *  重渲染(历史那次查询失败)时,旧动作还挂着 —— 实测点上午那个红,屏幕停在下午那份
 *  成功结果、`readonly: false`,那一轮的 503 读不到。重渲染那一道在
 *  `pipeHistorySig()`,这一道是它的冗余带:**两道都在,缺哪一道都有自己的窗口**。
 */
function pickPipeRun(key) {
  const run = pipeHistory.find(r => r.key === key);
  if (!run) return;
  return pipeIsCurrent(run) ? exitPipeHistory() : viewPipeRun(key);
}

/** 正在跑的那一轮:它还没收尾,**不在留档里**,所以单独出一行待在列表外面。
 *
 *  待在列表**外面**而不是插进去:`#pipe-history-list` 里只有留档那几行,位置不被挤掉
 *  (浏览器探针按 `nth-child` 点第几轮)。点它 = 回到最新,和首行那一枚同一个职责。
 */
function renderPipeHistoryLive() {
  const host = $('pipe-history-live');
  if (!host) return;
  if (!pipeRunning()) { if (host.innerHTML) host.innerHTML = ''; return; }
  const run = pipeSnapshot || {};
  // **按开工时刻念。** `pipeRunWhen` 先看 `finished_at`,而跑着的这一轮还没有 —— 真要
  // 落进那条回退的话,念出来的会是上一轮的收尾时刻,那一行就在冒充另一轮。
  const when = pipeRunWhen({ started_at: run.started_at });
  const on = !pipeViewing;
  const html = `<button type="button" class="pipe-history-row latest${on ? ' on' : ''}"
      data-status="running" aria-current="${on ? 'true' : 'false'}" onclick="exitPipeHistory()">
      <span class="pipe-history-r1"><span class="pipe-history-when">${esc(when)}</span>
      <span class="pipe-history-result">正在运行</span></span>
      <span class="pipe-history-r2"><span class="pipe-history-kind">${esc(pipeRunLabel(run))}</span>
      · <span class="pipe-history-where">${esc(pipeRunWhere(run))}</span></span></button>`;
  if (host.innerHTML !== html) host.innerHTML = html;
}

/** 轮询那一侧调一次:跑没跑这件事变了就把整列重渲染,否则只对一下那一行活的。
 *
 *  **不每次轮询都重渲染整列** —— 那一列有自己的滚动条,每 1.5 秒顶一次没法看。
 */
function syncPipeHistoryLive() {
  if (pipeHistoryRunState !== null && pipeHistoryRunState !== pipeHistorySig()) renderPipeHistory();
  else renderPipeHistoryLive();
}

async function loadPipeHistory() {
  if (pipeHistoryRequest) { pipeHistoryWanted = true; return; }
  pipeHistoryWanted = true;
  while (pipeHistoryWanted) {
    // **这一笔欠账清在发请求之前。** 清在"接受回包之后"的话,在途期间别人记的那
    // 一笔会被它一起清掉 —— 而那一笔恰恰是说"你这次取的已经不是最新的了"。
    pipeHistoryWanted = false;
    // 发请求**之前**记下「现在是哪一轮」。回包回来时它变了,说明这期间又跑完了
    // 一轮,而这份回包里没有它 —— 记一笔欠账,循环按新快照再查一次。版本走
    // `pipeVersion`(服务端那一份口径 `result_version`)。
    // **还没有快照(首次进页面)时这一道短路** —— 那一档由状态侧兜住:状态回包一到
    // `pipeFreshCheck` 就看出这一块落后、按退避补读。两边都不许单独拆掉。
    const runKey = pipeVersion(pipeSnapshot);
    pipeHistoryRequest = pipeJson('/api/pipeline/runs');
    try {
      const data = await pipeHistoryRequest;
      if (runKey && runKey !== pipeVersion(pipeSnapshot)) { pipeHistoryWanted = true; continue; }
      pipeHistory = Array.isArray(data.runs) ? data.runs : [];
      pipeKeepDays = Number.isFinite(data.keep_days) && data.keep_days > 0 ? data.keep_days : 0;
      // 后端那边留档失败了没有 —— 和「这一屏取到没取到」是两件事
      pipeFreshNote = typeof data.error === 'string' ? data.error : '';
      // 正在看的那一轮可能已经被挤出记录(或者刚被同一轮次的新结果替换):
      // 跟着那份新清单重新认一次,认不出来就退回最新 —— 留着一份谁都查不到的
      // 快照,屏幕上那条「正在查看」就指着一轮列表上没有的运行。
      if (pipeViewing) {
        const again = pipeHistory.find(r => r.key === pipeViewing.key);
        if (again) pipeViewing = again;
        else { exitPipeHistory(); break; }
      }
      renderPipeHistory();
      if (pipeViewing) renderPipeState(pipeSnapshot || { running: false });
      // **渲染成功了才算追平这一轮**(`pipe_fresh.js` 那套 desired/applied)
      // **凭据是这份清单自己能证明的最新那一轮**,不是此刻的页面快照(理由同
      // `pipe_fresh.js` 抬头那条 ②)。**例外是后端如实报了留档失败**:那一轮根本
      // 没写进文件,补读多少次也追不平 —— 后端能给的最新就是手上这一份,所以按
      // desired 盖章,面板另有一句如实说那一轮没留下。
      // 版本由**后端**给(和状态端点同一个 `result_version`),不在这里从清单首条
      // 自己拼 —— 自己拼的那份在「清单是空的」和「旧记录没有 run_id」两档上是空串,
      // 而空串会被当成已追平。
      pipeFreshOk('history', pipeFreshNote ? pipeFreshDesired('history')
                                           : (data.version || ''));
    } catch (e) {
      // 读不到**不在这里**无限重试(循环开头已经把那一笔欠账清了,这里也不置回去)
      // —— 但缺口要如实留着:交给 `pipe_fresh.js`,由后续状态轮询按退避补读,面板上
      // 同时说明这一块还是旧的。原来这里什么都不记,于是「见过这一轮」和「屏幕上对
      // 上了」被当成同一件事,列表长期停在旧那份清单、一个字都不说。
      pipeFreshMiss('history', e.message);
      if (!pipeHistory.length) {
        $('pipe-history-list').innerHTML =
          `<p class="pipe-history-empty">没能读到历史记录：${esc(e.message)}</p>`;
      }
    } finally { pipeHistoryRequest = null; }
  }
}

function viewPipeRun(key) {
  const run = pipeHistory.find(r => r.key === key);
  if (!run) return;
  // 同一行再点一次 = 回到最新
  if (pipeViewing && pipeViewing.key === key) return exitPipeHistory();
  pipeViewing = run;
  renderPipeHistory();
  renderPipeState(pipeSnapshot || { running: false });
  // **已经在视口里就别滚。** 桌面上轮次列和这张卡并排,点一行它本来就在眼前 ——
  // 再滚一次是无谓的跳动。窄屏单列时它在上面几百像素外,那一档照旧要带过去。
  const title = $('pipe-state-title'), box = title.getBoundingClientRect();
  title.focus({ preventScroll: box.top >= 0 && box.bottom <= window.innerHeight });
}

function exitPipeHistory() {
  if (!pipeViewing) return;
  pipeViewing = null;
  renderPipeHistory();
  renderPipeState(pipeSnapshot || { running: false });
}
