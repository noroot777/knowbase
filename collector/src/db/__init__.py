# -*- coding: utf-8 -*-
"""库连接(`connection`)、迁移(`migrations`)与仓储(`repository`)。

这里故意不 re-export 任何名字:调用方一律 `from src.db import connection` /
`from src.db.repository import ArticleRepository` 按子模块取。原来这个文件
re-export 了 `db_path`/`get_connection`/`init_database`/`ArticleRepository`
四个名字,一个调用方都没有,代价却是「import src.db 就把 repository 整个拉
进来」。
"""
