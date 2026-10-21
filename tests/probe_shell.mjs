// -*- coding: utf-8 -*-
/**
 * 起一个「一定会被收掉」的 chrome-headless-shell(12 个浏览器探针共用的唯一口径)。
 *
 * 硬约束在 docs/decisions/probe-shell.md 第一节 —— 改这块之前先读那一份。
 *
 * 原来 12 个探针各写一份 spawn + 等端口,而收尾那句 `proc.kill()` 一律写在
 * **正常路径的末尾** —— 探针整个是 ESM 顶层 await,中间任何一步抛异常(页面里
 * eval 报错、等不到元素、后端沙箱没起来、摆局中断)都让 node 以 unhandled
 * rejection 退出,那一句**永远到不了**。而 node 死掉不会带走 spawn 出来的子进程,
 * 于是 chrome 被 init 收养(ppid=1)、永久活着。
 *
 * 2026-10-09 在本机实测到 16 个这样的孤儿:存活 6 小时到 1 天 11 小时,连同渲染
 * 子进程共 66 个进程、3.8 GB,全部 ppid=1、0 条活跃 CDP 连接、没有任何测试在跑。
 *
 * 口径是 `process.on('exit')` 里**同步** SIGKILL,它覆盖三条路:正常退出、
 * 抛异常退出、`process.exit()`。信号要另外挂一遍 —— node 对**没有 handler** 的
 * SIGINT/SIGTERM 是直接终止,**不触发 'exit' 事件**。
 *
 * **杀用 SIGKILL,理由是构造上的,不是「SIGTERM 不够」** —— 后者**实测是够的**:
 * 信号投递是内核的事,发送方随后死掉不影响已经投递的那一下,chrome 自己响应
 * SIGTERM 就退了。选 SIGKILL 是因为**这是最后一道防线**,而 SIGTERM 要对端配合:
 * chrome 卡住(正在处理崩溃、被 SIGSTOP 停住、信号被屏蔽)时它不退出,而 'exit'
 * 回调只跑得了同步代码、**没法等**,node 一返回就没有第二次机会了。SIGKILL 由
 * 内核执行,不需要对端配合。代价是 chrome 自建的临时 profile 目录留在 /var/folders
 * 下,那是几十 KB 的空目录、系统会清,比留一个 80 MB 的进程轻得多。
 *
 * **这一层挡不住 SIGKILL**,而 Python 侧 `subprocess.run(timeout=)` 超时发的正是
 * 它(连 finally 都不执行)。那一层在 `tests/headless.py` 的 `run_probe()`:用独立
 * 会话起 node、按**进程组**收,chrome 是 node 的后代、在同一个组里,一起带走。
 *
 * **而独立会话本身又开出第三条泄漏路**:node 不在调用方的进程组里了,于是取消
 * 调用方(CI 取消作业、`kill` 那个 pytest、`kill -- -<pgid>`)再也波及不到它 ——
 * 而 Python 对**没有 handler** 的 SIGTERM 是直接结束,`run_probe` 的 `finally`
 * 一行都不跑。实测:调用方以 -15 退出,node 被过继给 init 后照旧活着、chrome 挂在
 * 它下面、DevTools 端口继续监听。所以这一层反过来由**子进程自己**盯着调用方:
 * 见下面 `watchParent()`。三层各管一段,缺哪一层都会按原样漏。
 *
 * **「原调用方是谁」只能由调用方自己说**(`KB_PROBE_PARENT`,`run_probe` 设),
 * 不能由我们现读 `process.ppid` 去猜。探针在起浏览器**之前**本来就有一段异步
 * (等沙箱后端监听、摆局、读 fixture),调用方死在那一段里时我们读到的 ppid 已经
 * 是 1 了 —— 把它当成「本来就没有调用方,不用盯」就等于**永久跳过这道检查**,
 * 随后照旧起一个浏览器留在后台。实测:调用方以 -15/-9 取消之后 node(ppid=1)
 * 继续起 chrome、DevTools 端口持续监听,3 秒后(远超 400ms 轮询间隔)仍都在
 * —— 不是检测慢,是检查压根没装。所以口径有两条:①期望的调用方来自环境变量,
 * 判据是「我还是不是它的子进程」;②**起任何长寿东西之前先问一次**,没有调用方
 * 在等结果就当场退出,一个浏览器都不许起。
 */

