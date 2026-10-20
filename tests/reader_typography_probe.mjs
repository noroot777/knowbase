import { mkdirSync, writeFileSync } from 'node:fs';
import {
  base, boot, check, evalJs, finish, fx, js, pageErrors, send, until,
} from './snip_probe_lib.mjs';

await boot();
await evalJs(`switchTab('subs'); true`);
if (!await until(`!!document.getElementById('subs-switch-rss')`, 25000))
  throw new Error('RSS 入口未加载');
await evalJs(`document.getElementById('subs-switch-rss').click(); true`);
if (!await until(`sourceRows.length >= 3`, 20000)) throw new Error('订阅列表未加载');

const markdown = [
  '# Atlas · 让项目文档更好读',
  '一个轻量的知识工具，把 **零散信息** 整理成可以复用的工作流。',
  '## 快速开始',
  '只需两步，即可在本地启动。命令和文件名保持原样，不会被翻译破坏。',
  '- 安装依赖，准备本地环境\n- 启动服务，打开阅读区\n  - 支持中文译文与原文切换',
  '```bash\nnpm install\nnpm run dev -- --host 127.0.0.1\n' + 'long-command '.repeat(30) + '\n```',
  '## 配置参考',
  '| 配置项 | 类型 | 默认值 | 说明 |\n| --- | --- | --- | --- |\n| `READER_WIDTH` | 尺寸 | `720px` | 适合中文与英文混排的阅读宽度 |\n| `DATA_DIR` | 路径 | `./data` | 存放索引与本地配置，不改变原始文档。 |',
  '> 不改变原文内容，只让结构更清晰。',
  '### 阅读体验',
  '段落之间留出呼吸感，章节之间建立清楚的层级。访问[项目主页](https://example.org/atlas)，或跳到[配置参考](#配置参考)。',
  'Use List<T>，<script>window.__readerXSS=1</script>。',
  '### 阅读体验',
  '重复的章节标题也能准确跳转。',
].join('\n\n');
await evalJs(`window.__readerFixture = {
  key:'reader-typography', title:'Atlas: a calmer knowledge workflow',
  title_zh:'Atlas：让项目文档更好读', body:'# Original README\\n\\nKeep the original **formatting**.',
  body_zh:${js(markdown)}, author:'GitHub Trending', published:'2026-10-18 09:30',
  url:'https://example.org/atlas', content_kind:'content'
}; sourceRows = [window.__readerFixture];
$('source-list').innerHTML = renderSourceEntry(sourceRows[0]);
readSourceItem('reader-typography'); true`);

check('README 渲染标题、强调、嵌套列表与代码块', await evalJs(`(() => {
  const body = $('source-reader-body');
  return body.querySelectorAll('h1,h2,h3').length === 5 && body.querySelector('strong').textContent === '零散信息'
    && !!body.querySelector('ul ul li') && body.querySelector('pre code').textContent.includes('npm install');
})()`));
check('表格和引用具有独立结构', await evalJs(`!!$('source-reader-body').querySelector('table th')
  && !!$('source-reader-body').querySelector('blockquote p')`));
check('字面 HTML 示例保留且不会执行', await evalJs(`$('source-reader-body').textContent.includes('List<T>')
  && $('source-reader-body').textContent.includes('<script>') && !$('source-reader-body').querySelector('script') && !window.__readerXSS`));
check('文章外链安全地在新标签打开', await evalJs(`(() => {
  const link = $('source-reader-body').querySelector('a[href^="https:"]');
  return link.target === '_blank' && link.rel.includes('noopener') && link.rel.includes('noreferrer');
})()`));
check('长文目录默认收起，重名章节保持独立', await evalJs(`!$('source-reader-outline').hidden
  && !$('source-reader-outline').open && $('source-reader-outline').querySelectorAll('nav button').length === 5
  && $('source-reader-body').querySelectorAll('[data-reader-anchor="阅读体验-1"]').length === 1`));
check('排版不污染收藏文本坐标系', await evalJs(`snipCollapse(snipFragText($('source-reader-body')))
  === snipRenderedText(${js(markdown)})`));
check('清除 inherited pre-wrap，正文段距不叠加原始空行', await evalJs(`(() => {
  const body = $('source-reader-body'), paragraph = body.querySelector('p'), style = getComputedStyle(paragraph);
  return getComputedStyle(body).whiteSpace === 'normal' && parseFloat(style.marginBottom) <= 18
    && getComputedStyle(body.querySelector('pre')).whiteSpace === 'pre';
})()`));

