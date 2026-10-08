# -*- coding: utf-8 -*-
"""feedgrab 通道:子进程调用用户自己装好的 feedgrab CLI。

**为什么是子进程而不是抄代码进来**:这条路要的是 X 内部 GraphQL 接口 + 那套
`x-client-transaction-id` 签名,签名算法要从登录态页面下发的 manifest 里取
(游客页永远拿不到),feedgrab 自己为此背着 60 多个 fetcher 和 playwright。把它
抄进 kb 等于把这份维护量一起抄过来,而 X 每改一次接口就要跟一次。装在外面、
按 CLI 调用,升级是 `git pull` 的事。

和 `collector/` 那个采集器是同一个模式(kb 只知道一条命令行),理由也一样。

**环境变量是白名单 + 显式清场,不是继承**。`OBSIDIAN_VAULT` 必须清掉:
feedgrab 的输出目录优先认它、其次才是 `OUTPUT_DIR`,而这台机器上它指的正是
知识库那个 vault —— 不清掉,抓来的推文会被 feedgrab 直接写进 vault/X/,绕过
feed_items、翻译和导出整条链,表现是 vault 里凭空多出一批谁也管不着的 md
(索引照收,清理找不到)。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from kb.scanner import parse_frontmatter

# feedgrab 把推文写在 {OUTPUT_DIR}/X/ 下,一条一个 md(PLATFORM_FOLDER_MAP)。
_PLATFORM_DIR = 'X'
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


class FeedgrabError(RuntimeError):
    """feedgrab 通道的失败。message 是成品文案。"""


def login_command(data_dir: str) -> str:
    """给用户照抄的登录命令。登录是交互式的(扫码/填 cookie),只能他自己跑。"""
    return f'FEEDGRAB_DATA_DIR={data_dir} feedgrab login twitter'


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
    entries, broken = [], 0
    for path in sorted(folder.glob('*.md')):
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
    返回:  {handle: {'entries': [...], 'error': '', 'watermark': 'YYYY-MM-DD'}}
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
        since = water or (datetime.now(timezone.utc)
                          - timedelta(days=FIRST_DAYS)).strftime('%Y-%m-%d')
        try:
            _run(exe, handle, _env(out, data_dir, since[:10]), base)
            entries = _read_output(out / _PLATFORM_DIR, handle)
            results[handle] = {'entries': entries, 'error': '',
                               'watermark': _newest(entries, water)}
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
