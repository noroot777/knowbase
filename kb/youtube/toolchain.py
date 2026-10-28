# -*- coding: utf-8 -*-
"""yt-dlp 的解析、版本与更新 —— 「我的订阅 → YouTube」那颗更新按钮的后端。

**硬约束在 `docs/decisions/youtube-intake.md` 第一节 —— 改这块之前先读那一份。**

**解析顺序是「自备优先、PATH 兜底」**(和 `settings.model_path` 同一个模式):自备那份装在
`DATA_DIR/tools/ytdlp` 的 venv 里。用户点过更新之后就该用上新版,而 PATH 里那份常年落后
(实测本机 brew 的 2026.06.09 对上游 2026.10.26 差了四个多月)。

**只往数据目录装,不动用户的系统包管理器。** `yt-dlp -U` 对 brew 装的那份会直接报错
(实测「You installed yt-dlp with pip or using the wheel from PyPi; Use that to update」),
所以更新既不能走它,也不该替用户跑 brew —— 那是在改他的系统状态,而且 Windows 上没有对应物。

**版本旧不是故障**:yt-dlp 跟 YouTube 的对抗靠版本跟进,但**旧版本不是「抓不到」的充分原因**
(实测 2026.06.09 和 2026.10.26 撞同一道 bot 墙),所以这里只报版本、不把它当错误 ——
别让用户以为点一下更新就能解决登录态的问题。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

PACKAGE = 'yt-dlp'
_PYPI = 'https://pypi.org/pypi/yt-dlp/json'
# 同时点两次更新会让两个 pip 装同一个 venv。单进程内一把锁就够(管道也跑在这个进程里)。
_INSTALL = threading.Lock()
#: 指针文件:内容是 `envs/` 下那一份环境的目录名。更新**不动正在用的那份**,
#: 而是新建一份、自检过了再原子改这个文件 —— 见 `_install_now`。
_POINTER = 'current.txt'
#: 发布之后留几份旧环境。留一份是给「发布那一刻正好有一轮采集已经解析过旧路径」
#: 兜底;留太多纯占磁盘(一份 venv 三十多兆)。
_KEEP = 2


def home() -> Path:
    from kb.config import DATA_DIR
    return DATA_DIR / 'tools' / 'ytdlp'


def _entry(env: Path) -> Path:
    directory = 'Scripts' if os.name == 'nt' else 'bin'
    return env / directory / ('yt-dlp.exe' if os.name == 'nt' else 'yt-dlp')


def _pointed() -> Path | None:
    """指针指到的那份环境;读不出、指到了别处、或那份已经不完整时返回 None。

    指针是**文件**而不是符号链接:`os.replace` 对文件在 POSIX 和 Windows 上都是原子的,
    而 Windows 建符号链接要特权(同 `docs/decisions/windows-entry.md` 那一类口径)。
    """
    try:
        name = (home() / _POINTER).read_text(encoding='utf-8').strip()
    except OSError:
        return None
    if not name or name.startswith('.') or '/' in name or '\\' in name or os.sep in name:
        return None
    target = home() / 'envs' / name
    return target if _entry(target).is_file() else None


def binary() -> Path:
    """自备那份 yt-dlp 的入口文件 —— **指针指到哪份就用哪份**。

    指针不在(存量库、或者那次更新没能走到发布)就退回旧布局:venv 直接装在 `home()`。
    旧布局那份一直留着不删,它同时是指针文件丢了之后的兜底。
    """
    pointed = _pointed()
    return _entry(pointed) if pointed else _entry(home())


def command() -> list[str]:
    """跑 yt-dlp 的命令前缀。自备那份优先,没有就用 PATH 里的。"""
    bundled = binary()
    if bundled.is_file() and os.access(bundled, os.X_OK):
        return [str(bundled)]
    found = shutil.which('yt-dlp')
    return [found] if found else []


def origin() -> str:
    bundled = binary()
    if bundled.is_file() and os.access(bundled, os.X_OK):
        return 'bundled'
    return 'path' if shutil.which('yt-dlp') else ''


def _run(arguments: list[str], timeout: float) -> tuple[int, str]:
    try:
        done = subprocess.run(arguments, capture_output=True, text=True, timeout=timeout,
                              encoding='utf-8', errors='replace')
    except FileNotFoundError:
        return 127, f'找不到命令 {arguments[0]}'
    except subprocess.TimeoutExpired:
        return 124, '命令超时'
    return done.returncode, ((done.stdout or '') + (done.stderr or '')).strip()


def _version_of(prefix: list[str]) -> str:
    """问**指定那一份** yt-dlp 的版本号。读不出来返回空串。

    自检要问的是刚装好那一份,而不是 `command()` 解析出来的那份 —— 发布之前
    `command()` 还指着旧环境,拿它自检等于用旧版给新版背书。
    """
    if not prefix:
        return ''
    code, text = _run([*prefix, '--version'], 30)
    if code:
        return ''
    # `--version` 只印一行版本号;nightly 之类带后缀的照原样返回。
    return text.splitlines()[0].strip() if text else ''


def installed_version() -> str:
    return _version_of(command())


def latest_version(deadline: float | None = None) -> str:
    """问 PyPI 最新版。走项目自己那套请求(含公网校验),不另开一条出口。"""
    from kb.feeds.fetching import request
    response = request(_PYPI, deadline=deadline or (time.monotonic() + 20))
    payload = json.loads(response.body.decode('utf-8', errors='replace'))
    version = str((payload.get('info') or {}).get('version') or '').strip()
    if not version:
        raise RuntimeError('PyPI 没有返回 yt-dlp 的版本号')
    return version


def _parts(version: str) -> tuple:
    return tuple(int(piece) if piece.isdigit() else -1
                 for piece in version.replace('stable@', '').split('.'))


def outdated(installed: str, latest: str) -> bool:
    """只在两边都解析成纯数字版本时才敢说「旧」—— 解析不了一律不报,免得拿 nightly 当旧版。"""
    if not installed or not latest:
        return False
    left, right = _parts(installed), _parts(latest)
    if -1 in left or -1 in right:
        return False
    return left < right


def local() -> dict:
    """**便宜的那一半**:只问本地装的是哪一版、从哪儿来。面板每次刷新都读它,
    所以这里不碰网络 —— 查上游版本是用户点「检查」时才做的事(见 `status`)。"""
    return {'installed': installed_version(), 'origin': origin()}


def upgrade(timeout: float = 600) -> dict:
    """把自备那份装成最新版。返回装完之后的实际版本。

    **先建 venv 再装**:`--target` 不生成可执行脚本,而 `--prefix` 的布局跨平台不一致。
    venv 两边都有确定的 `bin/`、`Scripts/`。

    **装完自检过了才发布,整个过程不碰正在用的那一份。** 原地 `pip install --upgrade`
    会在卸载和安装之间留一段「环境缺文件」的窗口,而这段时间里采集和登录检查照样会
    起 yt-dlp(`_INSTALL` 这把锁只有更新自己拿,`command()` 谁都不拿);pip 被超时杀掉
    或者进程中途没了,留下的就是一份半装的环境 —— 而 `command()` 只看入口文件在不在、
    能不能执行,看不出里头缺东西。所以口径是:新环境**建在它自己的最终位置**(不许建完
    再改名,venv 脚本里的 shebang 是绝对路径)、跑一次 `--version` 自检、过了才原子
    改指针;任何一步没过就丢掉这份新的,原来那份一个字节都没动过。
    """
    if not _INSTALL.acquire(blocking=False):
        raise RuntimeError('已经有一次 yt-dlp 更新正在进行，等它结束再试')
    try:
        return _install(timeout)
    finally:
        _INSTALL.release()


def _install(timeout: float) -> dict:
    try:
        return _install_now(timeout)
    except OSError as error:
        # **文件系统那一类失败要带上原因。** `intake_api` 只把 ValueError/RuntimeError
        # 翻成 JSON,`OSError` 漏出去就是 Starlette 的纯文本 500 —— 前端 `r.json()`
        # 当场炸,「只读目录」「权限不足」「磁盘满」全被吞成一句无信息的「失败」。
        raise RuntimeError(f'写不了 yt-dlp 的安装目录（{home()}）：{error}') from None


def _install_now(timeout: float) -> dict:
    created = not binary().is_file()
    envs = home() / 'envs'
    envs.mkdir(parents=True, exist_ok=True)
    # 名字带上时间和 pid:同名撞车会把别人正在装的那份当成自己的。
    target = envs / f'{time.strftime("%Y%m%d-%H%M%S", time.localtime())}-{os.getpid()}'
    try:
        code, text = _run([sys.executable, '-m', 'venv', str(target)], timeout=180)
        if code:
            raise RuntimeError(f'创建 yt-dlp 运行环境失败：{text[-400:]}')
        python = target / ('Scripts' if os.name == 'nt' else 'bin') / ('python.exe' if os.name == 'nt' else 'python')
        if not python.is_file():
            raise RuntimeError('yt-dlp 运行环境不完整，缺少 python 可执行文件')
        code, text = _run([str(python), '-m', 'pip', 'install', '--disable-pip-version-check',
                           PACKAGE], timeout=timeout)
        if code:
            raise RuntimeError(f'安装 yt-dlp 失败：{text[-400:]}')
        if not _version_of([str(_entry(target))]):
            raise RuntimeError('装完之后这一份 yt-dlp 跑不出版本号，已经丢掉它；'
                               '原来那份没有动过，仍然可用')
    except BaseException:
        shutil.rmtree(target, ignore_errors=True)
        raise
    _publish(target.name)
    _prune(target.name)
    # **键集合必须跟 `status()` 一样**:面板上那一行是同一个函数渲染的,它认的是
    # `installed`。这里回一个只有 `version` 的字典,装成功之后那行会变成红字
    # 「没有找到 yt-dlp」—— 装上了却报没找到,比不报更糟。回归钉在
    # tests/test_youtube_fallback.py 的 Toolchain 里。
    return {**status(), 'created': created}


def _publish(name: str) -> None:
    """把指针原子地指到 `name`。**这是「新版生效」的唯一时刻。**"""
    pointer = home() / _POINTER
    scratch = pointer.with_name(f'{pointer.name}.{os.getpid()}.tmp')
    scratch.write_text(name, encoding='utf-8')
    os.replace(scratch, pointer)


def _prune(current: str) -> None:
    """清掉过旧的环境。**清不掉不是失败** —— 新版已经发布了,这里只是回收磁盘。

    (Windows 上删正在跑的那份会失败;POSIX 上删掉目录对已经打开的进程无影响。)
    """
    try:
        names = sorted(item.name for item in (home() / 'envs').iterdir() if item.is_dir())
    except OSError:
        return
    keep = set(names[-_KEEP:]) | {current}
    for name in names:
        if name not in keep:
            shutil.rmtree(home() / 'envs' / name, ignore_errors=True)


def status(deadline: float | None = None) -> dict:
    """给面板用的一份状态。**查不到上游版本不是故障**,只是少报一行。"""
    installed = installed_version()
    result = {'installed': installed, 'origin': origin(), 'latest': '',
              'outdated': False, 'latest_error': ''}
    if not installed:
        return result
    try:
        result['latest'] = latest_version(deadline)
    except Exception as error:
        result['latest_error'] = str(error)[:200]
        return result
    result['outdated'] = outdated(installed, result['latest'])
    return result
