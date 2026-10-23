"""幺筒新手/老手模式：输入拦截、等待额度、杠后节奏与 AI 节奏。"""

import asyncio
import time

import pytest

from app.core.yaotong.engine import StartOptions, create_game
from app.core.yaotong.mode import action_protection, protection_reason
from app.core.yaotong.types import Command, GameEvent, GameState, Pending, Player
from app.game.yaotong_room import (
    PACE_MS_AFTER_CLAIM_GANG,
    PACE_MS_AFTER_KONG_SETTLE,
    YaotongRoomSession,
)

WALL = [tile for tile in range(27) for _ in range(4)]


def state_with_chicken_hand() -> GameState:
    players = [Player(name='P0', hand=[1, 1, 2, 3, 4, 5, 6, 10, 11, 12, 13, 14, 15, 26])
               for _ in range(4)]
    players[0].chicken_peng = [1]
    return GameState(players=players, wall=[], phase='discard', current=0)


def test_new_player_protection_blocks_chicken_discard():
    state = state_with_chicken_hand()
    protection = action_protection(state, 0)
    assert 0 in protection['discards']  # 手里两张 2m，至少一张被打鸡限制
    assert protection_reason(
        protection, Command(type='discard', index=0)) == '本巡已碰鸡，系统已拦截打鸡'


def test_room_easy_mode_rejects_protected_action_server_side():
    room = YaotongRoomSession('YT-MODE', yaotong_mode='easy')
    room.state = state_with_chicken_hand()
    ok, error = room.handle_client_message(0, {
        'kind': 'action',
        'action': {'type': 'discard', 'index': 0},
    })
    assert ok is False
    assert error == 'PROTECTED_ACTION'


def test_ai_think_seconds_match_single_player_constants():
    room = YaotongRoomSession('YT-PACE', pace={'play': 1})
    state = create_game(StartOptions(wall=list(WALL), dice=(1, 2), dealer=0))

    state.phase = 'original'
    assert room._ai_think_seconds(state, 0) == 0.5
    state.phase = 'discard'
    assert room._ai_think_seconds(state, 0) == 1.0
    state.phase = 'response'
    state.pending = Pending(kind='discard', tile=1, from_seat=1)
    assert room._ai_think_seconds(state, 0) == 0.5
    state.pending = Pending(kind='rob', tile=1, from_seat=1)
    assert room._ai_think_seconds(state, 0) == 0.0

    state.phase = 'discard'
    state.players[1].after_kong = True
    assert room._ai_think_seconds(state, 1) == 2.0
    assert PACE_MS_AFTER_CLAIM_GANG + 2.0 == 2.55
    assert PACE_MS_AFTER_KONG_SETTLE + 2.0 == 2.6

    room.state = state
    state.events.append(GameEvent(id=99, seat=1, text='补杠', kind='kong'))
    room._note_kong_pause()
    assert room._post_kong_pause == PACE_MS_AFTER_KONG_SETTLE
    state.events[-1] = GameEvent(id=100, seat=1, text='明杠', kind='kong')
    room._note_kong_pause()
    assert room._post_kong_pause == PACE_MS_AFTER_CLAIM_GANG

    room.ai_delay = 0
    assert room._ai_think_seconds(state, 1) == 0.0


@pytest.mark.asyncio
async def test_kong_pause_blocks_drive_before_replacement_discard():
    room = YaotongRoomSession('YT-KONG-PACE')
    room.state = create_game(StartOptions(wall=list(WALL), dice=(1, 2), dealer=0))
    room.state.phase = 'discard'
    room.state.current = 1
    room.ai_delay = 1.0
    room._post_kong_pause = 0.08
    serial = room.state.serial
    started = time.monotonic()
    await room._drive_step()
    assert time.monotonic() - started >= 0.07
    assert room.state.serial == serial  # 停顿期间不摸打
    assert room._post_kong_pause == 0.0
    room.close()


def test_room_remembers_mode():
    assert YaotongRoomSession('YT-E', yaotong_mode='easy').yaotong_mode == 'easy'
    assert YaotongRoomSession('YT-H', yaotong_mode='hard').yaotong_mode == 'hard'
    assert YaotongRoomSession('YT-X', yaotong_mode='invalid').yaotong_mode == 'easy'


