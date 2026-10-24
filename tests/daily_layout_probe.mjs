import assert from 'node:assert/strict';
import { mkdirSync, writeFileSync } from 'node:fs';
import { attachCdp } from './pipe_history_cdp.mjs';

const base = process.argv[2];
const { proc, ws, send, evalJs, until, pageErrors } = await attachCdp();
const passed = [];
const check = async (name, expression) => {
  assert.ok(await evalJs(expression), name);
  passed.push(name);
};
const body = `标题前条目甲
标题前条目乙

## 今日要点

只有一句话的普通段落。

## 工具与项目

Graphify — **为代码库构建知识图谱** [1]
\`Cloudflare\` — 协调安全审计 Agent [1]${'  '}
GrokBot — 实时查看执行过程 [1]

- 已有列表甲
- 已有列表乙

已有硬换行${'  '}
下一行

\`inline code\` 示例第一行
示例第二行

<code>代码内部第一行
代码内部第二行</code>

\`\`\`text
代码第一行
代码第二行
\`\`\`

| 名字 | 说明 |
| --- | --- |
| 表格工具 | 表格说明 |

## 观点与讨论

观点第一行
观点第二行

## 一句话速览

速览甲 [1]
速览乙 [1]

## 补录（一次性清空积压）

补录甲 [1]
补录乙 [1]

### 自定义子节

子节甲 [1]
子节乙 [1]

## 任意新标题

新节甲
新节乙

**跨行粗体甲
跨行粗体乙**

[跨行链接甲
跨行链接乙](https://example.org/multiline)

## 编辑备注

仍是单段说明。`;
const materials = '\n\n## 素材清单(1 条)\n\n1. [原文只在原版清单里的长标题尾部](https://example.org/layout) — RSS·作者';
const reportFor = mode => ({ date: '2026-10-17', body: body + materials,
  ...(mode === '旧接口' ? {} : { material_view: {
    before: mode === '插槽降级' ? '```text\n' : body,
    after: '', heading: '素材清单(1 条)', items: [
      { title: '原文', full_title: '原文只在原版清单里的长标题尾部',
        source: 'RSS·作者', url: 'https://example.org/layout' }
    ]
  } }) });
const screenshot = async name => {
  const directory = process.env.KB_DAILY_LAYOUT_SCREENSHOTS;
  if (!directory) return;
  mkdirSync(directory, { recursive: true });
  const shot = await send('Page.captureScreenshot', { format: 'png' });
  writeFileSync(`${directory}/daily-layout-${name}.png`, Buffer.from(shot.result.data, 'base64'));
};

