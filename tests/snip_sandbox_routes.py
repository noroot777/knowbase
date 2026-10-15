# -*- coding: utf-8 -*-
"""沙箱独有的那几条路由:把「盘上那一篇换了一版」摆成确定的时序。

从 `tests/serve_snippets.py` 里搬出来的一整块(那份到了行数上限)。摆盘、假的
问答流、manifest 仍在那一头,这一头只管「真改盘上那个文件」这件事 ——
`install_test_routes` 是唯一的入口,`TOUCH_MARK` 只有它自己用。
"""

from __future__ import annotations

from pathlib import Path

# ── 沙箱独有的两条路由:把「盘上那一篇换了一版」摆成确定的时序 ───────────────
# **它们一个响应都不伪造。** `touch` 真往那个文件末尾续一段(正文补抓、译文写回
# 落盘之后就是这个末态);`swap-after-read` 只是把 touch 的时刻挪到「那一次
# `/api/doc` 的响应已经生成好、客户端还没发出 POST」之间 —— 核对读到的是**那一刻
# 盘上真有的**那一份,随后文件真的变了。产品服务里没有这两条路由。
TOUCH_MARK = '补抓之后这一段才出现在盘上那一篇里'
_touched = {'n': 0}
_swap_armed: set[str] = set()


def _touch_file(vault: Path, rel: str) -> str:
    """真改盘上那一篇(末尾续一段),回它改完之后的正文版本号。"""
    from kb.scanner import body_version, read_md_text
    full = vault / rel
    _touched['n'] += 1
    text = read_md_text(full) + f"\n\n{TOUCH_MARK}(第 {_touched['n']} 次)\n"
    full.write_text(text, encoding='utf-8')
    return body_version(text)


def install_test_routes(app, vault: Path) -> None:
    from fastapi import HTTPException, Request

    @app.post('/__snip__/touch')
    def _touch(path: str):
        return {'version': _touch_file(vault, path)}

    @app.post('/__snip__/drop')
    def _drop(path: str, text: str):
        """把盘上那一篇里的一句话**真删掉**(`touch` 只在末尾续,原句还在)。

        两种改稿的后果不一样,所以要两条路由:续一段之后那段收藏照旧定位得到
        (该放行),删掉之后它在新那一版里压根不存在(该拒)。回新的正文版本号。
        """
        from kb.scanner import body_version, read_md_text
        full = vault / path
        body = read_md_text(full)
        if text not in body:
            raise HTTPException(404, f'那一篇里没有这句话:{text[:20]}')
        body = body.replace(text, '这一句已经被改稿换掉了')
        full.write_text(body, encoding='utf-8')
        return {'version': body_version(body)}

    @app.post('/__snip__/swap-after-read')
    def _arm(path: str):
        _swap_armed.add(path)
        return {'armed': path}

    @app.post('/__snip__/unreadable')
    def _unreadable(path: str):
        """把那个路径**换成一个同名目录**,再调一次换回来(内容一个字节不变)。

        要的是「读到了一个不是 FileNotFound 的 OSError」—— 权限、网盘没挂上、
        IO 错都是这一类,而目录这一种在任何用户下都稳定可复现(chmod 000 对
        root 无效)。
        """
        full, aside = vault / path, vault / (path + '.aside')
        if full.is_dir():
            full.rmdir()
            aside.rename(full)
            return {'readable': True}
        full.rename(aside)
        full.mkdir()
        return {'readable': False}

    @app.middleware('http')
    async def _swap_after_doc_read(request: Request, call_next):
        resp = await call_next(request)
        rel = request.query_params.get('path', '')
        if request.url.path == '/api/doc' and rel in _swap_armed:
            _swap_armed.discard(rel)
            # 响应体在路由里就序列化好了,所以客户端拿到的仍是改之前那一份
            _touch_file(vault, rel)
        return resp
