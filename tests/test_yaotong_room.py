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


def test_interactions_use_authoritative_seats_and_only_broadcast_valid_current_hand_messages(monkeypatch):
    room = YaotongRoomSession('YT3')
    room.join_or_rejoin('甲', player_id='wakudemo-a')
    room.join_or_rejoin('乙', player_id='wakudemo-b')
    events = []
    monkeypatch.setattr(room.conn, 'broadcast', events.append)
    hand = {'matchId': 'match', 'round': 1}
    phrase = {**hand, 'kind': 'quick_chat', 'phraseId': 'hello'}
    emote = {**hand, 'kind': 'emote', 'emote': 'tomato', 'targetSeat': 0}
    assert room.handle_client_message(1, phrase) == (False, 'STALE_INTERACTION')
    room.status = 'playing'
    room.match_id = hand['matchId']
    room.state = fixed_factory(StartOptions(length=4))

    assert room.handle_client_message(1, {**phrase, 'phraseId': []}) == (False, 'INVALID_INTERACTION')
    assert room.handle_client_message(1, {**emote, 'targetSeat': False}) == (False, 'INVALID_INTERACTION')
    assert room.handle_client_message(1, {**emote, 'targetSeat': 1}) == (False, 'INVALID_INTERACTION')
    assert room.handle_client_message(1, {**emote, 'matchId': 'old'}) == (False, 'STALE_INTERACTION')
    assert room.handle_client_message(1, {**phrase, 'round': True}) == (False, 'STALE_INTERACTION')

    assert room.handle_client_message(1, {**phrase, 'fromSeat': 3, 'text': '任意文本'}) == (True, '')
    assert room.handle_client_message(1, phrase) == (False, 'INTERACTION_RATE_LIMITED')
    assert room.handle_client_message(0, phrase) == (True, '')
    assert room.handle_client_message(1, emote) == (True, '')
    assert room.handle_client_message(1, emote) == (False, 'INTERACTION_RATE_LIMITED')
    assert events == [
        {**phrase, 'fromSeat': 1},
        {**phrase, 'fromSeat': 0},
        {**emote, 'fromSeat': 1},
    ]

    room.state.phase = 'settled'
    assert room.handle_client_message(1, emote) == (False, 'STALE_INTERACTION')
    room.state = fixed_factory(StartOptions(length=4))
    room.state.hand_number = 2
    room._prepare_opening()
    assert room.handle_client_message(1, phrase) == (False, 'STALE_INTERACTION')
    assert room.handle_client_message(1, {**phrase, 'round': 2}) == (True, '')
    assert events[-1] == {**phrase, 'round': 2, 'fromSeat': 1}


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
