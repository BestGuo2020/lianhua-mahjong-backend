"""Measure the delivered WS timeline at production pace, not only timing constants."""
import asyncio
import json
import time
from contextlib import suppress

import httpx
import pytest
import websockets

from app.game.room import room_registry
from tests.test_yaotong_pace import CASES, state_for
from tests.test_yaotong_api_ws import create_room, join_room, ready_room, start_room, recv_json, ws_url


@pytest.mark.asyncio
@pytest.mark.parametrize('scenario', CASES[:8], ids=lambda case: case['name'])
async def test_real_ws_ai_pace_matches_local_timeline(server, fresh_rooms, scenario):
    info = await create_room(server['http'])
    rid = info['roomId']
    peer_cookie = {'lgm_wakudemo_session': 'pace-peer'}
    peer = await join_room(server['http'], rid, '另一真人', cookie=peer_cookie)
    member = await join_room(server['http'], rid, '节奏验收')
    await ready_room(server['http'], rid, peer['seat'], peer['rejoinCode'])
    await ready_room(server['http'], rid, member['seat'], member['rejoinCode'])
    room = room_registry.get(rid)
    def factory(_):
        state = state_for(scenario)
        # 真人位于 0/1，两个 AI 位于 2/3；把单机本家 0 / 下家 AI 1 旋转到 1/2。
        state.players = [state.players[3], *state.players[:3]]
        state.current = (state.current + 1) % 4
        state.dealer = (state.dealer + 1) % 4
        for player in state.players:
            for meld in player.melds:
                if meld.from_seat is not None:
                    meld.from_seat = (meld.from_seat + 1) % 4
        return state
    room.game_factory = factory
    room.ai_delay = 1.0
    observed = []
    seen = 1  # create_game 的 start 事件在测试动作之前。
    command_sent = False
    peer_task = None
    try:
        async with websockets.connect(ws_url(server['ws'], rid, member['rejoinCode'])) as ws, \
                websockets.connect(ws_url(server['ws'], rid, peer['rejoinCode'])) as peer_ws:
            await recv_json(ws)
            await recv_json(ws)
            await recv_json(peer_ws)
            await recv_json(peer_ws)

            async def peer_reader():
                while True:
                    message = await recv_json(peer_ws, timeout=10)
                    if message.get('opening'):
                        await peer_ws.send(json.dumps({'kind': 'opening_done', 'round': message['round']}))

            peer_task = asyncio.create_task(peer_reader())
            await start_room(server['http'], rid, cookie=peer_cookie)
            while len(observed) < len(scenario['events']):
                message = await recv_json(ws, timeout=10)
                if message.get('kind') != 'yt_snapshot':
                    assert message.get('kind') != 'error', message
                    continue
                if message.get('opening'):
                    await ws.send(json.dumps({'kind': 'opening_done', 'round': message['round']}))
                    continue
                if scenario['actor'] == 0 and not command_sent:
                    command_sent = True
                    await ws.send(json.dumps({'kind': 'action', 'action': scenario['command']}))
                for event in message['view']['events']:
                    if event['id'] <= seen:
                        continue
                    seen = event['id']
                    observed.append((time.monotonic(), event['kind'], (event['seat'] - 1) % 4))
            peer_task.cancel()
            with suppress(asyncio.CancelledError):
                await peer_task
        baseline = observed[0][0]
        actual = [[round((stamp - baseline) * 1000), kind, seat] for stamp, kind, seat in observed]
        assert [event[1:] for event in actual] == [event[1:] for event in scenario['events']]
        for measured, expected in zip(actual, scenario['events']):
            assert expected[0] - 80 <= measured[0] <= expected[0] + 700, actual
        print(json.dumps({'case': scenario['name'], 'human_clients': 2, 'ws_events_ms': actual}))
    finally:
        if peer_task is not None and not peer_task.done():
            peer_task.cancel()
            with suppress(asyncio.CancelledError):
                await peer_task
        async with httpx.AsyncClient(base_url=server['http'], trust_env=False) as client:
            for participant in (member, peer):
                await client.post(f'/api/rooms/{rid}/leave', json={'seat': participant['seat'], 'rejoinCode': participant['rejoinCode']})
