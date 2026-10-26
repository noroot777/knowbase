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