mkdirSync('output/playwright', { recursive:true });
async function screenshot(name) {
  const result = await send('Page.captureScreenshot', { format:'png' });
  writeFileSync(`output/playwright/reader-${name}.png`, Buffer.from(result.result.data, 'base64'));
}
await screenshot('desktop');
await evalJs(`$('source-reader-expand').click(); true`);
check('专注模式标题与正文保持相同版心', await evalJs(`(() => {
  const body = $('source-reader-body').getBoundingClientRect(), header = document.querySelector('.source-reader-heading').getBoundingClientRect();
  return body.width <= 720 && Math.abs(body.left - header.left) < 1 && Math.abs(body.width - header.width) < 1;
})()`));
await screenshot('focused');
await evalJs(`$('source-reader-outline').open = true;
$('source-reader-outline').querySelector('button:last-child').click(); true`);
check('目录跳转只滚动阅读区，不改变工作区地址', await evalJs(`(() => {
  const heading = $('source-section-4'), scroll = $('source-reader-scroll');
  return scroll.scrollTop > 0 && document.activeElement === heading && !location.hash.includes('source-section');
})()`));
await evalJs(`$('source-reader-scroll').scrollTop = 0;
$('source-reader-body').querySelector('a[href^="#"]').click(); true`);
check('README 页内链接指向当前文章章节', await evalJs(`document.activeElement.dataset.readerAnchor === '配置参考'`));

await evalJs(`setSourceOriginal(true); true`);
check('原文也按 Markdown 排版，旧目录不残留', await evalJs(`$('source-reader-body').querySelector('h1').textContent === 'Original README'
  && $('source-reader-body').querySelector('strong').textContent === 'formatting' && $('source-reader-outline').hidden`));
await evalJs(`setSourceOriginal(false); true`);
await send('Emulation.setDeviceMetricsOverride', { width:390, height:844, deviceScaleFactor:1, mobile:true });
await evalJs(`$('source-reader-scroll').scrollTop = 0; true`);
check('窄屏长代码与宽表格局部滚动，不撑破页面', await evalJs(`(() => {
  const body = $('source-reader-body'), scroll = $('source-reader-scroll');
  return document.documentElement.scrollWidth <= innerWidth + 1 && scroll.scrollWidth <= scroll.clientWidth + 1
    && [...body.querySelectorAll('pre,table')].every(node => node.scrollWidth > node.clientWidth
      && getComputedStyle(node).overflowX === 'auto');
})()`));
await screenshot('mobile');
await evalJs(`$('source-reader-body').querySelector('table').scrollIntoView({ block:'center' }); true`);
await screenshot('mobile-table');
await evalJs(`sourceReaderItem = { ...window.__readerFixture, content_kind:'post', body_zh:'**这不是强调**\\n\\n#话题 保留社交观点原样' };
renderSourceReader(); true`);
check('社交观点不误识别为 Markdown，短文不显示目录', await evalJs(`$('source-reader-body').textContent === '**这不是强调**\\n\\n#话题 保留社交观点原样'
  && !$('source-reader-body').querySelector('strong') && $('source-reader-body').classList.contains('is-plain') && $('source-reader-outline').hidden`));
await evalJs(`window.__savedPurify = window.DOMPurify; window.DOMPurify = null;
renderSourceBody(${js(markdown)}); true`);
check('净化器缺失时安全降级为完整纯文本', await evalJs(`$('source-reader-body').textContent === ${js(markdown)}
  && !$('source-reader-body').querySelector('*') && $('source-reader-outline').hidden`));
await evalJs(`window.DOMPurify = window.__savedPurify; resetSourceReader(); true`);
check('清空阅读器同时清空正文和目录', await evalJs(`!$('source-reader-body').textContent
  && $('source-reader-outline').hidden && !$('source-reader-outline').querySelector('nav').childElementCount`));
await evalJs(`renderSourceBody('[危险链接](javascript:alert(1))\\n\\n![项目标志](/static/favicon.svg)'); true`);
check('Markdown 危险协议被净化，正常图片延迟加载', await evalJs(`!$('source-reader-body').querySelector('a[href]')
  && $('source-reader-body').querySelector('img').loading === 'lazy'
  && $('source-reader-body').querySelector('img').referrerPolicy === 'no-referrer'`));
await evalJs(`window.__savedMarked = window.marked; _snipMd = null; window.marked = null;
renderSourceBody(${js(markdown)}); true`);
check('解析器缺失时保留原文，不显示残留目录', await evalJs(`$('source-reader-body').textContent === ${js(markdown)}
  && $('source-reader-outline').hidden && !$('source-reader-body').querySelector('*')`));
await evalJs(`window.marked = window.__savedMarked; true`);

await send('Page.navigate', { url:base + '/doc/' + fx.markdown.split('/').map(encodeURIComponent).join('/') });
await until(`!!document.querySelector('#content table')`);
check('独立阅读页复用同一套文章排版', await evalJs(`document.querySelector('#content').classList.contains('reader-prose')
  && getComputedStyle(document.querySelector('#content table')).overflowX === 'auto'
  && getComputedStyle(document.querySelector('#content pre')).whiteSpace === 'pre'`));
check('无浏览器脚本错误', pageErrors.length === 0, pageErrors.join(' | '));
finish();
