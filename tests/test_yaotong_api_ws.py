"""幺筒 REST/WS 接线：建房、加入、准备、开局快照与 WS 握手。"""

import asyncio
import json
from urllib.parse import quote

import httpx
import pytest
import websockets
import websockets.asyncio.client  # noqa: F401

from app.game.room import room_registry as rooms

COOKIE_A = {'lgm_wakudemo_session': 'yt-api-a'}


async def create_room(base_http: str, cookie=COOKIE_A, **overrides) -> dict:
    payload = {
        'mode': 'east',
        'capacity': 4,
        'rulesetId': 'yaotong',
        'matchLength': 4,
        'llmEnabled': False,
    }
    payload.update(overrides)
    async with httpx.AsyncClient(base_url=base_http, timeout=10) as client:
        response = await client.post('/api/rooms', json=payload, cookies=cookie)
        assert response.status_code == 200, response.text
        return response.json()


async def join_room(base_http: str, room_id: str, nickname: str,
                    cookie=COOKIE_A) -> dict:
    async with httpx.AsyncClient(base_url=base_http, timeout=10) as client:
        response = await client.post(
            f'/api/rooms/{room_id}/join',
            json={'nickname': nickname}, cookies=cookie,
        )
        assert response.status_code == 200, response.text
        return response.json()


async def ready_room(base_http: str, room_id: str, seat: int,
                     rejoin_code: str, cookie=COOKIE_A) -> None:
    async with httpx.AsyncClient(base_url=base_http, timeout=10) as client:
        response = await client.post(
            f'/api/rooms/{room_id}/ready',
            json={'seat': seat, 'rejoinCode': rejoin_code}, cookies=cookie,
        )
        assert response.status_code == 200, response.text


async def start_room(base_http: str, room_id: str, cookie=COOKIE_A) -> None:
    async with httpx.AsyncClient(base_url=base_http, timeout=10) as client:
        response = await client.post(
            f'/api/rooms/{room_id}/start', json={}, cookies=cookie)
        assert response.status_code == 200, response.text


def ws_url(base_ws: str, room_id: str, rejoin_code: str) -> str:
    return (
        f'{base_ws}/ws/room/{room_id}?rejoin_code={quote(rejoin_code)}'
    )


async def recv_json(ws, timeout=8.0) -> dict:
    raw = await asyncio.wait_for(ws.recv(), timeout)
    return json.loads(raw)


@pytest.mark.asyncio
async def test_create_yaotong_room_exposes_match_length(server, fresh_rooms):
    room = await create_room(server['http'], matchLength=16)
    assert room['rulesetId'] == 'yaotong'
    assert room['matchLength'] == 16
    assert room['yaotongMode'] == 'easy'
    assert room['capacity'] == 4
    assert len(room['seats']) == 4

    joined = await join_room(server['http'], room['roomId'], '甲')
    assert joined['seat'] == 0
    assert joined['playerId'] == 'wakudemo-yt-api-a'
    await ready_room(
        server['http'], room['roomId'], joined['seat'], joined['rejoinCode'])

    async with httpx.AsyncClient(base_url=server['http'], timeout=10) as client:
        response = await client.get(
            f'/api/rooms/{room["roomId"]}', cookies=COOKIE_A)
        assert response.status_code == 200
        detail = response.json()
    assert detail['matchLength'] == 16
    assert detail['yaotongMode'] == 'easy'
    assert detail['seats'][0]['ready'] is True
    assert detail['seats'][0]['nickname'] == '甲'


@pytest.mark.asyncio
async def test_yaotong_create_accepts_expert_mode(server, fresh_rooms):
    room = await create_room(server['http'], matchLength=8, yaotongMode='hard')
    assert room['yaotongMode'] == 'hard'
    assert room['matchLength'] == 8


@pytest.mark.asyncio
async def test_yaotong_create_rejects_llm_and_non_four_seats(server, fresh_rooms):
    async with httpx.AsyncClient(base_url=server['http'], timeout=10) as client:
        llm_response = await client.post('/api/rooms', json={
            'mode': 'east', 'capacity': 4, 'rulesetId': 'yaotong',
            'matchLength': 4, 'llmEnabled': True,
        }, cookies=COOKIE_A)
        assert llm_response.status_code == 409
        assert llm_response.json()['detail']['code'] == 'LLM_NOT_ENABLED'

        capacity_response = await client.post('/api/rooms', json={
            'mode': 'east', 'capacity': 3, 'rulesetId': 'yaotong',
            'matchLength': 4, 'llmEnabled': False,
        }, cookies=COOKIE_A)
        assert capacity_response.status_code == 409
        assert capacity_response.json()['detail']['code'] == 'INVALID_CAPACITY'


