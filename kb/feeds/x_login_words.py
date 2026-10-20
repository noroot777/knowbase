"""「从日常浏览器导入 X 登录态」那条链的**文案层**:结论码 → 人话。

从 `x_login_import` 拆出来的一层(那个文件到了 700 行上限)。只放**纯函数和词表**:
不碰 profile、不读留痕、不认识 CDP,所以这一层改文案不必重跑浏览器那一组。
留痕(`LEDGER`)、`state()`、`summary()` 仍在 `x_login_import` 里 —— 它们有模块状态,
而测试是 `patch.object(x_login_import, 'LEDGER', …)` 打桩的,搬过来等于让那个桩静默失效。

`x_login_import` 用 `from` 导入这几个名字,所以 `patch.object(x_login_import, '_detail', …)`
照旧生效(它改的是调用方自己那张名字表)。
"""

from __future__ import annotations

#: 结论码 → 面板上该跟着说的那半句。**`ok` 之外一律不许说成「可以用了」**。
#: `writing` / `unsaved` 是这一块自己的两档,其余六档是当场那次验证的结论
#: (`x_browser.verify_timeline`)。
_VERDICT_TAIL = {
    'ok': '当场验证通过',
    'writing': '这一轮没走完 —— profile 里那份登录态现在是什么状态不确定，'
               '再导入一次，或者点「检查连接」看看',
    'unsaved': '关掉浏览器之后那几个关键 cookie 没留在 profile 里，等于没搬成 ——'
               '再导入一次，或者改用「打开登录窗口」',
    'relogin': '重新打开这个 profile 之后 X 又把它判成未登录了 —— 当场能用的那一份'
               '没能完整留下，再导入一次，或者改用「打开登录窗口」',
    'unconfirmed': '重新打开之后那一次验证没跑成（X 限流，或者首页没加载完），'
                   '所以还不能说明早那一轮不用点弹窗 —— 点「检查连接」再确认一次',
    'login': 'X 当场把它判成未登录 —— 这个账号多半换了浏览器实例就不认，建议改用'
             '「打开登录窗口」',
    'challenge': 'X 要求先过一次安全验证（账号访问受限）—— 这一关重新登录也绕不过'
                 '去，请在日常浏览器里打开 x.com/account/access 把验证做完',
    'limited': 'X 当时在限流，没验证成；点「检查连接」再确认',
    'not-loaded': 'X 首页当时没加载完（网络或代理），没验证成；点「检查连接」再确认',
    'no-tweets': '首页当时一条推文都没读到，没验证成；点「检查连接」再确认',
}


#: 「盘上跟验证完那一刻对不上」的几种记法。**它们在不在,就是「有没有保存差异」**
#: —— `_persistence_gap` 要么一个不给,要么四个一起给。
_GAP_KEYS = ('vanished', 'absent', 'stale', 'extra')


def _gap_why(last: dict) -> str:
    """保存差异那半句(没有差异就是空串)。**口径只有一份** —— 验证过了和验证没过
    这两条路都要说同一件事,各写一遍迟早只改一处。

    **五档的下一步各不一样,所以不许折叠。** 顺序是故意的:`vanished` 跟存盘无关
    (验证刚过那一刻浏览器里就没有它了),所以排在关闭结果前面;关闭失败是 `stale`
    的常见病因,所以排在它前面。
    """
    if not any(k in last for k in _GAP_KEYS):
        return ''
    if last.get('vanished'):
        return (f'验证刚结束时浏览器里就没有 {"、".join(last["vanished"])} 了，'
                f'这个账号的登录态当时正在变动')
    if last.get('closed_cleanly') is False:
        return '关闭那个无头浏览器时没能正常存盘'
    if last.get('stale'):
        # 这一档涵盖「值不同 / 有效期不同 / 读写属性变了 / 同名少了一条」——
        # 四种写法都只说得出「对不上」,所以话也只说到这一步,别声称是哪一种。
        return (f'{"、".join(last["stale"])} 在盘上跟验证通过时那一份不一致'
                f'（旧值、旧有效期、属性变了，或者少了一条）')
    if last.get('absent'):
        return (f'{"、".join(last["absent"])} 多半是会话 cookie'
                f'（关掉浏览器就失效的那种），搬不过来')
    if last.get('extra'):
        return (f'盘上多出了验证时没有的 {"、".join(last["extra"])}'
                f'（上一次导入留下的旧条目），下次请求会不会用到那一条说不准')
    return '盘上那几条跟验证通过时对不上'


def _detail(verdict: str, count: int, last: dict, note: str) -> str:
    """那一句给人看的结论。"""
    if verdict == 'ok':
        detail = (f'已把 {count} 个 x.com cookie 搬进独立 profile，'
                  f'并在里面读到 {last.get("articles", 0)} 条推文；'
                  f'重新打开之后它们还留在磁盘上、X 也还认这份登录态。'
                  f'以后定时同步用这个 profile，不再需要点授权弹窗')
    elif verdict == 'unsaved' and last.get('final'):
        # **同一档结论,两道闸,差在「哪一刻的那一份没留下」。** 这一道拦下的是
        # 「重开验证通过了,可它用的那一份是那次加载里新换发的,关掉就没了」——
        # 说成「没有这一轮写的那一份」就把人指错方向(他写进去的那份其实好好的)。
        detail = (f'{count} 个 cookie 写进去了，重新打开也还认这份登录态，但'
                  f'最后那次验证用的那一份关掉之后没留在盘上 —— {_gap_why(last)}')
    elif verdict == 'unsaved':
        # 这一档最容易被误读成「那就再试一次呗」,所以把**为什么**说出来。
        # **「当场也能用」只许出现在这一档** —— 它只在 `verdict == 'ok'` 时才到得了
        # 这里(见 `install`),所以这句话是有依据的。
        detail = (f'{count} 个 cookie 写进去了，当场也能用，但重新打开这个 profile '
                  f'之后没有这一轮写的那一份 —— {_gap_why(last)}')
    else:
        # 验证没过那几档。**保存差异也要说,但接在后面,不许顶掉已经查明的原因** ——
        # 站点拒绝登录时它多半顺手清掉了凭证,于是盘上确实也对不上(第 4 轮审查
        # 实测:回调明确返回 `login`,产品却报 `unsaved` 并声称「当场也能用」)。
        detail = f'cookie 已经搬进独立 profile，但{_VERDICT_TAIL.get(verdict, "没验证成")}'
        if why := _gap_why(last):
            detail += f'。另外重新打开这个 profile 之后 {why}'
    return f'{detail}。{note}' if note else detail
