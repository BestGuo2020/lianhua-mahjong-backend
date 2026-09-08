"""血流房间会话测试 —— REST 生命周期契约 + 快照对齐前端 seatView 形状 + 真人动作回路。"""

import asyncio
import time

import pytest

from app.core.blood_flow.config import BLOOD_FLOW_CONFIG, BLOOD_FLOW_PACE
from app.game.blood_flow_engine import BloodFlowEngine
from app.game.room import room_registry
from app.rules.blood_flow import BloodFlowRuleSet
from tests.test_blood_flow_engine import make_opening


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
    action_types: set[str] = set()
    saw_discard = False
    handled_window: str | None = None
    while not room.match_finished:
        message = await asyncio.wait_for(queue.get(), timeout=90)
        if message.get('kind') != 'bf_snapshot':
            continue
        snapshots += 1
        view = message['view']
        seen_fields.update(view.keys())
        assert message['mode'] == 'east'
        if view.get('lastDiscardAction'):
            saw_discard = True
        action_types.update(a['type'] for a in view.get('actionEvents') or [])
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
    # 局内过场数据齐备：弃牌流水 + 碰/杠/胡等动作流水（前端据此播动作字/语音/牌名播报）。
    assert saw_discard is True
    assert action_types & {'peng', 'chi', 'discard-gang', 'concealed-gang', 'added-gang', 'wind-kong'}
    assert action_types & {'self-draw', 'discard-win', 'robbed-kong-win'}
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
    assert first['round'] == 1, '局号对玩家应为 1-based（东1局）'
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
    ok, err = room.handle_client_message(0, {'kind': 'opening_done', 'round': 1})
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


@pytest.mark.asyncio
async def test_inter_round_continue_barrier():
    """局末结算后等在线真人回执 continue 再开下一局；过期回执忽略，超时兜底。"""
    room = room_registry.create('BF-CONT', mode='east', capacity=4,
                                ruleset_id='lotus-blood-flow', pace=0)
    room.join_or_rejoin('玩家1', None, 'p1')
    room.ready_seat(0, True)
    queue: asyncio.Queue = asyncio.Queue()
    room.conn.register(0, queue, None)
    room.on_connect(0)
    room.opening_timeout = 0.2
    room.decision_ms = 300
    room.continue_timeout = 5.0
    await room.start()

    settlement = None
    for _ in range(6000):  # 最多 ~120s 打完第一局
        try:
            message = queue.get_nowait()
        except asyncio.QueueEmpty:
            await asyncio.sleep(0.02)
            continue
        if message.get('kind') != 'bf_snapshot':
            continue
        if message.get('opening'):
            room.handle_client_message(0, {'kind': 'opening_done', 'round': message['round']})
        if (message.get('view') or {}).get('public', {}).get('roundResult'):
            settlement = message
            break
    assert settlement is not None, '未收到局末结算快照'
    assert room.round_index == 0

    # 屏障期：未回执不进下一局。
    await asyncio.sleep(0.6)
    assert room.round_index == 0

    # 过期回执忽略（不报错、不放行）。
    ok, _ = room.handle_client_message(0, {'kind': 'continue', 'round': 99})
    assert ok is True
    assert room._continue_confirmations == set()

    ok, err = room.handle_client_message(0, {'kind': 'continue', 'round': 1})
    assert ok, err
    for _ in range(400):
        if room.round_index == 1:
            break
        await asyncio.sleep(0.05)
    assert room.round_index == 1, '回执后应进入下一局'


@pytest.mark.asyncio
async def test_inter_round_barrier_times_out_without_confirmation():
    room = room_registry.create('BF-CONT-TO', mode='east', capacity=4,
                                ruleset_id='lotus-blood-flow', pace=0)
    room.join_or_rejoin('玩家1', None, 'p1')
    room.ready_seat(0, True)
    queue: asyncio.Queue = asyncio.Queue()
    room.conn.register(0, queue, None)
    room.on_connect(0)
    room.opening_timeout = 0.2
    room.decision_ms = 200
    room.continue_timeout = 0.2  # 局间兜底极短
    await room.start()
    for _ in range(6000):
        try:
            message = queue.get_nowait()
        except asyncio.QueueEmpty:
            await asyncio.sleep(0.02)
            continue
        if message.get('kind') == 'bf_snapshot' and message.get('opening'):
            room.handle_client_message(0, {'kind': 'opening_done', 'round': message['round']})
        if room.round_index >= 1:
            break
    assert room.round_index >= 1, '局间超时后应自动进入下一局'


