"""幺筒 REST/WS 接线：建房、加入、准备、开局快照与 WS 握手。"""

import asyncio
import json
from urllib.parse import quote

import httpx
import pytest
import websockets
import websockets.asyncio.client  # noqa: F401

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
    assert detail['seats'][0]['ready'] is True
    assert detail['seats'][0]['nickname'] == '甲'


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
