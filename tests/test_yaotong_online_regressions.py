import asyncio
from unittest.mock import AsyncMock

import pytest

from app.core.yaotong.engine import StartOptions, act, create_game, self_options
from app.core.yaotong.hu_window import hu_window
from app.core.yaotong.types import Command, Player
from app.game.yaotong_room import YaotongRoomSession


def tiles(text):
    return ['mps'.index(part[-1]) * 9 + int(n) - 1
            for part in text.split() for n in part[:-1]]


def table():
    state = create_game(StartOptions(dealer=0, dice=(2, 3)))
    state.players = [Player(name=f'P{i}', hand=tiles('147m 258p 369s 2468s'), draws=2)
                     for i in range(4)]
    state.wall = [17, 20, 23]
    state.phase = 'discard'
    state.current = 0
    return state


def human_room(mode='hard'):
    room = YaotongRoomSession('REGRESSION', yaotong_mode=mode)
    room.join_or_rejoin('P0', player_id='p0')
    room.on_connect(0)
    room.state = table()
    room.status = 'playing'
    return room


def test_pass_big_self_draw_serializes_score_and_shape_then_rejoins():
    room = human_room()
    room.state.players[0] = Player(name='P0', hand=tiles('22233344455566m'), draws=2, drawn=5)
    assert self_options(room.state, 0).h == 45
    assert room.handle_client_message(0, {'kind': 'action', 'action': {'type': 'passHu'}})[0]
    room.on_disconnect(0)
    room.on_connect(0)
    own = room.snapshot_for(0)['view']['players'][0]
    assert own['passedHu'] == 45
    assert own['passedShape'] == room.state.players[0].passed_shape
    assert room.snapshot_for(1)['view']['players'][0]['passedHu'] is None


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['easy', 'hard'])
@pytest.mark.parametrize('declaration', ['ready', 'original'])
async def test_locked_human_automatically_discards_without_turn_timeout(mode, declaration):
    room = human_room(mode)
    room.state.players[0] = Player(name='P0', hand=tiles('234567m 234s 66p 78s 1m'),
                                  draws=2, drawn=0, declaration=declaration)
    room._wait_for_action = AsyncMock(side_effect=AssertionError('must not wait for locked input'))
    await room._drive_step()
    assert room.state.players[0].river[-1].tile == 0
    assert room.state.players[0].hand == tiles('234567m 66p 23478s')
    assert not room._forced_finishing


@pytest.mark.asyncio
async def test_locked_human_keeps_legal_hu_choice():
    room = human_room()
    room.state.players[0] = Player(name='P0', hand=tiles('234567m 234s 66p 789s'),
                                  draws=2, drawn=26, declaration='ready')
    room._wait_for_action = AsyncMock(return_value=True)
    await room._drive_step()
    room._wait_for_action.assert_awaited_once()
    assert room.state.result is None
    assert room.state.players[0].river == []


@pytest.mark.asyncio
@pytest.mark.parametrize('reconnect', [True, False])
async def test_ai_thinking_loses_control_without_force_finishing(reconnect):
    room = human_room()
    room.state.players[0].hand.append(26)
    room.state.players[0].drawn = 26
    if reconnect:
        room.on_disconnect(0)
    else:
        room.handle_client_message(0, {'kind': 'auto', 'enabled': True})
    room.ai_delay = 0.03
    task = asyncio.create_task(room._act_ai(0))
    await asyncio.sleep(0.005)
    if reconnect:
        room.on_connect(0)
    else:
        room.handle_client_message(0, {'kind': 'auto', 'enabled': False})
    assert room.handle_client_message(0, {'kind': 'action', 'action': {'type': 'discard', 'index': 13}})[0]
    serial = room.state.serial
    await task
    assert room.status == 'playing'
    assert not room._forced_finishing
    assert room.state.serial == serial


@pytest.mark.asyncio
async def test_hard_hu_only_window_advances_and_can_claim_previous_discard():
    room = human_room()
    room.state.current = 1
    room.state.players[1].hand.append(26)
    room.state.players[1].drawn = 26
    room.state.players[0] = Player(name='P0', hand=tiles('234567m 234s 66p 78s'),
                                  draws=2, declaration='ready')
    assert act(room.state, 1, Command(type='discard', index=13))
    wall_before_draw = list(room.state.wall)
    room._wait_for_action = AsyncMock(side_effect=AssertionError('hu-only must not block table'))
    await room._drive_step()
    assert room.state.phase == 'discard'
    assert room.state.current == 2
    assert hu_window(room.state, 0) is not None
    assert room.state.players[0].passed_hu is None
    assert room.handle_client_message(0, {'kind': 'action', 'action': {'type': 'declareHu'}})[0]
    assert room.state.result.winners == [0]
    assert room.state.result.source == 1
    assert room.state.result.indicator == wall_before_draw[0]


def test_settlement_reveals_hands_but_live_snapshot_does_not():
    room = human_room()
    room.state.players[0] = Player(name='P0', hand=tiles('234567m 234s 66p 789s'), draws=2, drawn=26)
    assert room.snapshot_for(1)['view']['players'][0]['hand'] == []
    assert act(room.state, 0, Command(type='hu'))
    snapshot = room.snapshot_for(1)
    assert len(snapshot['view']['players'][0]['hand']) == 14
    assert snapshot['view']['players'][0]['drawn'] == 's9'