try {
  await send('Page.navigate', { url: `${base}/#daily` });
  assert.ok(await until(`document.querySelector('.daily-materials')`), '真实日报页面已加载');
  for (const mode of ['结构化清单', '旧接口', '插槽降级']) {
    await evalJs(`(() => {
      renderDailyReportBody(${JSON.stringify(reportFor(mode))});
      window.layoutHost = document.getElementById('daily-reader-body');
      window.layoutList = [...layoutHost.children].find(block => block.textContent.startsWith('Graphify'));
      window.layoutTextTop = text => {
        const walker = document.createTreeWalker(layoutHost, NodeFilter.SHOW_TEXT);
        for (let node = walker.nextNode(); node; node = walker.nextNode()) {
          const offset = node.nodeValue.indexOf(text);
          if (offset < 0) continue;
          const range = document.createRange();
          range.setStart(node, offset);
          range.setEnd(node, offset + text.length);
          return range.getClientRects()[0].top;
        }
        return null;
      };
    })()`);
    await check(`${mode}：行内代码与混合软硬换行不再否决整段`, `layoutList.tagName === 'UL'
      && layoutList.children.length === 3 && layoutList.querySelector('code').textContent === 'Cloudflare'
      && layoutList.querySelector('strong').textContent === '为代码库构建知识图谱'
      && !layoutList.querySelector('br')`);
    await check(`${mode}：标题变体、观点、新标题和子标题使用相同段落规则`, `(() => {
      return ['标题前条目甲', '观点第一行', '速览甲', '补录甲', '子节甲', '新节甲'].every(prefix =>
        [...layoutHost.children].some(block => block.tagName === 'UL'
          && block.classList.contains('daily-line-items') && block.children.length === 2
          && block.textContent.startsWith(prefix)));
    })()`);
    await check(`${mode}：真实列表有项目符号、悬挂缩进和条目间距`, `(() => {
      const rows = [...layoutList.children];
      return rows.every((row, index) => getComputedStyle(row).display === 'list-item'
        && getComputedStyle(row).listStyleType !== 'none'
        && row.getBoundingClientRect().left > layoutList.getBoundingClientRect().left + 10
        && (!index || row.getBoundingClientRect().top >= rows[index - 1].getBoundingClientRect().bottom + 8));
    })()`);
    await check(`${mode}：单行段落、已有列表、代码内部和表格不拆分`, String.raw`(() => {
      return [...layoutHost.children].some(block => block.tagName === 'P'
          && block.textContent === '只有一句话的普通段落。')
        && layoutHost.querySelectorAll('ul:not(.daily-line-items) > li').length === 2
        && layoutHost.querySelector('p > code').textContent === '代码内部第一行\n代码内部第二行'
        && layoutHost.querySelector('pre code').textContent === '代码第一行\n代码第二行\n'
        && layoutHost.querySelectorAll('table tbody tr').length === 1;
    })()`);
    await check(`${mode}：跨行粗体和链接按 DOM 边界保留`, `(() => {
      const bold = [...layoutHost.querySelectorAll('ul.daily-line-items')].find(list => list.textContent.startsWith('跨行粗体甲'));
      const links = [...layoutHost.querySelectorAll('ul.daily-line-items')].find(list => list.textContent.startsWith('跨行链接甲'));
      return bold.children.length === 2 && [...bold.children].every(row => row.querySelector('strong'))
        && links.children.length === 2 && [...links.children].every(row =>
          row.querySelector('a').href === 'https://example.org/multiline');
    })()`);
    await check(`${mode}：出处链接保留正确地址`, `layoutList.querySelectorAll('a.daily-ref').length === 3
      && [...layoutList.querySelectorAll('a.daily-ref')].every(link =>
        link.href === 'https://example.org/layout' && link.target === '_blank')`);
    await check(`${mode}：原始文字、收藏坐标及已有锚点保持不变`, `(() => {
      const raw = document.createElement('div');
      raw.innerHTML = mdHtml(${JSON.stringify(body)});
      layoutHost.append(raw);
      const text = raw.textContent;
      const index = snipTextIndex(raw).text;
      const quote = '为代码库构建知识图谱 [1] Cloudflare';
      const offset = index.indexOf(quote);
      const spec = { q: quote, p: index.slice(Math.max(0, offset - 30), offset),
        s: index.slice(offset + quote.length, offset + quote.length + 30), e: 1, u: 1 };
      preserveDailyLineItems(raw);
      const lists = [...raw.querySelectorAll('ul.daily-line-items')];
      preserveDailyLineItems(raw);
      const result = offset >= 0 && raw.textContent === text && snipTextIndex(raw).text === index
        && locateSnip(raw, spec) && lists.every(list => list.isConnected)
        && !raw.querySelector('ul ul');
      raw.remove();
      return result;
    })()`);
  }
  await evalJs(`(() => {
    renderDailyReportBody(${JSON.stringify(reportFor('结构化清单'))});
    const raw = document.createElement('div');
    raw.innerHTML = mdHtml(${JSON.stringify(body + materials)});
    layoutHost.append(raw);
    const text = snipTextIndex(raw).text;
    const quote = '只在原版清单里的长标题尾部';
    const offset = text.indexOf(quote);
    const spec = { q: quote, p: text.slice(offset - 30, offset),
      s: text.slice(offset + quote.length, offset + quote.length + 30), e: 1, u: 1 };
    raw.remove();
    consumeDailySnipJump({ ref: '2026-10-17', spec }, '2026-10-17', layoutHost);
  })()`);
  await check('收藏回跳的原版正文也有相同工具列表和有效高亮', `(() => {
    const context = layoutHost.querySelector('.daily-legacy-context');
    const body = context?.querySelector('div[data-snip-ref]');
    const list = body && [...body.children].find(block => block.textContent.startsWith('Graphify'));
    const ranges = CSS.highlights.get('kb-quote');
    return context?.open && list?.tagName === 'UL' && list.children.length === 3
      && ranges && [...ranges][0].toString() === '只在原版清单里的长标题尾部';
  })()`);
  await evalJs(`renderDailyReportBody(${JSON.stringify(reportFor('结构化清单'))})`);
  await evalJs(`document.querySelector('ul.daily-line-items').scrollIntoView({ block: 'center' })`);
  await screenshot('desktop');
  await send('Emulation.setDeviceMetricsOverride', {
    width: 390, height: 844, deviceScaleFactor: 1, mobile: false
  });
  await check('手机端长条目折行后仍有悬挂缩进及间距，无横向溢出', `(() => {
    const list = [...layoutHost.children].find(block => block.textContent.startsWith('Graphify'));
    list.firstElementChild.firstChild.nodeValue = 'Graphify — ' + '很长的项目用途说明'.repeat(12) + ' ';
    const rows = [...list.children];
    list.scrollIntoView({ block: 'start' });
    window.scrollBy(0, -80);
    return rows[0].getBoundingClientRect().height > 100
      && rows.every((row, index) => !index || row.getBoundingClientRect().top
        >= rows[index - 1].getBoundingClientRect().bottom + 8)
      && document.documentElement.scrollWidth <= innerWidth;
  })()`);
  await screenshot('mobile');
  assert.deepEqual(pageErrors, [], '页面没有 JavaScript 异常');
  console.log(JSON.stringify({ passed, errors: pageErrors }));
} finally {
  ws.close();
  proc.kill();
}
