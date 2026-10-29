# -*- coding: utf-8 -*-
"""「检查登录态」那一次**真连**:便宜的就绪判断(`readiness`)、两步检查(`check` / `_check`)、
结论旁边那条并列提醒(`_noticed`)。说明和硬约束见 `kb/youtube/login.py` 的模块说明。

从 `login.py` 拆出来只因为那份到了行数上限。**名字照旧从 `login` 取**(`login.check` /
`login.readiness`):调用方和打桩都认那儿。所以这里对 `login` 的每一次引用都走**属性查找**、
而且是**函数里**才 import —— 模块顶上 import 的话,谁先 import 这一份就会撞上循环导入;
直接引名字的话,打在 `login.installed` / `login.readiness` 上的桩就对这里失效了。
"""
from kb.youtube.diagnosis import CAPPED, COOKIES, excerpt, heard, witnessed


def readiness(settings) -> dict:
    """便宜的那一道:只看 yt-dlp 在不在、浏览器目录在不在。**不代表登录态有效。**"""
    from kb.youtube import login, toolchain
    from kb.youtube.settings import of
    spec = of(settings)['cookies_browser']
    if not toolchain.command():
        return {'ready': False, 'code': 'yt_dlp_missing', 'message': login.describe('yt_dlp_missing', spec),
                'browser': spec}
    if not login.installed(spec):
        return {'ready': False, 'code': 'no_browser', 'message': login.describe('no_browser', spec),
                'browser': spec}
    return {'ready': True, 'confirmed': False, 'code': 'unverified', 'browser': spec,
            'message': f'已配置从 {login.LABELS.get(login.browser_of(spec), spec)} 读取登录态。'
                       '文件上看不出 cookie 是否过期，点「检查登录态」才会真连一次。'}


def check(settings, deadline=None) -> dict:
    """真连一次。两步:先列频道(不需要登录态),再取单条视频元数据(bot 墙在这一步)。

    **整次检查一本诊断账**(`diagnosis.heard`):每一步的原话在 `media.run` 的完成边界
    就记下了,所以**哪一步失败都抹不掉前面那一步说过的话** —— 列频道时明说「读不出
    cookie」、随后 probe 普通超时的那一轮,原先只按后一条异常出结论,面板上只剩
    「yt-dlp 报了一个没能归类的错误」,先前那条铁证一个字都没有(审查实测)。
    """
    from kb.youtube import login
    from kb.youtube.settings import of

    spec = of(settings)['cookies_browser']
    with heard() as ledger:
        return login._noticed(login._check(settings, deadline, spec), ledger, spec)


def _noticed(result: dict, ledger, spec: str) -> dict:
    """把这一次检查里工具说过的话并列挂在结论旁边。**成功和失败同一个出口。**

    分档照旧在**完整文本**上算(账上那几份一个字都没裁过),然后和结论并列显示:
    取到了元数据那一轮 `ready` 仍是 `True`、`code` 仍是 `metadata_ok`,只多一条
    `warning` —— 把它升级成「登录态无效」是另一头的错,人刚刚明明取到了元数据。
    失败那一轮同理只是**并列**:`code` 还是那次失败自己的档,不许被先前那条警告顶掉
    (两件事可以同时成立,而「这一步为什么没成」是人要先知道的那一件)。
    **只认 YouTube 那个上游说的**:本机工具的报错不进这套判据(`diagnosis.witnessed`)。
    (`heard` / `witnessed` 从 `diagnosis` 直接引进来 —— 那一层不认识 `login`,没有环。)
    **`detail` 不许盖掉失败自己的原文**:那一份是人唯一的线索,警告另挂一行。
    """
    from kb.youtube import login

    said = witnessed(ledger)
    # **按档压,不按结论整条关**(`beside` / `MUTED`):和这次结论冲突的那一档不并列说,
    # 别的档照旧说 —— 那是前一步说过的话唯一的出口。
    flagged = login.beside(result.get('code', ''), ' | '.join(said)) if said else ''
    wording = login.caution(flagged, spec)
    # 这一次**实际用的**是哪一份 cookie 库:从账上读(见 `arguments`),不事后再准备一次
    # 看「现在钉不钉得住」。同一段里几次调用说的是同一句话,按原话去重、不堆 N 遍。
    unpinned = list(dict.fromkeys(witnessed(ledger, COOKIES)))
    if unpinned:
        # **并列,不互相顶掉。** 两件事可以同时成立,而且下一步不一样(一个去钥匙串放行,
        # 一个换浏览器或点名 profile)。只有这一条时它就是这次并列警告的档。
        flagged = flagged if wording else login.UNPINNED
        wording = ' '.join(filter(None, [wording, login.cautioned(login.UNPINNED, spec)]))
    if not wording:
        return result
    result['warning_code'] = flagged
    result['warning'] = wording
    result.setdefault('detail', excerpt(' | '.join([*said, *unpinned]), 400))
    return result


