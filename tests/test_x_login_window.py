# -*- coding: utf-8 -*-
"""登录窗口的生死:谁的窗口、什么时候关、关之后面板怎么说。

**判据一律落在后果上**,不是"标志打上没有":进程是不是真的没了(`os.kill(pid, 0)`)、
记录文件还在不在、面板那一行写的是哪句话。只验"调没调 release"的话,一个把窗口
记下来却从不动它的实现也能全绿。

**这里的"登录窗口"是真进程**,不是打桩的 pid:`subprocess.Popen` 起一个睡着的
`sleep`,再照 Chrome 的样子把 `SingletonLock` 链接成 `<主机名>-<pid>`。用一个假 pid
会让"它还活着吗"那道判据整条失效 —— 而那道正是"只关我们自己那一个"的安全边界。

反过来钉的那几边在 `ForeignWindowTests`:别人的窗口、坏记录、陈旧记录,一个都不许
碰。杀错进程比堵着一条采集通道严重得多。

跑:  python -m unittest tests.test_x_login_window -v
"""
import json
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from kb.feeds import x_login_window as xlw


def _sleeper() -> subprocess.Popen:
    """一个什么都不做、能被信号打断的真进程。"""
    return subprocess.Popen(['sleep', '300'], stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


class LoginWindowBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.profile = self._tmp.name
        self.procs: list[subprocess.Popen] = []
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        for proc in self.procs:
            try:
                proc.kill()
                proc.wait(timeout=5)
            except Exception:                            # noqa: BLE001 - 收尾
                pass
        self._tmp.cleanup()

    def _hold(self, pid: int | None = None) -> int:
        """照 Chrome 的样子占住这个 profile,返回占着它的 pid。"""
        if pid is None:
            proc = _sleeper()
            self.procs.append(proc)
            pid = proc.pid
        link = Path(self.profile) / 'SingletonLock'
        if link.is_symlink() or link.exists():
            link.unlink()
        link.symlink_to(f'testhost-{pid}')
        return pid

    def _note(self, pid: int, age: float = 0.0, ident: tuple | None = None) -> None:
        """照真实那份的样子写记录:**身份和 pid 一起**(`_proc_identity`)。

        只写 pid 的那种是上一版的格式,它在新口径下一律「认不出」—— 那一档单独
        有用例钉(`test_a_note_without_an_identity_is_not_trusted`)。
        """
        born, exe = ident if ident is not None else xlw._proc_identity(pid)
        (Path(self.profile) / xlw._LOGIN_NOTE).write_text(
            json.dumps({'pid': pid, 'at': time.time() - age,
                        'born': born, 'exe': exe}), encoding='utf-8')

    def _note_exists(self) -> bool:
        return (Path(self.profile) / xlw._LOGIN_NOTE).exists()


class ReleaseTests(LoginWindowBase):
    """到点的关掉,没到点的不动 —— 两边都钉。"""

    def test_window_past_its_ttl_is_really_killed(self):
        pid = self._hold()
        self._note(pid, age=20 * 60)
        note = xlw.release_login_window(self.profile, ttl=10 * 60)
        # 后果:那个进程真的没了,而不是"标记成该关"
        self.assertFalse(_alive(pid), '到点的登录窗口还活着')
        self.assertIn('自动关掉', note)
        self.assertFalse(self._note_exists(), '关掉之后记录该一起清掉')

    def test_a_window_that_will_not_die_is_not_reported_as_closed(self):
        """**审查 N2**:关不掉的那一档不许说「已自动关掉」,也不许删记录。

        两段宽限期都走满而它还活着(内核态卡死、信号压根没到),原来照旧返回那句
        「已自动关掉…」并把记录删掉 —— 面板上那一行没了、守望和下一次释放都读不到
        它,而那个窗口还占着 profile。用户照着那句话去抓,下一轮照旧起不来,而一个
        线索都没有。

        判据落在**面板那句话和记录还在不在**,不落在「调了几次 kill」。注入的是
        「它死不掉」这件事本身(`_alive` 恒真),不是把关闭函数整个换掉 —— 后者证明
        不了调用方会不会据它改口。
        """
        pid = self._hold()
        self._note(pid, age=20 * 60)
        # 注入的是「信号压根没到」这件事本身(`os.kill` 吞掉它),所以那个进程原封
        # 不动地活着、身份一路核得上 —— 两段宽限期真被走满。宽限期调短只是为了别
        # 让这一局跑 9 秒(`_CLOSE_GRACE` 存在的全部理由)。
        with patch.object(xlw.os, 'kill', lambda *a: None), \
             patch.object(xlw, '_CLOSE_GRACE', (0.05, 0.05)):
            note = xlw.release_login_window(self.profile, ttl=10 * 60)
        self.assertTrue(_alive(pid), '这一局的前提:那个进程还活着')
        self.assertIn('关不掉', note, f'关不掉却照旧报成已关掉:{note}')
        self.assertIn('手动', note, '没告诉人下一步该干什么')
        self.assertNotIn('已自动关掉', note)
        self.assertTrue(self._note_exists(), '关不掉却把那份诊断记录删了')

    def test_an_identity_that_cannot_be_read_is_not_taken_for_a_clean_exit(self):
        """**身份问不出来 ≠ 它已经走了**:这一档不许报已关掉,也不许发信号。

        `_proc_identity` 问的是 `ps`,它超时或读失败时返回 `('', '')`。原来「任何
        对不上原身份」都算确认退出:收个尸、报成功,调用方跟着删掉诊断记录、面板上
        写一句「已自动关掉」—— 而那个窗口还活着、还占着抓取要用的 profile,用户照着
        那句话去抓,下一轮照旧起不来(第 4 轮审查 N1)。而且这一档**下一句就是
        SIGTERM/SIGKILL**:身份不明的那个号可能已经被系统分给别人了,这一刀落下去
        就是误杀。

        触发形态照报告摆:**归属检查刚通过,关闭前再查一次时临时失败**(所以第一问
        给真身份、之后才答不出来)。判据落在面板那句话、记录还在不在、以及**一个
        信号都没发**。
        """
        pid = self._hold()
        self._note(pid, age=20 * 60)
        real, asked, sent = xlw._proc_identity(pid), [], []
        real_kill = xlw.os.kill

        def identity(p):
            asked.append(p)
            return real if len(asked) == 1 else ('', '')

        def kill(p, sig):
            # **0 号不是信号,是 `_alive` 的探活** —— 那一问照旧真做(不然这一局的
            # 「它还活着」就成了打桩打出来的)。真信号只记下来、不发出去。
            if sig == 0:
                return real_kill(p, sig)
            sent.append(sig)

        with patch.object(xlw, '_proc_identity', side_effect=identity), \
             patch.object(xlw.os, 'kill', kill), \
             patch.object(xlw, '_CLOSE_GRACE', (0.05, 0.05)):
            note = xlw.release_login_window(self.profile, ttl=10 * 60)
        self.assertGreater(len(asked), 1, '这一局的前提:关闭前确实又查了一次身份')
        self.assertEqual([], sent, '身份都问不出来,却照旧对那个号发了信号')
        self.assertIn('关不掉', note, f'状态未知却报成已关掉:{note}')
        self.assertNotIn('已自动关掉', note)
        self.assertTrue(self._note_exists(), '状态未知却把那份诊断记录删了')

    def test_a_handle_that_will_not_die_keeps_the_note_too(self):
        """**两份实现各有自己的红**:句柄那条路同口径(`_close_by_handle`)。

        守望线程在「认不出身份、句柄还活着」那一档走它。关不掉就不许删记录 ——
        理由同上一局。
        """
        proc = _sleeper()
        self.procs.append(proc)
        self._hold(proc.pid)
        self._note(proc.pid, age=20 * 60, ident=('对不上的时刻', '/对不上的程序'))

        class Stubborn:
            """关不掉的句柄:信号发得出去,它就是不退。"""

            pid = proc.pid

            def poll(self):
                return None

            def terminate(self):
                pass

            kill = terminate

            def wait(self, timeout=None):
                raise subprocess.TimeoutExpired('chrome', timeout or 0)

        self.assertFalse(xlw._close_by_handle(Stubborn()),
                         '关不掉却报成确认退出')
        self.assertTrue(self._note_exists())

    def test_a_window_that_does_die_is_still_reported_closed(self):
        """**反过来钉一边**:真关掉了照旧说「已自动关掉」、照旧清记录。

        光验「关不掉时改口」的话,一个「永远说关不掉」的实现也全绿 —— 而那会让
        每一轮抓取都念一句假的告警,记录也永远留着。
        """
        pid = self._hold()
        self._note(pid, age=20 * 60)
        note = xlw.release_login_window(self.profile, ttl=10 * 60)
        self.assertIn('已自动关掉', note)
        self.assertFalse(_alive(pid))
        self.assertFalse(self._note_exists())

    def test_window_within_its_ttl_is_left_alone(self):
        """反过来钉一边:用户可能正在输密码、过两步验证,那几分钟缩不短。"""
        pid = self._hold()
        self._note(pid, age=60)
        self.assertEqual(xlw.release_login_window(self.profile, ttl=10 * 60), '')
        self.assertTrue(_alive(pid), '没到点的窗口被关掉了')
        self.assertTrue(self._note_exists())

    def test_countdown_is_read_only(self):
        """面板每次刷新都调它 —— 它一个进程都不许碰、一个字节都不许写。"""
        pid = self._hold()
        self._note(pid, age=60)
        left = xlw.login_window_left(self.profile, ttl=10 * 60)
        self.assertTrue(530 < left <= 540, f'剩余时间不对:{left}')
        self.assertTrue(_alive(pid), 'login_window_left 把窗口关了')
        self.assertTrue(self._note_exists(), 'login_window_left 动了记录文件')

    def test_countdown_is_zero_once_past_ttl(self):
        """到点之后不再报"还剩多久" —— 报一个假的倒计时等于劝用户再等一会儿。"""
        pid = self._hold()
        self._note(pid, age=20 * 60)
        self.assertEqual(xlw.login_window_left(self.profile, ttl=10 * 60), 0.0)


class ForeignWindowTests(LoginWindowBase):
    """不是我们起的那些,一个都不许碰。杀错进程比堵着通道严重得多。"""

    def test_a_window_we_did_not_open_is_never_touched(self):
        """SingletonLock 指着别人:用户自己拿同一个 --user-data-dir 开的窗口。"""
        mine = _sleeper(); self.procs.append(mine)
        theirs = self._hold()                 # 占着目录的是另一个进程
        self._note(mine.pid, age=20 * 60)     # 我们记的是自己那个
        self.assertEqual(xlw.release_login_window(self.profile, ttl=1), '')
        self.assertTrue(_alive(theirs), '别人的窗口被我们关掉了')
        self.assertTrue(_alive(mine.pid), '连记录里那个无关进程也被杀了')
        self.assertEqual(xlw.login_window_pid(self.profile), 0)

    def test_a_broken_note_is_the_same_as_no_note(self):
        """坏记录等于没有记录 —— 两个方向(当成占着/当成该关)都会动错进程。"""
        held = self._hold()
        for payload in ('{', '{}', '{"pid": "x", "at": 1}', '{"pid": -1, "at": 1}', ''):
            (Path(self.profile) / xlw._LOGIN_NOTE).write_text(payload, encoding='utf-8')
            self.assertEqual(xlw.login_window_pid(self.profile), 0, payload)
            self.assertEqual(xlw.release_login_window(self.profile, ttl=0), '', payload)
            self.assertTrue(_alive(held), f'坏记录 {payload!r} 之下窗口被动了')

    def test_a_stale_note_is_dropped_so_the_pid_cannot_be_reused(self):
        """窗口早没了、记录还在:不清掉的话,系统把同一个 pid 分给别的进程时,
        「pid 对得上」那道会把一个无辜的进程认成我们的窗口。"""
        gone = _sleeper()
        gone.kill(); gone.wait(timeout=5)
        self._note(gone.pid, age=20 * 60)      # 没有 SingletonLock = 没占着
        self.assertEqual(xlw.release_login_window(self.profile, ttl=1), '')
        self.assertFalse(self._note_exists(), '陈旧记录没被清掉')

    def test_no_note_at_all_is_quiet(self):
        """profile 被占着、但不是我们开的(服务重启前的遗留也走这档)。"""
        held = self._hold()
        self.assertEqual(xlw.release_login_window(self.profile, ttl=0), '')
        self.assertTrue(_alive(held))

    def test_a_zombie_window_does_not_block_the_channel_forever(self):
        """窗口被强行结束、`SingletonLock` 来不及删:它在我们这儿是一格**僵尸**,
        而 `os.kill(pid, 0)` 对僵尸照样成功 —— 不收尸就是「`profile_busy` 永远为真、
        却没有窗口可关」,这条通道从此没有任何出路。

        判据是**结论**(这一轮不再被当成"窗口还开着")加**进程表**(那一格真的摘掉了),
        不是"调没调 waitpid"。"""
        proc = _sleeper()
        self.procs.append(proc)
        self._hold(proc.pid)                 # 锁留在原处,指着它
        self._note(proc.pid, age=60)         # 还没到点
        proc.kill()                          # 用户强退 / 崩溃:没人收尸
        time.sleep(0.2)
        self.assertTrue(_alive(proc.pid), '前提不成立:它该是一格僵尸')

        self.assertEqual(xlw.release_login_window(self.profile, ttl=10 * 60), '')
        self.assertFalse(_alive(proc.pid), '僵尸没被摘掉,这条通道会一直被判成占用')
        self.assertFalse(self._note_exists(), '窗口已经没了,记录该清掉')
        self.assertEqual(xlw.login_window_pid(self.profile), 0)


class ProcessIdentityTests(LoginWindowBase):
    """**pid 不是跨重启的身份**:系统会复用它(审查 R7)。

    触发前提是真实可达的:服务退出或机器异常期间 Chrome 死了,profile 里的
    `SingletonLock` 和我们那份记录都没来得及清;之后系统把这个号分给了别的进程。
    那时「pid 对得上」和「进程还活着」**同时成立** —— 只认这两条就会向一个无关
    进程发 SIGTERM、必要时 SIGKILL。

    判据落在后果上:那个无辜进程还活着,而且我们不再把它认成自己的窗口。
    """

    def test_a_recycled_pid_is_not_our_window(self):
        """同一个号、另一个进程:记录里的身份对不上,一个字都不许碰。"""
        other = self._hold()                   # 占着目录、也活着,就是来路不同
        self._note(other, age=20 * 60, ident=('四  1月/01 00:00:00 2000',
                                              '/usr/bin/some-other-program'))
        self.assertEqual(xlw.login_window_pid(self.profile), 0)
        self.assertEqual(xlw.release_login_window(self.profile, ttl=1), '')
        self.assertTrue(_alive(other), 'pid 被复用之后我们杀了一个无关进程')

    def test_a_note_without_an_identity_is_not_trusted(self):
        """上一版写的记录没有身份那一格 —— 它拿不出任何能排除复用的凭据。

        **拿不准就不动**:口径同「坏记录等于没有记录」。代价是这个窗口要用户自己
        关(面板上有话说),而反过来的代价是杀错进程。
        """
        held = self._hold()
        (Path(self.profile) / xlw._LOGIN_NOTE).write_text(
            json.dumps({'pid': held, 'at': time.time() - 20 * 60}), encoding='utf-8')
        self.assertEqual(xlw.login_window_pid(self.profile), 0)
        self.assertEqual(xlw.release_login_window(self.profile, ttl=1), '')
        self.assertTrue(_alive(held), '没有身份的旧记录之下窗口被动了')

    def test_our_own_window_is_still_recognised(self):
        """**反过来钉一边**:身份对得上的那个照旧认得出、照旧关得掉。

        比紧了的后果是自动关闭整条失效 —— 而那正是这整件事要解决的问题。
        """
        pid = self._hold()
        self._note(pid, age=20 * 60)
        self.assertEqual(xlw.login_window_pid(self.profile), pid)
        self.assertIn('自动关掉', xlw.release_login_window(self.profile, ttl=10 * 60))
        self.assertFalse(_alive(pid))

    def test_the_identity_is_rechecked_before_each_signal(self):
        """两刀之间隔着 6 秒宽限期:它在这中间走掉、号被分给别人,第二刀就落在
        一个无关进程上。所以**每发一次信号之前重新核对**。

        判据:身份在第一次核对之后就被换掉(摆成"号被复用了"),于是一个信号都
        不许发出去。
        """
        import unittest.mock as mock
        pid = self._hold()
        self._note(pid, age=20 * 60)
        sent = []
        with mock.patch.object(xlw, '_proc_identity',
                               side_effect=lambda p: ('换人了', '/usr/bin/other')), \
             mock.patch.object(xlw.os, 'kill', side_effect=lambda *a: sent.append(a)):
            xlw._close_login_window(pid, ('一 1月/01 00:00:00 2026', '/chrome'))
        self.assertEqual([], sent, '身份对不上还是发了信号')
        self.assertTrue(_alive(pid))


class StartupRaceTests(LoginWindowBase):
    """**`Popen` 返回不代表 Chrome 已经建好 ProcessSingleton 锁**(审查 R5)。

    那几百毫秒里 `login_window_pid` 认不出我们的窗口。把「认不出」当成「窗口已经
    关了」就会**把记录删掉** —— 窗口开着,而守望、抓取前释放、连接检查这三道再没有
    任何记录可读,等于退回"起完就不管了"。

    判据落在**记录文件还在不在**上,不是"有没有判过某个标志"。
    """

    def test_a_brand_new_note_survives_a_missing_lock(self):
        """刚写下的记录(锁还没出现)不许被清掉。"""
        proc = _sleeper(); self.procs.append(proc)
        self._note(proc.pid)                   # age=0,而且没有 SingletonLock
        self.assertEqual(xlw.release_login_window(self.profile, ttl=0), '')
        self.assertTrue(self._note_exists(), '启动中的记录被清掉了')

    def test_once_the_startup_grace_is_over_a_stale_note_still_goes(self):
        """反过来钉一边:过了启动宽限还是没有锁,那就是真没了 —— 照旧清掉。

        不清的话,下一次同一个号被分给别的进程时「pid 对得上」会把它认成我们的窗口。
        """
        gone = _sleeper()
        gone.kill(); gone.wait(timeout=5)
        self._note(gone.pid, age=xlw.LOGIN_WINDOW_STARTUP + 5)
        self.assertEqual(xlw.release_login_window(self.profile, ttl=1), '')
        self.assertFalse(self._note_exists(), '过了启动宽限的陈旧记录没被清掉')

    def test_the_watcher_does_not_drop_the_note_on_its_first_tick(self):
        """守望线程第一跳:锁还没出现、手里的句柄还活着 —— 接着守,不许清记录。

        这是线上那条路的形状(`x_browser.open_login_window` 先记录再守望,句柄
        一起交过去)。判据是**等它跑过第一跳之后记录还在**。
        """
        proc = _sleeper(); self.procs.append(proc)
        self._note(proc.pid)
        xlw.watch_login_window(self.profile, proc=proc, ttl=10 * 60)
        time.sleep(0.5)
        self.assertTrue(self._note_exists(), '守望线程第一跳就把记录删了')

    def test_the_watcher_keeps_watching_until_the_lock_shows_up(self):
        """锁晚到几百毫秒(Chrome 启动就是这样)——守望线程不许在第一跳就收工。

        **记录保住了还不够,守望线程自己要还活着**:它收工之后,即使锁随后出现了
        也再没有人到点去关那个窗口 —— 而「忘了关」正是这件事要解决的问题(审查 R5)。
        这一条和 `test_a_brand_new_note_survives_a_missing_lock` 是**两道各自独立的
        带**:只去掉守望那一道时,记录会被 `release_login_window` 的启动宽限留住,
        于是光看记录在不在的用例一条都不红(定点注入实测 0 条红)。

        判据是**后果**:先不给锁、起守望、等它跑过第一跳,再把锁摆上 —— 那个进程
        要真的被关掉。记录里的时刻早就过了 ttl,所以锁一被认出就该关。
        """
        proc = _sleeper(); self.procs.append(proc)
        self._note(proc.pid, age=100)          # 早就过了下面那个 ttl
        xlw.watch_login_window(self.profile, proc=proc, ttl=5)
        time.sleep(0.3)                        # 跑过第一跳:那时还没有锁
        self.assertTrue(_alive(proc.pid), '前提不成立:第一跳就把它关了')
        self._hold(proc.pid)                   # 锁来了
        for _ in range(60):
            if not _alive(proc.pid):
                break
            time.sleep(0.1)
        self.assertFalse(_alive(proc.pid),
                         '守望线程第一跳就收工了 —— 锁出现之后再没有人管这个窗口')

    def test_the_watcher_cleans_up_once_the_handle_is_gone(self):
        """反过来钉一边:手里那个进程自己退了 —— 那是「窗口关了」最硬的证据,
        记录要跟着清掉(留着就是下一次认错人的原料)。"""
        proc = _sleeper()
        proc.kill(); proc.wait(timeout=5)
        self._note(proc.pid)
        xlw.watch_login_window(self.profile, proc=proc, ttl=10 * 60)
        for _ in range(40):
            if not self._note_exists():
                break
            time.sleep(0.1)
        self.assertFalse(self._note_exists(), '句柄已经退了,记录还留着')


class IdentityGapTests(LoginWindowBase):
    """**身份那一问失败一次,不许让自动关闭永久失效**(审查 R5 第二轮)。

    `_proc_identity` 问的是 `ps`。那一问失败一次(系统抽一下、容器里没有 ps)记录
    里就存着 `('', '')`,而原来没有任何地方会再问第二遍 —— `login_window_pid` 从此
    恒为 0:守望线程守满 TTL、把记录一删了之,**那个窗口开着,占着整条采集通道**,
    而面板一路走到「Cookies 在 ⇒ 就绪」,一个字都不说。同判据的另一个实例是启动
    脚本先初始化再 `exec` 真 Chrome(同一个子进程的 `comm` 在我们记下身份之后变了)。

    出路有两条,各有自己的射程,**判据都落在后果上**:
    · 手里还有句柄时**重新认一次人**并到点用句柄关掉它 —— 句柄是精确身份,
      防误杀那道闸一点没松(pid 那条路照旧要身份对得上);
    · 没有句柄时(服务重启过)一个字都不碰,但**面板必须改口**:它不会自动关,
      请手动关掉。
    """

    def test_an_identity_probe_that_failed_is_asked_again(self):
        """起的时候 `ps` 没答上来(记录里是空身份)—— 守望线程要把它补上。

        判据是**后果**:补上之后 `login_window_pid` 重新认得出它,于是到点那一下
        照原来那条(要身份对得上的)路关掉它。
        """
        proc = _sleeper(); self.procs.append(proc)
        self._hold(proc.pid)
        self._note(proc.pid, ident=('', ''))        # 那一问当时失败了
        self.assertEqual(0, xlw.login_window_pid(self.profile),
                         '这一局的前提:空身份认不出来')
        self.assertTrue(xlw._relearn_identity(self.profile, proc), '没能重新认出来')
        self.assertEqual(proc.pid, xlw.login_window_pid(self.profile),
                         '重新认过之后还是认不出')

    def test_relearning_keeps_the_original_clock(self):
        """重新认人**不许重新计时** —— 换成"现在"的话那个窗口永远到不了点。"""
        proc = _sleeper(); self.procs.append(proc)
        self._hold(proc.pid)
        self._note(proc.pid, age=9 * 60, ident=('', ''))
        xlw._relearn_identity(self.profile, proc)
        left = xlw.login_window_left(self.profile, ttl=10 * 60)
        self.assertTrue(0 < left <= 65, f'起始时刻被改写了,剩余 {left}')

    def test_the_watcher_puts_the_identity_back_for_everyone_else(self):
        """守望线程补回来的身份**另外两道也要用得上** —— 这是它和句柄那条路的区别。

        句柄只在守望线程自己手里:它到点能关掉窗口,却帮不了**抓取开工前那一问**和
        **面板那一行**(那两道只有 profile 里的记录可读)。所以判据落在「别人看得见
        的那两件事」上:倒计时回来了、面板不再说"认不出、请手动关"。少了重新认人
        那一步,窗口最后虽然还是被句柄关掉,可这中间每一轮抓取都在红,而面板一直
        让用户自己去关一个马上会被自动关掉的窗口。
        """
        proc = _sleeper(); self.procs.append(proc)
        self._hold(proc.pid)
        self._note(proc.pid, age=60, ident=('', ''))     # 那一问当时失败了
        self.assertTrue(xlw.login_window_stuck(self.profile),
                        '这一局的前提:现在认不出它')
        xlw.watch_login_window(self.profile, proc=proc, ttl=10 * 60)
        for _ in range(60):
            if xlw.login_window_left(self.profile, ttl=10 * 60):
                break
            time.sleep(0.1)
        self.assertTrue(xlw.login_window_left(self.profile, ttl=10 * 60),
                        '守望线程没把身份补回来 —— 另外两道一直认不出这个窗口')
        self.assertFalse(xlw.login_window_stuck(self.profile),
                         '补回来了,面板却还在说"认不出、请手动关"')

    def test_a_window_we_cannot_identify_is_still_closed_by_its_handle(self):
        """身份永久对不上(`ps` 坏了 / 启动脚本换了可执行文件):到点照样要关掉它。

        这是这一条的要害:**认不出不等于已经关了**。原来守望线程守满 TTL 之后
        调 `release_login_window`,而那一下认不出、只把记录删掉就收工 —— 窗口留着
        挡住每一轮同步,下一次抓取连记录都读不到了。
        """
        proc = _sleeper(); self.procs.append(proc)
        self._hold(proc.pid)
        self._note(proc.pid, ident=('陈旧的启动时刻', '/不是这个程序'))
        with patch.object(xlw, '_proc_identity', lambda pid: ('', '')):
            xlw.watch_login_window(self.profile, proc=proc, ttl=0.5)
            for _ in range(80):
                if not _alive(proc.pid):
                    break
                time.sleep(0.1)
        self.assertFalse(_alive(proc.pid),
                         '认不出身份 ⇒ 守望线程把记录一删了之,窗口留着挡通道')
        self.assertFalse(self._note_exists(), '关掉之后记录该一起清掉')

    def test_the_handle_route_never_touches_a_window_that_is_not_ours(self):
        """反过来钉一边:句柄那条路只动**手里那一个**。

        记录指着别人(用户自己开的窗口)时,`_relearn_identity` 一个字都不许改 ——
        改了就等于把别人的窗口认成我们的,而下一步是 SIGKILL。
        """
        mine = _sleeper(); self.procs.append(mine)
        theirs = _sleeper(); self.procs.append(theirs)
        self._hold(theirs.pid)
        self._note(theirs.pid, ident=('', ''))      # 记录说的不是手里这一个
        self.assertFalse(xlw._relearn_identity(self.profile, mine),
                         '把别人的窗口重新认成了我们的')
        self.assertEqual(0, xlw.login_window_pid(self.profile))
        self.assertTrue(_alive(theirs.pid))

    def test_no_handle_means_no_killing_at_all(self):
        """反过来钉一边:没有句柄(服务重启过)时认不出就什么都不碰。

        那一档唯一能排除 pid 复用的凭据就是记录里那份身份,而它对不上。这里宁可
        不关 —— 代价由下面那条面板用例兜着:它会说清"不会自动关,请手动关掉"。
        """
        proc = _sleeper(); self.procs.append(proc)
        self._hold(proc.pid)
        self._note(proc.pid, age=20 * 60, ident=('对不上的时刻', '/对不上的程序'))
        self.assertEqual('', xlw.release_login_window(self.profile, ttl=1))
        self.assertTrue(_alive(proc.pid), '没有句柄、身份又对不上,却还是发了信号')
        self.assertFalse(xlw._relearn_identity(self.profile, None))


class PanelCopyTests(LoginWindowBase):
    """面板那一行:窗口开着时说的是"去关它",不是"还没有登录过 X"。"""

    def setUp(self):
        super().setUp()
        from kb.feeds import x_channels
        self.x_channels = x_channels
        self._orig = x_channels.BROWSER_PROFILE
        x_channels.BROWSER_PROFILE = Path(self.profile)
        self.addCleanup(lambda: setattr(x_channels, 'BROWSER_PROFILE', self._orig))

    def test_open_window_outranks_never_logged_in(self):
        """两档的下一步正好相反:还没登录要去开窗口,窗口开着要去关它。
        顺序反了,刚点开窗口的人收到的是一句正确但没用的话。"""
        pid = self._hold()
        self._note(pid, age=60)               # Cookies 不存在 = 从没登录过
        ready, why, nxt = self.x_channels.readiness({'x_channel': 'browser'})
        self.assertFalse(ready)
        self.assertIn('登录窗口', why)
        self.assertNotIn('还没有登录过', why)
        self.assertIn('自动关闭', nxt)

    def test_a_window_we_cannot_identify_is_not_passed_off_as_ready(self):
        """认不出那个窗口时**不许沉默**:它还占着浏览器,而面板不能说"就绪"。

        这一档就是 R5 的后半截:身份对不上 ⇒ 倒计时是 0 ⇒ 那段话整段不说 ⇒ 一路
        走到「Cookies 在就是就绪」。于是每一轮同步全红,而面板上一个字都没有 ——
        正是这块代码当初要解决的那场事故,换了个入口又回来了。
        **承诺也要跟着收回**:这一档不会自动关闭,只能手动。
        """
        (Path(self.profile) / 'Default').mkdir(parents=True)
        (Path(self.profile) / 'Default' / 'Cookies').write_text('x', encoding='utf-8')
        pid = self._hold()
        self._note(pid, age=60, ident=('对不上的时刻', '/对不上的程序'))
        ready, why, nxt = self.x_channels.readiness({'x_channel': 'browser'})
        self.assertFalse(ready, '窗口占着浏览器,面板却说就绪')
        self.assertIn('登录窗口', why)
        self.assertIn('手动', nxt, '没说"得自己关"')
        self.assertIn('不会自动关闭', nxt, '没把那句承诺收回去')
        self.assertNotIn('分钟后', nxt, '认不出它,却还给了一个自动关闭的倒计时')

    def test_a_release_does_not_erase_the_manual_close_hint(self):
        """**审查 N2**:抓取和「检查连接」各会先释放一次 —— 那一下不许把记录删掉。

        上面那一局的前提是记录还在。而「认不出身份」在释放那条路上原来和「窗口已经
        退出」走的是同一档:过了启动宽限就清记录。于是服务重启之后(守望线程没了、
        身份对不上)面板第一次说得出「请手动关掉」,一次抓取或一次「检查连接」之后
        记录就没了 —— readiness 一路走到「Cookies 在,就绪」,窗口还占着浏览器。
        这两条路一天里走好几遍,所以那句提示活不过几分钟。

        **保留记录不许放宽安全边界**:这一档照旧一个信号都不发。
        """
        (Path(self.profile) / 'Default').mkdir(parents=True)
        (Path(self.profile) / 'Default' / 'Cookies').write_text('x', encoding='utf-8')
        pid = self._hold()
        self._note(pid, age=20 * 60, ident=('对不上的时刻', '/对不上的程序'))
        self.assertEqual('', xlw.release_login_window(self.profile, ttl=1),
                         '认不出身份却声称关掉了它')
        self.assertTrue(self._note_exists(), '释放把那份诊断记录删掉了')
        self.assertTrue(_alive(pid), '认不出身份却还是发了信号')
        ready, why, nxt = self.x_channels.readiness({'x_channel': 'browser'})
        self.assertFalse(ready, '释放过一次之后,窗口占着浏览器而面板说就绪')
        self.assertIn('手动', nxt, '那句「得自己关」没了')

    def test_a_closed_window_restores_the_normal_verdict(self):
        """反过来钉一边:窗口关掉之后,面板立刻回到原来那套判断。"""
        (Path(self.profile) / 'Default').mkdir(parents=True)
        (Path(self.profile) / 'Default' / 'Cookies').write_text('x', encoding='utf-8')
        ready, why, _ = self.x_channels.readiness({'x_channel': 'browser'})
        self.assertTrue(ready, f'窗口没开着却判成不就绪:{why}')


if __name__ == '__main__':
    unittest.main()