def test_pace_table_wired_for_real_rooms():
    """REST 建房注入的经典节奏表（dict）→ 血流用自带节奏表；测试用 0 → 无节奏。"""
    room = room_registry.create('BF-PACE', mode='east', capacity=4,
                                ruleset_id='lotus-blood-flow',
                                pace={'afterDiscardToNextTurn': 450})
    assert room.pace == BLOOD_FLOW_PACE
    assert set(BLOOD_FLOW_PACE) >= {'aiThinkTurn', 'aiThinkClaim', 'afterDiscardToNextTurn',
                                    'afterClaimPeng', 'afterClaimGang', 'afterKongSettle',
                                    'beforeRobKong', 'winEffectBase', 'winEffectLarge',
                                    'winEffectTop', 'multiWinIntro'}
    quiet = room_registry.create('BF-PACE-0', mode='east', capacity=4,
                                 ruleset_id='lotus-blood-flow', pace=0)
    assert quiet.pace == {}


def test_step_delay_matches_local_timing():
    room = room_registry.create('BF-PACE-K', mode='east', capacity=4,
                                ruleset_id='lotus-blood-flow', pace=BLOOD_FLOW_PACE)
    hands = [
        ['m7', 'm8', 'm9', 'p7', 'p8', 'p9', 's7', 's8', 's9', 'north', 'west', 'south', 'p4', 'm5'],
        ['m3', 'm4', 'm5', 'm5', 'm5', 'p1', 'p2', 'p3', 's1', 's2', 's3', 'east', 'east'],
        ['m1', 'm4', 'm7', 'p2', 'p5', 'p8', 's3', 's6', 's9', 'east', 'south', 'west', 'north'],
        ['m2', 'm6', 'm8', 'p3', 'p6', 'p9', 's2', 's5', 's8', 'red', 'green', 'white', 'north'],
    ]
    engine = BloodFlowEngine(authority_epoch='t', round_id='pace', rules=BloodFlowRuleSet(),
                             opening=make_opening(hands=hands, wall_front=['north']))
    # 开局摸牌后的首个窗口：无额外停顿（AI 思考 650ms 覆盖，对齐经典无 afterDraw）。
    assert room._step_delay_ms(engine, 0, 0, 0) == 0
    assert engine.submit(engine.command(0, {'kind': 'discard', 'index': 13}))
    assert engine.submit(engine.command(1, {'kind': 'peng'}))
    assert room._step_delay_ms(engine, 0, 0, 0) == BLOOD_FLOW_PACE['afterClaimPeng']

    win_engine = BloodFlowEngine(authority_epoch='t', round_id='pace-win', rules=BloodFlowRuleSet(),
                                 opening=make_opening(hands=hands, wall_front=['north']))
    assert win_engine.submit(win_engine.command(0, {'kind': 'discard', 'index': 13}))
    assert win_engine.submit(win_engine.command(1, {'kind': 'win'}))
    # 平胡档：1815 + 1200 尾量 + 100 交接余量（对齐前端 bloodFlowWinTiming）。
    assert room._step_delay_ms(win_engine, 0, 0, 0) == 1815 + 1200 + 100


@pytest.mark.asyncio
async def test_snapshot_players_carry_room_identity_and_fresh_deadline():
    """玩家信息来自房间座位（昵称/角色/人类或 AI）；真人窗口的倒计时从窗口出现起算。"""
    room = room_registry.create('BF-ID', mode='east', capacity=4,
                                ruleset_id='lotus-blood-flow', pace=0)
    room.join_or_rejoin('阿甲', None, 'p1')
    room.ready_seat(0, True)
    queue: asyncio.Queue = asyncio.Queue()
    room.conn.register(0, queue, None)
    room.on_connect(0)
    room.decision_ms = 5_000
    await room.start()

    human_view = None
    for _ in range(80):
        message = await asyncio.wait_for(queue.get(), timeout=10)
        if message.get('opening'):
            room.handle_client_message(0, {'kind': 'opening_done', 'round': message['round']})
        view = message['view']
        # 开局动画期间窗口尚未计时（deadlineAt=0）；屏障放行后驱动重播快照才带读秒。
        if view.get('window') and view.get('ownActions') and view['window'].get('deadlineAt'):
            human_view = view
            break
    assert human_view is not None, '未等到带读秒的真人窗口'
    players = human_view['players']
    assert players[0]['name'] == '阿甲'
    assert players[0]['playerKind'] == 'human'
    assert players[0]['characterId'] == 'deepseek'
    assert players[1]['playerKind'] == 'bot'
    assert players[1]['isLlm'] is False
    remaining = human_view['window']['deadlineAt'] - int(time.time() * 1000)
    assert 0 < remaining <= 5_000, f'真人窗口剩余时间应从窗口出现起算: {remaining}'


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
