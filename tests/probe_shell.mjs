// -*- coding: utf-8 -*-
/**
 * 起一个「一定会被收掉」的 chrome-headless-shell(12 个浏览器探针共用的唯一口径)。
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
 */

import { spawn } from "node:child_process";

/** chrome 把 DevTools 地址打在 stderr 上,端口是 0 时由内核分配、只能这么读回来。 */
const PORT_RE = /ws:\/\/127\.0\.0\.1:(\d+)\//;

/** 每个探针都要的那几个参数;`--window-size` 之类各不相同的由调用方给。 */
const BASE_ARGS = ["--headless", "--remote-debugging-port=0", "--disable-gpu",
                   "--hide-scrollbars"];

/** 多久问一次「调用方还在吗」:调用方一死,最多再过这么久浏览器就被收掉。 */
const PARENT_POLL_MS = 400;

let watchingParent = false;

/**
 * 盯着调用方:它一死,我们就自己退出 —— 于是上面那个 'exit' 钩子把浏览器收掉。
 *
 * **为什么判据是 ppid 而不是别的**:`process.ppid` 是实时 getter(每次都
 * `uv_os_getppid`),调用方一死,内核立刻把我们过继给 init,这个值就变了。它是
 * **唯一不依赖调用方跑任何代码**的信号 —— 因此连调用方被 SIGKILL 那一档也盖得住,
 * 而任何写在 Python 侧的 handler / `finally` 都盖不住(那时它已经不执行代码了)。
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
 * 只装一次(幂等):一个探针可能起浏览器 + 起沙箱后端,各调一次 `reapOnExit`。
 */
export function watchParent() {
  if (watchingParent) return null;
  const born = process.ppid;
  if (born <= 1) return null;        // 本来就没有调用方可盯
  watchingParent = true;
  const timer = setInterval(() => {
    if (process.ppid === born) return;
    try {
      process.stderr.write(
        `tests/probe_shell.mjs: 调用方 ${born} 已经退出,收掉浏览器并结束\n`);
    } catch { /* stderr 是通向那个已死调用方的管道,写不进去很正常 */ }
    process.exit(143);               // 走 'exit' 钩子;143 = 被 SIGTERM 收场的惯例
  }, PARENT_POLL_MS);
  timer.unref();
  return timer;
}

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
 * @param {string[]} extraArgs 这个探针自己要的参数(窗口尺寸、色彩配置…)
 * @returns {Promise<{proc: import("node:child_process").ChildProcess, port: number}>}
 */
export async function launchShell(extraArgs = [], { timeout = 20000 } = {}) {
  const bin = process.env.HEADLESS_SHELL;
  if (!bin) {
    throw new Error("没有 HEADLESS_SHELL —— 这个探针要由 tests/headless.py 的 "
                    + "probe_env() 起,或者自己 export 一个 chrome-headless-shell 路径");
  }
  const proc = spawn(bin, [...BASE_ARGS, ...extraArgs, "about:blank"],
                     { stdio: ["ignore", "pipe", "pipe"] });
  // 先挂回收、再去等端口:等端口本身就会超时抛异常,那时它已经起来了。
  reapOnExit(proc);
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
  // 末尾那句 `proc.kill()` 顺手把这几个 handle 释放掉了(它们当中只有 10 个还写了
  // `process.exit()`);而一个忘了收尾的新探针会直接挂死。unref 只改「要不要为它等
  // 下去」,不影响 'exit' 钩子(那个跟 handle 引用无关),所以回收照旧。
  proc.unref();
  proc.stderr?.unref();
  proc.stdout?.unref();
  return { proc, port };
}
