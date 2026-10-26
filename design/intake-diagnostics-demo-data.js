window.diagnosticIcons = {
  library: '<path d="M4 4h5v16H4zM12 4h4v16h-4zM18 5l3 14"/>',
  bookmark: '<path d="M6 3h12v18l-6-4-6 4z"/>',
  layers: '<path d="m12 3 9 5-9 5-9-5zM3 12l9 5 9-5M3 16l9 5 9-5"/>',
  sun: '<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M2 12h2M20 12h2M5 5l1.5 1.5M17.5 17.5 19 19M5 19l1.5-1.5M17.5 6.5 19 5"/>',
  sync: '<path d="M20 7v5h-5M4 17v-5h5M5.5 7a7 7 0 0 1 12-2L20 9M4 15l2.5 4a7 7 0 0 0 12-2"/>',
  inbox: '<path d="M4 4h16l2 11v5H2v-5zM2 15h6l2 3h4l2-3h6"/>',
  sprout: '<path d="M12 21v-8M12 16C4 16 3 10 4 6c6 0 9 3 8 10ZM12 12c0-6 3-9 9-9 0 6-3 9-9 9Z"/>',
  history: '<path d="M3 4v5h5M3.6 9A9 9 0 1 1 4 17M12 7v5l3 2"/>',
  play: '<path d="m8 4 12 8-12 8z"/>',
  search: '<circle cx="10.5" cy="10.5" r="6.5"/><path d="m16 16 5 5"/>',
  shield: '<path d="M12 3 3 7v5c0 5 9 9 9 9s9-4 9-9V7zM8 12l3 3 5-6"/>',
  flask: '<path d="M9 3h6M10 3v6L4 19a1.5 1.5 0 0 0 1.3 2h13.4a1.5 1.5 0 0 0 1.3-2L14 9V3M7 14h10"/>',
  external: '<path d="M14 3h7v7M10 14 21 3M10 4H4v16h16v-6"/>',
  check: '<path d="m5 12 4 4L19 6"/>',
  edit: '<path d="m15 4 5 5M4 20l5-1L21 7l-4-4L5 15z"/>',
  file: '<path d="M14 2H4v20h16V8zM14 2v6h6M8 13h8M8 17h6"/>'
};