def test_continue_requires_settled_current_round():
    room = human_room()
    assert room.handle_client_message(0, {'kind': 'continue', 'round': 1}) == (False, 'STALE_ACTION')
    room.state.players[0] = Player(name='P0', hand=tiles('234567m 234s 66p 789s'), draws=2, drawn=26)
    assert act(room.state, 0, Command(type='hu'))
    assert room.handle_client_message(0, {'kind': 'continue', 'round': 99}) == (False, 'STALE_ACTION')
    assert room.handle_client_message(0, {'kind': 'continue', 'round': 1})[0]
    assert room.continuation()['readySeats'] == [0]


@pytest.mark.asyncio
@pytest.mark.parametrize('first_passes', [False, True])
async def test_two_humans_keep_independent_windows_and_can_win_together(first_passes):
    room = human_room()
    room.status = 'lobby'
    room.join_or_rejoin('P1', player_id='p1')
    room.join_or_rejoin('P2', player_id='p2')
    room.join_or_rejoin('P3', player_id='p3')
    room.status = 'playing'
    room.on_connect(3)
    room.state.current = 1
    room.state.players[1].hand.append(26)
    room.state.players[1].drawn = 26
    for seat in (0, 3):
        room.state.players[seat] = Player(name=f'P{seat}', hand=tiles('234567m 234s 66p 78s'),
                                          draws=2, declaration='ready')
    assert act(room.state, 1, Command(type='discard', index=13))
    await room._drive_step()
    await room._drive_step()
    assert room.state.phase == 'discard'
    for seat in (0, 3):
        assert hu_window(room.state, seat) is not None
        wire = room.snapshot_for(seat)['huWindow']
        assert wire['ownOptions']['hu']
        assert wire['players'][1]['hand'] == []
    if first_passes:
        assert room.handle_client_message(0, {'kind': 'action', 'action': {'type': 'passHu'}})[0]
        assert hu_window(room.state, 0) is None
        assert hu_window(room.state, 3) is not None
    else:
        assert room.handle_client_message(0, {'kind': 'action', 'action': {'type': 'declareHu'}})[0]
        assert room.state.result is None
    assert room.handle_client_message(3, {'kind': 'action', 'action': {'type': 'declareHu'}})[0]
    assert room.state.result.winners == ([3] if first_passes else [0, 3])


@pytest.mark.asyncio
@pytest.mark.parametrize('extended', [False, True])
async def test_hu_only_observation_uses_one_or_two_seconds(extended):
    room = human_room()
    room.ai_delay = 1.0
    room.state.current = 1
    room.state.players[1].hand.append(26)
    room.state.players[1].drawn = 26
    room.state.players[1].after_kong = extended
    room.state.players[0] = Player(name='P0', hand=tiles('234567m 234s 66p 78s'), draws=2, declaration='ready')
    assert act(room.state, 1, Command(type='discard', index=13))
    room._act_ai = AsyncMock()
    await room._drive_step()
    command = room._act_ai.call_args.kwargs['command']
    assert command.type == 'respond' and command.defer_hu
    assert (1.95 if extended else 0.95) < room._act_ai.call_args.kwargs['delay'] <= (2 if extended else 1)


@pytest.mark.asyncio
async def test_hard_peng_window_still_requires_explicit_human_choice():
    room = human_room()
    room.state.current = 1
    room.state.players[1].hand.append(26)
    room.state.players[1].drawn = 26
    room.state.players[0] = Player(name='P0', hand=tiles('147m 258p 369s 2699s'), draws=2)
    assert act(room.state, 1, Command(type='discard', index=13))
    assert room.state.pending.options[0].peng
    room._wait_for_action = AsyncMock(return_value=True)
    await room._drive_step()
    room._wait_for_action.assert_awaited_once()
    assert 0 not in room.state.pending.choices


@pytest.mark.asyncio
@pytest.mark.parametrize('join_win', [False, True])
async def test_later_hu_reopens_deferred_seat_once_for_multi_win(join_win):
    room = human_room()
    room.state.current = 1
    room.state.players[1].hand.append(26)
    room.state.players[1].drawn = 26
    room.state.players[0] = Player(name='P0', hand=tiles('234567m 234s 66p 78s'), draws=2, declaration='ready')
    room.state.players[3] = Player(name='P3', hand=tiles('22233344499s 66p'), draws=2)
    assert act(room.state, 1, Command(type='discard', index=13))
    assert room.state.pending.options[3].hu and room.state.pending.options[3].peng
    await room._drive_step()
    assert hu_window(room.state, 0) is not None
    assert room.handle_client_message(3, {'kind': 'action', 'action': {'type': 'respond', 'action': 'hu'}})[0]
    assert room.state.result is None
    assert 0 not in room.state.pending.choices
    if join_win:
        assert room.handle_client_message(0, {'kind': 'action', 'action': {'type': 'declareHu'}})[0]
        assert set(room.state.result.winners) == {0, 3}
    else:
        await room._drive_step()
        assert room.state.result.winners == [3]


def test_passing_last_deferred_seat_does_not_stale_another_players_window():
    state = table()
    state.current = 1
    state.players[1].hand.append(26)
    state.players[1].drawn = 26
    for seat in (0, 2):
        state.players[seat] = Player(name=f'P{seat}', hand=tiles('234567m 234s 66p 78s'), draws=2, declaration='ready')
    state.players[3] = Player(name='P3', hand=tiles('147m 258p 369s 2699s'), draws=2)
    assert act(state, 1, Command(type='discard', index=13))
    for seat in (0, 2):
        assert act(state, seat, Command(type='respond', action='pass', defer_hu=True))
    assert act(state, 2, Command(type='passHu'))
    assert act(state, 3, Command(type='respond', action='peng'))
    assert hu_window(state, 0).pending.choices[3] == 'peng'
    assert act(state, 0, Command(type='declareHu'))
    assert state.result.winners == [0]
