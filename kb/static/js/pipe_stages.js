// ── 一次同步跑哪几段,以及跑完要不要出日报 ─────────────────────────────────────
// 原来两个入口各自固定:「新建同步」跑六段不出日报,「同步全部并生成」跑四段出日
// 报,名字上一个字都看不出区别;而那两段全库的慢活(整理工具、更新 Wiki)一跑就
// 是几十分钟,从哪儿进来都没人说一声。所以步骤改成就地勾,慢活自己带标记。
//
// **「跑完出日报」不是第七个步骤,它换的是另一条起跑通道**(`/api/daily/run`,见
// `kb/daily_run.py`):那一轮的步骤由它自己排(每个来源各抓一遍,再统一索引与提
// 取),出报之后还接着整理图鉴与更新 Wiki(`kb/daily_library.py`)。所以勾上它之
// 后,步骤这一组**固定成它那一份、并且禁掉** —— 留着能改而后端照自己那一套跑,
// 等于把勾选静默忽略。同一个道理:日报那条通道**不认订阅名单也不认抓取日期段**
// (`RunRequest` 只有 since/until/sync/channels,而 since/until 说的是「哪些素材
// 进这份日报」,不是抓哪几天),所以那两块也一起禁掉,并就地说明为什么。
//
// 不勾「抓取内容」时**不能一概而论**:导出也是按来源跑的(`_run_export` 走
// `_per_channel`),所以只勾「导出文章」那一轮照旧要带来源 —— 跟着抓取一起禁掉、
// 连来源字段都不往请求里放的话,后端收到一个没有来源的来源敏感请求,`crawl_channels`
// 给回一个空通道、`active_collector('')` 回落到**默认采集器**:用户选的是订阅源,
// 实际导出的是公众号,而面板上一句话都不说(第 3 轮审查实页实测,证据
// `export-only-api.json`)。真正只对抓取有效的是**订阅名单和日期段**
// (`FetchScope` 收它们,`materialize()` 压根不收),所以那两块单独禁、来源那块不禁。
const PIPE_STAGE_DEFS = [
  { k: 'crawl', name: '抓取内容', d: '从所选来源收集文章' },
  { k: 'export', name: '导出文章', d: '保存到本地知识库' },
  { k: 'index', name: '建立索引', d: '让新内容能被搜到' },
  { k: 'extract', name: '提取要点', d: '模型读一遍，写摘要和标签', cost: 1 },
  { k: 'projects', name: '整理工具', d: '把文章里提到的工具拆成图鉴条目', cost: 1, slow: 1 },
  { k: 'wiki', name: '更新 Wiki', d: '双链、汇总页、三张关系图谱', cost: 1, slow: 1 },
];
// 日报那一轮自己排的步骤(`daily_run.FETCH_STAGES` + `DIGEST_STAGES`)。这里是
// **照着念给人看**,不是传给后端的参数 —— 真正跑哪几段由 `daily_run` 定。
const PIPE_DAILY_STAGES = ['crawl', 'export', 'index', 'extract'];
// 出完报之后还有两段(`daily_library.LIBRARY_STAGES`)。**它们跟着定时设置里那个
// 勾**(`daily_run.load_settings()['library']`),所以不能像上面那几段一样写死:
// 少念了,用户就会被一段几十分钟的慢活偷袭;多念了,是替后端许诺一件不会发生的事。
const PIPE_DAILY_TAIL = ['projects', 'wiki'];
// 出完报那一段整理跑不跑。true / false / **null = 读不到**(读不到不许当成"不跑",
// 也不许当成"跑" —— 那一档只说"由设置里那个勾决定",见下面那句提示)。
function pipeDailyLibrary() {
  const s = schedules()?.daily;
  return s ? s.library !== false : null;
}
let pipeStageSel = new Set(PIPE_STAGE_DEFS.map(s => s.k));
// 勾上「跑完出日报」之前用户自己那一份:取消勾选时原样还回去,不是重置成全选。
let pipeStageKept = null;

function pipeDailyOn() { return !!$('pipe-daily')?.checked; }
// 这一次实际会跑哪几段。**顺序按 `PIPE_STAGE_DEFS` 定死**,不按勾选先后 ——
// 后端 `start_pipeline` 也是按 `STAGES` 过滤的,两边念出来的顺序得是同一个。
function pipeStageList() {
  const sel = pipeDailyOn()
    ? new Set([...PIPE_DAILY_STAGES,
               ...(pipeDailyLibrary() ? PIPE_DAILY_TAIL : [])])
    : pipeStageSel;
  return PIPE_STAGE_DEFS.filter(s => sel.has(s.k)).map(s => s.k);
}
function pipeStageOn(k) { return pipeStageList().includes(k); }
// **按来源跑的是抓取和导出这两步**,别的几步是全库的(`index` 扫整个 vault、
// `extract` 扫全库待提取队列,`projects`/`wiki` 连通道参数都不收)。
// 后端同一份口径在 `kb/pipeline_stages.py` 的 `run_scopes`(判据同样是
// `{'crawl','export'} & planned`),**两边必须是同一个判据** —— 这边松一格就是把
// 用户选的来源丢在请求外面,后端只好回落到默认采集器。
const PIPE_SOURCE_STAGES = ['crawl', 'export'];
// 这一次要不要带来源。
function pipeNeedsSource() { return PIPE_SOURCE_STAGES.some(k => pipeStageOn(k)); }
// 订阅名单和日期段只有抓取收(见 `PIPE_STAGE_DEFS` 上面那段)。
function pipeNeedsTargets() { return pipeStageOn('crawl'); }

