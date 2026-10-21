# -*- coding: utf-8 -*-
"""夹具自己什么时候退 —— 判据一组,后果一组。

**后果那一半在 `test_fixture_life_outcome.py`**;这一份只打进程内那几层,所以这里不满足于打 `should_exit` 的返回值:真起一个
`tests/serve_workspace.py`,等它自己退(或者钉住它不该退时确实还在)。判据那一组是
为了把边界铺开,后果那一组才是这件事本身。

**每一条该退的都配一条不该退的**:空闲到点要退 / 一直有请求不许退、在途没回完不许退;
丢了 owner 要退 / 没有 owner(手起、跨 shell 共用)不许退。后面那半边是这套设计的核心
—— 它一旦失守,会话就没法再手起夹具跑探针了。

**收尾只认有所有权证据的进程**:端口和脚本名都不是凭据(另一轮的同型夹具可能正好接手
那个号),所以孙进程的 pid 由中间进程交回,动手前再核一次它的启动时刻。按端口找的那条路
已经整个删掉了。
"""

from __future__ import annotations

import asyncio
import http.server
import os
import socket
import tempfile
import threading
import time
import unittest

from tests import fixture_life
from tests.fixture_life_probe import (   # noqa: F401 - 见那边的说明
    _shut_fds, _stat_of, _open_fds, _free_port, _alive, _wait_gone, _ping, _wait_listening, _owner_pipe, _identity, _reset_state, _age, _closed,
    ROOT,
)


class 判据(unittest.TestCase):
    """`should_exit` 是唯一那份判据,边界都打在它身上。

    它是**纯函数**:`busy` 和 `idle` 都由调用方给。以前它自己去读 `_LAST`,用例只能
    拨模块全局来表达「空闲很久了」—— 那既让用例对实现的内部名字敏感,也把「两个值
    不是同一瞬取的」这个真问题藏了起来(见 `_decide` 那一组)。
    """

    def setUp(self):
        _reset_state()

    def test_空闲没到不退(self):
        self.assertEqual(fixture_life.should_exit(60, False, 0, 0.5), "")

    def test_空闲到点要退(self):
        self.assertIn("没有请求", fixture_life.should_exit(1, False, 0, 10))

    def test_空闲上限为零就永不因空闲退(self):
        """反过来钉一边:手动调试页面的人把它关掉之后,放着一天也不许自己退。"""
        self.assertEqual(fixture_life.should_exit(0, False, 0, 86400), "")

    def test_有在途请求就不按空闲退(self):
        """只在进门时续一次命的话,一个慢请求跑到一半夹具就退了,客户端拿到 502。"""
        self.assertEqual(fixture_life.should_exit(1, False, 1, 86400), "")

    def test_在途回完之后照旧会退(self):
        """反过来钉一边:在途归零就不该再拦着,否则这道防线等于没装。"""
        self.assertIn("没有请求", fixture_life.should_exit(1, False, 0, 86400))

    def test_丢了owner立刻退且不等在途(self):
        """那一刻已经没人在等这个结果了,在途请求不该再拖着。"""
        self.assertIn("原调用方", fixture_life.should_exit(0, True, 7, 0))

    def test_没有owner就不看它(self):
        """反过来钉一边:手起的夹具没有 owner,父 shell 退掉也不许自杀。"""
        self.assertEqual(fixture_life.should_exit(60, False, 0, 0.5), "")


class 登记回收的写法(unittest.TestCase):
    """**「身份记早了」那一条的表示级一边。**

    `tests/test_fixture_life_outcome.py` 里有好几处要「起一个进程、回头按身份收掉它」。
    后果那一份只钉得住**一处**写对了;而这个错一旦再犯一次,漏下的夹具没有任何别的东西
    会来收它,还得等某台机器忙到让 exec 落在 pid 那一行后面才看得见。所以这里不验某一处,
    验**没有一处**把身份固定在登记那一刻。
    """

    def test_登记回收时不许就地记身份(self):
        """`addCleanup(self._reap_pid, pid, _identity(pid))` 这个写法本身就是错的。

        `Popen` 一返回就能拿到 pid,但那一刻子进程可能还是父进程的一份副本、命令行还没
        换 —— 记下这个转瞬即逝的样子,回收时一比对不上,保守分支一个字都不碰。身份要走
        `_settled_identity`(等它换完命令行),登记却要早(号一到手就登记),两件事只能分开
        写,所以这一行上不该同时出现 `addCleanup` 和就地的 `_identity(`。
        """
        src = (ROOT / "tests" / "test_fixture_life_outcome.py").read_text(
            encoding="utf-8")
        bad = [(n, line.strip()) for n, line in enumerate(src.splitlines(), 1)
               if "addCleanup" in line and "_identity(" in line
               and "_settled_identity(" not in line]
        self.assertFalse(bad, f"登记回收要记 `_settled_identity` 的结果:{bad}")


