"""血流房间会话测试 —— REST 生命周期契约 + 快照对齐前端 seatView 形状 + 真人动作回路。"""

import asyncio

import pytest

from app.core.blood_flow.config import BLOOD_FLOW_CONFIG
from app.game.room import room_registry


def choose(view: dict) -> dict:
    """按快照 ownActions 选择动作（测试驱动，等价前端玩家可提交的合法动作）。"""
    actions = view.get('ownActions') or []
    win = next((a for a in actions if a['kind'] == 'win'), None)
    if win:
        return win
    locked = view['public']['seats'][view['seat']]['locked']
    if locked:
        drawn = view['players'][view['seat']]['drawnTileIndex']
        return {'kind': 'discard', 'index': drawn}
    discards = [a for a in actions if a['kind'] == 'discard']
    if discards:
        hand = view['players'][view['seat']]['hand']
        ordinary = [a for a in discards if hand[a['index']] not in {*view['jokers'], 'white'}]
        return (ordinary or discards)[0]
    return {'kind': 'pass'}


@pytest.fixture(autouse=True)
def _clear_registry():
    room_registry.clear()
    yield
    room_registry.clear()


@pytest.mark.asyncio
async def test_all_bot_match_settles_four_rounds():
    room = room_registry.create('BF-BOT', mode='east', capacity=4,
                                ruleset_id='lotus-blood-flow', pace=0)
    await room.start()
    for _ in range(2400):  # 最多 120s
        if room.match_finished:
            break
        await asyncio.sleep(0.05)
    assert room.match_finished is True
    assert sum(room.scores) == BLOOD_FLOW_CONFIG.initial_score * 4
    assert room.round_result is not None
    assert room.round_result['reason'] == 'wall-exhausted'
    assert room.status == 'finished'


@pytest.mark.asyncio
async def test_human_action_loop_over_snapshot_contract():
    room = room_registry.create('BF-HUM', mode='east', capacity=4,
                                ruleset_id='lotus-blood-flow', pace=0)
    state = None
    seat, is_rejoin, state = room.join_or_rejoin('玩家1', None, 'p1')
    assert seat == 0 and is_rejoin is False
    assert room.ready_seat(0, True)

    queue: asyncio.Queue = asyncio.Queue()
    room.conn.register(0, queue, None)
    room.on_connect(0)
    room.decision_ms = 60_000  # 负载下避免 15s 窗口过期与提交竞态（慢机回归 flake）
    await room.start()
    actions = 0
    snapshots = 0
    seen_fields: set[str] = set()
    handled_window: str | None = None
    while not room.match_finished:
        message = await asyncio.wait_for(queue.get(), timeout=90)
        if message.get('kind') != 'bf_snapshot':
            continue
        snapshots += 1
        view = message['view']
        seen_fields.update(view.keys())
        assert message['mode'] == 'east'
        if message.get('opening'):
            ok, err = room.handle_client_message(0, {
                'kind': 'opening_done', 'round': message['round'],
            })
            assert ok, err
        if view.get('window') and view.get('ownActions'):
            window_id = view['window']['id']
            if window_id == handled_window:
                continue  # 同一窗口的重复快照（提交后与驱动循环各广播一次）
            handled_window = window_id
            action = choose(view)
            ok, err = room.handle_client_message(0, {
                'kind': 'action',
                'windowId': window_id,
                'stateVersion': view['window']['version'],
                'action': action,
            })
            assert ok, err
            actions += 1
    assert actions > 40
    assert snapshots > 40
    assert {'authorityEpoch', 'roundId', 'version', 'seat', 'players', 'currentPlayer',
            'wallCount', 'headDrawn', 'flipTile', 'jokers', 'flipStack', 'flipSeat',
            'wallBreakIndex', 'window', 'ownActions', 'ownScore', 'waitingSeats',
            'public', 'actionEvents', 'lastDiscardAction', 'kongEvents'} <= seen_fields
    assert room.match_finished is True
    assert sum(room.scores) == BLOOD_FLOW_CONFIG.initial_score * 4