// 「这一次起不起得了跑」。**只判一处**:那颗按钮的 disabled 和脚下那句汇总都读
// 它,免得一个说得通、另一个按不下去。返回空串 = 可以跑。
function pipeStartBlocked() {
  if (!pipeStageList().length) return '一步都没勾';
  if (!pipeNeedsSource()) return '';             // 全库那几步:来源和订阅都不参与
  if (pipeLoadingChannels().length) return '订阅还在读';
  if (!pipeReadyChannels().length) return '来源读不出来';
  if (pipeDailyOn()) return '';                  // 日报那条通道只认来源
  // 只导出那一轮**不看订阅**(导出是按来源扫的,不按订阅),所以不许拿「一个订阅
  // 都没勾」把它拦住 —— 拦住了用户就没有任何办法只补一次导出。
  if (!pipeNeedsTargets()) return '';
  return pipeSelCount() ? '' : '一个订阅都没勾';
}

function pipeStageSlowCount() {
  const on = pipeStageList();
  return PIPE_STAGE_DEFS.filter(s => s.slow && on.includes(s.k)).length;
}
// 「这次会跑:A → B → C」。汇总那一句和日报那一轮的说明都用它。
function pipeStagePlan() {
  const on = pipeStageList();
  const name = k => PIPE_STAGE_DEFS.find(s => s.k === k)?.name;
  if (!pipeDailyOn())
    return '这次会跑：' + PIPE_STAGE_DEFS.filter(s => on.includes(s.k)).map(s => s.name).join(' → ');
  // 日报那条通道的顺序是**出报插在中间**:入库那几段 → 出报 → 整理那两段
  // (`daily_run` 收尾那个 finally 里调 `daily_library.library_tail`)。按
  // `PIPE_STAGE_DEFS` 的顺序一路念下去,会把「先整理、后出报」念成事实。
  const tail = PIPE_DAILY_TAIL.filter(k => on.includes(k)).map(name).join(' → ');
  return '这次会跑：' + PIPE_DAILY_STAGES.map(name).join(' → ')
         + '，跑完生成今天的日报' + (tail ? '，再' + tail : '');
}
function pipeStageTail() {
  const slow = pipeStageSlowCount();
  return slow ? ` 含 ${slow} 段全库的慢活，一次要跑几十分钟。` : '';
}

function renderPipeStages() {
  const daily = pipeDailyOn();
  const on = pipeStageList();
  $('pipe-stage-list').innerHTML = PIPE_STAGE_DEFS.map(s => `<label class="pipe-channel">
      <input type="checkbox" data-stage="${s.k}"${on.includes(s.k) ? ' checked' : ''}${daily ? ' disabled' : ''}>
      <span><b>${s.name}${s.cost ? '<i class="pipe-chip">烧额度</i>' : ''}${s.slow ? '<i class="pipe-chip">几十分钟</i>' : ''}</b>
      <small>${s.d}</small></span></label>`).join('');
  $('pipe-stage-count').textContent = `已选 ${on.length} / ${PIPE_STAGE_DEFS.length}`;
  $('pipe-stage-all').textContent = on.length === PIPE_STAGE_DEFS.length ? '清空选择' : '全选';
  $('pipe-stage-all').disabled = daily;
  // 两块范围各自为什么被禁,**分开说**:一个是「日报那一轮不认它们」,一个是
  // 「这次压根不抓」,笼统一句「不可用」两档都对、哪一档都没说明白。
  const source = pipeNeedsSource(), crawl = pipeNeedsTargets();
  $('pipe-source-fields').disabled = !source;
  $('pipe-scope-fields').disabled = !crawl || daily;
  // 出完报那一段**不许一律承诺**:它跟着定时设置里那个勾,关着的时候图鉴和三张
  // 图谱就是不更新(`daily_library.library_round` 读不到设置也不跑)。
  const lib = pipeDailyLibrary();
  $('pipe-stage-hint').textContent = daily
    ? '日报那一轮的步骤由它自己排：每个来源各抓一遍，再统一建索引、提取要点；'
      + (lib === true ? '出完报接着整理工具和更新 Wiki。'
         : lib === false ? '出完报不整理图鉴和 Wiki（定时设置里关掉了）。'
         : '出完报整不整理图鉴和 Wiki，看定时设置里那个勾（现在读不到）。')
      + '它抓的是各来源的新内容，不按订阅和日期筛。'
    : !source ? '这几步都是全库的，上面的来源、时间范围和订阅这次都不作数。'
    : !crawl ? '没勾「抓取内容」，时间范围和订阅这次不作数；导出仍按上面选的来源跑。'
    : '';
}

// 定时设置读到(或被别处保存)之后,上面那几句话跟着换 —— 弹窗开着时就地重画。
// 不重画的话,开着弹窗的人看到的还是"读不到"那一档,而设置早就到了。
watchSchedules(() => {
  if (typeof pipeModalOpen === 'function' && pipeModalOpen()) {
    renderPipeStages(); renderPipeEst();
  }
});

// 勾一段 / 取消一段
$('pipe-stage-list').addEventListener('change', e => {
  const k = e.target.dataset?.stage;
  if (!k) return;
  if (e.target.checked) pipeStageSel.add(k); else pipeStageSel.delete(k);
  renderPipeStages(); renderPipeEst();
});
function pipeStageToggleAll() {
  pipeStageSel = pipeStageSel.size === PIPE_STAGE_DEFS.length
    ? new Set() : new Set(PIPE_STAGE_DEFS.map(s => s.k));
  renderPipeStages(); renderPipeEst();
}
function onPipeDailyToggle() {
  if (pipeDailyOn()) {
    if (!pipeStageKept) pipeStageKept = new Set(pipeStageSel);
    pipeStageSel = new Set(PIPE_DAILY_STAGES);
  } else if (pipeStageKept) {
    pipeStageSel = pipeStageKept; pipeStageKept = null;
  }
  renderPipeStages(); renderPipeEst();
}
