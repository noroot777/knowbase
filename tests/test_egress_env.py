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
        whole = '\n'.join(p.read_text(encoding='utf-8')
                          for p in (KB_SH, WEB_ENV_SH))
        for name in ('HTTP_PROXY', 'http_proxy', 'ALL_PROXY', 'all_proxy',
                     'KB_FEED_DOH_SECONDS'):
            with self.subTest(name=name):
                self.assertEqual(1, whole.count(name), f'{name} 出现了不止一次')

    def test_the_resolver_switch_is_passed_through_too(self):
        # 不透传它,可信解析那一层在服务里就永远读不到开关 —— 等于没做。
        self.assertIn('KB_FEED_DOH=https://doh.example/q',
                      built(KB_FEED_DOH='https://doh.example/q'))


if __name__ == '__main__':
    unittest.main()


class EgressWarningTests(unittest.TestCase):
    """配了 DoH 却探不到出口时那一句提示。

    **判据是真探测,不是环境变量。** 第一版按变量判,在 macOS 上是误报:那里
    `getproxies()` 读系统代理设置,一个 `*_proxy` 都没 export 也照样有出口(实测
    `env -i` 下 DoH 解析成功,而告警照样打「多半连不上」)。所以这几条把
    `urllib.request.getproxies()` 这件外部事实替掉,只验「探到了 / 探不到」各走哪条路。
    """

    def warn(self, probe_rc: int, **env) -> str:
        """跑真 `warn_egress_gap`,把它那次探测换成一个固定答案的 uv 桩。"""
        with tempfile.TemporaryDirectory() as tmp:
            stub = Path(tmp) / 'uv'
            stub.write_text(f'#!/bin/sh\nexit {probe_rc}\n', encoding='utf-8')
            stub.chmod(0o755)
            script = ('set -uo pipefail\n'
                      'say() { echo "$*" >&2; }\n'
                      'CLEAN_ENV=(env -i HOME="$HOME" PATH="$PATH")\n'
                      f'. "{WEB_ENV_SH}"\n'
                      'build_web_env\n'
                      'warn_egress_gap\n')
            done = subprocess.run([BASH, '-c', script], capture_output=True, text=True,
                                  env={'HOME': tmp, 'PATH': f'{tmp}:/usr/bin:/bin', **env})
            return done.stderr

    def test_no_egress_found_says_so(self):
        out = self.warn(1, KB_FEED_DOH='https://doh.example/q')
        self.assertIn('探不到代理', out)

    def test_a_system_proxy_is_enough_to_stay_quiet(self):
        """**反过来钉一边**:macOS 上那次误报就是这一格。探到了就什么都不说。"""
        self.assertEqual('', self.warn(0, KB_FEED_DOH='https://doh.example/q').strip())

    def test_nothing_is_said_when_the_switch_is_off(self):
        self.assertEqual('', self.warn(1).strip())

    def test_the_status_line_masks_the_endpoint_path(self):
        """端点的 path / query 可能带 token(NextDNS、AdGuard 那类带 profile id 的),
        日志里不该留。"""
        script = ('set -uo pipefail\n'
                  'say() { echo "$*" >&2; }\n'
                  f'. "{WEB_ENV_SH}"\n'
                  'doh_line\n')
        done = subprocess.run([BASH, '-c', script], capture_output=True, text=True,
                              env={'HOME': '/tmp', 'PATH': '/usr/bin:/bin',
                                   'KB_FEED_DOH': 'https://dns.nextdns.io/abc123secret?x=1'})
        self.assertIn('https://dns.nextdns.io', done.stderr)
        self.assertNotIn('abc123secret', done.stderr, '端点里的 token 进了日志')
        self.assertNotIn('x=1', done.stderr)

    def test_the_status_line_says_off_and_how_to_turn_it_on(self):
        script = ('set -uo pipefail\n'
                  'say() { echo "$*" >&2; }\n'
                  f'. "{WEB_ENV_SH}"\n'
                  'doh_line\n')
        done = subprocess.run([BASH, '-c', script], capture_output=True, text=True,
                              env={'HOME': '/tmp', 'PATH': '/usr/bin:/bin'})
        self.assertIn('关', done.stderr)
        self.assertIn('KB_FEED_DOH', done.stderr, '没说怎么开')
