import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import vm from 'node:vm';

const source = await readFile(new URL('../kb/static/browser-extension/knowbase-capture/api.js', import.meta.url), 'utf8');
const context = vm.createContext({Response});
vm.runInContext(source, context);

assert.equal(await context.apiFail(new Response('Internal Server Error', {status:500})), 'Internal Server Error(HTTP 500)');
assert.equal(await context.apiFail(new Response(JSON.stringify({detail:'条目已变化'}), {status:409})), '条目已变化');
assert.equal(await context.apiFail(new Response(JSON.stringify({detail:[{msg:'正文过长'},{msg:'网址不正确'}]}), {status:422})), '正文过长; 网址不正确');
assert.equal(await context.apiFail(new Response('<html>Error</html>', {status:502}), '连接失败'), '连接失败(HTTP 502)');
await assert.rejects(context.apiResponse(new Response('Internal Server Error', {status:500})), error => error.status === 500 && error.message.includes('HTTP 500') && !error.message.includes('Unexpected token'));
await assert.rejects(context.apiResponse(new Response('{"detail":"已过期"}', {status:404})), error => error.status === 404 && error.message === '已过期');
await assert.rejects(context.apiResponse(new Response('Not JSON')), error => error.message.includes('无法识别的数据'));
assert.equal((await context.apiResponse(new Response('{"saved":true}'))).saved, true);

console.log('browser capture feedback: passed');