import { spawn } from "node:child_process";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

/** chrome 把 DevTools 地址打在 stderr 上,端口是 0 时由内核分配、只能这么读回来。 */
const PORT_RE = /ws:\/\/127\.0\.0\.1:(\d+)\//;

/** 每个探针都要的那几个参数;`--window-size` 之类各不相同的由调用方给。 */
const BASE_ARGS = ["--headless", "--remote-debugging-port=0", "--disable-gpu",
                   "--hide-scrollbars"];

/** 多久问一次「调用方还在吗」:调用方一死,最多再过这么久浏览器就被收掉。 */
const PARENT_POLL_MS = 400;

/** `run_probe` 把自己的 pid 放这儿;它是「原调用方是谁」唯一可靠的那一份。 */
export const PARENT_ENV = "KB_PROBE_PARENT";

/**
 * 原调用方的 pid,0 = 压根没有调用方可盯。
 *
 * **环境变量优先,现读 ppid 只是手动跑时的回退**:两者的区别正是上面那条启动窗口
 * —— 调用方死在「起浏览器之前那段异步」里时,现读得到的是 1,而环境变量里写着的
 * 还是它原来那个 pid,于是「我已经不是它的子进程了」这件事看得出来。
 * 回退那一支在模块加载那一刻取一次(越早越好),`<= 1` 时按「没有调用方」办 ——
 * 手动 `node tests/x_probe.mjs &` 之后关掉终端就是这一档,没有谁在等结果。
 *
 * **`tests/serve_*.py` 读同一个变量,但读法相反(没拿到就不盯)**:夹具的生命周期本来
 * 就跨多个 shell,现读 ppid 会让它一起跑就自杀。理由在 docs/decisions/fixture-life.md。
 */
const CALLER = (() => {
  const want = Number.parseInt(process.env[PARENT_ENV] || "", 10);
  if (Number.isInteger(want) && want > 0) return want;
  const now = process.ppid;
  return now > 1 ? now : 0;
})();

let watchingParent = false;

/** 有原调用方,而且我们已经不是它的子进程了 —— 它死了,没人在等这一轮的结果。 */
function callerGone() {
  return CALLER > 0 && process.ppid !== CALLER;
}

/**
 * 已经没有调用方就立刻退出 —— 走 'exit' 钩子,已经起来的东西跟着收掉。
 *
 * **起任何长寿东西之前都要问一次**:那一刻还什么都没创建,退出的代价是零;
 * 等到起完再靠轮询发现,中间那段(最多 `PARENT_POLL_MS`)后台就真多了一个
 * 浏览器和一个监听端口,而放行那段异步的条件(沙箱后端终于起来了)跟调用方死没死
 * 毫无关系 —— 它照样会到。
 */
function bailIfOrphan() {
  if (!callerGone()) return;
  try {
    process.stderr.write(
      `tests/probe_shell.mjs: 原调用方 ${CALLER} 已经退出,不再起新东西并结束\n`);
  } catch { /* stderr 是通向那个已死调用方的管道,写不进去很正常 */ }
  process.exit(143);                 // 走 'exit' 钩子;143 = 被 SIGTERM 收场的惯例
}