def _check(settings, deadline, spec) -> dict:
    """真连那两步本身。**结论由 `check` 统一配上这一次的诊断**,所以这儿只管判这一步。"""
    import time

    from kb.feeds import registry as reg
    from kb.youtube import login, media
    from kb.youtube.channels import VIDEO_ID

    base = login.readiness(settings)
    if not base['ready']:
        return {**base, 'checked': True}
    deadline = deadline or (time.monotonic() + 120)
    source = next((item for item in reg.subscriptions('youtube') if item['enabled']), None)
    if not source:
        return {'ready': False, 'code': 'no_channel', 'message': login.describe('no_channel', spec),
                'browser': spec, 'checked': True}
    try:
        listed = media.channel_videos(source, settings, deadline, lambda: False, limit=1)
    except Exception as error:
        # 列频道这一步**本来不需要登录态**(实测裸跑可通),所以它失败时有把握的只有两档:
        # 「cookie 读不出来」和「出口撞上机器人验证」—— 两者都和这一步本身对得上。
        # 其余一律算连不上,不拿它冒充「没登录」:那会把人送去反复登录,而问题在另一头。
        code = login.coded(error)
        # 本机上限那一档照旧说它自己(说成「连不上」会让人去查网络)。
        code = code if code in ('cookie_unreadable', 'bot_wall', 'rate_limited', CAPPED) else 'unreachable'
        return {'ready': False, 'code': code, 'browser': spec, 'checked': True,
                'message': login.describe(code, spec), 'detail': login.evidence(error)}
    video_id = next((item['video_id'] for item in listed if VIDEO_ID.fullmatch(item['video_id'])), '')
    if not video_id:
        return {'ready': False, 'code': 'unreachable', 'browser': spec, 'checked': True,
                'message': login.describe('unreachable', spec),
                'detail': f"频道 {source['name']} 这一轮没有列出任何视频"}
    row = {'video_id': video_id, 'entry': {'url': 'https://www.youtube.com/watch?v=' + video_id}}
    try:
        media.probe(row, settings, deadline, lambda: False, force=True)
    except Exception as error:
        code = login.coded(error)
        return {'ready': False, 'code': code, 'browser': spec, 'checked': True,
                'message': login.describe(code, spec), 'detail': login.evidence(error)}
    # `confirmed` 和 `ready` 是两件事:ready=「这一轮没撞上任何已知故障」,
    # confirmed=「那份登录态被证实有效」。后者我们现在拿不到证据,就如实报 False ——
    # 面板按它显示中性而不是绿色结论。
    # `confirmed` 和 `ready` 是两件事,见上面那段注释;这一份的警告由 `check` 配上。
    return {'ready': True, 'confirmed': False, 'code': 'metadata_ok', 'browser': spec,
            'checked': True, 'message': login.describe('metadata_ok', spec)}
