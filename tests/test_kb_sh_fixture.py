# -*- coding: utf-8 -*-
"""`tests/kb_sh_fixture.py` 现算出来的那份清单,够不够把 `kb.sh` 跑起来。

**判据落在后果上**:不验「清单里有几个名字」,而是真把夹具装进一个空目录、真跑一次
`kb.sh`,看它加载得起来。装完跑不起来 == 那四个命令级测试全红,而红出来的话指不到
「夹具没拷全」(实测一次按 700 行上限拆出 `scripts/web_env.sh` 的改动,让 25 条用例红在
图谱闸门、grab 命令、code_stale 提示、web start 四个互不相关的文件上)。

**反过来钉一边**:只拷 `kb.sh` 一个文件就该跑不起来 —— 不然哪天 `kb.sh` 不再 source
任何东西了,上面那条照样全绿,而它已经什么都没在守了,得当场知道。
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from tests import kb_sh_fixture

BASH = shutil.which('bash') or '/bin/bash'
#: 顶层(不缩进)的 source 行:`.` 和 `source` 两种写法都算
_ANY_SOURCE = re.compile(r'^(?:\.|source)\s+(\S.*)$', re.M)


def _load(root: Path) -> subprocess.CompletedProcess:
    """不带参数跑一次:打帮助就退,不碰库、不起服务 —— 只验加载。"""
    return subprocess.run([BASH, str(root / 'kb.sh')], cwd=root,
                          capture_output=True, text=True, timeout=60,
                          env={'HOME': str(root), 'PATH': os.environ.get('PATH', '')})


class KbShFixtureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = Path(os.path.realpath(tempfile.mkdtemp(prefix='kbfix-')))
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)

    def test_an_installed_copy_actually_loads(self):
        kb_sh_fixture.install(self.dir)
        done = _load(self.dir)
        self.assertEqual(0, done.returncode,
                         f'夹具装完还是跑不起来:\n{done.stderr}')
        self.assertNotIn('No such file', done.stderr, done.stderr)

    def test_kb_sh_alone_does_not_load(self):
        """没有这份清单会怎样 —— 少了它,上面那条就不知道自己在守什么。"""
        shutil.copy2(kb_sh_fixture.KB_SH, self.dir / 'kb.sh')
        done = _load(self.dir)
        self.assertNotEqual(0, done.returncode,
                            '光一个 kb.sh 就跑起来了:那份清单已经空转了')
        self.assertIn('No such file', done.stderr, done.stderr)

    def test_extra_files_land_where_the_script_looks_for_them(self):
        """`extra` 走的是同一套相对路径(grab 那四件套就靠它)。"""
        kb_sh_fixture.install(self.dir, 'scripts/grab_cli.py')
        self.assertTrue((self.dir / 'scripts' / 'grab_cli.py').is_file())

    def test_everything_sourced_lives_under_scripts(self):
        """**第五处夹具靠这条守着。**

        `tests/test_fulltext_held_exit.py` 也跑真 `kb.sh`,但它把整个 `scripts/` 软链到
        仓库,所以构造上免疫、没有接 `install()` —— 也不该接:它那个 root 下的
        `scripts` 是软链,`install()` 往里写就写进了真仓库。

        免疫的前提是**被 source 的东西都在 `scripts/` 下**。哪天 source 到别处(比如
        `lib/` 或仓库根),它就会断,而红出来的话照样指不到「夹具没拷全」。所以把那个
        前提钉在这里:要么新文件放 `scripts/`,要么顺手把那一处夹具改掉。
        """
        outside = [rel for rel in kb_sh_fixture.sourced()
                   if not rel.startswith('scripts/')]
        self.assertEqual([], outside,
                         '这些不在 scripts/ 下,test_fulltext_held_exit 的软链豁免不再成立')

    def test_every_top_level_source_line_is_recognized(self):
        """清单是正则扫出来的,**扫不到的写法要当场红**,不是静默漏掉一个文件。

        `source "$ROOT/x"`、`. scripts/x`、`. "$ROOT/$var"` 都扫不到 —— 真出现了就
        改成那一种写法(或者把正则跟着改),别让它安静地不进清单。
        """
        text = kb_sh_fixture.KB_SH.read_text(encoding='utf-8')
        lines = _ANY_SOURCE.findall(text)
        recognized = {f'"$ROOT/{rel}"' for rel in kb_sh_fixture.sourced()}
        self.assertEqual([], [line for line in lines if line not in recognized],
                         f'这些 source 行没进夹具清单:{lines}')


if __name__ == '__main__':
    unittest.main()
