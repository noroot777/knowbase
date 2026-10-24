"""隔离模型设置夹具的调度闸门,只延迟真实事务或真实响应。"""

import asyncio
from threading import Event, Lock

from fastapi import Request
from fastapi.responses import JSONResponse

from kb import llm


def install(app):
    guard = Lock()
    gates = {}
    last_ask = {}
    original = llm.update_settings

    def claim(key):
        with guard:
            gate = gates.get(key)
            if not gate or gate["remaining"] <= 0:
                return None
            gate["remaining"] -= 1
            return gate

    def wait(gate):
        with guard:
            gate["waiting"] += 1
        if not gate["release"].wait(20):
            raise RuntimeError("测试闸门未释放")

    def delayed(mutate, **options):
        gate = claim("write")
        try:
            if gate:
                wait(gate)
            return original(mutate, **options)
        finally:
            if gate:
                with guard:
                    gate["finished"] += 1

    llm.update_settings = delayed

    @app.middleware("http")
    async def delay_read(request: Request, call_next):
        gate = claim(request.url.path) if request.method == "GET" else None
        response = await call_next(request)
        if gate:
            await asyncio.to_thread(wait, gate)
            with guard:
                gate["finished"] += 1
        return response

    @app.post("/__fixture/arm")
    def arm(body: dict):
        with guard:
            for key, count in body.items():
                gates[key] = {"remaining": count, "waiting": 0, "finished": 0, "release": Event()}
        return {"armed": list(body)}

    @app.post("/__fixture/release")
    def release():
        with guard:
            for gate in gates.values():
                gate["release"].set()
        return {"released": True}

    @app.get("/__fixture/status")
    def status():
        with guard:
            return {"gates": {key: {field: value for field, value in gate.items() if field != "release"}
                              for key, gate in gates.items()}, "last_ask": last_ask}

    @app.post("/api/ask/stream")
    def capture_ask(body: dict):
        last_ask.update(body)
        return JSONResponse({"detail": "夹具只记录模型,不调用外部服务"}, status_code=503)
