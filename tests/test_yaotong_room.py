"""幺筒房间：座位、快照裁剪、AI 驱动到终局。"""

import asyncio

import pytest

from app.core.yaotong.engine import StartOptions, create_game
from app.game.yaotong_room import YaotongRoomSession

WALL = [tile for tile in range(27) for _ in range(4)]


def fixed_factory(options):
    return create_game(StartOptions(
        wall=list(WALL), dice=(1, 2), dealer=0, length=options.length,
    ))


def test_join_rejoin_and_ready():
    room = YaotongRoomSession('YT1')
    seat, rejoin, state = room.join_or_rejoin(
        '甲', player_id='wakudemo-a', character_id='deepseek')
    assert (seat, rejoin) == (0, False)
    assert state.player_id == 'wakudemo-a'
    assert room.creator_seat == 0

    same_seat, same_rejoin, same_state = room.join_or_rejoin(
        '甲', player_id='wakudemo-a')
    assert (same_seat, same_rejoin) == (0, True)
    assert same_state is state

    by_code, code_rejoin, _ = room.join_or_rejoin(
        '甲', rejoin_code=state.rejoin_code)
    assert (by_code, code_rejoin) == (0, True)
    assert room.ready_seat(0) is True
    assert room.ready_seat(0) is False
    assert room.has_humans() is True

    room.release_seat(0)
    assert room.has_humans() is False
    assert room.creator_seat is None


def test_snapshot_hides_other_hands_and_exposes_own_options():
    room = YaotongRoomSession('YT2')
    room.join_or_rejoin('甲', player_id='wakudemo-a')
    room.join_or_rejoin('乙', player_id='wakudemo-b')
    room.state = fixed_factory(StartOptions(length=4))
    room.match_length = 4

    first = room.snapshot_for(0)
    second = room.snapshot_for(1)
    assert first['kind'] == 'yt_snapshot'
    assert first['rulesetId'] == 'yaotong'
    assert first['matchLength'] == 4
    assert first['view']['phase'] == 'original'
    assert len(first['view']['players'][0]['hand']) == 13
    assert first['view']['players'][1]['hand'] == []
    assert first['view']['players'][0]['concealedTileCount'] == 13
    assert first['view']['ownOriginalPending'] is True
    assert second['view']['players'][1]['hand']
    assert second['view']['players'][0]['hand'] == []


def test_start_rejects_llm_seats():
    room = YaotongRoomSession('YT3')
    room.join_or_rejoin('甲', player_id='wakudemo-a')
    room.ready_seat(0, True)
    with pytest.raises(Exception):
        asyncio.run(room.start(llm_seats=[{'seat': 1, 'providerId': 'x'}]))


@pytest.mark.asyncio
async def test_ai_room_drives_to_finished_match():
    room = YaotongRoomSession('YT4', match_length=1)
    room.game_factory = fixed_factory
    room.join_or_rejoin('甲', player_id='wakudemo-a')
    room.ready_seat(0, True)
    await room.start()
    assert room._drive_task is not None
    await asyncio.wait_for(room._drive_task, timeout=10)
    assert room.status == 'finished'
    assert room.match_finished is True
    assert room.state is not None
    assert room.state.result is not None
    assert sum(room.state.result.deltas) == 0
    assert room.state.phase == 'settled'