@pytest.mark.asyncio
async def test_hard_hold_deducts_only_actual_wait_time():
    room = YaotongRoomSession('YT-HOLD', yaotong_mode='hard')
    room.join_or_rejoin('甲', player_id='hold-a')
    room.state = create_game(StartOptions(wall=list(WALL), dice=(1, 2), dealer=0))
    room.state.wait_remaining_ms[0] = 15000

    ok, error = room.handle_client_message(0, {'kind': 'hold', 'holding': True})
    assert ok, error
    assert room.hold_remaining_ms(0) is not None
    await asyncio.sleep(0.18)
    assert 14500 <= (room.hold_remaining_ms(0) or 0) < 15000

    room.handle_client_message(0, {'kind': 'hold', 'holding': False})
    remaining = room.state.wait_remaining_ms[0]
    assert 14500 <= remaining < 15000
    assert room.hold_remaining_ms(0) is None

    # 可分次使用：第二次只扣第二次的实际等待。
    room.handle_client_message(0, {'kind': 'hold', 'holding': True})
    await asyncio.sleep(0.1)
    room.handle_client_message(0, {'kind': 'hold', 'holding': False})
    assert room.state.wait_remaining_ms[0] < remaining
    room.close()


@pytest.mark.asyncio
async def test_hard_hold_expires_at_budget_and_resets_next_hand():
    room = YaotongRoomSession('YT-HOLD-EXP', yaotong_mode='hard')
    room.join_or_rejoin('甲', player_id='hold-b')
    room.state = create_game(StartOptions(wall=list(WALL), dice=(1, 2), dealer=0))
    room.state.wait_remaining_ms[0] = 80

    ok, _ = room.handle_client_message(0, {'kind': 'hold', 'holding': True})
    assert ok
    await asyncio.sleep(0.2)
    assert room.hold_remaining_ms(0) is None
    assert room.state.wait_remaining_ms[0] == 0

    # 新小局重置为 15 秒。
    room.state = create_game(StartOptions(wall=list(WALL), dice=(1, 2), dealer=0))
    assert room.state.wait_remaining_ms[0] == 15000
    room.close()


@pytest.mark.asyncio
async def test_hold_pauses_drive_and_action_releases_budget():
    room = YaotongRoomSession('YT-HOLD-PAUSE', yaotong_mode='hard')
    room.join_or_rejoin('甲', player_id='hold-pause')
    room.state = create_game(StartOptions(wall=list(WALL), dice=(1, 2), dealer=0))
    room.state.phase = 'discard'
    room.state.current = 0
    serial = room.state.serial

    ok, _ = room.handle_client_message(0, {'kind': 'hold', 'holding': True})
    assert ok
    await room._drive_step()
    assert room.state.serial == serial  # 等期间整桌不推进
    await asyncio.sleep(0.12)

    # 等期间不能摸打，只能胡/过。
    ok, error = room.handle_client_message(0, {
        'kind': 'action', 'action': {'type': 'discard', 'index': 0},
    })
    assert ok is False
    assert error == 'HOLD_ACTION_NOT_ALLOWED'
    room.handle_client_message(0, {'kind': 'hold', 'holding': False})

    ok, error = room.handle_client_message(0, {
        'kind': 'action', 'action': {'type': 'discard', 'index': 0},
    })
    assert ok, error
    assert room.hold_remaining_ms(0) is None
    assert room.state.wait_remaining_ms[0] < 15000
    assert room.state.serial != serial
    room.close()


def test_easy_mode_has_no_hold():
    room = YaotongRoomSession('YT-HOLD-EASY', yaotong_mode='easy')
    room.join_or_rejoin('甲', player_id='hold-c')
    room.state = create_game(StartOptions(wall=list(WALL), dice=(1, 2), dealer=0))
    ok, error = room.handle_client_message(0, {'kind': 'hold', 'holding': True})
    assert ok is False
    assert error == 'HOLD_NOT_AVAILABLE'
    room.close()
