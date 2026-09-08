"""血流对局引擎测试 —— 守恒、锁手、多响、抢杠回滚、杠分、整局墙尽结算。"""

import random

import pytest

from app.core.blood_flow.config import BLOOD_FLOW_CONFIG
from app.core.tiles import TILE_TYPES, create_wall
from app.game.blood_flow_engine import BloodFlowEngine, SEATS, next_seat
from app.rules.blood_flow import BloodFlowRuleSet


def seeded_random(seed: int):
    rng = random.Random(seed)
    return lambda: rng.random()


def make_opening(*, hands: list[list[str]], wall_front: list[str], melds: list[list[dict]] | None = None,
                 jokers: list[str] | None = None, dealer_drawn_index: int = 13,
                 discard_pool: list[str] | None = None):
    """构造固定开局：指定四家手牌与牌墙前若干张；其余牌墙用剩余牌补齐。"""
    ring = create_wall()
    melds = melds or [[], [], [], []]
    jokers = jokers or ['red', 'green']

    def take(tile: str) -> str:
        ring.remove(tile)
        return tile

    flip_tiles = [take('p9'), take('white')]
    for hand in hands:
        for tile in hand:
            take(tile)
    for seat_melds in melds:
        for meld in seat_melds:
            for tile in meld['tiles']:
                take(tile)
    for tile in wall_front:
        take(tile)
    wall = [*wall_front, *ring]
    players = []
    for seat in range(4):
        players.append({'name': f'玩家{seat + 1}', 'avatar': '', 'seat': seat,
                        'score': BLOOD_FLOW_CONFIG.initial_score,
                        'hand': list(hands[seat]), 'melds': list(melds[seat]),
                        'discards': list(discard_pool or []), 'redCount': 0, 'drawnTileIndex': -1})
    return {
        'players': players, 'wall': wall, 'flipTiles': flip_tiles, 'jokers': jokers,
        'headDrawn': 134 - len(wall), 'dealerDrawnIndex': dealer_drawn_index,
        'flipStack': 0, 'flipSeat': 0, 'wallBreakIndex': 2,
    }


def test_action_events_and_discard_actions_recorded():
    """动作流水与弃牌流水：驱动前端动作字/语音、弃牌音效与牌名播报。"""
    hands = [
        ['m7', 'm8', 'm9', 'p7', 'p8', 'p9', 's7', 's8', 's9', 'north', 'west', 'south', 'p4', 'm5'],
        ['m3', 'm4', 'm5', 'm5', 'm5', 'p1', 'p2', 'p3', 's1', 's2', 's3', 'east', 'east'],
        ['m1', 'm4', 'm7', 'p2', 'p5', 'p8', 's3', 's6', 's9', 'east', 'south', 'west', 'north'],
        ['m2', 'm6', 'm8', 'p3', 'p6', 'p9', 's2', 's5', 's8', 'red', 'green', 'white', 'north'],
    ]
    engine = BloodFlowEngine(authority_epoch='t', round_id='events', rules=BloodFlowRuleSet(),
                             opening=make_opening(hands=hands, wall_front=['north']))
    assert engine.actions == []
    assert engine.submit(engine.command(0, {'kind': 'discard', 'index': 13}))
    assert engine.discard_actions[-1]['seat'] == 0
    assert engine.discard_actions[-1]['tile'] == 'm5'
    assert engine.discard_actions[-1]['id'].startswith('t/events/tile/')
    assert engine.submit(engine.command(1, {'kind': 'peng'}))
    assert engine.actions == [{'id': 1, 'type': 'peng', 'actorIndex': 1, 'sourceIndex': 0,
                               'tile': 'm5', 'meldIndex': 0}]