@pytest.mark.asyncio
async def test_round_opening_payload_and_ready_barrier():
    """每局首份快照带骰点；真人未回执 opening_done 前不开打（回执后放行，超时兜底）。"""
    room = room_registry.create('BF-OPEN', mode='east', capacity=4,
                                ruleset_id='lotus-blood-flow', pace=0)
    room.join_or_rejoin('玩家1', None, 'p1')
    room.ready_seat(0, True)
    queue: asyncio.Queue = asyncio.Queue()
    room.conn.register(0, queue, None)
    room.on_connect(0)
    room.opening_timeout = 5.0
    room.decision_ms = 1_000  # 屏障放行后靠过期推进窗口，测试不必等 15s
    await room.start()

    first = await asyncio.wait_for(queue.get(), timeout=10)
    assert first['kind'] == 'bf_snapshot'
    assert first['round'] == 0
    assert first['opening'] is not None, '首份快照必须携带开局骰点'
    for key in ('firstDice', 'secondDice'):
        dice = first['opening'][key]
        assert len(dice) == 2 and all(1 <= n <= 6 for n in dice), first['opening']
    window_id = first['view']['window']['id']

    # 未回执：屏障期内窗口不推进（机器人不下手、也不过期）。
    await asyncio.sleep(0.6)
    assert room.engine is not None
    assert room.engine.window['id'] == window_id
    assert room.engine.window['decisions'][0] is None

    # 过期回执不报错也不放行。
    ok, _ = room.handle_client_message(0, {'kind': 'opening_done', 'round': 99})
    assert ok is True
    assert room._opening_confirmations == set()

    # 正确回执：屏障放行，窗口推进。
    ok, err = room.handle_client_message(0, {'kind': 'opening_done', 'round': 0})
    assert ok, err
    for _ in range(200):
        if room.engine and room.engine.window['id'] != window_id:
            break
        await asyncio.sleep(0.05)
    assert room.engine is not None and room.engine.window['id'] != window_id


@pytest.mark.asyncio
async def test_opening_barrier_times_out_without_confirmation():
    room = room_registry.create('BF-OPEN-TO', mode='east', capacity=4,
                                ruleset_id='lotus-blood-flow', pace=0)
    room.join_or_rejoin('玩家1', None, 'p1')
    room.ready_seat(0, True)
    room.conn.register(0, asyncio.Queue(), None)
    room.on_connect(0)
    room.opening_timeout = 0.2
    room.decision_ms = 500
    await room.start()
    for _ in range(100):  # 等引擎建立，记下屏障期的窗口
        if room.engine is not None:
            break
        await asyncio.sleep(0.05)
    assert room.engine is not None
    window_id = room.engine.window['id']
    for _ in range(200):  # 兜底超时后照常开打：窗口推进
        if room.engine is not None and room.engine.window['id'] != window_id:
            break
        await asyncio.sleep(0.05)
    assert room.engine is not None and room.engine.window['id'] != window_id


def test_stale_and_invalid_actions_rejected():
    room = room_registry.create('BF-ERR', mode='east', capacity=4,
                                ruleset_id='lotus-blood-flow', pace=0)
    room.join_or_rejoin('玩家1', None, 'p1')
    room.ready_seat(0, True)
    ok, err = room.handle_client_message(0, {'kind': 'action', 'windowId': 'x', 'action': {'kind': 'pass'}})
    assert not ok and err == 'STALE_ACTION'
    ok, err = room.handle_client_message(0, {'kind': 'action', 'windowId': 'x', 'stateVersion': 0,
                                             'action': {'kind': 'win'}})
    assert not ok and err in ('STALE_ACTION', 'INVALID_ACTION')
    ok, err = room.handle_client_message(0, {'kind': 'nonsense'})
    assert not ok and err == 'INVALID_MESSAGE'
