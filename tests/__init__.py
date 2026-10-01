# -*- coding: utf-8 -*-
"""让 tests 成为一个包,`unittest discover -s tests -t .` 才认它。

没有这个文件时,带 `-t .` 的那条命令直接报 `Start directory is not importable`
(discover 在 start_dir != top_level_dir 时会要求目录里有 __init__.py);而
`-t .` 是 README 和 CI 写的那条命令,也是 collector/tests 一直用的形式。
不带 `-t` 的 `discover -s tests` 两种情况都能跑,不受影响。
"""
