# -*- coding: utf-8 -*-
"""feedgrab 通道:子进程调用用户自己装好的 feedgrab CLI。

**为什么是子进程而不是抄代码进来**:这条路要的是 X 内部 GraphQL 接口 + 那套
`x-client-transaction-id` 签名,签名算法要从登录态页面下发的 manifest 里取
(游客页永远拿不到),feedgrab 自己为此背着 60 多个 fetcher 和 playwright。把它
抄进 kb 等于把这份维护量一起抄过来,而 X 每改一次接口就要跟一次。装在外面、
按 CLI 调用,升级是 `git pull` 的事。

和 `collector/` 那个采集器是同一个模式(kb 只知道一条命令行),理由也一样。

**退出码 0 不代表这一批抓全了**:上游翻页半途失败只打一行日志就 `break`、单条
失败只记一笔状态就继续往下跑,两种都照常返回 0。没认出来就是静默丢数据 ——
水位会推过那几条漏掉的推文,从此谁也捞不回来。判据和后果写在 `_incomplete` /
`_batch_failures`。

**环境变量是白名单 + 显式清场,不是继承**。`OBSIDIAN_VAULT` 必须清掉:
feedgrab 的输出目录优先认它、其次才是 `OUTPUT_DIR`,而这台机器上它指的正是
知识库那个 vault —— 不清掉,抓来的推文会被 feedgrab 直接写进 vault/X/,绕过
feed_items、翻译和导出整条链,表现是 vault 里凭空多出一批谁也管不着的 md
(索引照收,清理找不到)。
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from kb.feeds import x_article
from kb.scanner import parse_frontmatter

# **产物路径不许写死**:feedgrab 的保存位置是「平台目录 + category」拼出来的
# (`utils/storage.py` 的 `save_to_markdown`),而账号推文的 category 是
# `status_author/<显示名>`(`fetchers/twitter_user_tweets.py`,api 那条实现同样)。
# 显示名只过了一遍文件名清理,中文、空格、emoji 全留着 —— 猜不出来,只能整棵扫。
# 归属判据在 `_entry`(推文 URL 里的 handle),不在它落在哪个子目录。

# 单个账号的墙上时间上限。它内部会分页、必要时还会起浏览器补抓,给得宽一些。
ACCOUNT_SECONDS = 180.0
# 没有水位时(新订阅)往回要多少天。真正的下界由 `sync._within_history` 钉死。
FIRST_DAYS = 14
# 这些变量会改变 feedgrab 往哪里写、用谁的身份,一律由我们显式给值。
_OWNED_ENV = ('OUTPUT_DIR', 'OBSIDIAN_VAULT', 'FEEDGRAB_DATA_DIR',
              'X_USER_TWEETS_ENABLED', 'X_USER_TWEETS_SINCE')
# 透传的变量:网络要代理、Chrome/playwright 要 HOME 和 PATH。
_PASS_ENV = ('PATH', 'HOME', 'LANG', 'LC_ALL', 'TMPDIR', 'SSL_CERT_FILE',
             'HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'NO_PROXY',
             'http_proxy', 'https_proxy', 'all_proxy', 'no_proxy')
# 没登录时它的报错里会有这些字样 —— 这一档要翻成"去跑这条命令",
# 而不是把一行 CLI 报错原样糊到面板上。
_LOGIN_HINTS = ('login twitter', 'Twitter Cookie', '未找到有效 Cookie', 'cookie')
# 上游批量记录里"这一条没问题"的两种状态(`fetched` 抓到了 / `skipped` 它自己
# 按去重或互动阈值跳的)。别的一律当成没抓下来 —— 见 `_batch_failures`。
_OK_STATUS = ('fetched', 'skipped')
# 收尾摘要那一行的形状:`总数: 2, 成功: 1, 跳过: 0, 失败: 1`。要求带上「成功」
# 那半截是为了不被日志里的「失败: @某人 - 原因」「补充抓取失败: …」蒙到 ——
# 误报会把水位永久冻住,那比漏报还难看出来。
_SUMMARY_FAILED = re.compile(r'成功[:：]\s*\d+[^\n]*?失败[:：]\s*(\d+)')
# 翻页半途中断那一行:`[UserTweets] >>> 第 2 页所有 1 个账号均失败 <<< 已抓取 1 条,
# 停止分页`(`fetchers/twitter_user_tweets.py` 的 `if not response` 分支 —— 同一句
# 话在它那几个分页抓取器里逐字相同)。它走 `logger.error`,而上游的 sink 是
# `logger.add(sys.stderr, level=INFO)`(`cli.py`),所以 `_run` 合起来的那份输出
# 里一定有它。
# **要连「第 N 页」「M 个账号」一起认**:松成裸「均失败」会把 `twitter.py` 里
# 那句「所有 GraphQL Cookie 均失败,降级到 FxTwitter」也算进来 —— 那是降级之后
# 照样拿到了内容,不是漏,而误报把水位**永久**冻住(见 `_incomplete`)。
# **这条判据唯一要防住的事是:别把用户自己的内容当成一次故障。** 办法是只认
# 上游那句话的**形状**,不是它的几个关键词,三道各有射程:
# ①`>>>` / `<<<` 两个标记 —— 上游把推文标题和显示名回显进**路径**时,
#   `utils/storage.py` 的 `_ILLEGAL_CHARS_RE` 和 `_sanitize_folder_name` 都把
#   `<` `>` 换成空格,所以路径这条通道带不进标记;
# ②**紧挨在 `>>>` 前面的那个方括号标签**(`[{mode_label}] `,就是上游那句
#   f-string 自己的消息前缀)。显示名是写在 `用户信息: @X (…)` 的圆括号里的,
#   它前面是 `(` 不是 `] `;
# ③**整句话必须落在行尾**(`已抓取 N 条,停止分页` 之后这一行就结束了)。这一道
#   **跟用户内容写成什么样没有关系**:那条回显后面永远还跟着 `), ID: …`,所以
#   不管昵称里写什么都够不到行尾 —— ② 能被一个自带 `[x] >>>` 的昵称绕过去,
#   ③ 不行,它才是这条通道的结构性闸门。
# 前两道各漏过一次,都是实测出来的:裸认那句话时,一条标题里逐字写着它的推文让
# 面板永久显示「部分同步」、水位永久冻在原处(2026-10-09 第 4 轮审查);补上标记
# 和路径那两道之后,**显示名**又从另一条不带路径的 INFO 日志进来了 ——
# `[UserTweets] 用户信息: @Example (>>> 第 2 页所有 1 个账号均失败 <<<), ID: …`
# (`twitter_user_tweets.py:171`,原样回显、不过任何清理):两条推文都抓到了、
# 上游摘要写着「失败: 0」、stderr 里一行 ERROR 都没有,面板照样报翻页失败,再
# 同步也不恢复(2026-10-09 第 5 轮审查,同样拿上游原版 `fetch_user_tweets` 复现)。
# **没有改成按日志级别判**(审查建议的那条路):`ERROR` 这个词出现在哪儿取决于
# loguru 的格式,而这条判据**漏报一次就是静默丢数据**(水位越过没读到的那一段),
# 误报顶多是下一轮重叠重抓 —— 两个方向差着一个量级,所以不拿一个会随格式变化的
# 条件去 AND 掉一次真中断;昵称这条通道由 ③ 结构性地关掉,级别那一项加上去也
# 不多关掉任何一条已知的路。
# **已知留着的一道缝,记在这儿备查**:显示名里塞一个换行能自己伪造出一整行(那
# 时 ③ 的行尾就是昵称自己的结尾)。X 的 name 字段不接受换行,而要防住它只能逐位
# 解析 loguru 的前缀 —— 那正是上一段不做的那件事。
# 第三道(跳过回显了本轮产物目录的行)在 `_aborted`。
_PAGE_ABORT = re.compile(
    r'\[[^\[\]\n]+\]\s*>>>\s*第\s*\d+\s*页所有\s*\d+\s*个账号均失败\s*<<<\s*'
    r'已抓取\s*\d+\s*条[,，]\s*停止分页\s*\Z')


class FeedgrabError(RuntimeError):
    """feedgrab 通道的失败。message 是成品文案。"""


def login_command(data_dir: str, binary: str = '') -> str:
    """给用户照抄的登录命令。登录是交互式的(扫码/填 cookie),只能他自己跑。

    **可执行文件和采集时用的必须是同一个**(所以这里过一遍 `find_binary`)。
    原来写死裸 `feedgrab`:用户把它装在独立虚拟环境里、只在面板上填了绝对路径
    (`harvest`/`doctor` 都认这个设置)时,照页面抄下来那条命令在终端里是
    `feedgrab: command not found`,首次配置从此走不通 —— 而面板上明明显示着
    那条可用的路径(2026-10-08 审查实测 `exit 127`)。保存设置不会往用户终端的
    PATH 里加任何东西,所以这条提示得自己把路径带上。

    两头都 `shlex.quote`:路径里有空格时不 quote 的命令会被 shell 拆成两段。
    """
    try:
        exe = find_binary(binary)
    except FeedgrabError:
        # 配了路径但当下不可执行时照它原样显示 —— 用户看得见自己填错在哪儿,
        # 「路径不可执行」那句话由面板的就绪判断单独说。两头都没有才退回裸命令
        # (那一档面板写的是"装好之后在通道设置里填它的完整路径")。
        exe = binary.strip() or 'feedgrab'
    return (f'FEEDGRAB_DATA_DIR={shlex.quote(data_dir)} '
            f'{shlex.quote(exe)} login twitter')


def find_binary(explicit: str = '') -> str:
    if explicit:
        if not os.access(explicit, os.X_OK):
            raise FeedgrabError(f'指定的 feedgrab 路径不可执行:{explicit}')
        return explicit
    if found := shutil.which('feedgrab'):
        return found
    raise FeedgrabError('没找到 feedgrab 命令。装好之后在通道设置里填它的完整路径'
                        '(feedgrab 不在 PyPI,要从 GitHub 安装)')


def _env(output: Path, data_dir: str, since: str) -> dict:
    env = {k: os.environ[k] for k in _PASS_ENV if k in os.environ}
    env.update({
        'OUTPUT_DIR': str(output),
        'OBSIDIAN_VAULT': '',            # 见模块注释:不清掉会写进知识库的 vault
        'FEEDGRAB_DATA_DIR': data_dir,
        'X_USER_TWEETS_ENABLED': 'true',  # 不开这个它拒绝按账号抓
    })
    if since:
        env['X_USER_TWEETS_SINCE'] = since
    return env


def _published(meta: dict) -> str:
    """feedgrab 的 published 只到天(它按 `%Y-%m-%d` 格式化),原样用。

    比浏览器通道粗一档,这是这条路的固有精度:`X_USER_TWEETS_SINCE` 本身也只
    接受日期。所以水位按天记、每轮重叠一天重抓 —— `put_items` 按 URL 去重
    (键是 `sha256(kind:url)` + `ON CONFLICT DO NOTHING`),重叠不会产生重复条目。
    """
    value = str(meta.get('published') or '').strip()
    return value[:16] if value else ''


def _entry(meta: dict, body: str, handle: str) -> dict | None:
    """一条产物 md → feed_items 的条目形状。

    **置顶照收**(口径同浏览器通道的 `_in_timeline`):原来按 `is_pinned` 丢掉,
    理由是"它每天都在"。可那是在拿一个不存在的问题换一个真的:`put_items` 按
    `sha256(kind:url)` + `ON CONFLICT DO NOTHING` 去重,旧置顶重复出现一条都不会
    重复入库;而**刚发出来就被钉上去**的推文(宣布新版本、活动预告 —— 正是最该
    收的那种)被这一句永久丢掉,下一轮它的日期已经低于水位,再也补不回来。
    """
    url = str(meta.get('source') or '').strip()
    if not url or f'/{handle.lower()}/status/' not in url.lower():
        return None                      # 别的号(引用/转推产物)不算这个订阅的
    author = meta.get('author')
    name = (meta.get('author_name') or
            (author[0] if isinstance(author, list) and author else author) or handle)
    title = str(meta.get('title') or '').strip() or f'{name} 的动态'
    text = body.strip()
    if not text and not title:
        return None
    return {'url': url, 'title': title[:500], 'body': text or title,
            'author': str(name), 'published': _published(meta),
            'content_kind': 'post'}


def _read_output(folder: Path, handle: str) -> list[dict]:
    """把这一轮的产物目录**整棵**读下来。

    原来按 `X/*.md` 只扫顶层,而账号推文落在 `X/status_author/<显示名>/` 里
    (见上面那段注释),于是**抓到的推文一条也读不到**:`harvest` 把这个空结果
    当成功存下来、随后把产物目录删掉,面板上是「已同步 · 0 条」而知识库里什么
    都没有 —— 错得最狠的一种,因为它长得和"这个号今天没发推"一模一样
    (2026-10-08 审查实测,产物落在 `X/status_author/Example Person/` 下)。

    递归不放松归属:这个目录是本轮为这个账号新建的(`harvest` 里先
    `rmtree` 再 `mkdir`),而真正的判据是 `_entry` 按推文 URL 里的 handle 过滤,
    跟文件在哪一层没有关系。顺带也不再漏掉顶层那些产物(单条推文/长文线程)。
    """
    entries, broken = [], 0
    for path in sorted(folder.rglob('*.md')):
        try:
            meta, body = parse_frontmatter(path.read_text(encoding='utf-8'))
        except (OSError, UnicodeDecodeError, ValueError):
            broken += 1
            continue
        if entry := _entry(meta, body, handle):
            entries.append(entry)
    if broken and not entries:
        raise FeedgrabError(f'@{handle}:feedgrab 产出了 {broken} 个读不出来的文件')
    return entries


def _batch_failures(folder: Path) -> int:
    """这一批有几条没抓下来。证据是上游自己落的**批量记录**。

    `fetch_user_tweets` 逐条抓,**单条异常只记一笔 `status: failed` 就继续**
    (`fetchers/twitter_user_tweets.py` 的 except 分支),收尾把整份清单写进
    `{OUTPUT_DIR}/X/index/status_<号>_*.json`(`_save_batch_record` →
    `utils/dedup.get_index_path().parent`)—— 就在我们这一轮为这个账号新建的那个
    产物目录里面,所以里面的每一条都是这一次的,不会混进别轮别号。

    **认形状不认路径**(`rglob('*.json')` + 「有一个 tweets 列表」):同一份记录
    有两个写入方,文件名前缀还不一样(GraphQL 那条是 `status_`、TwitterAPI.io
    那条是 `api_status_`),按名字取等于漏掉一半。去重索引
    (`index/item_id_url.json`)是个 `{item_id: url}` 的字典,这道形状判据正好
    把它排除掉。

    **只有 `fetched`/`skipped` 算没问题**,别的状态一律当成没抓下来:漏报是静默
    丢数据(见 `_incomplete`),误报的代价只是下一轮重叠重抓 + 面板上一句
    「部分同步」。两个方向的代价差着一个量级,所以宁可多报。
    """
    failed = 0
    for path in sorted(folder.rglob('*.json')):
        try:
            payload = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, UnicodeDecodeError, ValueError):
            continue
        rows = payload.get('tweets') if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            continue
        failed += sum(1 for row in rows if isinstance(row, dict)
                      and str(row.get('status') or '') not in _OK_STATUS)
    return failed


def _aborted(output: str, folder: Path) -> bool:
    """这份输出里有没有那行「翻页半途中断」。**逐行看,而且先把路径回显摘掉。**

    真正防住用户内容的一共三道,各有射程,缺任何一道都会把推文正文或作者昵称当成
    一次故障(三道分别是什么、各自漏过哪一次,见 `_PAGE_ABORT` 那段注释):

    ①②`>>>` / `<<<` 两个标记、紧挨着它的方括号标签、以及**整句话落在行尾**,
      都在 `_PAGE_ABORT` 自己身上。
    ③**跳过回显了本轮产物目录的行**:上游打的保存路径(`Saved to Markdown:`、
      `批量记录已保存:`)都在这个目录下面,而路径里带着推文标题和显示名。真正那
      句报错里压根没有路径,所以跳过它不会漏报;这一道**不依赖上游的清理规则**
      —— 清理哪天放开 `<` `>`,只剩标记那一道就守不住了。

    **逐行看是 ③ 的前提**(要按行决定跳不跳),同时也是「整句话落在行尾」那一道
    的量词:行尾说的是**这一行**的结尾,不是整份输出的结尾 —— 在整份输出上
    `search` 的话,只有最后一行上的那句话才算得上落在行尾。
    """
    own = str(folder)
    return any(_PAGE_ABORT.search(line) for line in output.splitlines()
               if own not in line)


def _incomplete(output: str, folder: Path) -> str:
    """「这一批抓全了吗」。返回没读完的原因,全抓到了就是空串。

    **退出码 0 不等于全成功**:上游单条失败只往批量记录里记一笔就继续,收尾
    把计数包成一句普通正文(`reader.py` 的 `summary`),CLI 那条账号主页分支
    `print` 完直接 return —— 只有抛异常才 `sys.exit(1)`(`cli.py`)。所以
    `_run` 不报错的那一轮里完全可能有几条没抓下来。

    **而这一档不认出来就是静默丢数据**:水位推到"这一轮抓到的最新那天",下一轮
    的 `X_USER_TWEETS_SINCE` 就是它,上游按这个下界把更早的推文整段丢掉
    (`stop_pagination`)—— 漏掉那几条比旧水位新、又比新水位老,**从此任何一轮
    增量都选不到**。审查两次各实测一遍:面板报「已同步 · 新增 1 条」、`errors`
    和 `partial` 都是空的、水位从 10-05 跳到 10-08;外部故障解除之后再点同步
    `added=0`,那条推文再也回不来。

    「没抓全」有**两个来源不同的档**,各自的证据也不在一处:

    ①**翻页半途中断**(`_PAGE_ABORT`)。某一页所有轮换账号都失败时,上游打一行
      `logger.error` 就 `break`(`fetchers/twitter_user_tweets.py` 的
      `if not response`)—— 这一页**压根没拿到**,没拿到的条目不会进 `tweet_list`,
      所以 `failed` 一个都不加:批量记录里只有成功那几行、摘要写的是「失败: 0」。
      下面那两道判据各跑一遍都是 0,证据只有那行日志(2026-10-09 第 3 轮审查拿
      上游原版 `fetch_user_tweets` 复现)。**这一档只有这一个证据源** —— 它的
      缺口在"没拿到的那一页"里,条数、日期我们一个都看不到,没有结构化的第二道
      可言;「我们拿到的最早那条还没到下界」倒是看得见,但那证明不了漏(这个号
      本来就可能没有更早的推文),所以不能当判据。正因为只有这一个证据源,认它
      的时候要认准:怎么把这一行和回显了用户内容(推文标题、作者昵称)的日志分开,
      见 `_PAGE_ABORT` 和 `_aborted`。
    ②**逐条失败**:批量记录里的逐条状态(`_batch_failures`),或者收尾摘要那行
      文本里的「失败: N」。这两道**不是同一份实现的两种写法** —— 记录换了位置/
      改了文件名时还有文本兜着,摘要改了措辞时还有记录兜着。而且摘要那道自己
      就有一类独占的证据:上游那条「补充抓取」(UserTweets 没够到下界时用搜索往
      回补)**只把计数并进摘要,不往批量记录里写**(`tweet_list` 只在它自己那个
      循环里追加,`_save_batch_record` 排在补充抓取之后),所以补充抓取里逐条
      失败的那些,只有摘要数得出来。

    两档同时命中就两句话都说:它们是不同的原因,用户的下一步也不一样。

    **这里仍然不管「补充抓取整段失败/没开」那一档**:它是第三个形状 —— 上游
    `logger.warning('补充抓取失败: …')` 之后 `failed` 一个都不加,缺口在 `since`
    和它实际够到的最早那天之间,而这两个数我们在摘要里都看不到。要认它就得按
    那句警告的措辞判,而它一旦是长期坏的(没浏览器、没 API key),水位就**永久**
    冻在原处、每一轮重抓整个窗口 —— 那个取舍得单独算,不在本轮的射程里。①和它
    的区别是:①有一行**明确说了"这一页失败了、停止分页"**的 `logger.error`,
    不是一句笼统的警告。
    """
    notes = []
    if _aborted(output, folder):
        notes.append('feedgrab 翻页中途失败,没有读到上次的位置')
    failed = _batch_failures(folder) or (
        int(hit.group(1)) if (hit := _SUMMARY_FAILED.search(output)) else 0)
    if failed:
        notes.append(f'feedgrab 这一批有 {failed} 条没抓下来')
    return ';'.join(notes)


def _run(binary: str, handle: str, env: dict, cwd: Path) -> str:
    """跑一次抓取。返回它的输出(给失败时的文案用)。"""
    try:
        proc = subprocess.run(
            [binary, f'https://x.com/{handle}'], env=env, cwd=cwd,
            capture_output=True, text=True, timeout=ACCOUNT_SECONDS,
            stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        raise FeedgrabError(f'@{handle}:feedgrab 超过 {int(ACCOUNT_SECONDS)} 秒没有返回') from None
    except OSError as e:
        raise FeedgrabError(f'feedgrab 执行失败:{e}') from None
    output = ((proc.stdout or '') + '\n' + (proc.stderr or '')).strip()
    if proc.returncode != 0:
        tail = output[-600:] or f'退出码 {proc.returncode}'
        if any(h.lower() in output.lower() for h in _LOGIN_HINTS):
            raise FeedgrabError('feedgrab 还没有 X 登录态。在终端跑一次登录命令后重试')
        raise FeedgrabError(f'@{handle}:feedgrab 失败 —— {tail}')
    return output


def harvest(jobs: list[tuple[str, str]], *, binary: str = '', data_dir: str,
            workdir: str, budget: float = 0.0, on_progress=None,
            should_stop=None) -> dict[str, dict]:
    """逐个账号跑 feedgrab,返回和浏览器通道同形状的结果。

    jobs:  [(handle, 水位 'YYYY-MM-DD' 或空串)]
    返回:  {handle: {'entries': [...], 'error': '', 'incomplete': '没抓全的原因',
                     'watermark': 'YYYY-MM-DD'}}
    """
    results: dict[str, dict] = {}
    if not jobs:
        return results
    if should_stop and should_stop():
        # 停止先于一切:否则 find_binary 失败时整批报的是"没装 feedgrab",
        # 而用户按的是停止 —— 那会在面板上留下一轮假故障。
        return {h: {'entries': [], 'error': '', 'skipped': '已按停止请求中断'}
                for h, _ in jobs}
    try:
        exe = find_binary(binary)
    except FeedgrabError as e:
        return {handle: {'entries': [], 'error': str(e)} for handle, _ in jobs}
    base = Path(workdir)
    base.mkdir(parents=True, exist_ok=True)
    Path(data_dir).mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + budget if budget else None
    for index, (handle, water) in enumerate(jobs, 1):
        stopping = should_stop() if should_stop else False
        if stopping or (deadline and time.monotonic() > deadline):
            results[handle] = {
                'entries': [], 'error': '',
                'skipped': '已按停止请求中断' if stopping
                           else '本轮时间预算用完,留待下次同步'}
            continue
        if on_progress:
            on_progress(index, len(jobs), handle)
        # 每个账号一个干净的输出目录:共用一个的话,这一轮读到的 md 里会混进
        # 上一个账号(以及上一轮)的产物,而"哪些是这次抓的"没有别的判据。
        out = base / f'run-{handle.lower()}'
        shutil.rmtree(out, ignore_errors=True)
        out.mkdir(parents=True, exist_ok=True)
        # 这一轮真正交给上游的那个下界。有水位时它就是水位,没有(新订阅首轮)
        # 时按「当天往回 FIRST_DAYS 天」现算一个。
        since = (water or (datetime.now(timezone.utc)
                           - timedelta(days=FIRST_DAYS)).strftime('%Y-%m-%d'))[:10]
        try:
            output = _run(exe, handle, _env(out, data_dir, since), base)
            entries = _read_output(out, handle)
            # 长文(Article)推文的产物正文就是一条指向 `x.com/i/article/<id>` 的
            # 链接,正文要单独取一次(见 `x_article`)。
            entries, missing = x_article.fill(entries)
            # **没抓全就不推进水位 —— 存回这一轮真正用过的那个下界**(口径同浏览器
            # 通道那句 `started if incomplete`)。抓到的照旧入库,漏掉的那几条下
            # 一轮重叠重抓,`put_items` 按 `sha256(kind:url)` +
            # `ON CONFLICT DO NOTHING` 去重;不留着下界的话它们从此任何一轮增量
            # 都选不到(见 `_incomplete`)。
            # **首轮尤其要存**:新订阅的水位是空的,「留着旧水位」在它身上什么都
            # 没留下 —— 空值压根不产生 config 补丁(见 `x_channels.watermark_patch`),
            # 于是下一轮又现算一个「当天往回 FIRST_DAYS 天」,那段没读完的日期
            # **随时间往前滑走**。实测:9-20 建的订阅首轮翻页中断只收进 1 条,
            # 10-01 恢复之后下界变成 9-17,9-15 那条推文再也选不到,而面板上写的
            # 是「已同步」(2026-10-09 第 4 轮审查 —— 那条订阅自己的
            # `history_floor` 还停在 9-15,按原下界取回来的那条照样过得了
            # `sync._within_history`,所以不是历史闸门拦的)。
            # 存回下界本身不会漏掉边界那一天:上游的日期过滤是
            # `tweet_date < since_date` 才丢(`fetchers/twitter_user_tweets.py`,
            # 含下界当天),所以下一轮的窗口和这一轮逐字相同。
            note = _incomplete(output, out)
            if missing:
                note = f'{note};{missing}' if note else missing
            results[handle] = {'entries': entries, 'error': '', 'incomplete': note,
                               'watermark': since if note else _newest(entries, water)}
        except FeedgrabError as e:
            results[handle] = {'entries': [], 'error': str(e)}
        finally:
            shutil.rmtree(out, ignore_errors=True)
    return results


def _newest(entries: list[dict], fallback: str) -> str:
    """这一轮见过的最新日期。一条都没抓到时沿用旧水位,不要宣布"抓到今天"。"""
    stamps = [e['published'][:10] for e in entries if e.get('published')]
    return max(stamps) if stamps else fallback


def doctor(binary: str, data_dir: str) -> tuple[bool, str]:
    """问一次「登录态还在吗」。返回 (就绪, 成品文案)。"""
    try:
        exe = find_binary(binary)
    except FeedgrabError as e:
        return False, str(e)
    try:
        proc = subprocess.run([exe, 'doctor', 'x'], env=_env(Path(data_dir) / '_doctor',
                              data_dir, ''), cwd=data_dir, capture_output=True,
                              text=True, timeout=60, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, f'feedgrab 体检没跑完:{e}'
    text = (proc.stdout or '') + (proc.stderr or '')
    if '❌' in text and any(h.lower() in text.lower() for h in _LOGIN_HINTS):
        return False, '还没有 X 登录态。在终端跑一次登录命令后回来重试'
    if proc.returncode != 0:
        return False, f'feedgrab 体检未通过:{text.strip()[-300:]}'
    return True, 'feedgrab 已就绪'
