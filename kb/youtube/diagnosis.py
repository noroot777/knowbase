"""工具诊断证据的运输口径:**分类在完整文本上做,裁字数只许发生在展示那一端。**

这一层单独拿出来,是因为同一条链上**两次**丢掉了决定性的那一行(都实测复现过):

1. `diagnostics()` 原先只留尾部 800 字。429 那一行在最前面,后面跟着一长串重试诊断和
   一句机器人验证 —— 按尾部一裁,429 不见了,同一次失败被判成 bot 墙,照它的提示去
   重新登录完全没用(用户本来就带着 cookie)。
2. 面板上的「yt-dlp 原文」取前 400 字。真正的最终错误在尾部,从另一头裁又把它丢了。

所以口径钉成两件事:**完整的脱敏诊断先过分类**(结论随异常一起往上走,见 `ToolFailure`),
**给人看的那一份两头都留**(`excerpt`)。消费方不许再拿 `str(error)` 重新分类。
"""


def excerpt(text, limit=800):
    """给人看的那一份:超了就**两头都留**,中间省略。

    单端裁是上面两处丢证据的直接原因 —— 开头那行和结尾那行恰恰是最有信息的两行。
    """
    text = str(text or '').strip()
    if len(text) <= limit:
        return text
    half = max(1, (limit - 3) // 2)
    return f'{text[:half]} … {text[-half:]}'


class ToolFailure(RuntimeError):
    """一次外部工具失败。**分档在抛出的那一刻就算完了**,算的是未裁切的完整诊断。

    `code` 给消费方直接用(`login.coded`),`diagnostic` 是完整脱敏诊断、给展示端自己裁。
    异常消息本身只是摘要,**不许再拿它去分类** —— 它为了给人看已经裁过。
    """

    def __init__(self, summary, diagnostic='', code=''):
        self.diagnostic = str(diagnostic or '')
        self.code = code
        super().__init__(f'{summary}；{excerpt(self.diagnostic)}'.strip('；'))