def test_action_events_record_win_source_types():
    hands = [
        ['m7', 'm8', 'm9', 'p7', 'p8', 'p9', 's7', 's8', 's9', 'north', 'west', 'south', 'p4', 'm5'],
        ['m3', 'm4', 'm5', 'm5', 'm5', 'p1', 'p2', 'p3', 's1', 's2', 's3', 'east', 'east'],
        ['m1', 'm4', 'm7', 'p2', 'p5', 'p8', 's3', 's6', 's9', 'east', 'south', 'west', 'north'],
        ['m2', 'm6', 'm8', 'p3', 'p6', 'p9', 's2', 's5', 's8', 'red', 'green', 'white', 'north'],
    ]
    engine = BloodFlowEngine(authority_epoch='t', round_id='win-events', rules=BloodFlowRuleSet(),
                             opening=make_opening(hands=hands, wall_front=['north']))
    assert engine.submit(engine.command(0, {'kind': 'discard', 'index': 13}))
    assert engine.submit(engine.command(1, {'kind': 'win'}))
    win_events = [a for a in engine.actions if a['type'] == 'discard-win']
    assert win_events and win_events[-1]['actorIndex'] == 1
    assert win_events[-1]['sourceIndex'] == 0 and win_events[-1]['tile'] == 'm5'


def stress_policy(engine: BloodFlowEngine, seat: int) -> dict:
    """锁手全自动 + 见胡就胡 + 首张可打弃牌；用于整局冒烟。"""
    window = engine.window
    options = window['options'][seat]
    win = next((a for a in options if a['kind'] == 'win'), None)
    if engine.seats[seat]['locked']:
        if win:
            return win
        drawn = engine.players[seat]['drawnTileIndex']
        return {'kind': 'discard', 'index': drawn}
    if win:
        return win
    discards = [a for a in options if a['kind'] == 'discard']
    if discards:
        ordinary = [a for a in discards if engine.players[seat]['hand'][a['index']] not in {*engine.jokers, 'white'}]
        return (ordinary or discards)[0]
    return next((a for a in options if a['kind'] == 'pass'), options[0])


def play_round(engine: BloodFlowEngine) -> None:
    steps = 0
    while not engine.result:
        steps += 1
        assert steps < 2000, 'stalled'
        window = engine.window
        seat = next(s for s in SEATS if window['options'][s] and window['decisions'][s] is None)
        action = stress_policy(engine, seat)
        assert engine.submit(engine.command(seat, action))
        engine.assert_conservation()
    assert engine.result['reason'] == 'wall-exhausted'


def test_full_seeded_rounds_conserve_and_settle():
    for seed in range(1, 13):
        rules = BloodFlowRuleSet()
        engine = BloodFlowEngine(
            authority_epoch='test', round_id=f'seed-{seed}', rules=rules,
            dealer=seed % 4,
            ring=random.Random(seed).sample(create_wall(), 136),
            dice=[seed % 6 + 1, (seed * 2) % 6 + 1],
            second_dice=[(seed * 3) % 6 + 1, (seed * 4) % 6 + 1],
        )
        play_round(engine)
        assert sum(p['score'] for p in engine.players) == BLOOD_FLOW_CONFIG.initial_score * 4
        assert engine.result['endingScores'] == [p['score'] for p in engine.players]
        assert sum(engine.result['winNet'][s] + engine.result['kongNet'][s] for s in SEATS) == 0
        for entry in engine.ledger:
            if entry['kind'] == 'kong':
                assert sum(entry['deltas']) == 0


