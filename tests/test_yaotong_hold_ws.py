"""真实双客户端验证老手等的 15 秒上限与整桌恢复推进。"""
import asyncio
import json
import time

import pytest
import websockets

from app.game.room import room_registry
from tests.test_yaotong_api_ws import (
    create_room, join_room, ready_room, recv_json, start_room, ws_url,
)
from tests.test_yaotong_online_regressions import table


async def snapshot_until(ws, predicate, timeout=20):
    deadline = time.monotonic() + timeout
    while True:
        message = await recv_json(ws, timeout=max(0.01, deadline - time.monotonic()))
        assert message.get('kind') != 'error', message
        if message.get('kind') == 'yt_snapshot' and predicate(message):
            return message


@pytest.mark.asyncio
async def test_two_real_clients_hold_expires_in_15_seconds_and_ai_resumes(server, fresh_rooms):
    info = await create_room(server['http'], yaotongMode='hard')
    rid = info['roomId']
    first = await join_room(server['http'], rid, '甲')
    second = await join_room(server['http'], rid, '乙',
                             cookie={'lgm_wakudemo_session': 'hold-peer'})
    for member in (first, second):
        await ready_room(server['http'], rid, member['seat'], member['rejoinCode'])
    room = room_registry.get(rid)

    def factory(_):
        state = table()
        state.current = 2
        state.players[2].hand.append(26)
        state.players[2].drawn = 26
        return state

    room.game_factory = factory
    room.ai_delay = 1
    async with websockets.connect(ws_url(server['ws'], rid, first['rejoinCode'])) as ws, \
            websockets.connect(ws_url(server['ws'], rid, second['rejoinCode'])) as peer:
        await start_room(server['http'], rid)
        await snapshot_until(ws, lambda msg: msg.get('opening'))
        await snapshot_until(peer, lambda msg: msg.get('opening'))
        await ws.send(json.dumps({'kind': 'hold', 'holding': True}))
        held = await snapshot_until(ws, lambda msg: msg['holdSeats'] == [0])
        assert 14900 <= held['view']['waitRemainingMs'][0] <= 15000
        started = time.monotonic()
        await peer.send(json.dumps({'kind': 'hold', 'holding': True}))
        await snapshot_until(peer, lambda msg: msg['holdSeats'] == [0, 1])
        for client in (ws, peer):
            await client.send(json.dumps({'kind': 'opening_done', 'round': 1}))

        # 另一玩家提前结束等待；本家再点等也不得从 15 秒重新起算。
        await asyncio.sleep(1)
        await peer.send(json.dumps({'kind': 'hold', 'holding': False}))
        remaining = await snapshot_until(ws, lambda msg: msg['holdSeats'] == [0]
                                         and msg['view']['waitRemainingMs'][1] < 15000)
        assert 0 < remaining['view']['waitRemainingMs'][1] < 14500
        assert 0 < remaining['view']['waitRemainingMs'][0] < 14500
        await ws.send(json.dumps({'kind': 'hold', 'holding': True}))

        expired = await snapshot_until(ws, lambda msg: not msg['holdSeats'])
        elapsed = time.monotonic() - started
        assert 14.8 <= elapsed <= 16.5
        assert expired['view']['waitRemainingMs'][0] == 0
        assert not any(event['kind'] == 'discard' for event in expired['view']['events'])
        resumed = await snapshot_until(ws, lambda msg: any(
            event['kind'] == 'discard' and event['seat'] == 2 for event in msg['view']['events']), timeout=4)
        assert resumed['view']['waitRemainingMs'][0] == 0
        await ws.send(json.dumps({'kind': 'hold', 'holding': True}))
        while True:
            response = await recv_json(ws)
            if response.get('kind') == 'error':
                assert response['code'] == 'NO_HOLD_BUDGET'
                break
