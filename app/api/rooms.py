"""房间 REST API —— 房间生命周期管理（创建 / 加入 / 离开 / 准备 / 开局）

Phase 6 起房间生命周期由本层接管：
- POST   /api/rooms            创建房间（mode / capacity），签发 6 位房间码
- GET    /api/rooms/{id}       房间详情 + 座位表 + 准备状态
- POST   /api/rooms/{id}/join  加入（占座 + 签发 rejoinCode，写 room_seats 落库）
- POST   /api/rooms/{id}/leave 离开（释放座位）
- POST   /api/rooms/{id}/ready 切换准备态
- POST   /api/rooms/{id}/start 开局（所有已占真人座位 ready 后触发）

认证（首版轻量）：座位级操作带 rejoinCode 校验，防止误操作他人座位。
路由全部定义为同步函数 → FastAPI 自动放线程池，不阻塞事件循环。
"""

import os
import secrets
from typing import Literal, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.game.manager import PLAY_PACE
from app.game.room import RoomError, RoomSession, room_registry
from app.storage.db import storage

router = APIRouter(prefix='/api/rooms', tags=['rooms'])

# 本服务器最多同时存在的房间数（可环境变量覆盖）
MAX_ROOMS = int(os.environ.get('ROOM_MAX', '4'))

# 6 位房间码字母表：去掉易混淆字符（0/O、1/I、U）
_ROOM_ALPHABET = 'ABCDEFGHJKLMNPQRSTUVWXYZ23456789'


def _new_room_id() -> str:
    while True:
        code = ''.join(secrets.choice(_ROOM_ALPHABET) for _ in range(6))
        if room_registry.get(code) is None:
            return code


def _room_or_404(room_id: str) -> RoomSession:
    room = room_registry.get(room_id)
    if room is None:
        raise HTTPException(status_code=404, detail={'code': 'ROOM_NOT_FOUND'})
    return room


def _verify_seat(room: RoomSession, seat: int, rejoin_code: str) -> None:
    """座位级操作身份校验：该座位存在且重进码匹配。"""
    state = room.seats[seat] if 0 <= seat < room.player_count else None
    if state is None:
        raise HTTPException(status_code=404, detail={'code': 'SEAT_EMPTY'})
    if state.rejoin_code != rejoin_code:
        raise HTTPException(status_code=403, detail={'code': 'INVALID_REJOIN_CODE'})


def _room_response(room: RoomSession) -> dict:
    return {
        'roomId': room.room_id,
        'mode': room.mode,
        'capacity': room.capacity,
        'status': room.status,
        'creatorSeat': room.creator_seat,
        'timeLimitSeconds': room.lifetime,  # 房间限时（前端静态提示用）
        'seats': [
            None if state is None else {
                'seat': state.seat,
                'nickname': state.nickname,
                'ready': state.ready,
                'connected': state.controller.connected,
            }
            for state in room.seats
        ],
    }


# ─── 请求 / 响应模型 ─────────────────────────────────────

class CreateRoomRequest(BaseModel):
    mode: Literal['east', 'hanchan'] = 'east'
    capacity: int = Field(default=4, ge=2, le=4)


class JoinRequest(BaseModel):
    nickname: str = Field(min_length=1, max_length=20)
    playerId: Optional[str] = Field(default=None, max_length=64)  # 客户端匿名身份（guestId）


class SeatActionRequest(BaseModel):
    seat: int = Field(ge=0, le=3)
    rejoinCode: str
    ready: Optional[bool] = None  # ready 动作可选显式指定


# ─── 路由 ────────────────────────────────────────────────

