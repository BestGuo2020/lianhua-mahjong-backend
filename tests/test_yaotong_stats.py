"""幺筒战绩：真实结算落库、按登录身份与玩法汇总。"""

import asyncio

import httpx
import pytest
from fastapi import FastAPI

from app.api import matches as matches_api
from app.core.yaotong.engine import StartOptions, create_game
from app.game.yaotong_room import YaotongRoomSession
from app.storage.db import Storage


@pytest.fixture()
def stats_storage(tmp_path, monkeypatch):
    storage = Storage(str(tmp_path / 'yaotong-stats.db'))
    storage.init()
    monkeypatch.setattr(matches_api, 'storage', storage)
    return storage


def add_match(storage, ruleset_id, player_id='wakudemo-stats-user'):
    match_id = storage.create_match('STATS', 'east', ruleset_id)
    storage.upsert_match_players(match_id, [
        {'seat': 1, 'player_id': player_id, 'nickname': '同名玩家'},
    ])
    return match_id


def test_yaotong_stats_count_multi_winner_draw_and_penalty(stats_storage):
    match_id = add_match(stats_storage, 'yaotong')
    for round_number, kind, winners, deltas in [
        (1, 'win', [1, 2], [-100, 50, 50, 0]),
        (2, 'draw', [], [-10, 30, -10, -10]),
        (3, 'penalty', [], [45, -135, 45, 45]),
    ]:
        stats_storage.insert_round_result(match_id, {
            'round': round_number, 'rulesetId': 'yaotong',
            'kind': kind, 'winners': winners, 'deltas': deltas,
        })
    assert stats_storage.get_player_stats_by_id('wakudemo-stats-user') == {
        'playerId': 'wakudemo-stats-user', 'matches': 1, 'hands': 3,
        'wins': 1, 'totalDelta': -55,
    }


@pytest.mark.asyncio
async def test_my_stats_filter_ruleset_and_keep_login_identity(stats_storage):
    classic_id = add_match(stats_storage, 'lotus-classic')
    stats_storage.insert_round_result(classic_id, {
        'round': 1, 'winner_index': 1,
        'deltas': [{'playerIndex': 1, 'amount': 999}],
    })
    for ruleset_id, player_id, delta in [
        ('yaotong', 'wakudemo-stats-user', 50),
        ('yaotong', 'wakudemo-other-user', 200),
    ]:
        match_id = add_match(stats_storage, ruleset_id, player_id)
        stats_storage.insert_round_result(match_id, {
            'round': 1, 'rulesetId': 'yaotong', 'kind': 'win',
            'winners': [1], 'deltas': [-delta, delta, 0, 0],
        })
    app = FastAPI()
    app.include_router(matches_api.router)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url='http://test',
        cookies={'lgm_wakudemo_session': 'stats-user'},
    ) as client:
        filtered = await client.get('/api/me/stats?rulesetId=yaotong')
        assert filtered.status_code == 200
        assert filtered.json() == {
            'playerId': 'wakudemo-stats-user', 'matches': 1, 'hands': 1,
            'wins': 1, 'totalDelta': 50,
        }
        all_stats = await client.get('/api/me/stats')
        assert all_stats.json()['matches'] == 2
        assert all_stats.json()['totalDelta'] == 1049
        empty = await client.get('/api/me/stats?rulesetId=lotus-legacy')
        assert empty.json()['hands'] == 0


@pytest.mark.asyncio
async def test_room_persists_each_settlement_once_and_supports_next_match(stats_storage):
    room = YaotongRoomSession('STATS', match_length=1, storage=stats_storage)
    room.game_factory = lambda options: create_game(StartOptions(
        wall=[tile for tile in range(27) for _ in range(4)],
        dice=(1, 2), dealer=0, length=options.length,
    ))
    room.join_or_rejoin('改名之后', player_id='wakudemo-stats-user')
    room.ready_seat(0, True)
    total_delta = 0
    hands = 0
    for match_number in (1, 2):
        await room.start()
        await asyncio.wait_for(room._drive_task, timeout=10)
        match = stats_storage.get_match(room.match_id)
        assert match['rulesetId'] == 'yaotong'
        assert len(match['rounds']) == room.state.hand_number
        assert match['rounds']
        expected_delta = sum(row['result']['deltas'][0] for row in match['rounds'])
        assert expected_delta == room.state.players[0].score
        total_delta += expected_delta
        hands += len(match['rounds'])
        await room._persist_round()
        assert len(stats_storage.get_match(room.match_id)['rounds']) == len(match['rounds'])
        stats = stats_storage.get_player_stats_by_id('wakudemo-stats-user', 'yaotong')
        assert stats['matches'] == match_number
        assert stats['totalDelta'] == total_delta
        assert stats['hands'] == hands
    app = FastAPI()
    app.include_router(matches_api.router)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url='http://test',
        cookies={'lgm_wakudemo_session': 'stats-user'},
    ) as client:
        response = await client.get('/api/me/stats?rulesetId=yaotong')
        assert response.status_code == 200
        assert response.json() == stats


@pytest.mark.asyncio
@pytest.mark.parametrize('committed', [False, True])
async def test_round_write_retries_without_losing_or_duplicating_result(stats_storage, monkeypatch, committed):
    room = YaotongRoomSession('RETRY', storage=stats_storage)
    room.match_id = add_match(stats_storage, 'yaotong')
    room.state = create_game(StartOptions())
    from app.core.yaotong.settlement import Ending
    from app.core.yaotong.engine import finish
    finish(room.state, Ending(kind='draw'))
    insert = stats_storage.insert_round_result
    attempts = []

    def flaky_insert(match_id, data):
        attempts.append(data['round'])
        if len(attempts) == 1:
            if committed:
                insert(match_id, data)
            raise RuntimeError('temporary database outage')
        insert(match_id, data)

    monkeypatch.setattr(stats_storage, 'insert_round_result', flaky_insert)
    await room._persist_round()
    assert len(stats_storage.get_match(room.match_id)['rounds']) == int(committed)
    await room._persist_round()
    await room._persist_round()
    assert len(stats_storage.get_match(room.match_id)['rounds']) == 1
    assert len(attempts) == 2