class 一致的那道边界(unittest.TestCase):
    """`_decide`:取状态、下判断、落闸门,**三件事在同一把锁里**。

    这一组钉的是上一版的竞争:`_leave` 在锁里把在途减到 0、**放了锁**才刷新完成时刻,
    中间那一瞬是「没有在途,而完成时刻还是上一个请求的」,看门狗正好在这里醒来便把刚
    回完的请求判成空闲很久 —— 实测(确定性线程注入)一个 4 秒的慢请求正常返回之后 19
    毫秒夹具就退了。
    """

    def setUp(self):
        _reset_state()
        self.addCleanup(_reset_state)

    def test_回完那一瞬在锁外看不到(self):
        """**攥着锁就能证明中间状态不存在**:`_leave` 要改的两样都在锁里面,所以它
        撞上锁的时候一样都没动;锁一放,两样一起到位。
        """
        _age(100)
        self.assertTrue(fixture_life._enter())
        with fixture_life._COUNT:
            worker = threading.Thread(target=fixture_life._leave)
            worker.start()
            time.sleep(0.3)           # 让它确实撞在锁上
            self.assertEqual(fixture_life._INFLIGHT, 1,
                             "锁没放之前,在途一个都不许减")
            self.assertGreater(time.monotonic() - fixture_life._LAST, 99,
                               "完成时刻也还没动 —— 两样要么都没动,要么都动了")
        worker.join(5)
        self.assertEqual(fixture_life._INFLIGHT, 0)
        self.assertLess(fixture_life.idle_seconds(), 1, "锁一放,两样一起到位")

    def test_每一次放锁时那份状态都是自洽的(self):
        """**这份状态只在放锁那一刻对外可见,所以每一个放锁点都必须自洽。**

        换掉 `_COUNT`,在每次放锁时记一笔 `(在途, 完成时刻有多旧)`,然后跑一遍「进门 →
        把完成时刻拨旧 → 回完」。判据只会在这些点上看到状态,所以**不许有任何一笔是
        「在途 0 + 完成时刻很旧」** —— 那正是看门狗会判退的组合,而这一刻其实有个请求
        刚刚回完。
        - 现在这样(两样在一个临界区里落):放锁点只有「在途 1」和「在途 0 + 刚刚」。
        - 上一版(锁里减到 0、放了锁再 `touch()`):多出一个「在途 0 + 100 秒前」。

        **这样写才确定**:靠两个线程去抢那个窗口是抢不到的 —— 放锁的那个线程还攥着
        GIL,它在别人被调度起来之前就把 `touch()` 里的锁又拿回去了。实测过:分两步落
        的版本在 2000 轮抢锁里一次都没被抓到。
        """
        real = fixture_life._COUNT
        snaps = []

        class 记一笔:
            def __enter__(self):
                real.acquire()
                return self

            def __exit__(self, *exc):
                snaps.append((fixture_life._INFLIGHT,
                              time.monotonic() - fixture_life._LAST))
                real.release()
                return False

        fixture_life._COUNT = 记一笔()
        self.addCleanup(setattr, fixture_life, "_COUNT", real)
        try:
            self.assertTrue(fixture_life._enter())
            _age(100)                     # 这一刻的旧时刻是「上一个请求留下的」
            fixture_life._leave()
        finally:
            fixture_life._COUNT = real
        self.assertTrue(snaps, "一次锁都没放,这条用例没验到东西")
        bad = [(busy, round(age)) for busy, age in snaps if busy == 0 and age > 5]
        self.assertEqual(bad, [], "放锁时不许出现「没有在途 + 完成时刻很旧」")
        self.assertEqual(snaps[-1][0], 0, "最后一笔该是回完了")
        self.assertLess(snaps[-1][1], 5, "而且完成时刻是刚刚")

    def test_判据只取一次锁(self):
        """**取状态和下判断必须在同一个临界区里。**

        分两次取(`should_exit(..., inflight(), idle_seconds())`)的话,两个值不是同一瞬
        的 —— 中间进来或回完一个请求,判据用的就是过期的那个。这里直接数 `_check` 走
        一遍取了几次锁:**一次**。
        """
        real = fixture_life._COUNT
        taken = [0]

        class 数一次:
            def __enter__(self):
                taken[0] += 1
                real.acquire()
                return self

            def __exit__(self, *exc):
                real.release()
                return False

        saved_limit = fixture_life._LIMIT
        fixture_life._LIMIT = 0           # 关掉空闲,别让它真去收场
        fixture_life._COUNT = 数一次()
        self.addCleanup(setattr, fixture_life, "_COUNT", real)
        self.addCleanup(setattr, fixture_life, "_LIMIT", saved_limit)
        try:
            fixture_life._check()
        finally:
            fixture_life._COUNT = real
            fixture_life._LIMIT = saved_limit
        self.assertEqual(taken[0], 1,
                         "判据取了多次锁 —— 那几个值就不是同一瞬的了")

    def test_决定收场之后不再接纳新请求(self):
        """判定和接纳共用一把锁,所以不存在「读到 busy=0 之后、动手之前又进来一个」
        那个缝 —— 那个请求只能被拒。
        """
        _age(100)
        self.assertIn("没有请求", fixture_life._decide(1, False))
        self.assertFalse(fixture_life._enter(), "已经在收场了,不许再接纳")
        self.assertEqual(fixture_life._decide(1, False), "",
                         "闸门落下之后重复判出来只给空串,收场只走一次")

    def test_没在收场就照旧接纳(self):
        """反过来钉一边:这道闸门一旦误落,夹具会对所有请求装死而面板上看不出来。"""
        _age(100)
        self.assertEqual(fixture_life._decide(0, False), "")   # 关掉空闲,不该判退
        self.assertTrue(fixture_life._enter(), "没在收场就该照旧接纳")
        fixture_life._leave()