window.diagnosticSeed = [
  {
    id: 'KB-0217', title: 'Safeguards', source: '网页专栏', subscription: 'Anthropic News',
    url: 'https://www.anthropic.com/news/safeguards', stage: '正文抓取', status: 'attention',
    type: 'failed', preferred: 'manual', attempts: 3, date: '10/26 09:18', reason: '没有识别出可确认的文章正文',
    explanation: '页面可能依赖脚本渲染，也可能是提取规则不适用。已达到 3 次重试上限，不会继续自动补抓。',
    recommendation: '先打开原文确认是否能读到正文。能读到时，直接补入正文通常比原样再试更有效。',
    evidence: '页面上没有识别出正文，可能需要登录或由脚本渲染',
    history: [['09:18', '第 3 次未识别到正文，退出自动重试'], ['09:17', '文章链接已发现，尚无完整正文']]
  },
  {
    id: 'KB-0216', title: '为什么人工智能生成的语言这么难懂？', source: 'RSS', subscription: 'Hacker News · Buzzing',
    url: 'https://arxiv.org/abs/2610.29162', stage: '正文抓取', status: 'attention',
    type: 'failed', preferred: 'manual', attempts: 3, date: '10/26 09:16', reason: '摘要页未提取到完整正文',
    explanation: '链接指向论文摘要页，不一定包含论文全文。继续抓取同一个页面，仍可能只得到摘要。',
    recommendation: '提供可访问的全文网页链接，或手动粘贴正文。不把 PDF 地址直接当作 HTML 正文抓取。',
    evidence: '页面上没有识别出正文，可能需要登录或由脚本渲染',
    history: [['09:16', '补抓失败，已达到本轮重试上限'], ['10/25', '首次补抓未识别到文章正文']]
  },
  {
    id: 'KB-0215', title: '一篇需要人工确认正文的文章（示例）', source: 'RSS', subscription: '订阅正文判断示例',
    url: '', stage: '正文抓取', status: 'attention', type: 'held', attempts: 1, date: '10/26 09:14',
    reason: '无法确认抓回的内容是否是正文',
    explanation: '提取结果疑似提示页，但证据不足以断定被拦截。已挂起等待人工确认，不扣完重试额度后静默降级。',
    recommendation: '核对原文后补入正文，或确认可以重试再重新排队。不要把“证据不足”误报成“网站已封禁”。',
    evidence: '无法确认正文；需要人工核对', history: [['09:14', '证据不足，已挂起等待确认']]
  },
  {
    id: 'KB-0214', title: '一篇连接超时的技术文章（示例）', source: 'RSS', subscription: '网络连接示例',
    url: '', stage: '正文抓取', status: 'attention', type: 'failed', attempts: 3, date: '10/26 09:12',
    reason: '连接超时，连续 3 次没有取回网页', explanation: '这只是连接失败，不代表文章不存在。已用完自动重试预算，需要你决定是否再试一轮。',
    recommendation: '确认网络恢复后重新排队。重置本轮次数，不会抹掉之前的失败记录。',
    evidence: 'ReadTimeout: 本次请求未在允许时间内完成', history: [['09:12', '连接超时，本轮自动重试停止']]
  },
  {
    id: 'KB-0213', title: 'X 时间线暂时限流（示例）', source: '大V观点', subscription: '示例账号 · 时间线',
    url: '', stage: '来源连接', status: 'attention', type: 'source', attempts: 1, date: '10/26 09:10',
    reason: '这次没有读到账号时间线', explanation: '示例情形：页面返回访问频率限制，本轮没有获得完整时间线。不能据此说这个来源没有新内容。',
    recommendation: '过一段时间后重试这个来源，不需要反复登录。只重试当前来源，不扩大到全部订阅。',
    evidence: '示例：HTTP 429 / 本轮未读到时间线', history: [['09:10', '来源连接受限，待人工决定重试']]
  },
  {
    id: 'KB-0212', title: '一篇未补齐正文的公众号文章（示例）', source: '公众号', subscription: '正文补齐示例',
    url: '', stage: '正文抓取', status: 'attention', type: 'held', attempts: 2, date: '10/26 09:08',
    reason: '抓取结果不足以确认完整正文', explanation: '示例情形：文章链接已发现，但正文还未可靠取回。人工补入内容后，仍需继续执行后续入库。',
    recommendation: '如果你能打开原文，可以粘贴正文补齐；否则先暂缓，保留问题和尝试记录。',
    evidence: '示例：链接已保存，正文未通过确认', history: [['09:08', '正文待确认，尚未完成入库']]
  },
  {
    id: 'KB-0211', title: '一期简报的中文翻译（示例）', source: '简报', subscription: '简报翻译示例',
    url: '', stage: '翻译', status: 'attention', type: 'translation', attempts: 3, date: '10/26 09:06',
    reason: '正文已取得，中文翻译仍未完成', explanation: '正文不需要再抓。问题发生在翻译环节，重试只从翻译继续，不重复补抓已经取得的内容。',
    recommendation: '确认翻译服务可用后重新排队，后续只补翻译与入库。',
    evidence: '示例：翻译服务响应超时', history: [['09:06', '中文翻译失败，原文保留'], ['09:05', '原文正文已取得']]
  },
  {
    id: 'KB-0210', title: '《Block Blast》如何利用玩家赚取广告收入', source: 'RSS', subscription: 'Hacker News · Buzzing',
    url: '', stage: '正文抓取', status: 'queued', type: 'pending', attempts: 0, date: '10/26 09:04',
    reason: '还没有轮到这一条', explanation: '当前只有订阅提供的链接与标题。已在待办中，但没有后台工作线程正在处理它。',
    recommendation: '可以继续等待下一轮同步，也可以现在执行本页演示。排队不是失败，也不算完成。',
    evidence: '等待调度，尚未尝试补抓', history: [['09:04', '链接已发现，加入待执行队列']]
  },
  {
    id: 'KB-0209', title: 'Goose 编程语言', source: 'RSS', subscription: 'Hacker News · Buzzing',
    url: '', stage: '正文抓取', status: 'queued', type: 'pending', attempts: 0, date: '10/26 09:04',
    reason: '等待下一轮调度，不在执行中', explanation: '订阅只提供了链接。本条尚未启动正文补抓，正文完成后再处理翻译与入库。',
    recommendation: '无需把“排队中”当成错误。需要尽快入库时，可单独执行这一条。',
    evidence: '等待调度，尚未尝试补抓', history: [['09:04', '链接已发现，加入待执行队列']]
  },
  {
    id: 'KB-0208', title: '丹麦政府悄然放弃了弃用微软 Office 的计划', source: 'RSS', subscription: 'Hacker News · Buzzing',
    url: '', stage: '正文抓取', status: 'queued', type: 'pending', attempts: 0, date: '10/26 09:04',
    reason: '只保存了链接，等待补齐正文', explanation: '尚未开始抓取，不代表已经失败。下一步是补齐正文，然后继续完成入库。',
    recommendation: '与其他排队内容一起执行，或者勾选后单独运行。',
    evidence: '等待调度，尚未尝试补抓', history: [['09:04', '链接已发现，加入待执行队列']]
  },
  {
    id: 'KB-0207', title: 'Show HN: 偷走我的比特币', source: 'RSS', subscription: 'Hacker News · Buzzing',
    url: '', stage: '正文抓取', status: 'queued', type: 'pending', attempts: 0, date: '10/26 09:04',
    reason: '等待正文补抓', explanation: '当前还只有一条链接，不会把这份链接壳误报成已取得完整正文。',
    recommendation: '执行待办后查看结果。只有本轮确实执行了，状态才会从待执行切换为执行中。',
    evidence: '等待调度，尚未尝试补抓', history: [['09:04', '链接已发现，加入待执行队列']]
  },
  {
    id: 'KB-0206', title: '一篇已经恢复收录的文章（示例）', source: 'RSS', subscription: '处理完成示例',
    url: '', stage: '入库处理', status: 'resolved', type: 'recovered', attempts: 1, date: '10/26 09:02',
    reason: '正文与后续处理已完成（示例）', explanation: '演示记录：正文已补齐、翻译与入库已完成。只有走完必要环节的内容才进入这里。',
    recommendation: '保留处理记录，便于以后追溯。这个原型没有真实知识库文档可打开。',
    evidence: '演示数据：已取得正文并完成入库', history: [['09:02', '演示：正文与入库处理完成'], ['09:01', '演示：重新补抓成功']]
  }
];
