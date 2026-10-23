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
const body = `## 今日要点

普通段落第一行
普通段落第二行

## 工具与项目

Graphify — 为代码库构建知识图谱 [1]
Cloudflare — 协调安全审计 Agent [1]
GrokBot — 实时查看执行过程 [1]

- 已有列表甲
- 已有列表乙

已有硬换行${'  '}
下一行

\`inline code\` 示例第一行
示例第二行

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

## 补录

补录甲 [1]
补录乙 [1]

## 编辑备注

备注第一行
备注第二行`;
const materials = '\n\n## 素材清单(1 条)\n\n1. [原文](https://example.org/layout) — RSS·作者';

try {
  await send('Page.navigate', { url: `${base}/#daily` });
  assert.ok(await until(`document.querySelector('.daily-materials')`), '真实日报页面已加载');
  for (const structured of [true, false]) {
    const report = { date: '2026-10-17', body: body + materials };
    if (structured) report.material_view = {
      before: body, after: '', heading: '素材清单(1 条)', items: [
        { title: '原文', full_title: '原文', source: 'RSS·作者', url: 'https://example.org/layout' }
      ]
    };
    await evalJs(`(() => {
      renderDailyReportBody(${JSON.stringify(report)});
      window.layoutHost = document.getElementById('daily-reader-body');
      window.layoutText = layoutHost.textContent;
      window.layoutIndex = snipTextIndex(layoutHost).text;
      window.layoutParagraph = [...layoutHost.children].find(block =>
        block.tagName === 'P' && block.textContent.startsWith('Graphify'));
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
    const mode = structured ? '结构化清单' : '旧接口';
    await check(`${mode}：连续工具条目各占一行`, `(() => {
      const tops = ['Graphify', 'Cloudflare', 'GrokBot'].map(layoutTextTop);
      return tops.every((top, index) => top !== null && (!index || top > tops[index - 1] + 10));
    })()`);
    await check(`${mode}：速览与补录也保留逐行布局`, `['速览', '补录'].every(prefix =>
      layoutTextTop(prefix + '乙') > layoutTextTop(prefix + '甲') + 10)`);
    await check(`${mode}：普通段落和其他小节保持标准 Markdown`, `(() => {
      const paragraphs = [...layoutHost.children].filter(block => block.tagName === 'P');
      return ['普通段落', '观点', '备注'].every(prefix => paragraphs.some(block =>
        block.textContent.startsWith(prefix) && getComputedStyle(block).whiteSpace === 'normal'));
    })()`);
    await check(`${mode}：不改已有列表、硬换行、代码或表格`, `(() => {
      return layoutHost.querySelectorAll('ul > li').length === 2
        && layoutHost.querySelectorAll('br').length === 1
        && [...layoutHost.querySelectorAll('br, code, table, li')].every(block =>
          !block.closest('.daily-line-items'));
    })()`);
    await check(`${mode}：出处链接保留正确地址`, `layoutParagraph.querySelectorAll('a.daily-ref').length === 3
      && [...layoutParagraph.querySelectorAll('a.daily-ref')].every(link =>
        link.href === 'https://example.org/layout' && link.target === '_blank')`);
    await check(`${mode}：文本、收藏坐标与重复渲染不变`, `(() => {
      layoutHost.querySelectorAll('.daily-line-items').forEach(block => block.classList.remove('daily-line-items'));
      const before = snipTextIndex(layoutHost).text;
      preserveDailyLineItems(layoutHost);
      preserveDailyLineItems(layoutHost);
      return before === layoutIndex && snipTextIndex(layoutHost).text === layoutIndex
        && layoutHost.textContent === layoutText;
    })()`);
  }
  await send('Emulation.setDeviceMetricsOverride', {
    width: 390, height: 844, deviceScaleFactor: 1, mobile: false
  });
  await check('手机端长条目换行后，下一条仍独立起行且无横向溢出', `(() => {
    layoutParagraph.firstChild.nodeValue = 'Graphify — ' + '很长的项目用途说明'.repeat(12) + ' ';
    return layoutTextTop('Cloudflare') > layoutTextTop('Graphify') + 40
      && layoutTextTop('GrokBot') > layoutTextTop('Cloudflare') + 10
      && document.documentElement.scrollWidth <= innerWidth;
  })()`);
  const artifacts = process.env.KB_DAILY_LAYOUT_SCREENSHOTS;
  if (artifacts) {
    mkdirSync(artifacts, { recursive: true });
    await evalJs(`layoutParagraph.previousElementSibling.scrollIntoView({ block: 'start' })`);
    const shot = await send('Page.captureScreenshot', { format: 'png', captureBeyondViewport: true });
    writeFileSync(`${artifacts}/daily-layout-mobile.png`, Buffer.from(shot.result.data, 'base64'));
  }
  assert.deepEqual(pageErrors, [], '页面没有 JavaScript 异常');
  console.log(JSON.stringify({ passed, errors: pageErrors }));
} finally {
  ws.close();
  proc.kill();
}
