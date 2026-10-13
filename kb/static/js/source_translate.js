// 翻译积压那行提示和它那颗按钮。单独一个文件**只因为行数**(`sources.js` 到了
// CLAUDE.md 那条 700 行上限),口径和调用点一个字都没改:`renderSourceItems` 拼
// note 时调 `stuckNote()`,`source-list` 上那个点击分派认 `[data-retry-translate]`。
//
// 下面这三个状态由 `renderSourceView` / `moreSourceItems` 从
// `/api/intake/{kind}/items` 的回包里喂进来(`stuck` / `translate_ready`)。
let sourceStuck = null, sourceTranslateReady = true, sourceRetryBusy = false;

// 翻译积压那一档的提示 + 一个能自己动手的入口。两件事:
//
// **「试过了没成功」要和「还没轮到」分开说**:后者等下一轮就好(一轮只译
// MAX_ITEMS 条,剩下的自然轮到),前者的下一步完全相反。原来它们共用那句「还在
// 等翻译」,于是一批已经失败两次、再失败一次就永久按英文入库的内容,和刚抓回来
// 还没排到的内容在面板上长得一模一样(2026-10-13 实测:中转站 503 让 246 条
// 卡住,面板只说「18 条还在等翻译」)。
//
// **`shipped` 那半必须说出来**:那批条目已经离开翻译队列、按英文写进了知识库,
// 既不在上面那个计数里、条目上也没有任何标记 —— 不说的话面板上没有一个字提到
// 它们存在,而它们正是这颗按钮唯一救得回来的东西。
//
// **按钮两档都给,不只给失败过的那一档。** 没失败过的积压也得有一个"现在就译"
// —— 定时同步默认是关的,不给入口的话那批内容要等到用户下一次手动点「同步全部」
// (而那一趟还会连着重新采集),在系统里没有别的办法让它们动起来。
//
// **没有可用翻译模型时只说事实、不给按钮**:那时点下去什么也不会翻(后端那道
// 闸也会拒),按钮摆在那里是在承诺一件做不到的事。
function stuckNote() {
  const s = sourceStuck || {};
  const failed = s.failed || 0;
  // 射程 = 这个范围里所有还没译的,分两段互不重叠:还排在队列里的(`queued`,
  // 试满次数的已经不在里面)+ 已经试满、离开了队列的那批(`exhausted`,重试会
  // 把它们的额度清零、放回队列)。和端点那边的 `total` 是同一个口径。
  // **这里不能用 `shipped`** —— 它只数其中**真的按原文落了盘**的那些,而没落盘
  // 的照样会被放回队列,按它算就又变成"说 3 条、动 4 条"(见 `translate.stuck`)。
  const scopeTotal = (s.queued || 0) + (s.exhausted || 0);
  if (!failed && !scopeTotal) return '';
  const scope = $('source-filter').value ? '这条订阅' : '这个来源';
  const lines = [];
  const wrap = () => lines.map(t => `<span class="source-note-line">${t}</span>`).join('');
  if (failed) {
    const shipped = s.shipped ? `，其中 ${s.shipped} 条已按原文写进知识库（不再自动重试）` : '';
    lines.push(`${scope}有 ${failed} 条外文内容翻译失败${shipped}。`
             + (s.reason ? ` 最近一次失败：${esc(s.reason)}` : ''));
  }
  if (!scopeTotal) return wrap();
  if (!sourceTranslateReady) {
    lines.push('还没有可用的翻译模型，先到顶栏「模型」里给「外文翻译」这一步配好 API key，再回来重试。');
    return wrap();
  }
  // **那句话里的数字必须是按钮真会动的那些**(`scopeTotal`),不是"失败了几条"
  // —— 后者只是其中一段。说窄了就是在骗人:用户看着「3 条」点下去,库里动的是
  // 二十几条(口径见 `translate.manual_sql()`,它比 `RETRY_SQL` 宽是有意的)。
  // 反过来 `queued`/`exhausted` 两段都**不含**正文还在补抓的那些 —— 点名集合
  // 同样排除它们,三处逐字同一个射程(第 2 轮审查:补抓和翻译在同一轮里先后跑,
  // 口径一漂就是确认 1 条、实际译 2 条)。
  let say;
  if (!failed) say = `${scope}共 ${scopeTotal} 条等着译。`;
  else if (scopeTotal > failed) say = `连同还排着队的，这个范围共 ${scopeTotal} 条等着译。`;
  else say = `这 ${scopeTotal} 条都等着译。`;
  lines.push(`${say} <button class="link-btn" data-retry-translate`
           + `${sourceRetryBusy ? ' disabled' : ''}>${failed ? '重新翻译' : '现在就译'}</button>`);
  return wrap();
}
// 重试 = 把这个范围里还没译的条目放回队列队首 + 立刻跑一轮翻译和导出
// (不重新采集)。射程跟着当前选中的订阅走,和那行提示里的数字是同一个范围。
async function retryTranslate() {
  if (sourceRetryBusy) return;
  // **范围在打开确认框之前就定死,确认之后不再重读。** 确认框是一个 `await`:
  // 它开着的那十几秒里面板自己会刷新一轮(`queueSourcePoll`),而被删掉的订阅
  // 会让筛选值回退成空(`loadSourceModule` 重建选项那一句),**空在后端正好是
  // 整个来源**。确认之后才读的话,用户确认的是「这条订阅的 1 条」、发出去的是
  // 整个来源(2026-10-14 第 4 轮审查实测:确认框开着时在另一个窗口删掉那条
  // 订阅,等面板自己刷新完再点确认,他压根没选的另一条订阅也被译了,确认
  // 1 条、实际 2 条)。`sourceKind` 同理 —— 切来源 tab 就会改它。
  const kind = sourceKind, sid = $('source-filter').value;
  const scope = sid ? '这条订阅' : '这个来源';
  const s = sourceStuck || {};
  // **说的数字必须是真会动的那些**(同 `stuckNote` 的 `scopeTotal`),不是
  // "失败了几条" —— 后者只是其中一段,照它说就把花钱的规模说小了。
  const n = (s.queued || 0) + (s.exhausted || 0);
  // 会真的调模型花钱,而数量可能是上百条:先说清这一次要动多少、译完会怎样。
  if (!await confirmBox({
    title: `翻译${scope}还没译的 ${n} 条内容？`,
    body: '会把它们放回翻译队列最前面并立刻开始翻译，译完自动写进知识库并重建检索索引。'
        + '这一步要调用翻译模型（按字数计费），一轮译不完的留到下一轮同步继续。',
    ok: '开始翻译',
  })) return;
  // **确认期间范围变了就停在这儿,不许改用别的范围。** 订阅被删、用户自己改了
  // 筛选、刷新把值顶掉 —— 哪一种都让刚才那句承诺不再成立。这里只回答一个问题:
  // 「他确认的那份范围此刻还是不是原样」;**不去判断新范围是大了还是小了** ——
  // 两个方向的代价不对等,按他确认的那条 id 发出去顶多被后端 404 挡掉(它已经
  // 不存在了),按漂过去的新范围发出去是花他没确认过的钱。
  if (sourceKind !== kind || $('source-filter').value !== sid) {
    toast('选中的范围在确认期间变了，没有开始翻译',
          { detail: '先确认要译哪个范围，再点一次。' });
    return;
  }
  sourceRetryBusy = true; renderSourceItems();
  const id = Number(sid) || null;
  try {
    const r = await pipePost(`/api/intake/${kind}/translate-retry`,
                             id ? { source_id: id } : {});
    toast('已开始重新翻译', { type: 'ok', detail: r.detail });
    // **被接纳的那一刻就重读一次条目**:额度清零和"这一轮只译它们"的标记是
    // 导出阶段落的,但条目上那几档状态文案(等翻译 / 已按原文入库)此刻就该跟着
    // 走。`false` 保留当前筛选和阅读位置,`sourceRows.length` 保留已加载条数
    // —— 翻译要几分钟,把用户滚到的位置重置掉等于让他从头找一遍。
    await refreshPipe(); updateSourceRunStatus(); queueSourcePoll();
    await renderSourceView(false, sourceRows.length);
  } catch (e) { toast('未能开始重新翻译', { detail: e.message }); }
  finally { sourceRetryBusy = false; renderSourceItems(); }
}