class 所有权凭据(unittest.TestCase):
    """`claim_owner()` 只认「确实是起我们那一位给的那根 pipe」。"""

    def setUp(self):
        self._saved = os.environ.get(fixture_life.OWNER_ENV)
        self.addCleanup(self._restore)

    def _restore(self):
        if self._saved is None:
            os.environ.pop(fixture_life.OWNER_ENV, None)
        else:
            os.environ[fixture_life.OWNER_ENV] = self._saved

    def test_没给就是没有(self):
        os.environ.pop(fixture_life.OWNER_ENV, None)
        self.assertEqual(fixture_life.claim_owner(), -1)

    def test_给了真凭据就认(self):
        r, w, spec = _owner_pipe()
        self.addCleanup(lambda: [os.close(fd) for fd in (r, w)
                                 if not _closed(fd)])
        os.environ[fixture_life.OWNER_ENV] = spec
        self.assertEqual(fixture_life.claim_owner(), r)

    def test_token对不上不认(self):
        """**这是防陈旧继承值那一半**:环境变量能被继承,那根 pipe 不能。

        **顺带钉住「认领这一下不许阻塞」**:这里环境里那个 token 比管道里的长,
        第一版拿阻塞读循环去凑字数,读完已有数据之后就永远等下去 —— 而这一段跑在
        模块加载里,夹具会卡死在 import,一个字的日志都没有。
        """
        r, w, _ = _owner_pipe()
        self.addCleanup(lambda: [os.close(fd) for fd in (r, w)
                                 if not _closed(fd)])
        os.environ[fixture_life.OWNER_ENV] = f"{r}:别的token0000000"
        self.assertEqual(fixture_life.claim_owner(), -1)

    def test_凭据被拒时不改借来fd的模式(self):
        """**借来的 fd 不是我们的私有对象。**

        上一版读 token 之前把它设成非阻塞、`finally` 里再**无条件**设成阻塞 —— 那不是
        恢复原状,是按我们的猜测改了别人的东西。实测:给一个原本非阻塞的 pipe、token
        故意写错,凭据被拒,而它被留成了阻塞的。现在用 `select` 问「可读了吗」,一个比特
        都不动。
        """
        r, w, _ = _owner_pipe("tok0123456789abcd")
        self.addCleanup(_shut_fds, r, w)
        os.set_blocking(r, False)         # 调用方那边本来就是非阻塞的
        os.environ[fixture_life.OWNER_ENV] = f"{r}:这个token对不上"
        self.assertEqual(fixture_life.claim_owner(), -1, "token 对不上就不该认")
        self.assertFalse(os.get_blocking(r), "凭据被拒也不许改它的模式")

    def test_认下来的也不改模式(self):
        """反过来钉一边:认下来了也不动它 —— 认了不等于它就归我们处置。"""
        r, w, spec = _owner_pipe()
        self.addCleanup(_shut_fds, r, w)
        os.set_blocking(r, False)
        os.environ[fixture_life.OWNER_ENV] = spec
        self.assertEqual(fixture_life.claim_owner(), r, "凭据是对的,该认")
        self.assertFalse(os.get_blocking(r), "认下来也不许改它的模式")

    def test_高位的凭据照样认得出来(self):
        """**`os.pipe()` 不保证给低号,`pass_fds` 又原号传下来。**

        调用方只要先开着一千来个文件,凭据就落在 fd 1024 以上 —— 上一版用
        `select.select()` 等它可读,而那个函数在 `FD_SETSIZE` 以上抛 `ValueError`,
        被当成「从来没有 owner」:调用方取消之后页面照旧 200、进程还是同一个。
        """
        hold = [os.open(os.devnull, os.O_RDONLY) for _ in range(1100)]
        self.addCleanup(_shut_fds, *hold)
        r, w, spec = _owner_pipe()
        self.addCleanup(_shut_fds, r, w)
        self.assertGreater(r, 1023, f"没把凭据挤到高位去(fd={r}),这条用例没验到东西")
        os.environ[fixture_life.OWNER_ENV] = spec
        self.assertEqual(fixture_life.claim_owner(), r,
                         "高位的 fd 一样是有效凭据")

    def test_盯不住就抛不许静默退回无主(self):
        """**「少一道防线」必须是响的,不能是静的。**

        等待机制建不出来时退回 `-1` 的话,夹具会当成「手起、本来就没有 owner」接着
        跑 —— 而调用方以为副防线在,它取消之后页面还开着、进程还留着。所以这一类
        抛 `OwnerUnusable`:模块加载失败,夹具起不来,启动那一侧当场看得见。
        """
        r, w, spec = _owner_pipe()
        self.addCleanup(_shut_fds, r, w)
        os.environ[fixture_life.OWNER_ENV] = spec
        real = fixture_life.selectors.DefaultSelector

        def boom():
            raise ValueError("filedescriptor out of range in select()")

        fixture_life.selectors.DefaultSelector = boom
        self.addCleanup(setattr, fixture_life.selectors, "DefaultSelector", real)
        try:
            with self.assertRaises(fixture_life.OwnerUnusable):
                fixture_life.claim_owner()
            with self.assertRaises(fixture_life.OwnerUnusable):
                fixture_life._arm_owner()      # 装防线那一步也得抛
        finally:
            fixture_life.selectors.DefaultSelector = real

    def test_验不过还是安静地退回无主(self):
        """反过来钉一边:**「凭据验不过」跟「盯不住」是两件事。**

        token 对不上说的是「继承来的陈旧值」—— 手起那条合法的路就靠它安静地退回无主,
        这一类绝不许抛,否则夹具手起就起不来了。
        """
        r, w, _ = _owner_pipe("tok0123456789abcd")
        self.addCleanup(_shut_fds, r, w)
        os.environ[fixture_life.OWNER_ENV] = f"{r}:对不上的token"
        self.assertEqual(fixture_life.claim_owner(), -1, "陈旧值就该安静地不认")
        fixture_life._arm_owner()              # 不许抛

    def test_不是管道就不认(self):
        """陈旧的 fd 号在当前进程里可能指着一个普通文件。"""
        handle = tempfile.NamedTemporaryFile(delete=False)
        self.addCleanup(os.unlink, handle.name)
        self.addCleanup(handle.close)
        os.environ[fixture_life.OWNER_ENV] = f"{handle.fileno()}:tok0123456789abcd"
        self.assertEqual(fixture_life.claim_owner(), -1)

    def test_stdio的号不认(self):
        os.environ[fixture_life.OWNER_ENV] = "2:tok0123456789abcd"
        self.assertEqual(fixture_life.claim_owner(), -1)

    def test_垃圾值不认(self):
        for bad in ("", "不是数字:tok", "7", ":tok", "7:"):
            os.environ[fixture_life.OWNER_ENV] = bad
            self.assertEqual(fixture_life.claim_owner(), -1, bad)


