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
        # 空值透传过去不是没事:`NO_PROXY=''` 和「没设 NO_PROXY」在有些库里不是一回事,
        # 而 httpx 读到空字符串的 proxy 会当成「配了一个空代理」。
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
        # 第二份名单会静默过期 —— 本轮的形态就是两处各判一遍、只补了一处。
        whole = '\n'.join(p.read_text(encoding='utf-8')
                          for p in (KB_SH, WEB_ENV_SH))
        self.assertEqual(1, whole.count('HTTP_PROXY'))

    def test_the_resolver_switch_is_passed_through_too(self):
        # 不透传它,可信解析那一层在服务里就永远读不到开关 —— 等于没做。
        self.assertIn('KB_FEED_DOH=https://doh.example/q',
                      built(KB_FEED_DOH='https://doh.example/q'))


if __name__ == '__main__':
    unittest.main()
