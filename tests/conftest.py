"""共享测试 fixture —— 真实 uvicorn 服务 + 房间注册表清理

test_ws.py / test_api.py 共用：
- server：后台线程起一个真实 uvicorn，返回 {http, ws} 基础地址
- fresh_rooms：每测例清空房间注册表（取消残留游戏任务）

重要：游戏开局（RoomSession.start）必须通过 REST 路由触发 —— REST async 路由
在 uvicorn 事件循环执行，game_task 因此绑定 uvicorn 循环，与 WS 处理器一致。
测试直接调用 room.start() 会把 game_task 绑定到 pytest 循环，与 uvicorn 跨循环死锁。
"""

import threading
import time

import pytest
import uvicorn

from app.main import app
from app.game.room import room_registry as rooms


@pytest.fixture(scope='module')
def server():
    """后台线程起一个真实 uvicorn 服务，返回 {http, ws} 基础地址。"""
    config = uvicorn.Config(app, host='127.0.0.1', port=0, log_level='warning')
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 10
    while not getattr(server, 'started', False):
        if time.time() > deadline:
            thread.join(timeout=0)
            raise RuntimeError('uvicorn 启动超时')
        time.sleep(0.02)
    port = server.servers[0].sockets[0].getsockname()[1]
    yield {'http': f'http://127.0.0.1:{port}', 'ws': f'ws://127.0.0.1:{port}'}
    server.should_exit = True
    thread.join(timeout=5)


@pytest.fixture()
def fresh_rooms():
    """每测例清空房间注册表（取消残留的游戏任务）。"""
    rooms.clear()
    yield
    rooms.clear()
