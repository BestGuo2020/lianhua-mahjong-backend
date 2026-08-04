"""WebSocket 游戏端点 —— /ws/room/{room_id}

流程（Phase 6：REST 接管房间生命周期，WS 只管实时游戏）：
1. 握手鉴权：query 携带 rejoin_code（REST join 签发）→ resume_by_code 恢复原座位
2. 绑定座位：注册出站队列 + 发送任务，下发 rejoin_ok + 全量快照
3. 开局由 REST POST /api/rooms/{id}/start 显式触发（本端点不再自动开局）
4. 接收循环：客户端动作 → handle_client_message（服务端权威校验）→ 拒绝回错误
5. 断线：on_disconnect 标记 + AI 托管；发送任务随连接关闭终止

发送采用「出站队列 + 后台发送任务」：广播/请求只入队，慢/断客户端不阻塞游戏主循环。
"""

import asyncio

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.game.room import RoomError, build_snapshot, room_registry

router = APIRouter()


async def _sender(queue: asyncio.Queue, websocket: WebSocket) -> None:
    """后台发送任务：从出站队列取消息真正 send_json；连接断开即结束。"""
    try:
        while True:
            message = await queue.get()
            await websocket.send_json(message)
    except Exception:
        pass  # 连接已断开，发送失败无需上报（游戏循环不感知）


@router.websocket('/ws/room/{room_id}')
async def game_ws(websocket: WebSocket, room_id: str) -> None:
    await websocket.accept()

    rejoin_code = (websocket.query_params.get('rejoin_code') or '').strip()

    room = room_registry.get(room_id)
    if room is None:
        await websocket.send_json({'kind': 'rejoin_err', 'code': 'ROOM_NOT_FOUND'})
        await websocket.close()
        return
    if not rejoin_code:
        await websocket.send_json({'kind': 'rejoin_err', 'code': 'REJOIN_CODE_REQUIRED'})
        await websocket.close()
        return
    try:
        seat, state = room.resume_by_code(rejoin_code)
    except RoomError as exc:
        await websocket.send_json({'kind': 'rejoin_err', 'code': str(exc)})
        await websocket.close()
        return

    # 绑定座位：出站队列 + 后台发送任务
    queue: asyncio.Queue = asyncio.Queue()
    sender = asyncio.create_task(_sender(queue, websocket))
    room.conn.register(seat, queue, sender)
    room.on_connect(seat)

    await room.conn.send_to_seat(seat, {
        'kind': 'rejoin_ok',
        'seat': seat,
        'rejoin': True,
        'roomId': room.room_id,
        'mode': room.mode,
        'nickname': state.nickname,
        'rejoinCode': state.rejoin_code,
    })
    await room.conn.send_to_seat(seat, build_snapshot(room, seat))

    try:
        while True:
            message = await websocket.receive_json()
            ok, err = room.handle_client_message(seat, message)
            if not ok:
                await room.conn.send_to_seat(seat, {'kind': 'error', 'code': err})
    except WebSocketDisconnect:
        pass
    finally:
        room.on_disconnect(seat)
        room.conn.unregister(seat)
        if not sender.done():
            sender.cancel()
