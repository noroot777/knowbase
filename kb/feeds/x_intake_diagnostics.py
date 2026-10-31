"""Structured automatic-X queue facts; free-form notes are never evidence of coverage."""

PROBLEMS = {
    'x_intake_history': {
        'state': '历史待追补', 'label': '五天历史覆盖未完成', 'step': 2,
        'explanation': '本轮已取得的内容照常保存，历史覆盖与最新发现独立记账。',
        'advice': '后续同步在滚动五天窗口内有界重扫；不保证从像素或原生游标断点续页，超过五天不再自动补。',
        'retry_title': '重扫五天历史',
    },
    'x_intake_body': {
        'state': '正文待补', 'label': '正文在独立队列等待补全', 'step': 2,
        'explanation': '时间线发现可以已经成功，预览尚未成为可交付正文；这不是来源连接失败。',
        'advice': '等待正文队列按自己的冷却重试，或同步此来源处理已到期的正文；强制同步不跳过单条正文冷却。',
        'retry_title': '检查独立正文队列',
    },
    'x_intake_unconfirmed': {
        'state': '候选待确认', 'label': '已保存候选仍缺扫描收尾确认', 'step': 2,
        'explanation': '候选已经持久化，但尚未获准投递，不能冒充知识库成品。',
        'advice': '下一轮优先重扫该来源的五天历史，取得完整区间收尾证明后恢复；部分扫描不放行。',
        'retry_title': '重确认五天候选',
    },
    'x_intake_expired': {
        'state': '已超自动窗口', 'label': '最近24小时有待办超出五天窗口', 'step': 3,
        'explanation': '这些待办未计完成，已离开自动五天范围；提示只保留24小时，不累计历史总数。',
        'advice': '普通同步不会追回窗口外内容；如确有需要，应单独指定历史区间补抓。',
    },
}


def issues(state):
    result = []
    for field, kind, label in [('unconfirmed', 'x_intake_unconfirmed', '候选待确认'),
                               ('pending', 'x_intake_body', '正文待补'),
                               ('expired', 'x_intake_expired', '最近24小时超窗')]:
        if count := state.get(field, 0):
            result.append(dict(kind=kind, count=count, evidence=f'{label} {count} 条',
                               retryable=field != 'expired'))
    attempted = state.get('discovery') != 'unattempted' or state.get('history_at', 0) > 0
    if state.get('gap') and attempted and not state.get('error'):
        result.append(dict(kind='x_intake_history', evidence=state['gap'], retryable=True))
    return result
