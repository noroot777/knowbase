/* 整站共用的 HTTP 错误解析:知识库页面(`index.html` 在
 * `ui.js` 之前加载它)和浏览器扩展各一份消费方,实现只有这一份。
 *
 * **不许写成 `(await r.json()).detail || '失败'`**:未捕获异常时 Starlette 回的是
 * 纯文本 500,`r.json()` 会当场抛,真因就被吞成一句无信息的「失败」。所以这里先
 * 取文本、再试着当 JSON 解,两条路都兜住,并且把 HTTP 状态码带上。口径同
 * `docs/decisions/frontend-feedback.md`;它住在扩展目录是因为下发的 ZIP 要带上它,
 * 扩展装在浏览器里,取不到 `kb/static/js/` 下的文件。
 */
async function apiFail(response, fallback = '请求失败') {
  let text = '';
  try { text = (await response.text()).trim(); } catch {}
  if (text) {
    let result = null;
    try { result = JSON.parse(text); } catch {}
    if (result) {
      const detail = result.detail ?? result.message ?? result.error;
      if (typeof detail === 'string' && detail) return detail;
      if (Array.isArray(detail) && detail.length) return detail.map(item => item.msg || JSON.stringify(item)).join('; ');
    } else if (text.length <= 200 && text[0] !== '<') {
      return `${text}(HTTP ${response.status})`;
    }
  }
  return `${fallback}(HTTP ${response.status})`;
}

async function apiResponse(response, fallback = '请求失败') {
  if (!response.ok) {
    const error = new Error(await apiFail(response, fallback));
    error.status = response.status;
    throw error;
  }
  try { return await response.json(); }
  catch {
    const error = new Error(`服务返回了无法识别的数据，请稍后重试(HTTP ${response.status})`);
    error.status = response.status;
    throw error;
  }
}
