"""空房间 TTL 清理单测 —— 全退出的房间空闲超 TTL 后自动回收

对应 Phase 8 讨论：房间不会随全部玩家退出而解散，需 TTL 清扫防止内存累积。
规则（RoomSession.is_expired）：
- 对局中（playing）绝不回收
- 有人在座且在线（connected）不回收
- 大厅/全员掉线 → 空闲超 IDLE_TTL（30 分钟）回收
- 已结束（finished）→ 空闲超 FINISHED_TTL（10 分钟）回收

直接操作注册表 + 回拨 last_active，无需真实服务。
"""

import time

from app.game.room import FINISHED_TTL, IDLE_TTL, room_registry as rooms


def _backdate(room, seconds: float) -> None:
    """把房间最后活动时间拨到过去，模拟空闲。"""
    room.last_active = time.monotonic() - seconds


def test_empty_lobby_room_expires(fresh_rooms):
    """空大厅房间空闲超 IDLE_TTL → 回收。"""
    room = rooms.create('TTL1', mode='east', capacity=2)
    _backdate(room, IDLE_TTL + 1)
    assert rooms.sweep_expired() == ['TTL1']
    assert rooms.get('TTL1') is None


def test_empty_lobby_room_kept_within_ttl(fresh_rooms):
    """空大厅房间空闲未超 TTL → 保留。"""
    room = rooms.create('TTL2', mode='east', capacity=2)
    _backdate(room, IDLE_TTL - 60)
    assert rooms.sweep_expired() == []
    assert rooms.get('TTL2') is not None


def test_connected_room_not_expired(fresh_rooms):
    """有人在座且在线（WS connected）→ 即使空闲也不回收。"""
    room = rooms.create('TTL3', mode='east', capacity=2)
    seat, _, _ = room.join_or_rejoin('甲')
    room.on_connect(seat)
    _backdate(room, IDLE_TTL + 1)
    assert rooms.sweep_expired() == []


def test_disconnected_ghost_room_expires(fresh_rooms):
    """全员掉线（幽灵座位，connected=False）空闲超 TTL → 回收（释放占用的昵称）。"""
    room = rooms.create('TTL4', mode='east', capacity=2)
    room.join_or_rejoin('甲')
    room.join_or_rejoin('乙')
    _backdate(room, IDLE_TTL + 1)
    assert rooms.sweep_expired() == ['TTL4']


def test_playing_room_never_expired(fresh_rooms):
    """对局中（playing）即使空闲超 TTL 也不回收。"""
    room = rooms.create('TTL5', mode='east', capacity=2)
    room.status = 'playing'
    _backdate(room, IDLE_TTL + 1)
    assert rooms.sweep_expired() == []


def test_finished_room_expires_faster(fresh_rooms):
    """已结束房间空闲超 FINISHED_TTL → 回收（比大厅更短）。"""
    room = rooms.create('TTL6', mode='east', capacity=2)
    room.status = 'finished'
    _backdate(room, FINISHED_TTL + 1)
    assert rooms.sweep_expired() == ['TTL6']


def test_finished_room_kept_within_finished_ttl(fresh_rooms):
    """已结束房间空闲未超 FINISHED_TTL → 保留（供查看结算）。"""
    room = rooms.create('TTL7', mode='east', capacity=2)
    room.status = 'finished'
    _backdate(room, FINISHED_TTL - 60)
    assert rooms.sweep_expired() == []


def test_get_prunes_expired_lazily(fresh_rooms):
    """get() 惰性清扫：过期房在下次访问时被移除。"""
    room = rooms.create('TTL8', mode='east', capacity=2)
    _backdate(room, IDLE_TTL + 1)
    rooms._last_sweep = 0.0   # 绕过节流，强制本次 get 触发清扫
    assert rooms.get('TTL8') is None
    assert rooms.get('TTL8') is None


def test_activity_refreshes_last_active(fresh_rooms):
    """活动（join / ready / WS 消息）会刷新 last_active。"""
    room = rooms.create('TTL9', mode='east', capacity=2)
    before = room.last_active
    seat, _, state = room.join_or_rejoin('甲')
    assert room.last_active >= before
    # resume 与 WS 消息同样刷新
    room.join_or_rejoin('甲', rejoin_code=state.rejoin_code)
    room.handle_client_message(seat, {'type': 'ping'})
    assert room.last_active >= before
