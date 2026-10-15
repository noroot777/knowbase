// 定时配置的共享状态层。**界面上那几句说明只做派生展示,不各自缓存一份。**
//
// 原来「这一类来源什么时候会自动同步」在 source_forms.js 里自己缓存一份:另一处
// 保存成功之后这份缓存既不失效、常规来源轮询也不重读它 —— 于是同一页上两句话各说
// 一套(一处写「每天 07:30」、另一处写「目前不会自动同步」)。留在页面上等它自己刷
// 也不会好,只有整页刷新才对得上。
//
// 所以口径收在这里:**谁保存成功都把新配置交到这一层**(`schedulesSaved`),这一层
// 负责通知全部派生展示(`watchSchedules`)。**读取走另一道门**(`schedulesRead`,代次
// 占在发请求之前):在途的旧回包不许把新配置倒退成旧配置。
//
// **定时只剩日报这一条了**(页头那颗「定时同步」连同它的接口一起删了,见
// `docs/decisions/sync-schedule.md`),但这一层**按条记**的形状原样留着:表单一处、
// 派生展示若干,代次、三态、解包这几件事一条也省不掉;再加一条定时时也不用重接。
// 包形的差异就在这里解一次:`/api/daily/settings` 的 GET 外面套了一层
// `{settings, channels, default_channels}`(外层那个 channels 是可选来源目录,不是
// 待同步名单),它的 POST 回的却是扁的。把外层当设置对象读的话 `enabled` 永远取不
// 到,开着的定时会被一律说成「未启用」。

// null = 还没读过(别替它报"没开");每条的 null = 这条读不到,也不是"没开"。
let schedCache = null;
// 代次按条算:一条的新读取/保存不许把另一条的在途回包连带作废。
const schedSeq = { daily: 0 };
const schedWatchers = [];

/** 订阅定时配置的变化。注册时不立即回调 —— 调用方自己先画一次当前值。 */
function watchSchedules(fn) { schedWatchers.push(fn); }

/** 当前这份定时配置;null = 还没读过。 */
function schedules() { return schedCache; }

function schedulesApply(next) {
  schedCache = {
    daily: next.daily || null,
    // 读不到 ≠ 没开。每一条都读到了,派生展示才敢说「不会自动同步」。
    unknown: next.daily ? [] : ['日报'],
  };
  // 一个派生展示抛了不该带走别的(面板上那几句互不相干)。
  for (const fn of schedWatchers) { try { fn(schedCache); } catch (e) { console.error(e); } }
  return schedCache;
}

/**
 * 某一条定时保存成功了:只换它那一份,另一条原样留着(含"读不到"那一档)。
 * 推它自己的代次 —— 刚保存的这一份比任何在途读取都新。
 */
function schedulesSaved(which, settings) {
  schedSeq[which]++;
  return schedulesApply({ ...(schedCache || {}), [which]: settings || null });
}

/**
 * **要去读了:先占代次,回包落地时交给 `accept` 结账。**
 *
 * `accept({daily?})`(读失败就给 null)做两件事,**而且只做一次**:
 * ① 按代次决定这趟回包要不要写进共享事实 —— 被**更晚的读取**或**期间的保存**
 *    顶掉就不写(保存也推同一个代次;按条算,一条被顶掉不许连带作废另一条);
 * ② 返回**当前已确认的事实**(只含这趟要的那几条),可能是别人刚写进去的那一份。
 *
 * **调用方一律照返回值画,不许照自己那份回包画。** 这是第 2 轮双页面实测那条:
 * 另一份页面在两次读取之间保存成功,被顶掉的那趟照着自己的回包画,就把已经显示
 * 出来的新事实退回旧事实(开关变回未勾选、提示写「定时生成未启用」),而共享事实
 * 拒绝退回 —— 同一页上来源说明还承诺每天同步,两处当场打架。"两趟读到的本来是
 * 同一个事实"这个假设不成立:中间可以有一次成功的保存。
 * - 某条回 null = 这条当前"不知道"(最新那次读取失败了):调用方走它自己的
 *   读取失败那条路(保留上次的值 + 重读按钮),不许拿一个旧值冒充当前事实。
 * - **用户正在编辑的草稿另算**:草稿不进代次(没保存就没有新事实),各消费方
 *   自己那个 dirty/saving 标志要跟这道门一起判。
 */
function schedulesRead(...keys) {
  const seq = {};
  for (const key of keys) seq[key] = ++schedSeq[key];
  let settled = false;
  return answers => {
    // 一趟读取只结账一次:异常分支里再调一次会把刚写进去的好值盖成"不知道"。
    if (!settled) {
      settled = true;
      const next = { ...(schedCache || {}) };
      let fresh = false;
      for (const key of keys) {
        // **被更晚的读取、或者期间的一次保存顶掉就不写**(保存也推这同一个代次,
        // 所以一个判据就够:单独再数一遍保存次数是冗余的,实测注入去掉那一条
        // 红 0 条)。按条算:一条被顶掉不许连带作废另一条。
        if (seq[key] !== schedSeq[key]) continue;
        next[key] = answers[key] || null;
        fresh = true;
      }
      if (fresh) schedulesApply(next);
    }
    const fact = {};
    for (const key of keys) fact[key] = schedCache ? schedCache[key] : null;
    return fact;
  };
}

/**
 * 一张定时表单这一趟读取该落到哪个**终态**。判据收在这里。
 *
 * **配置可用 ≠ 这张表单的依赖都可用。** 它们除了定时本身还要一份「可勾选的来源
 * 目录」,而目录和设置是**两趟请求、各自会失败**。只看配置就进"已加载"的后果(第 4 轮
 * 真实鼠标实测):弹窗写着「已保存:每天 07:30 自动同步」、字段开放、重读藏着,而来源
 * 一个都没有 —— 用户关掉定时去保存,只换来一句「请至少选择一个同步来源」,一个都选不
 * 了、也没法重读,关掉重开又被草稿守卫挡住,只能刷新整页。
 *
 * 四个终态,只有这四个:
 * - `saving` 保存那条路自己会收尾,这里一个字都不许动;
 * - `draft` 用户有未保存的改动,优先保住它(草稿不进代次:没保存就没有新事实);
 * - `ready` 配置和依赖都齐了,这才允许进"已加载";
 * - `unknown` 当前不知道 —— `missing` 非空时是"定时读到了、某份依赖没读到",调用方
 *   照它说清是哪一份,别把"没得选"说成"请选一个"。
 */
function scheduleSettle({ fact, deps = [], saving, dirty }) {
  if (saving) return { state: 'saving' };
  if (dirty) return { state: 'draft' };
  if (!fact) return { state: 'unknown', missing: '' };
  const gap = deps.find(d => !d.ready);
  return gap ? { state: 'unknown', missing: gap.what } : { state: 'ready' };
}

/** 每条定时各读一次。读不到的那条留 null(= 不知道),不当成"没开"。 */
async function loadSchedules() {
  const accept = schedulesRead('daily');
  const ask = async (url, pick) => { try { return pick(await pipeJson(url)); } catch { return null; } };
  const [daily] = await Promise.all([
    ask('/api/daily/settings', d => d.settings),
  ]);
  accept({ daily });
  return schedCache;
}
