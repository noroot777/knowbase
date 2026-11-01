"""Structured automatic-X queue facts; free-form notes are never evidence of coverage."""
from kb.feeds.x_intake_capacity import RESCAN_NOTICE

PROBLEMS = {
    'x_intake_throttled': {
        'state': '启动限流', 'label': '页面应用启动受限，等待会话冷却', 'step': 0,
        'explanation': 'X 的启动请求被限流；这不是登录失效，也不是账号没有新内容。已取得内容和原水位保留。',
        'advice': '等待后端会话冷却后重试；45秒起、反复受限递增至180秒，预算允许时排尾重试一次。不要反复登录。',
        'retry_title': '冷却后重试此来源',
    },
    'x_intake_rate': {
        'state': '访问限流', 'label': 'X 限制了内容访问频率', 'step': 0,
        'explanation': '访问频率限制已停止本轮后续联网；不代表登录态失效。已取得内容保留。',
        'advice': '稍后重试此来源或降低同步频率；不要连续点击重试，也不要为限流反复登录。',
        'retry_title': '稍后重试此来源',
    },
    'x_intake_challenge': {
        'state': '需人工验证', 'label': 'X 要求完成安全验证', 'step': 0,
        'explanation': '安全验证阻断了本轮访问；不是自动重扫历史可以解决的问题。',
        'advice': '在当前采集通道对应的浏览器中完成安全验证，再重试此来源；反复同步不会让验证自己过去。',
        'retry_title': '完成验证后重试',
    },
    'x_intake_account': {
        'state': '账号不可用', 'label': '该账号目前无法读取', 'step': 0,
        'explanation': '源站已明确给出账号不存在、被冻结或受保护等状态；原话保留，不猜测成登录失效。',
        'advice': '根据原话核对账号地址和访问权限；账号改名时更新订阅，冻结或受保护时等待恢复权限。',
        'retry_title': '核对账号后重试',
    },
    'x_intake_login': {
        'state': '登录失效', 'label': '当前采集通道需要重新登录', 'step': 0,
        'explanation': '页面明确返回登录要求；这与限流、安全验证和账号不存在不同。',
        'advice': '在当前采集通道的设置里重新登录，或按该通道的指引重新导入登录态，然后重试。',
        'retry_title': '恢复登录后重试',
    },
    'x_intake_session': {
        'state': '会话不可用', 'label': '浏览器会话无法继续读取', 'step': 0,
        'explanation': '正文读取报告会话不可用，具体原因见原话；不把所有会话故障都说成需要登录。',
        'advice': '按原话检查当前通道连接；只有明确要求登录时才恢复登录，再重试此来源。',
        'retry_title': '检查会话后重试',
    },
    'x_intake_history': {
        'state': '历史待追补', 'label': '五天历史覆盖未完成', 'step': 2,
        'explanation': '本轮已取得的内容照常保存，历史覆盖与最新发现独立记账。',
        'advice': RESCAN_NOTICE,
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
    observed = state.get('blocks', {'discovery': state.get('block') or {}})
    for phase, block in observed.items():
        if block and phase != 'body':
            result.append(dict(kind=f"x_intake_{block['kind']}", evidence=block['reason'], retryable=True,
                               phase=phase, step=0 if phase == 'discovery' else 1))
    for field, kind, label in [('unconfirmed', 'x_intake_unconfirmed', '候选待确认'),
                               ('pending', 'x_intake_body', '正文待补'),
                               ('expired', 'x_intake_expired', '最近24小时超窗')]:
        if count := state.get(field, 0):
            reason = observed.get('body', {}).get('reason', '') if field == 'pending' else ''
            evidence = f'{label} {count} 条' + (f'；正文补全：{reason}' if reason else '')
            result.append(dict(kind=kind, count=count, evidence=evidence,
                               retryable=field != 'expired'))
    attempted = state.get('discovery') != 'unattempted' or state.get('history_at', 0) > 0
    if state.get('coverage_warning') or state.get('gap') and attempted:
        evidence = '；'.join(filter(None, [state.get('gap'), state.get('coverage_warning')]))
        result.append(dict(kind='x_intake_history', evidence=evidence, retryable=True))
    return result
