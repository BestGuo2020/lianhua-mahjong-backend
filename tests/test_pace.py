"""视觉节奏（pace）注入测试 —— 真人联机房间有可读节奏，测试路径保持即用即答

问题背景：REST 房间 pace=0，AI 出牌/碰杠瞬间完成，真人体验像快进。
修复：REST create_room 注入 PLAY_PACE（对齐前端 PACE_MS）；直接构造 RoomSession
（测试/单机）保持 None → GameManager 默认全 0，后端测试套件不拖慢。
"""

import asyncio
import time

import httpx
import pytest

from app.game.manager import GameManager, PLAY_PACE
from app.game.player import AIPlayer
from app.game.room import RoomSession, room_registry as rooms


async def run_until(manager: GameManager, phase: str, max_steps: int = 4000) -> None:
    steps = 0
    while manager.phase != phase and steps < max_steps:
        steps += 1
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_rest_create_injects_play_pace(server, fresh_rooms):
    """REST 创建的房间注入真人节奏 PLAY_PACE（AI 出牌有可读延迟）。"""
    async with httpx.AsyncClient(base_url=server['http']) as http:
        resp = await http.post('/api/rooms', json={'mode': 'east', 'capacity': 2})
        assert resp.status_code == 200, resp.text
    room = rooms.get(resp.json()['roomId'])
    assert room is not None
    assert room.pace == PLAY_PACE
    # 常量本身非空：至少一个关键节奏点有延迟（否则问题 1 回归）
    assert PLAY_PACE['afterDiscardToNextTurn'] > 0
    # 开局表现等待：对齐客户端开局动画，防止 AI 在动画期间推进
    assert PLAY_PACE['openingDelayStart'] > 0
    assert PLAY_PACE['openingDelay'] > 0


def test_room_session_default_pace_is_none():
    """直接构造 RoomSession（测试路径）pace=None → GameManager 默认全 0。"""
    room = RoomSession('PACE-TEST', capacity=2)
    assert room.pace is None


@pytest.mark.asyncio
async def test_injected_pace_slows_first_round():
    """注入节奏后整局时长显著变慢；默认 0 保持即用即答。"""
    async def first_round_seconds(pace=None) -> float:
        manager = GameManager(mode='east', controllers=[AIPlayer() for _ in range(4)],
                              pace=pace)
        t0 = time.perf_counter()
        await manager.start_game('east')
        await run_until(manager, 'settled')
        return time.perf_counter() - t0

    fast = await first_round_seconds()
    slow = await first_round_seconds({'afterDiscardToNextTurn': 20})

    # 一局约 15+ 次弃牌 × 20ms ≈ 300ms 的节奏量；留 200ms 判定余量
    assert slow - fast >= 0.2, f'节奏注入未生效: fast={fast:.3f}s slow={slow:.3f}s'


@pytest.mark.asyncio
async def test_opening_delay_gates_first_turn():
    """开局表现等待生效：注入 openingDelayStart 后首回合决策至少等满该时长；默认 0 即用即答。"""
    from app.game.player import AIPlayer

    class Recorder(AIPlayer):
        def __init__(self):
            super().__init__()
            self.first_request: float | None = None

        async def request_turn(self, ctx):
            if self.first_request is None:
                self.first_request = time.perf_counter()
            return await super().request_turn(ctx)

    controllers = [Recorder() for _ in range(4)]
    manager = GameManager(mode='east', controllers=controllers, pace={'openingDelayStart': 200})
    t0 = time.perf_counter()
    task = asyncio.create_task(manager.start_game('east'))
    try:
        deadline = asyncio.get_event_loop().time() + 5
        while (not any(c.first_request for c in controllers)
               and asyncio.get_event_loop().time() < deadline):
            await asyncio.sleep(0.005)
        first = min((c.first_request for c in controllers if c.first_request is not None),
                    default=None)
        assert first is not None, '首回合决策从未发生'
        elapsed = first - t0
        # 留调度余量：至少等满注入的 200ms（宽松判 150ms）
        assert elapsed >= 0.15, f'开局表现等待未生效: {elapsed:.3f}s'
    finally:
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
