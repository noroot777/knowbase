# -*- coding: utf-8 -*-
"""起服务那条命令到底把哪些环境变量交给了 kb-web(`kb.sh` 的 `build_web_env`)。

**为什么这一份值得存在**:`KB_FEED_DOH` 不透传的话,可信解析那一层在服务里**永远读不到
开关**,等于没做。而代理那几个是让出口变显式 —— 注意别把理由记成「否则就没有出口」:
实测 macOS 上 `env -i` 并不会让抓取失去代理(httpx 的 trust_env 走
`urllib.request.getproxies()`,在 macOS 上读的是系统代理设置),Linux / 容器 / CI 上才是
只看环境变量、`env -i` 才真的断掉。两种机器的差别正是开源交付要走的那条路。

形态上这是**同一件事在两处各判一遍、只补了一处**:装依赖那一步(`web_prepare_env`)一直
在透传这一族、注释里还写明「这台机器上 pypi 常常得走代理」,而起服务那一行用的是裸
`CLEAN_ENV`。所以这里钉两件事:**拼出来的环境对不对**(真用 bash 跑一遍那段代码),以及
**起服务那一行有没有用它**(文本守卫 —— 行为测试起不了真服务,而那一行恰恰就是漏掉的
那一处)。顺带钉住名单只有一份:谁再手写第二遍循环,那几个变量名就会出现两次。

bash 用 `/bin/bash`(macOS 自带的是 3.2):`WEB_ENV` 那段刻意不用 `mapfile`,这一条
顺带也验了它在 3.2 上跑得起来。
"""
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
KB_SH = ROOT / 'kb.sh'
WEB_ENV_SH = ROOT / 'scripts' / 'web_env.sh'
BASH = shutil.which('bash') or '/bin/bash'


def built(**env) -> list[str]:
    """source 真正那一份,不是复制一段 —— 复制出来的那份不会跟着原文一起改。"""
    script = ('set -euo pipefail\n'
              'CLEAN_ENV=(env -i HOME="$HOME" PATH="$PATH")\n'
              f'. "{WEB_ENV_SH}"\n'
              'build_web_env\n'
              'printf \'%s\\n\' "${WEB_ENV[@]}"\n')
    done = subprocess.run([BASH, '-c', script], capture_output=True, text=True,
                          env={'HOME': '/tmp/home', 'PATH': '/usr/bin:/bin', **env})
    if done.returncode:
        raise AssertionError(f'kb.sh 那段跑不起来:{done.stderr.strip()}')
    return done.stdout.strip().splitlines()