@router.post('')
def create_room(body: CreateRoomRequest) -> dict:
    """创建房间。同一房间码唯一，重复创建 → ROOM_EXISTS。

    房间数上限：先清扫到期房间（绕过惰性节流，确保到期房释放槽位），
    在册房间已达 MAX_ROOMS → ROOM_LIMIT_REACHED（客户端提示「房间已满」）。
    """
    room_registry.sweep_expired()
    if room_registry.count() >= MAX_ROOMS:
        raise HTTPException(status_code=409, detail={'code': 'ROOM_LIMIT_REACHED'})
    room_id = _new_room_id()
    try:
        # 真人联机房间注入视觉节奏（AI 出牌/碰杠有可读延迟，对齐前端 PACE_MS）；
        # 测试直接构造 RoomSession 不经此路径，保持默认 0 加速
        room = room_registry.create(
            room_id, mode=body.mode, capacity=body.capacity, storage=storage,
            pace=PLAY_PACE)
    except RoomError as exc:
        raise HTTPException(status_code=409, detail={'code': str(exc)})
    storage.create_room(room_id, body.mode, body.capacity)
    storage.update_room_status(room_id, 'lobby')
    return _room_response(room)


@router.get('/{room_id}')
def get_room(room_id: str) -> dict:
    return _room_response(_room_or_404(room_id))


@router.post('/{room_id}/join')
def join_room(room_id: str, body: JoinRequest) -> dict:
    """加入：占第一个空座并签发 rejoinCode（写 room_seats / players 落库）。

    允许加入大厅（lobby）与已结束（finished）房间——对局结束后离开的玩家可
    重新加入房间打下一场（start 已允许 finished 房间再开局）。对局中（playing）
    拒绝：运行中的牌局控制器已接线，新加入者无法参与当前场。
    """
    room = _room_or_404(room_id)
    if room.status not in ('lobby', 'finished'):
        raise HTTPException(status_code=409, detail={'code': 'ROOM_CLOSED'})
    try:
        seat, is_rejoin, state = room.join_or_rejoin(body.nickname, player_id=body.playerId)
    except RoomError as exc:
        raise HTTPException(status_code=409, detail={'code': str(exc)})
    return {
        'roomId': room.room_id,
        'seat': seat,
        'nickname': state.nickname,
        'rejoinCode': state.rejoin_code,
        'playerId': state.player_id,
        'rejoin': is_rejoin,
    }


@router.post('/{room_id}/leave')
def leave_room(room_id: str, body: SeatActionRequest) -> dict:
    """离开：释放座位（带 rejoinCode 身份校验）。

    房主在非对局中（大厅/场次结束）离开 → 房间自动解散；对局中离开仍走
    AI 代打（房间保留，房主转移给下一座位）。
    """
    room = _room_or_404(room_id)
    _verify_seat(room, body.seat, body.rejoinCode)
    was_creator = room.creator_seat == body.seat
    room.release_seat(body.seat)
    if was_creator and room.status != 'playing':
        room_registry.remove(room_id)
    return {'roomId': room.room_id, 'seat': body.seat, 'left': True}


@router.post('/{room_id}/ready')
def ready_room(room_id: str, body: SeatActionRequest) -> dict:
    """准备 / 取消准备：返回该座位当前准备态。"""
    room = _room_or_404(room_id)
    _verify_seat(room, body.seat, body.rejoinCode)
    ready = room.ready_seat(body.seat, body.ready)
    return {'roomId': room.room_id, 'seat': body.seat, 'ready': ready}


@router.post('/{room_id}/start')
async def start_room(room_id: str) -> dict:
    """开局：所有已占（真人）座位 ready 后触发，独立 game_task 驱动整场。

    async 以便 game_task 创建在事件循环线程（与 WS 处理器一致）。
    """
    room = _room_or_404(room_id)
    try:
        await room.start()
    except RoomError as exc:
        raise HTTPException(status_code=409, detail={'code': str(exc)})
    return {'roomId': room.room_id, 'status': room.status}


@router.delete('/{room_id}')
def close_room(room_id: str, body: SeatActionRequest) -> dict:
    """关闭房间：仅创建者可执行（带 rejoinCode 身份校验），解散房间并取消对局任务。

    对局中（playing）不允许关闭 —— 创建者可先「退出对局」释放座位。
    """
    room = _room_or_404(room_id)
    _verify_seat(room, body.seat, body.rejoinCode)
    if room.creator_seat != body.seat:
        raise HTTPException(status_code=403, detail={'code': 'NOT_CREATOR'})
    if room.status == 'playing':
        raise HTTPException(status_code=409, detail={'code': 'ROOM_PLAYING'})
    room_registry.remove(room_id)
    return {'roomId': room_id, 'closed': True}
