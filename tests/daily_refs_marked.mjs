// 拿**页面那一份** marked(kb/static/vendor/marked.min.js)渲染日报正文的「加链接
// 之前 / 之后」,把每条链接的 (href, 内部 HTML) 报回去。
//
// 后端的裁决器是 markdown-it,和页面的 marked 是两个实现 —— 第 5 轮就是栽在这上
// 面(linkify 的 TLD 名单里没有 `dev`/`app`,于是 `www.example.dev/path/[1]` 在后
// 端眼里只是一段文字、在页面上却是一条链接,替换完点过去是另一个地址)。所以回
// 归必须用页面那一份渲染器来比:在后端渲染器里比一遍,只能证明它自己前后一致。
//
// 用法:node tests/daily_refs_marked.mjs <cases.json>,stdin 不读,结果走 stdout。
// 页面实际是 `DOMPurify.sanitize(marked.parse(md))`,这里只跑 marked —— DOMPurify
// 不改动 http(s) 链接的地址和文字,而这里比的就是这两样。
import { readFileSync } from 'node:fs';

const mod = await import(new URL('../kb/static/vendor/marked.min.js', import.meta.url));
const marked = mod.default ?? mod;

// 内部写 `[\s\S]*?` 而不是 `[^<]*`:图片链接的内部是一个 `<img>`。改坏成链接套
// 链接时这条会抓到半截(内层的 `</a>` 先收尾),集合当场对不上 —— 那正是要抓的。
const A_RE = /<a\s[^>]*href="([^"]*)"[^>]*>([\s\S]*?)<\/a>/g;
const links = (html) => [...html.matchAll(A_RE)].map((m) => [m[1], m[2]]);

const cases = JSON.parse(readFileSync(process.argv[2], 'utf8'));
process.stdout.write(JSON.stringify(cases.map((c) => ({
  name: c.name,
  before: links(marked.parse(c.before)),
  after: links(marked.parse(c.after)),
}))));