/**
 * 盯着调用方:它一死,我们就自己退出 —— 于是上面那个 'exit' 钩子把浏览器收掉。
 *
 * **为什么判据是 ppid 而不是别的**:`process.ppid` 是实时 getter(每次都
 * `uv_os_getppid`),调用方一死,内核立刻把我们过继给 init,这个值就变了。它是
 * **唯一不依赖调用方跑任何代码**的信号 —— 因此连调用方被 SIGKILL 那一档也盖得住,
 * 而任何写在 Python 侧的 handler / `finally` 都盖不住(那时它已经不执行代码了)。
 * 比的是 `CALLER` 而不是「现在的 ppid 跟刚才那次读的一样吗」,理由见 `CALLER`。
 *
 * **为什么不写在 Python 侧**:`run_probe` 要装一个进程级的 SIGTERM handler 才接得住
 * 取消,那会把跑测试那个进程自己的信号行为静默换掉(pytest/unittest 各有自己的
 * 一套),还是盖不住 SIGKILL。防线待在子进程里就没有这两个毛病。
 *
 * **必须 unref**:它只是「探针还活着的时候顺便盯一眼」,不该自己把 node 留住 ——
 * 不 unref 的话每个探针跑完都要挂到这个 interval 被清掉为止(永远不会)。unref 的
 * timer 照旧会在 event loop 因为别的事活着时触发,所以"探针正忙"这一段盯得到;
 * 探针闲下来那一刻 node 本来就退出了,'exit' 钩子接着收尾。
 *
 * **顺带的后果,知情**:把探针脱离终端跑(`node tests/x_probe.mjs &` 之后关掉那个
 * shell)会让它自己结束。那正是这条防线要的行为 —— 没有调用方在等结果的探针,留着
 * 的只有浏览器。真要长跑就别让它的调用方先死。
 *
 * 定时器只装一次(幂等),但**那次检查每回都做**:一个探针可能起浏览器 + 起沙箱
 * 后端,各调一次 `reapOnExit`,而两次之间调用方完全可能已经没了。
 */
export function watchParent() {
  bailIfOrphan();
  if (watchingParent || CALLER <= 0) return null;
  watchingParent = true;
  const timer = setInterval(bailIfOrphan, PARENT_POLL_MS);
  timer.unref();
  return timer;
}

// 模块一加载就装上:起浏览器**之前**那段异步(等沙箱后端、摆局)同样要盯着,
// 而探针对 probe_shell 的 import 就是它执行的第一件事。
watchParent();

/**
 * 把这个子进程挂到本进程的生命周期上:本进程怎么结束,它都跟着走。
 *
 * 单独导出是为了让「起浏览器」之外的长寿子进程(沙箱后端之类)也用得上同一份口径。
 */
export function reapOnExit(proc) {
  let reaped = false;
  const reap = () => {
    if (reaped) return;
    reaped = true;
    try { proc.kill("SIGKILL"); } catch { /* 它自己先死了,没什么要做的 */ }
  };
  process.on("exit", reap);
  for (const sig of ["SIGINT", "SIGTERM", "SIGHUP"]) {
    // 挂了 handler 就得自己退出 —— 默认那套「直接终止」的行为被我们顶掉了。
    process.on(sig, () => { reap(); process.exit(1); });
  }
  // 有了要回收的东西,才有必要盯调用方:它死了而我们没察觉,这个子进程就是下一个孤儿。
  watchParent();
  return reap;
}

/**
 * 起一个无头壳并等到它的 DevTools 端口。
 *
 * **要独立 profile 的话别自己建目录,把前缀交给这里**(`{ profile: "kb-xxx-" }`)。
 * 探针自己 `mkdtempSync` + `finally` 里 `rmSync` 的那种写法漏一条路:建目录、起浏览器、
 * 连 CDP 这几步**在 try 之前**,那一段抛异常(等不到端口、连不上调试口)时 finally
 * 一行都不跑,目录就留在 /var/folders 下。这里建的目录跟着回收挂在同一个
 * `process.on('exit')` 上,**正常退出、抛异常、收到信号三条路都删**;顺序也是对的:
 * 先 SIGKILL 浏览器、再删目录(回收比这一行先注册)。
 *
 * **第四条路只有调用方救得了:`run_probe` 的 timeout 发的是 SIGKILL,'exit' 一行都不跑。**
 * 所以目录建在它给的那个根里(`KB_PROBE_PROFILE_ROOT`),它在自己的 finally 里整根删
 * (`tests/headless.py`);跟沙箱那份临时库同一条口径(`tests/fixture_root.py`):
 * **给了根就建在里面,没给(手动跑这个探针)才落到系统临时目录**。下面那个 'exit'
 * 钩子两种情形都留着:有根时它只是先把自己那一格删掉,根照样由调用方收。
 *
 * @param {string[]} extraArgs 这个探针自己要的参数(窗口尺寸、色彩配置…)
 * @param {{timeout?: number, profile?: string}} opts profile = 临时 profile 的目录名前缀
 * @returns {Promise<{proc: import("node:child_process").ChildProcess, port: number,
 *                    profile: string|null}>}
 */