class EgressEnvTests(unittest.TestCase):
    def test_proxy_and_resolver_settings_reach_the_service(self):
        out = built(HTTPS_PROXY='http://127.0.0.1:7897', http_proxy='http://127.0.0.1:7897',
                    ALL_PROXY='socks5://127.0.0.1:7897', NO_PROXY='example.internal',
                    KB_FEED_DOH='https://cloudflare-dns.com/dns-query')
        for expected in ('HTTPS_PROXY=http://127.0.0.1:7897',
                         'http_proxy=http://127.0.0.1:7897',
                         'ALL_PROXY=socks5://127.0.0.1:7897',
                         'NO_PROXY=example.internal',
                         'KB_FEED_DOH=https://cloudflare-dns.com/dns-query'):
            self.assertIn(expected, out)

    def test_clean_env_still_leads_and_nothing_else_rides_along(self):
        out = built(HTTPS_PROXY='http://127.0.0.1:7897',
                    KB_DATA_DIR='/somewhere/else', ANTHROPIC_API_KEY='nope',
                    GZH_DATA_HOME='/old/name')
        self.assertEqual(['env', '-i', 'HOME=/tmp/home', 'PATH=/usr/bin:/bin'], out[:4])
        # **透传的只有出口这一族。** `KB_DATA_DIR` 跟着进去会静默换掉服务读的那个库;
        # 而 `env -i` 本来防的就是继承外层(尤其嵌套 claude)的登录态变量。
        self.assertEqual([], [line for line in out
                              if line.startswith(('KB_DATA_DIR=', 'ANTHROPIC_', 'GZH_'))])

    def test_unset_variables_are_not_passed_as_empty_strings(self):
        # 钉住「只有真正配过的才进去」。**如实记一笔**:原先写的理由(httpx 会把空值
        # 当成配了一个空代理)实测**不成立** —— urllib 的 getproxies_environment() 跳过
        # 空值。留着这一条的理由是排查时的噪声:服务进程的环境里只该有配过的东西,
        # 否则「到底配没配」要多看一眼。
        out = built(HTTPS_PROXY='http://127.0.0.1:7897')
        self.assertEqual([], [line for line in out if line.endswith('=')])
        self.assertEqual(['env', '-i', 'HOME=/tmp/home', 'PATH=/usr/bin:/bin',
                          'HTTPS_PROXY=http://127.0.0.1:7897'], out)

    def test_nothing_to_pass_through_leaves_the_clean_env_untouched(self):
        self.assertEqual(['env', '-i', 'HOME=/tmp/home', 'PATH=/usr/bin:/bin'], built())

    def test_service_launch_line_uses_the_passthrough_env(self):
        lines = [line for line in KB_SH.read_text(encoding='utf-8').splitlines()
                 if 'uv run --offline kb-web' in line]
        self.assertEqual(1, len(lines), lines)
        self.assertIn('"${WEB_ENV[@]}"', lines[0])
        self.assertNotIn('"${CLEAN_ENV[@]}"', lines[0])

    def test_the_passthrough_list_exists_in_exactly_one_place(self):
        """第二份名单会静默过期 —— 本轮的形态就是两处各判一遍、只补了一处。

        **大小写两套都数**:原先只数 `HTTP_PROXY`,于是另一处只写小写
        (`http_proxy`、`all_proxy`)的第二份循环照样过得去 —— 而那正是这一族最可能
        被重写的形态(小写名是 curl / requests 的习惯)。
        """
        # **只数代码行**:注释里提到变量名是说明(web_env.sh 的文件头就在逐条讲这一族
        # 为什么要透传),不是第二份判据 —— 把注释也数进去的话,这条守卫会逼着人删掉
        # 恰恰最该留的那段理由。
        whole = '\n'.join(line for p in (KB_SH, WEB_ENV_SH)
                           for line in p.read_text(encoding='utf-8').splitlines()
                           if not line.lstrip().startswith('#'))
        # `KB_FEED_DOH` 也收进来了:shell 不再自己判开关(那一句改成问
        # `egress.summary()`),于是它在两份 shell 里只该出现在名单那一处。
        # **按词边界数**,不然 `KB_FEED_DOH` 会把 `KB_FEED_DOH_SECONDS` 一起数进去。
        for name in ('HTTP_PROXY', 'http_proxy', 'ALL_PROXY', 'all_proxy',
                     'KB_FEED_DOH', 'KB_FEED_DOH_SECONDS'):
            with self.subTest(name=name):
                hits = re.findall(rf'\b{name}\b', whole)
                self.assertEqual(1, len(hits), f'{name} 出现了不止一次')

    def test_the_resolver_switch_is_passed_through_too(self):
        # 不透传它,可信解析那一层在服务里就永远读不到开关 —— 等于没做。
        self.assertIn('KB_FEED_DOH=https://doh.example/q',
                      built(KB_FEED_DOH='https://doh.example/q'))


class EgressLineTests(unittest.TestCase):
    """起服务/看状态时那两三行的**管道**:Python 说的话有没有原样打出来。

    **内容判据不在这儿**(开没开、端点怎么脱敏、探不到代理要不要提):那些搬进了
    `kb.feeds.egress.summary()`,回归在 `tests/test_feed_egress.py`。原先那一版在 shell 里
    自己判 `[ -n "$KB_FEED_DOH" ]`,而端点和代理现在以设置页为主 —— 判第二遍的那一版
    会在「设置里填了、环境变量没填」时打「关」,可服务是开着的。
    """

    def lines(self, stdout: str, rc: int = 0) -> str:
        """跑真 `egress_lines`,把它问 Python 那一下换成一个固定答案的 uv 桩。"""
        with tempfile.TemporaryDirectory() as tmp:
            stub = Path(tmp) / 'uv'
            stub.write_text(f'#!/bin/sh\nprintf \'%s\' "{stdout}"\nexit {rc}\n',
                            encoding='utf-8')
            stub.chmod(0o755)
            script = ('set -uo pipefail\n'
                      'say() { echo "$*" >&2; }\n'
                      f'ROOT="{tmp}"\n'
                      'CLEAN_ENV=(env -i HOME="$HOME" PATH="$PATH")\n'
                      f'. "{WEB_ENV_SH}"\n'
                      'build_web_env\n'
                      'egress_lines\n')
            done = subprocess.run([BASH, '-c', script], capture_output=True, text=True,
                                  env={'HOME': tmp, 'PATH': f'{tmp}:/usr/bin:/bin'})
            return done.stderr

    def test_every_line_python_prints_reaches_the_terminal(self):
        """两行都要出来 —— 只打第一行的话「探不到代理」那句提示就永远看不见。"""
        out = self.lines('可信解析:开(端点 https://doh.example)\n出口代理:探不到\n')
        self.assertIn('[web] 可信解析:开(端点 https://doh.example)', out)
        self.assertIn('[web] 出口代理:探不到', out)

    def test_nothing_is_said_when_python_cannot_answer(self):
        """**反过来钉一边**:venv 还没装好时什么都不打 —— 那一步自己会说,这里再喊一遍
        只是噪声。而且 uv 的报错里有路径,原样打出来像是出口配置出了问题。"""
        self.assertEqual('', self.lines('boom: no venv\n', rc=1).strip())


if __name__ == '__main__':
    unittest.main()
