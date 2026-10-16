// 采集 → 核验 → 落库 这条链上,**采集那一刻算出来的事实不许在半路丢掉**。
//
// 载的是仓库里那两份**生产**代码(`snip_text.js` + `snip.js`),只补几个和这件事
// 无关的桩(事件注册、console)—— 不另写一份"差不多的"匹配算法:这条链出事的
// 形态正是两头各算一遍、算出来的数不一样,自己再写一份就把要验的那件事验没了。
//
// 判据落在**后果**上:`snipVerified` 返回的 `reason`、`fix.anchored`(会原样落进
// `favorites.anchor_exact`)、以及带出去的那个版本号(后端那道 409 全靠它)——
// 不落在"传了几个字段"。每一格都配着反过来的一边。
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import vm from 'node:vm';

const root = new URL('../kb/static/js/', import.meta.url);
const box = vm.createContext({ console, document: { addEventListener() {} },
                               addEventListener() {} });
for (const name of ['snip_text.js', 'snip.js'])
  vm.runInContext(readFileSync(new URL(name, root), 'utf8'), box, { filename: name });
const run = code => vm.runInContext(code, box);

// 两份语料:`shown` 是屏幕上那一份(两节各写着同一句引文,两侧各 220 字一字不差
// —— 超过采集的最大窗口 200 字,所以采集那一刻谁也说不出用户选的是第几处),
// `one` 是只有一处的那一份。引文、前后文一律由生产的 `snipContext` 现算,不手写
// "歧义状态"。
run(`
  const pad = '甲乙丙丁戊己庚辛壬癸'.repeat(22);
  const quote = 'AmbiguousSelection';
  const block = pad + ' ' + quote + ' ' + pad;
  const shown = '第一节 ' + block + ' 第二节 ' + block;
  const one = '序言 ' + block + ' 收尾';
  const specAt = (text, at, unique) => {
    const c = snipContext(text, at, at + quote.length);
    return { quote, prefix: c.prefix, suffix: c.suffix, unique, locatable: c.exact };
  };
  // 选的是**第二节**那一处(屏幕上那一份里的后一处)
  const ambiguous = specAt(shown, shown.lastIndexOf(quote), false);
  const solo = specAt(one, one.indexOf(quote), true);
  const disk = (text, version) => async () => ({ text, version });
  const verify = (src, spec) => snipVerified(src, spec);
`);
// 回来的对象是 VM 那个 realm 造的,原型不是本 realm 的 Object.prototype ——
// `deepEqual` 会在那上面红。摊成本地对象再比。
const outcome = async code => ({ ...await run(
  `(async () => { const v = await ${code};
     return { reason: v.reason, anchored: !!(v.fix && v.fix.anchored),
              version: v.src.doc_version, fell: !!v.fell }; })()`) });

test('采集就分不出两处的锚点,跨版核对不许靠「现在只剩一处」升级成定得住', async () => {
  // 采集如实给出"分不出"
  assert.equal(await run('ambiguous.locatable'), false);
  assert.equal(await run('ambiguous.unique'), false);
  // 页面不刷新,盘上把**用户选的那一节**整节删掉:剩下的那一处是他从没选过的第一节
  const got = await outcome(`verify(
    { origin: 'doc', path: 'f.md', doc_version: 'old',
      docText: disk('第一节 ' + block, 'new') }, ambiguous)`);
  assert.equal(got.reason, 'mismatch');
  assert.equal(got.anchored, false);
  // **版本号照旧是面板那一版** —— 后端那道 409(「面板旧了,刷新再收藏」)只靠
  // 「面板这一版 ≠ 盘上这一版」判得出来,换成新那一版它就永远说不出口了
  assert.equal(got.version, 'old');
});

test('这一道拒绝来自采集那一刻的事实,不是盘上那一份找不到', async () => {
  // 同一份盘上文本、同一条锚点,**只少传采集那一刻的"分不出"**:两侧各唯一命中
  // 剩下那一处,阶梯当场采信([起点, 终点])。所以上一格那条红只能由这个事实
  // 传到位来消 —— 不是因为这段话在盘上压根没有了(那是另一格)。
  const hit = await run(`snipSeek('第一节 ' + block,
    { ...snipSpecOf(ambiguous), u: 0, same: false })`);
  assert.ok(Array.isArray(hit) && hit.length === 2, JSON.stringify(hit));
  const refused = await run(`snipSeek('第一节 ' + block,
    { ...snipSpecOf(ambiguous), u: 0, e: 0, same: false })`);
  assert.equal(refused, null);
});

test('同一份语料的正常全文核对照旧放行', async () => {
  assert.equal(await run('solo.locatable'), true);
  const got = await outcome(`verify(
    { origin: 'doc', path: 'f.md', doc_version: 'v1', docText: disk(one, 'v1') }, solo)`);
  assert.deepEqual(got, { reason: '', anchored: true, version: 'v1', fell: false });
});

test('盘上末尾补了一段,照旧核得过并绑新那一版', async () => {
  const got = await outcome(`verify(
    { origin: 'doc', path: 'f.md', doc_version: 'v1',
      docText: disk(one + '\\n\\n后来又补了一段', 'v2') }, solo)`);
  assert.deepEqual(got, { reason: '', anchored: true, version: 'v2', fell: false });
});

test('同一份语料上这一道一分钱不要:两处都还在,本来就说不清', async () => {
  // 采集分不出、而盘上**一个字没改**:两侧各命中两处、取交集还是两处,阶梯自己
  // 就判说不清。所以上面那道拦住的只有「原处没了、剩下一处别处」,没有连带把
  // 同版核对判死(这一格在改之前就是这个结果,改之后一字没变)。
  const got = await outcome(`verify(
    { origin: 'doc', path: 'f.md', doc_version: 'v1', docText: disk(shown, 'v1') },
    ambiguous)`);
  assert.equal(got.reason, 'mismatch');
  assert.equal(got.anchored, false);
  assert.equal(got.version, 'v1');
});

test('切片说「这一截里分不出」是未知,不替整篇作答', async () => {
  // 列表卡、搜索卡摊的只是那一篇的一截(`partial: true`,搜索那一截还是建索引那
  // 一刻切下来的),切片里分不出可能只是前后文窗口被切片边界截断了 —— 那是**未
  // 知**,不是"已证明分不出",不许拿它把整篇的核对封死。整篇那一份此刻能证明
  // 位置的,照旧核得过,并按整篇重算唯一性与定位(这一档的去留另有裁定,这里只
  // 钉住"未知不当成已证明")。
  const got = await outcome(`verify(
    { origin: 'doc', path: 'f.md', doc_version: '', partial: true,
      docText: disk(one, 'v9') }, ambiguous)`);
  assert.deepEqual(got, { reason: '', anchored: true, version: 'v9', fell: false });
});

test('核不过时有退路的那一类退回来源,而不是悄悄存成功', async () => {
  const got = await outcome(`verify(
    { origin: 'doc', path: 'f.md', doc_version: 'old', fallback: { origin: 'feed', origin_ref: '7' },
      docText: disk('第一节 ' + block, 'new') }, ambiguous)`);
  assert.equal(got.reason, 'mismatch');
  assert.equal(got.fell, true);
  // 退回来源那一条按屏幕那一份存,而屏幕那一份说的正是"定不住"
  assert.equal(await run('ambiguous.locatable'), false);
});
