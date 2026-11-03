// 「问题与待办」探针里全选那几节(由 `intake_diag_probe.mjs` 按顺序调用,共用它那一个
// 页面和那几个 CDP 助手)。拆出来是因为那一份连上这几节就越过了单文件 700 行的上限。
//
// 进来时页面停在「待决定」、已经装载完;出去时停在「待决定」、勾选已清空。
// 页面里的 `window.fetch` 只在各自那一节里临时换掉,出这一节之前换回真的。

export async function pickProbe({ evalJs, until, check, sleep, send }) {
  // ── 10c-2. 全选:节头那一格管这一节,批量栏那一格管整屏 ─────────────────────────
  // 先断言后果:勾中的是**哪几条**(`CARE.picked` 的 key 集合),不是「打没打勾」。
  // 反过来钉一边:来源问题那一节没有全选、整屏全选也勾不进来源问题 —— 它的出口是
  // 点名一条订阅同步一次,批量勾三条就是第一条占住管道、后两条当场 409。
  const pickedKeys = `JSON.stringify([...CARE.picked.keys()].sort())`;
  const laneKeys = lane => `JSON.stringify(CARE.sections.find(s => s.lane.id === '${lane}')
    .items.filter(r => !r.id).map(r => r.key).sort())`;
  check("来源问题那一节没有全选那一格",
        await evalJs(`!document.querySelector('#care-list input[data-pick-lane="source_problems"]')`));
  check("能勾的每一节都有全选那一格",
        await evalJs(`CARE.sections.filter(s => s.items.some(r => !r.id) && (s.lane.exits || []).length)
          .every(s => !!document.querySelector('#care-list input[data-pick-lane="' + s.lane.id + '"]'))`),
        await evalJs(`JSON.stringify([...document.querySelectorAll('#care-list input[data-pick-lane]')].map(b => b.dataset.pickLane))`));
  const firstLane = await evalJs(`document.querySelector('#care-list input[data-pick-lane]')?.dataset.pickLane || ''`);
  await evalJs(`document.querySelector('#care-list input[data-pick-lane]').click()`);
  check("节头那一格勾中的正好是这一节列出的那几条",
        firstLane && (await evalJs(pickedKeys)) === (await evalJs(laneKeys(firstLane))),
        `${firstLane}: ${await evalJs(pickedKeys)}`);
  check("这一节的行复选框跟着勾上,批量栏露出来",
        await evalJs(`[...document.querySelectorAll('#care-list input[data-pick-key]')]
          .filter(b => CARE.picked.has(b.dataset.pickKey)).every(b => b.checked) && !$('care-batch').hidden`));
  await evalJs(`document.querySelector('.care-batch-all input').click()`);
  check("整屏全选勾满所有能勾的行,来源问题一条不进",
        await evalJs(`(() => {
          const want = CARE.sections.filter(s => !s.source && (s.lane.exits || []).length)
            .flatMap(s => s.items.map(r => r.key)).sort();
          const got = [...CARE.picked.keys()].sort();
          const src = new Set((CARE.sections.find(s => s.source)?.items || []).map(r => r.key || r.id));
          return want.length > 1 && JSON.stringify(want) === JSON.stringify(got)
            && got.every(k => !src.has(k));
        })()`), await evalJs(pickedKeys));
  await evalJs(`document.querySelector('.care-batch-all input').click()`);
  check("整屏全选再点一下全部放掉,批量栏收起",
        await evalJs(`CARE.picked.size === 0 && $('care-batch').hidden
          && [...document.querySelectorAll('#care-list input[type=checkbox]')].every(b => !b.checked && !b.indeterminate)`));
  // 半选:已搁置那一组夹具里有两条,勾满之后放掉一条,两处全选都该变成半选,
  // 而半选时点一下是**补齐**(浏览器把 indeterminate 的点击翻成 checked)。
  await evalJs(`careSetGroup('archived')`);
  await until(`CARE.group === 'archived' && CARE.sections.some(s => s.lane.id === 'archived' && s.items.length >= 2)`);
  await evalJs(`document.querySelector('#care-list input[data-pick-lane="archived"]').click()`);
  check("已搁置那一组也能全选(批量栏给的是「恢复」)",
        (await evalJs(pickedKeys)) === (await evalJs(laneKeys('archived')))
          && await evalJs(`$('care-batch').textContent.includes('恢复')`), await evalJs(pickedKeys));
  await evalJs(`document.querySelector('#care-list input[data-pick-key]').click()`);
  check("放掉一条之后节头和批量栏那两格都是半选",
        await evalJs(`(() => {
          const head = document.querySelector('#care-list input[data-pick-lane="archived"]');
          const all = document.querySelector('.care-batch-all input');
          return head.indeterminate && !head.checked && all.indeterminate && !all.checked;
        })()`));
  await evalJs(`document.querySelector('#care-list input[data-pick-lane="archived"]').click()`);
  check("半选时点节头那一格是补齐,不是清空",
        (await evalJs(pickedKeys)) === (await evalJs(laneKeys('archived'))), await evalJs(pickedKeys));

  // ── 10c-3. 全选越过单次请求上限 ─────────────────────────────────────────────
  // 全选让「勾 501 条」一点就到,而服务端每个写请求最多 500 条(`batch_max`,随概览
  // 给出)。已搁置那一组的取数和「恢复」都在页面里拦下,换成一份 501 条的样例库 ——
  // 验的是**前端拿到这个规模之后怎么发**,后端那道 422 由它自己的模型钉着。
  // 第一遍让第一批失败:失败那几条要照旧勾着,而且刷新之后**展开深度还在**
  // (退回前 40 条的话,批量栏只认屏幕上还在的勾选,其余 461 条的勾选当场蒸发)。
  // 第二遍全部成功:每一批都不超上限,合起来正好是勾中的那 501 条。
  await evalJs(`(async () => {
    const real = window.fetch;
    window.__careRealFetch = real;
    const tpl = await (await real('/api/intake-care/items?group=archived&offset=0&limit=1')).json();
    const sec = tpl.sections[0], row = sec.items[0];
    window.__careRows = Array.from({ length: 501 }, (_, i) => ({ ...row,
      key: 'bulk' + String(i).padStart(4, '0'), title: '批量样例 ' + i, title_zh: '批量样例 ' + i,
      shelf_decision_id: row.shelf_decision_id }));
    window.__careSent = [];
    window.__careFailFirst = true;
    window.fetch = async (input, init) => {
      const url = new URL(String(input instanceof Request ? input.url : input), location.origin);
      if (url.pathname === '/api/intake-care/items' && url.searchParams.get('group') === 'archived') {
        const off = +url.searchParams.get('offset') || 0, lim = +url.searchParams.get('limit') || 40;
        const rows = window.__careRows;
        window.__careGets = (window.__careGets || 0) + 1;
        return new Response(JSON.stringify({ sections: [{ ...sec, total: rows.length,
          items: rows.slice(off, off + lim) }] }), { headers: { 'Content-Type': 'application/json' } });
      }
      if (url.pathname === '/api/intake-care/shelf/release') {
        const body = JSON.parse(init.body);
        window.__careSent.push(body.keys.length);
        window.__careGets = 0;
        if (window.__careFailFirst) {
          window.__careFailFirst = false;
          return new Response('样例:这一批没成功', { status: 500 });
        }
        const gone = new Set(body.keys);
        window.__careRows = window.__careRows.filter(r => !gone.has(r.key));
        return new Response(JSON.stringify({ released: body.keys.length, asked: body.keys.length, detail: '' }),
          { headers: { 'Content-Type': 'application/json' } });
      }
      return real(input, init);
    };
  })()`);
  await evalJs(`careLoadSections()`);
  await until(`CARE.sections[0]?.total === 501`);
  await evalJs(`(async () => { const s = CARE.sections[0];
    while (s.items.length < s.total) { const n = s.items.length; await careMore('archived');
      if (CARE.sections[0].items.length === n) break; } })()`);
  await evalJs(`document.querySelector('#care-list input[data-pick-lane="archived"]').click()`);
  check("展开到 501 条之后节头全选勾满 501 条",
        await evalJs(`CARE.picked.size === 501 && CARE.overview.batch_max === 500`),
        await evalJs(`JSON.stringify([CARE.picked.size, CARE.overview.batch_max])`));
  await evalJs(`[...$('care-batch').querySelectorAll('button')].find(b => b.textContent === '恢复').click()`);
  // 等失败之后那一次重取**补页补完**(501 条要 13 页);只取了第一页就停的旧行为会卡在这里。
  await until(`window.__careSent.length >= 1 && window.__careGets >= 13`);
  await sleep(300);
  check("第一批失败:只发了一批、而且没超上限",
        await evalJs(`JSON.stringify(window.__careSent)`) === "[500]", await evalJs(`JSON.stringify(window.__careSent)`));
  check("失败之后 501 条照旧勾着,列表还展开在 501 条",
        await evalJs(`CARE.picked.size === 501 && CARE.sections[0].items.length === 501
          && document.querySelectorAll('#care-list input[data-pick-key]:checked').length === 501`),
        await evalJs(`JSON.stringify([CARE.picked.size, CARE.sections[0]?.items.length])`));
  await evalJs(`window.__careSent = []`);
  await evalJs(`[...$('care-batch').querySelectorAll('button')].find(b => b.textContent === '恢复').click()`);
  await until(`window.__careSent.length === 2 && CARE.picked.size === 0 && !CARE.sections.length`);
  check("全部成功:分成 500 + 1 两批依次发出,勾选清空、列表清空",
        await evalJs(`JSON.stringify(window.__careSent)`) === "[500,1]"
          && await evalJs(`CARE.picked.size === 0 && $('care-batch').hidden`),
        await evalJs(`JSON.stringify([window.__careSent, CARE.picked.size, CARE.sections.length])`));
  await evalJs(`window.fetch = window.__careRealFetch`);
  // 立即执行**不分批**(管道一次只跑一轮):超过上限时那颗按钮禁用、说明原因。
  // 用真实那几条走一遍,只把上限临时压到 1 —— 判的是「勾中数 > 服务端给的上限」。
  await evalJs(`careSetGroup('attention')`);
  await until(`CARE.group === 'attention' && CARE.sections.length > 0
               && CARE.sections.every(s => s.lane.group === 'attention')`);
  // ── 10c-4. 分批途中改了选择:后面几批的身份不许跟着丢 ─────────────────────────
  // 第一批在路上时人点了「清除选择」,又重新勾上第一批里的一条。第二批要带着**点下
  // 去那一刻**它那一世(`lives`);服务端对空 `life` 是放行的,丢了它这一下就会落到
  // 同一链接后来重新收进来的那一条上。第一批回来时也不许把人新勾的那一条抹掉 ——
  // 那是一次新的选择。正文重试已停止那一档的取数和「仅重新排队」都在页面里拦下。
  await evalJs(`(async () => {
    const real = window.__careRealFetch || window.fetch;
    window.__careRealFetch = real;
    const tpl = await (await real('/api/intake-care/items?lane=body_exhausted&offset=0&limit=1')).json();
    const sec = tpl.sections[0], row = sec.items[0];
    window.__careRows = Array.from({ length: 501 }, (_, i) => ({ ...row,
      key: 'race' + String(i).padStart(4, '0'), life: 'life-old-' + i,
      title: '身份样例 ' + i, title_zh: '身份样例 ' + i }));
    window.__careBodies = [];
    window.__careGets = 0;
    let open;
    window.__careGateWait = new Promise(r => { open = r; });
    window.__careGate = open;
    window.fetch = async (input, init) => {
      const url = new URL(String(input instanceof Request ? input.url : input), location.origin);
      if (url.pathname === '/api/intake-care/items' && url.searchParams.get('lane') === 'body_exhausted') {
        const off = +url.searchParams.get('offset') || 0, lim = +url.searchParams.get('limit') || 40;
        window.__careGets += 1;
        if (off >= (window.__careFailFrom ?? Infinity)) return new Response('样例:补页失败', { status: 500 });
        return new Response(JSON.stringify({ sections: [{ ...sec, total: window.__careRows.length,
          items: window.__careRows.slice(off, off + lim) }] }), { headers: { 'Content-Type': 'application/json' } });
      }
      if (url.pathname === '/api/intake-care/requeue') {
        const body = JSON.parse(init.body);
        window.__careBodies.push(body);
        if (window.__careBodies.length === 1) await window.__careGateWait;
        window.__careGets = 0;
        return new Response(JSON.stringify({ requeued: body.keys.length, detail: '' }),
          { headers: { 'Content-Type': 'application/json' } });
      }
      return real(input, init);
    };
  })()`);
  await evalJs(`careLoadSections()`);
  await until(`CARE.sections.find(s => s.lane.id === 'body_exhausted')?.total === 501`);
  await evalJs(`(async () => { const s = () => CARE.sections.find(x => x.lane.id === 'body_exhausted');
    while (s().items.length < s().total) { const n = s().items.length; await careMore('body_exhausted');
      if (s().items.length === n) break; } })()`);
  await evalJs(`document.querySelector('#care-list input[data-pick-lane="body_exhausted"]').click()`);
  await until(`CARE.picked.size === 501`);
  await evalJs(`[...$('care-batch').querySelectorAll('button')].find(b => b.textContent === '仅重新排队').click()`);
  await until(`!!document.querySelector('.confirm-mask [data-act=ok]')`);
  await evalJs(`document.querySelector('.confirm-mask [data-act=ok]').click()`);
  await until(`window.__careBodies.length === 1`);
  await evalJs(`[...$('care-batch').querySelectorAll('button')].find(b => b.textContent === '清除选择').click()`);
  await evalJs(`document.querySelector('#care-list input[data-pick-key="race0000"]').click()`);
  const midway = await evalJs(`JSON.stringify([CARE.picked.size, CARE.picked.has('race0000')])`);
  await evalJs(`window.__careGate()`);
  await until(`window.__careBodies.length === 2 && window.__careGets >= 13`);
  await sleep(300);
  const second = JSON.parse(await evalJs(`JSON.stringify(window.__careBodies[1] || null)`));
  check("等待中清除了选择:第二批照旧带着点下去那一刻那一世",
        midway === "[1,true]" && second && JSON.stringify(second.keys) === '["race0500"]'
          && second.lives?.race0500 === 'life-old-500',
        `${midway} ${JSON.stringify(second)}`);
  check("第一批回来时不抹掉人等待中新勾的那一条",
        await evalJs(`CARE.picked.size === 1 && CARE.picked.has('race0000')
          && document.querySelector('#care-list input[data-pick-key="race0000"]')?.checked`),
        await evalJs(`JSON.stringify([...CARE.picked.keys()])`));
  // 刷新时补页半路失败:已经取回的那几页留着、说一句,不把整张列表清空
  // (清空的话批量栏还挂着上一屏的勾选和按钮)。
  await evalJs(`window.__careFailFrom = 80; careLoadSections()`);
  await until(`[...document.querySelectorAll('.toast')].some(t => t.textContent.includes('只取回了前 80 条'))`);
  await sleep(200);
  check("补页半路失败:取回的前 80 条照旧列着,勾着的那一条还在",
        await evalJs(`CARE.sections.find(s => s.lane.id === 'body_exhausted')?.items.length === 80
          && document.querySelectorAll('#care-list input[data-pick-key^="race"]').length === 80
          && CARE.picked.has('race0000') && !$('care-batch').hidden`),
        await evalJs(`JSON.stringify([CARE.sections.find(s => s.lane.id === 'body_exhausted')?.items.length, CARE.picked.size])`));
  await evalJs(`window.fetch = window.__careRealFetch; CARE.picked.clear(); careLoadSections()`);
  await until(`CARE.sections.every(s => s.total < 501)`);
  const runCap = JSON.parse(await evalJs(`(() => {
    const rows = CARE.sections.filter(s => (s.lane.exits || []).includes('run'))
      .flatMap(s => carePickableRows(s).map(r => [r, s.lane]));
    const kind = rows.map(([r]) => r.kind).find((k, i, all) => all.indexOf(k) !== i);
    const two = rows.filter(([r]) => r.kind === kind).slice(0, 2);
    for (const [r, lane] of two) careSetPicked(r, lane, true);
    const keep = CARE.overview.batch_max;
    const button = () => [...$('care-batch').querySelectorAll('button')].find(b => b.textContent === '立即执行已选');
    careRenderBatch();
    const under = button() && !button().disabled;
    CARE.overview.batch_max = 1;
    careRenderBatch();
    const over = button() ? { disabled: button().disabled, title: button().title } : null;
    CARE.overview.batch_max = keep;
    CARE.picked.clear(); careRenderBatch();
    return JSON.stringify({ picked: two.length, under, over });
  })()`));
  check("勾中数超过上限时「立即执行已选」禁用并说明;没超时照旧能点",
        runCap.picked === 2 && runCap.under && runCap.over?.disabled && /最多 1 条/.test(runCap.over.title),
        JSON.stringify(runCap));
  // 键盘:在批量栏那一格上按空格,批量栏整个重建之后焦点要还在那一格上。
  await evalJs(`document.querySelector('#care-list input[data-pick-lane]').click()`);
  await evalJs(`document.querySelector('.care-batch-all input').focus()`);
  await send("Input.dispatchKeyEvent", { type: "keyDown", key: " ", code: "Space", text: " ", windowsVirtualKeyCode: 32 });
  await send("Input.dispatchKeyEvent", { type: "keyUp", key: " ", code: "Space", windowsVirtualKeyCode: 32 });
  await sleep(200);
  check("键盘在批量栏全选那一格上按空格,焦点留在那一格",
        await evalJs(`document.activeElement === document.querySelector('.care-batch-all input')
          && document.activeElement.checked`),
        await evalJs(`document.activeElement?.outerHTML?.slice(0, 80) || '(没有焦点)'`));
  await evalJs(`CARE.picked.clear(); careRenderBatch()`);
  // 回到真实的已搁置那两条、勾上,再切分组 —— 下面那条「切分组清空」问的才不是空集。
  await evalJs(`careSetGroup('archived')`);
  await until(`CARE.group === 'archived' && CARE.sections.some(s => s.lane.id === 'archived' && s.items.length >= 2)`);
  await evalJs(`document.querySelector('#care-list input[data-pick-lane="archived"]').click()`);
  await until(`CARE.picked.size >= 2`);
  await evalJs(`careSetGroup('attention')`);
  await until(`CARE.group === 'attention' && CARE.sections.every(s => s.lane.group === 'attention')`);
  check("切分组之后勾选照旧清空(全选不把上一组的带过来)",
        await evalJs(`CARE.picked.size === 0`));
}