export async function launchShell(extraArgs = [], { timeout = 20000, profile } = {}) {
  const bin = process.env.HEADLESS_SHELL;
  if (!bin) {
    throw new Error("没有 HEADLESS_SHELL —— 这个探针要由 tests/headless.py 的 "
                    + "probe_env() 起,或者自己 export 一个 chrome-headless-shell 路径");
  }
  // 起之前最后同步问一次:这段工作里调用方可能已经没了,那就一个浏览器都不该起。
  // 上面那个轮询**不够**:node 是单线程的,探针在这之前做的是一件同步的事
  // (`execSync`、读一份大 fixture)时它一次都跑不起来。这一问不依赖事件循环
  // 哪一刻恰好会转起来。实测它只换来「少起一个随后被 'exit' 钩子秒杀的 chrome」,
  // 所以定点注入掉它红 0 条 —— 留着的理由是构造上的,见 tests/test_probe_leak.py
  // 里同步那条用例的说明。
  bailIfOrphan();
  // profile 目录在 spawn **之前**建(浏览器要用它),但删它的那一下挂在 'exit' 上,
  // 所以这中间抛什么异常都不会把它留下。
  // 优先建在调用方拥有的那个根里(超时 SIGKILL 时钩子跑不了,得由它整根删)
  const root = process.env.KB_PROBE_PROFILE_ROOT || tmpdir();
  const dir = profile ? mkdtempSync(join(root, profile)) : null;
  const args = [...BASE_ARGS, ...extraArgs, ...(dir ? [`--user-data-dir=${dir}`] : []),
                "about:blank"];
  const proc = spawn(bin, args, { stdio: ["ignore", "pipe", "pipe"] });
  // 先挂回收、再去等端口:等端口本身就会超时抛异常,那时它已经起来了。
  reapOnExit(proc);
  if (dir) {
    // 注册在回收之后 = 执行在回收之后:先把浏览器 SIGKILL 掉再删它的目录。
    // `force` 是必须的 —— 被 SIGKILL 的 chrome 可能留下半写的文件和锁。
    process.on("exit", () => { try { rmSync(dir, { recursive: true, force: true }); } catch {} });
  }
  let timer;
  let port;
  try {
    port = await new Promise((res, rej) => {
      let buf = "";
      proc.stderr.on("data", d => {
        buf += d;
        const m = buf.match(PORT_RE);
        if (m) res(+m[1]);
      });
      proc.on("exit", c => rej(new Error(`headless shell 退出 ${c}\n${buf}`)));
      timer = setTimeout(() => rej(new Error(`没等到 DevTools 端口\n${buf}`)), timeout);
    });
  } finally {
    // 这个 timer 非清不可:它是一个 **ref 的** handle,拿到端口之后它还在 event loop
    // 里挂着,于是**探针主体跑完 node 也不退出、要等它到期**(实测多挂满 20 秒)。
    // 下面那几个 unref 管不着它 —— 那是子进程和管道的事。
    clearTimeout(timer);
  }
  // 拿到端口就把它从 event loop 的计数里摘掉:子进程 handle 和它的 stdio 管道都是
  // ref 的,不摘的话**探针跑完 node 也不退出**。原来那 12 个探针看不出这件事,是因为
  // 末尾那句 `proc.kill()` 顺手把这几个 handle 释放掉了 —— 靠的**不是** `process.exit()`:
  // 12 个里只有 daily_probe / redesign_probe 真的调了它,另外 8 个写的是
  // `process.exitCode = …`(只记退出码、不强制退出,照样要等 handle 清空)。
  // 而一个忘了收尾的新探针会直接挂死。unref 只改「要不要为它等
  // 下去」,不影响 'exit' 钩子(那个跟 handle 引用无关),所以回收照旧。
  proc.unref();
  proc.stderr?.unref();
  proc.stdout?.unref();
  return { proc, port, profile: dir };
}