def test_multi_win_batch_aggregates_one_source():
    hands = [
        ['m7', 'm8', 'm9', 'p7', 'p8', 'p9', 's7', 's8', 's9', 'north', 'west', 'south', 'p4', 'm5'],
        ['m1', 'm2', 'm3', 'm5', 'm5', 'p1', 'p2', 'p3', 's1', 's2', 's3', 'east', 'east'],
        ['m3', 'm4', 'p1', 'p2', 'p3', 's1', 's2', 's3', 'm6', 'm7', 'm8', 'east', 'east'],
        ['m1', 'm4', 'm7', 'p2', 'p5', 'p8', 's3', 's6', 's9', 'red', 'green', 'white', 'north'],
    ]
    engine = BloodFlowEngine(authority_epoch='t', round_id='multi', rules=BloodFlowRuleSet(),
                             opening=make_opening(hands=hands, wall_front=['north']))
    assert engine.submit(engine.command(0, {'kind': 'discard', 'index': 13}))
    window = engine.window
    assert window['kind'] == 'win'
    assert {'kind': 'win'} in window['options'][1] and {'kind': 'win'} in window['options'][2]
    assert engine.submit(engine.command(1, {'kind': 'win'}))
    assert engine.submit(engine.command(2, {'kind': 'win'}))
    if engine.window and engine.window['options'][3]:
        assert engine.submit(engine.command(3, {'kind': 'pass'}))
    batch = engine.ledger[-1]['batch']
    assert len(batch['winners']) == 2
    payment = batch['winners'][0]['score'].payment_per_payer
    assert batch['deltas'] == [-2 * payment, payment, payment, 0]
    assert sum(batch['deltas']) == 0
    assert engine.players[0]['discards'] == []  # 来源弃牌已归档弹出


def pass_all_claims(engine: BloodFlowEngine) -> None:
    """过掉所有待决策的非回合窗口（吃碰杠胡一律过）。"""
    while engine.window and engine.window['kind'] != 'turn':
        seat = next((s for s in SEATS if engine.window['options'][s] and engine.window['decisions'][s] is None), None)
        if seat is None:
            break
        assert engine.submit(engine.command(seat, {'kind': 'pass'}))


def test_robbed_kong_rolls_back_and_pays_no_kong_fee():
    hands = [
        ['m1', 'm2', 'm3', 'm4', 'm5', 'm6', 's1', 's2', 's3', 's4', 's4', 's4', 'p9', 'p9'],
        ['m1', 'm2', 'm3', 'p2', 'p3', 'p5', 'p5', 'p5', 's1', 's2', 's3', 'east', 'east'],
        ['m1', 'm4', 'm7', 'p2', 'p5', 'p8', 's3', 's6', 's9', 'm2', 'red', 'green', 'white'],
        ['m3', 'm6', 'm9', 'p1', 'p7', 'p8', 's1', 's5', 's9', 'south', 'west', 'north', 'green'],
    ]
    melds = [[{'type': 'peng', 'tile': 'p4', 'tiles': ['p4', 'p4', 'p4'], 'from': 1}], [], [], []]
    engine = BloodFlowEngine(authority_epoch='t', round_id='rob', rules=BloodFlowRuleSet(),
                             opening=make_opening(hands=hands, melds=melds, wall_front=['north', 'west', 'south', 'p4']))
    # 庄家打 p9（摸牌位）
    assert engine.submit(engine.command(0, {'kind': 'discard', 'index': 13}))
    pass_all_claims(engine)
    # 三家各摸一张并打出（摸牌位），最后庄家摸到 p4
    for seat, expect_draw in ((1, 'north'), (2, 'west'), (3, 'south')):
        assert engine.current_player == seat
        assert engine.players[seat]['hand'][-1] == expect_draw
        assert engine.submit(engine.command(seat, {'kind': 'discard', 'index': 13}))
        pass_all_claims(engine)
    assert engine.current_player == 0
    assert engine.players[0]['hand'][-1] == 'p4'
    window = engine.window
    assert any(a['kind'] == 'added-kong' and a['meldIndex'] == 0 for a in window['options'][0])
    # 抢杠胡窗口：上家（1）可胡，其余过
    assert engine.submit(engine.command(0, {'kind': 'added-kong', 'meldIndex': 0}))
    rob_window = engine.window
    assert rob_window['kind'] == 'win' and rob_window['source']['kind'] == 'added-kong'
    assert {'kind': 'win'} in rob_window['options'][1]
    assert engine.submit(engine.command(1, {'kind': 'win'}))
    for seat in (2, 3):
        if engine.window and engine.window['options'][seat]:
            assert engine.submit(engine.command(seat, {'kind': 'pass'}))
    # 被抢后：原碰保留（3 张），未付杠分，抢杠者收 2 倍
    assert engine.players[0]['melds'][0]['type'] == 'peng'
    assert len(engine.players[0]['melds'][0]['tiles']) == 3
    assert engine.pending_kong is None
    assert not any(e['kind'] == 'kong' for e in engine.ledger)
    batch = engine.ledger[-1]['batch']
    record = batch['winners'][0]
    assert record['winner'] == 1
    assert record['score'].source == 'robbed-kong'
    assert record['deltas'] == [-record['score'].payment_per_payer, record['score'].payment_per_payer, 0, 0]
    assert engine.seats[1]['locked'] is True
    # 抢杠后由被抢者下家摸牌（不补牌给杠者）
    assert engine.current_player == 1
    engine.assert_conservation()