class 空闲上限的读法(unittest.TestCase):
    def setUp(self):
        self._saved = os.environ.get(fixture_life.IDLE_ENV)
        self.addCleanup(self._restore)

    def _restore(self):
        if self._saved is None:
            os.environ.pop(fixture_life.IDLE_ENV, None)
        else:
            os.environ[fixture_life.IDLE_ENV] = self._saved

    def test_没设用默认(self):
        os.environ.pop(fixture_life.IDLE_ENV, None)
        self.assertEqual(fixture_life.idle_limit(), fixture_life.IDLE_DEFAULT)

    def test_设零就是关掉(self):
        os.environ[fixture_life.IDLE_ENV] = "0"
        self.assertEqual(fixture_life.idle_limit(), 0)

    def test_负数按关掉办(self):
        os.environ[fixture_life.IDLE_ENV] = "-5"
        self.assertEqual(fixture_life.idle_limit(), 0)

    def test_垃圾值回默认(self):
        """判不准就回那个保守的默认值,不许变成「永不退出」。"""
        os.environ[fixture_life.IDLE_ENV] = "一刻钟"
        self.assertEqual(fixture_life.idle_limit(), fixture_life.IDLE_DEFAULT)

    def test_非有限值回默认(self):
        """`nan` 任何比较都是假、`inf` 永远不到点 —— 照单全收就是悄悄丢掉主防线。"""
        for bad in ("nan", "inf", "-inf", "Infinity"):
            os.environ[fixture_life.IDLE_ENV] = bad
            self.assertEqual(fixture_life.idle_limit(),
                             fixture_life.IDLE_DEFAULT, bad)


