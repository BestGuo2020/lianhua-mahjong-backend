"""两个真实 WS 客户端验收模式边界、公开结算和重连恢复。"""
import json
from contextlib import AsyncExitStack

import pytest
import websockets

from app.game.room import room_registry
from tests.test_yaotong_api_ws import create_room, join_room, ready_room, recv_json, start_room, ws_url
from tests.test_yaotong_mode_online import CASES, scenario


async def until(ws, predicate):
    for _ in range(30):
        message = await recv_json(ws, timeout=5)
        if predicate(message):
            return message
    raise AssertionError('未收到预期的模式校验结果')


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['easy', 'hard'])
@pytest.mark.parametrize('name', CASES)
async def test_mode_protection_or_penalty_through_two_real_clients(server, fresh_rooms, mode, name):
    info = await create_room(server['http'], yaotongMode=mode)
    rid = info['roomId']
    observer = await join_room(server['http'], rid, '旁观真人')
    actor = await join_room(server['http'], rid, '操作真人', cookie={'lgm_wakudemo_session': 'mode-actor'})
    for member in (observer, actor):
        await ready_room(server['http'], rid, member['seat'], member['rejoinCode'])
    room = room_registry.get(rid)
    initial, action, reason = scenario(name, actor['seat'])
    room.game_factory = lambda _: initial
    room.ai_delay = 1
    async with AsyncExitStack() as stack:
        ws = await stack.enter_async_context(websockets.connect(ws_url(server['ws'], rid, actor['rejoinCode'])))
        peer = await stack.enter_async_context(websockets.connect(ws_url(server['ws'], rid, observer['rejoinCode'])))
        await start_room(server['http'], rid)
        for client in (ws, peer):
            opening = await until(client, lambda msg: bool(msg.get('opening')))
            await client.send(json.dumps({'kind': 'opening_done', 'round': opening['round']}))
        before = await until(ws, lambda msg: msg.get('kind') == 'yt_snapshot' and not msg.get('opening'))
        assert before['yaotongMode'] == mode
        assert before['view']['players'][actor['seat']]['hand']
        assert before['view']['players'][observer['seat']]['hand'] == []
        await ws.send(json.dumps({'kind': 'action', 'action': action}))
        if mode == 'easy':
            rejected = await until(ws, lambda msg: msg.get('kind') == 'error')
            assert rejected['code'] == 'PROTECTED_ACTION'
            assert room.state.result is None
            assert room.state.serial == before['view']['serial']
        else:
            own = await until(ws, lambda msg: bool(msg.get('roundResult')))
            other = await until(peer, lambda msg: bool(msg.get('roundResult')))
            assert own['roundResult'] == other['roundResult']
            result = own['roundResult']
            assert result['kind'] == 'penalty'
            assert result['offender'] == actor['seat']
            assert reason in result['reason']
            assert sum(result['deltas']) == 0
            assert all(player['hand'] for player in other['view']['players'])
        # 另一位真人重连也必须拿到相同模式、保护信息或包牌结果。
        await peer.close()
        recovered = await stack.enter_async_context(websockets.connect(ws_url(server['ws'], rid, observer['rejoinCode'])))
        assert (await recv_json(recovered))['yaotongMode'] == mode
        snapshot = await until(recovered, lambda msg: msg.get('kind') == 'yt_snapshot')
        assert snapshot['yaotongMode'] == mode
        if mode == 'easy':
            assert snapshot['roundResult'] is None
        else:
            assert snapshot['roundResult'] == result
