"""联机操作限时与每小局等额度独立；消息和重连不能刷新操作时限。"""
import asyncio
from types import SimpleNamespace

import pytest

from app.core.yaotong.engine import act
from app.core.yaotong.types import Command, Player
from app.game import yaotong_room
from tests.test_yaotong_online_regressions import human_room, tiles


def prepare(room, phase):
    state = room.state
    if phase == 'original':
        state.phase = 'original'
        state.players[0] = Player(name='P0', hand=tiles('234567m 234s 66p 78s'))
        state.original_decisions = [False, True, True, True]
    elif phase in ('peng', 'kong'):
        state.current = 1
        state.players[1].hand.append(4)
        state.players[0].hand = tiles('55m 147m 258p 369s 24s' if phase == 'peng'
                                     else '555m 147m 258p 369s 2s')
        assert act(state, 1, Command(type='discard', index=13))
        assert state.pending.options[0].peng
        assert state.pending.options[0].kong == (phase == 'kong')
    else:
        state.players[0].hand.append(26)
        state.players[0].drawn = 26
    return state


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['easy', 'hard'])
@pytest.mark.parametrize('phase', ['discard', 'original', 'peng', 'kong'])
async def test_unanswered_decision_times_out_and_advances(mode, phase):
    room = human_room(mode)
    assert room.turn_timeout == 20
    room.turn_timeout = 0.03
    state = prepare(room, phase)
    try:
        assert 0 < room.snapshot_for(0)['decisionRemainingMs'] <= 30
        await asyncio.wait_for(room._human_or_ai(0), 1)
        if phase == 'original':
            assert state.original_decisions[0]
        elif phase == 'discard':
            assert state.players[0].river
        else:
            assert state.phase != 'response' or 0 in state.pending.choices
        assert room.snapshot_for(0)['decisionRemainingMs'] == 0 or state.current == 0
        assert 0 not in room.auto_seats  # 只代当前操作，不强制长期托管。
    finally:
        room.close()


@pytest.fixture
def clock(monkeypatch):
    now = [100.0]
    monkeypatch.setattr(yaotong_room, 'time', SimpleNamespace(
        monotonic=lambda: now[0], time=lambda: 1000000 + now[0]))
    return now


@pytest.mark.asyncio
async def test_hold_pauses_remaining_time_without_restarting_decision(clock):
    room = human_room()
    prepare(room, 'discard')
    try:
        assert room.snapshot_for(0)['decisionRemainingMs'] == 20000
        clock[0] += 7
        assert room.handle_client_message(0, {'kind': 'hold', 'holding': True})[0]
        held = room.snapshot_for(0)
        assert held['decisionRemainingMs'] == 13000
        assert held['decisionPaused'] is True
        assert held['decisionDeadlineAt'] == 0
        clock[0] += 5
        assert room.handle_client_message(0, {'kind': 'hold', 'holding': True})[0]
        assert room.snapshot_for(0)['decisionRemainingMs'] == 13000
        assert room.handle_client_message(0, {'kind': 'action', 'action': {'type': 'passHu'}})[0]
        assert room.state.wait_remaining_ms[0] == 10000
        assert room.snapshot_for(0)['decisionRemainingMs'] == 13000
        clock[0] += 3
        assert room.snapshot_for(0)['decisionRemainingMs'] == 10000
        # 普通过胡、重复释放、重连不换窗口；不能再获得完整 20 秒。
        assert room.handle_client_message(0, {'kind': 'action', 'action': {'type': 'passHu'}})[0]
        assert room.handle_client_message(0, {'kind': 'hold', 'holding': False})[0]
        room.on_disconnect(0)
        assert room.snapshot_for(0)['decisionRemainingMs'] == 0
        clock[0] += 2
        room.on_connect(0)
        assert room.snapshot_for(0)['decisionRemainingMs'] == 8000
        assert room.handle_client_message(0, {'kind': 'action', 'action': {'type': 'discard', 'index': 13}})[0]
        assert room.snapshot_for(0)['decisionRemainingMs'] == 0
    finally:
        room.close()


@pytest.mark.asyncio
async def test_overlapping_holds_pause_once_and_other_player_messages_do_not_reset(clock):
    room = human_room()
    room.status = 'lobby'
    room.join_or_rejoin('P1', player_id='p1')
    room.on_connect(1)
    room.status = 'playing'
    prepare(room, 'discard')
    try:
        assert room.snapshot_for(0)['decisionRemainingMs'] == 20000
        clock[0] += 5
        assert room.handle_client_message(0, {'kind': 'hold', 'holding': True})[0]
        clock[0] += 2
        assert room.handle_client_message(1, {'kind': 'hold', 'holding': True})[0]
        clock[0] += 3
        assert room.handle_client_message(0, {'kind': 'hold', 'holding': False})[0]
        assert room.snapshot_for(0)['decisionPaused'] is True
        clock[0] += 2
        assert room.handle_client_message(1, {'kind': 'hold', 'holding': False})[0]
        clock[0] += 1
        assert room.snapshot_for(0)['decisionRemainingMs'] == 14000
        assert room.state.wait_remaining_ms[:2] == [10000, 10000]
        room.handle_client_message(1, {'kind': 'auto', 'enabled': True})
        room.handle_client_message(1, {'kind': 'auto', 'enabled': False})
        assert room.snapshot_for(0)['decisionRemainingMs'] == 14000
    finally:
        room.close()


@pytest.mark.asyncio
async def test_concurrent_claims_start_together_and_choices_do_not_reset_peer(clock):
    room = human_room()
    room.status = 'lobby'
    room.join_or_rejoin('P1', player_id='p1')
    room.on_connect(1)
    room.status = 'playing'
    state = room.state
    state.current = 2
    state.players[2].hand.append(26)
    for seat in (0, 1):
        state.players[seat] = Player(name=f'P{seat}', hand=tiles('111m 222p 333s 44s 99s'))
    assert act(state, 2, Command(type='discard', index=13))
    try:
        # 用一炮多响的窗口同时提供两个合法选择；只给本人返回其操作时限。
        first, second = room.snapshot_for(0), room.snapshot_for(1)
        assert first['decisionRemainingMs'] == second['decisionRemainingMs'] == 20000
        assert room.snapshot_for(2)['decisionRemainingMs'] == 0
        clock[0] += 6
        assert room.handle_client_message(0, {'kind': 'action', 'action': {'type': 'respond', 'action': 'pass'}})[0]
        assert room.snapshot_for(1)['decisionRemainingMs'] == 14000
        assert room.snapshot_for(0)['decisionRemainingMs'] == 0
        state.hand_number += 1
        room._prepare_opening()
        assert room.snapshot_for(1)['decisionRemainingMs'] == 0
    finally:
        room.close()