class ASGI那条路(unittest.TestCase):
    """7 个 uvicorn 夹具的在途走 `guard`,`serve_workspace` 走 Mixin —— 两份实现
    各有自己的红。后果(进程真的退)由 `后果` 那一组在 Mixin 那条路上钉;这一组只
    钉这一层的职责:把请求记进在途、照旧传下去、而且不许把 lifespan 当成请求。
    """

    def setUp(self):
        _reset_state()
        self.addCleanup(_reset_state)

    def test_请求期间算在途回完才续命(self):
        seen, busy = [], []

        async def app(scope, receive, send):
            seen.append(scope)
            busy.append(fixture_life.inflight())

        wrapped = fixture_life.guard(app, limit=0)
        _age(100)
        asyncio.run(wrapped({"type": "http"}, None, None))
        self.assertEqual(busy, [1], "请求还在跑的时候就该算在途")
        self.assertEqual(fixture_life.inflight(), 0, "回完了要还回去")
        self.assertLess(fixture_life.idle_seconds(), 1, "回完那一刻才开始计空闲")
        self.assertEqual(len(seen), 1, "记完在途还得把请求原样传下去")

    def test_lifespan不算在途(self):
        """它从起到停贯穿整个进程 —— 算进在途的话空闲超时永远不会到点。"""
        busy = []

        async def app(scope, receive, send):
            busy.append(fixture_life.inflight())

        wrapped = fixture_life.guard(app, limit=0)
        asyncio.run(wrapped({"type": "lifespan"}, None, None))
        self.assertEqual(busy, [0], "lifespan 不是一次请求")
        fixture_life.touch()


class http那条路(unittest.TestCase):
    """`TouchingHandlerMixin` 直接打单元 —— 只有同进程才读得到 `inflight()`。

    **夹具自己是 `HTTP/1.0`,一个请求就关连接**,所以 keep-alive 空闲读那个场景在它身上
    构造不出来:曾经有一版用真夹具发 keep-alive 请求来钉这道防线,它永远是绿的 —— 绿在
    服务端把连接关了,跟在途怎么算一点关系都没有。要钉就得自己搭一个 `HTTP/1.1` 的。
    """

    def setUp(self):
        _reset_state()                # 收场闸门一旦落下,请求就都不被接纳了
        self.addCleanup(_reset_state)

    def _serve(self):
        class Handler(fixture_life.TouchingHandlerMixin,
                      http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"ok")

        class Quiet(http.server.ThreadingHTTPServer):
            def handle_error(self, request, client_address):
                pass                  # 收尾时直接关掉挂着的连接,那个
                                      # ConnectionResetError 不是被验的东西

        srv = Quiet(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=srv.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 2)
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        return srv

    def test_空闲的keepalive连接不算在途(self):
        """**反过来钉在途那一边**:keep-alive 连接等下一个请求时阻塞在读请求行上,把那
        一段算成在途的话,浏览器挂着一条连接就能让夹具永不退出 —— 空闲超时还是等于没装,
        只是换了个失守的方向。所以在途从**请求行已经读到**开始算。
        """
        srv = self._serve()
        before = fixture_life.inflight()
        conn = socket.create_connection(srv.server_address, timeout=5)
        self.addCleanup(conn.close)
        conn.sendall(b"GET / HTTP/1.1\r\nHost: l\r\n\r\n")
        self.assertIn(b"200", conn.recv(4096), "先确认这条连接真的还活着")
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and fixture_life.inflight() > before:
            time.sleep(0.05)
        self.assertEqual(fixture_life.inflight(), before,
                         "连接挂着等下一个请求,那一段不算在途")




if __name__ == "__main__":
    unittest.main()