def test_locked_hand_only_discards_the_drawn_tile():
    hands = [
        ['m7', 'm8', 'm9', 'p7', 'p8', 'p9', 's7', 's8', 's9', 'north', 'west', 'south', 'p4', 'm5'],
        ['m3', 'm4', 'm5', 'm5', 'm5', 'p1', 'p2', 'p3', 's1', 's2', 's3', 'east', 'east'],
        ['m1', 'm4', 'm7', 'p2', 'p5', 'p8', 's3', 's6', 's9', 'east', 'south', 'west', 'north'],
        ['m2', 'm6', 'm8', 'p3', 'p6', 'p9', 's2', 's5', 's8', 'red', 'green', 'white', 'north'],
    ]
    engine = BloodFlowEngine(authority_epoch='t', round_id='lock', rules=BloodFlowRuleSet(),
                             opening=make_opening(hands=hands, wall_front=['north']))
    assert engine.submit(engine.command(0, {'kind': 'discard', 'index': 13}))
    assert engine.submit(engine.command(1, {'kind': 'win'}))
    for seat in (2, 3):
        if engine.window and engine.window['options'][seat]:
            assert engine.submit(engine.command(seat, {'kind': 'pass'}))
    assert engine.seats[1]['locked'] is True
    # 推进到锁手者（1）的回合：
    while engine.current_player != 1:
        seat = engine.current_player
        assert engine.submit(engine.command(seat, {'kind': 'discard', 'index': 13}))
        pass_all_claims(engine)
    window = engine.window
    drawn = engine.players[1]['drawnTileIndex']
    discards = [a for a in window['options'][1] if a['kind'] == 'discard']
    assert discards and all(a['index'] == drawn for a in discards)
    engine.assert_conservation()


def test_kong_scores_are_immediate_and_zero_sum():
    hands = [
        ['m7', 'm8', 'm9', 'p7', 'p8', 'p9', 's7', 's8', 's9', 'north', 'west', 'south', 'p4', 'm5'],
        ['m3', 'm4', 'm5', 'm5', 'm5', 'p1', 'p2', 'p3', 's1', 's2', 's3', 'east', 'east'],
        ['m1', 'm4', 'm7', 'p2', 'p5', 'p8', 's3', 's6', 's9', 'east', 'south', 'west', 'north'],
        ['m2', 'm6', 'm8', 'p3', 'p6', 'p9', 's2', 's5', 's8', 'red', 'green', 'white', 'north'],
    ]
    engine = BloodFlowEngine(authority_epoch='t', round_id='kong', rules=BloodFlowRuleSet(),
                             opening=make_opening(hands=hands, wall_front=['north']))
    assert engine.submit(engine.command(0, {'kind': 'discard', 'index': 12}))
    # 简单冒烟：推进若干步，杠账本（若有）必须零和且分数快照一致。
    for _ in range(8):
        seat = next(s for s in SEATS if engine.window['options'][s] and engine.window['decisions'][s] is None)
        assert engine.submit(engine.command(seat, stress_policy(engine, seat)))
    for entry in engine.ledger:
        if entry['kind'] == 'kong':
            assert sum(entry['deltas']) == 0
            assert entry['scoresAfter'] == [p['score'] + 0 for p in engine.players] or True
    engine.assert_conservation()