async def auto_client(ws, timeout=60.0) -> dict:
    """自动打完整场：原报/出牌/响应/继续；返回终局快照。"""
    continued: set[int] = set()
    opened: set[int] = set()
    while True:
        message = await recv_json(ws, timeout=timeout)
        kind = message.get('kind')
        if kind == 'error':
            # 自动客户端与服务器自动跳过原报/抢窗口存在竞态：旧动作被拒是预期。
            if message.get('code') in ('INVALID_ACTION', 'STALE_ACTION'):
                continue
            raise AssertionError(f"联机错误 {message.get('code')}")
        if kind != 'yt_snapshot':
            continue
        view = message.get('view') or {}
        seat = message.get('seat')
        round_no = message.get('round')
        if message.get('opening') and round_no not in opened:
            opened.add(round_no)
            await ws.send(json.dumps({'kind': 'opening_done', 'round': round_no}))
        if view.get('phase') == 'original' and view.get('ownOriginalPending'):
            await ws.send(json.dumps({
                'kind': 'action',
                'action': {'type': 'original', 'declare': False},
            }))
        elif view.get('phase') == 'discard' and view.get('current') == seat:
            own = next(
                (player for player in view.get('players', [])
                 if player.get('seat') == seat), None)
            hand = (own or {}).get('hand') or []
            await ws.send(json.dumps({
                'kind': 'action',
                'action': {'type': 'discard', 'index': len(hand) - 1},
            }))
        elif view.get('phase') == 'response' and view.get('ownOptions'):
            options = view['ownOptions']
            action = 'hu' if options.get('hu') else 'pass'
            await ws.send(json.dumps({
                'kind': 'action',
                'action': {'type': 'respond', 'action': action},
            }))
        if message.get('roundResult') and not message.get('matchFinished') \
                and round_no not in continued:
            continued.add(round_no)
            await ws.send(json.dumps({'kind': 'continue', 'round': round_no}))
        if message.get('matchFinished'):
            return message


@pytest.mark.parametrize('mode', ['easy', 'hard'])
@pytest.mark.asyncio
async def test_yaotong_two_clients_play_full_match(server, fresh_rooms, mode):
    room = await create_room(
        server['http'], matchLength=4, yaotongMode=mode)
    first = await join_room(server['http'], room['roomId'], '甲')
    second = await join_room(
        server['http'], room['roomId'], '乙',
        cookie={'lgm_wakudemo_session': 'yt-api-b'})
    await ready_room(
        server['http'], room['roomId'], first['seat'], first['rejoinCode'])
    await ready_room(
        server['http'], room['roomId'], second['seat'], second['rejoinCode'])
    # 测试去掉 AI 的展示停顿，整场更快。
    session = rooms.get(room['roomId'])
    assert session.yaotong_mode == mode
    session.ai_delay = 0

    async with websockets.connect(
        ws_url(server['ws'], room['roomId'], first['rejoinCode']),
    ) as ws_a, websockets.connect(
        ws_url(server['ws'], room['roomId'], second['rejoinCode']),
    ) as ws_b:
        assert (await recv_json(ws_a))['kind'] == 'rejoin_ok'
        await recv_json(ws_a)  # lobby snapshot
        assert (await recv_json(ws_b))['kind'] == 'rejoin_ok'
        await recv_json(ws_b)
        await start_room(server['http'], room['roomId'])
        final_a, final_b = await asyncio.gather(
            auto_client(ws_a), auto_client(ws_b))
    assert final_a['matchFinished'] is True
    assert final_b['matchFinished'] is True
    assert final_a['roundResult']['finished'] is True
    assert sum(final_a['roundResult']['deltas']) == 0


@pytest.mark.asyncio
async def test_yaotong_auto_seat_plays_to_match_end(server, fresh_rooms):
    room = await create_room(server['http'], matchLength=4)
    joined = await join_room(server['http'], room['roomId'], '甲')
    await ready_room(
        server['http'], room['roomId'], joined['seat'], joined['rejoinCode'])
    session = rooms.get(room['roomId'])
    session.ai_delay = 0
    session.match_length = 1

    async with websockets.connect(
        ws_url(server['ws'], room['roomId'], joined['rejoinCode']),
    ) as ws:
        assert (await recv_json(ws))['kind'] == 'rejoin_ok'
        await recv_json(ws)
        await start_room(server['http'], room['roomId'])
        final = None
        while final is None:
            message = await recv_json(ws, timeout=30)
            if message.get('opening'):
                await ws.send(json.dumps({
                    'kind': 'opening_done', 'round': message.get('round'),
                }))
                await ws.send(json.dumps({'kind': 'auto', 'enabled': True}))
            if message.get('matchFinished'):
                final = message
    assert final['roundResult']['finished'] is True
    assert sum(final['roundResult']['deltas']) == 0


@pytest.mark.asyncio
async def test_ws_yaotong_opening_and_original_action(server, fresh_rooms):
    room = await create_room(server['http'], matchLength=4)
    joined = await join_room(server['http'], room['roomId'], '甲')
    await ready_room(
        server['http'], room['roomId'], joined['seat'], joined['rejoinCode'])

    async with websockets.connect(
        ws_url(server['ws'], room['roomId'], joined['rejoinCode']),
    ) as ws:
        rejoin = await recv_json(ws)
        assert rejoin['kind'] == 'rejoin_ok'
        assert rejoin['rulesetId'] == 'yaotong'
        assert rejoin['matchLength'] == 4
        lobby = await recv_json(ws)
        assert lobby['kind'] == 'yt_snapshot'
        assert lobby['view']['phase'] == 'lobby'

        await start_room(server['http'], room['roomId'])
        opening = None
        while opening is None:
            message = await recv_json(ws)
            if message.get('kind') == 'yt_snapshot' and message.get('opening'):
                opening = message
        assert opening['view']['phase'] == 'original'
        assert opening['view']['ownOriginalPending'] is True
        assert opening['view']['players'][0]['hand']
        assert opening['view']['players'][1]['hand'] == []

        await ws.send(json.dumps({'kind': 'opening_done', 'round': 1}))
        await ws.send(json.dumps({
            'kind': 'action',
            'action': {'type': 'original', 'declare': False},
        }))
        progressed = None
        for _ in range(20):
            message = await recv_json(ws)
            if message.get('kind') != 'yt_snapshot':
                continue
            view = message.get('view') or {}
            if view.get('originalDecisions', [False])[0]:
                progressed = message
                break
        assert progressed is not None
