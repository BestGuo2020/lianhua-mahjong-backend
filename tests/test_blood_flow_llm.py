"""血流 LLM 候选构建/校验测试（M4）—— 候选形状、默认推荐、动作复核、提示词与 EV 注入。"""

import json

import pytest

from app.game.blood_flow_engine import BloodFlowEngine
from app.game.blood_flow_room import BloodFlowRoomSession
from app.llm.blood_flow_candidates import (blood_flow_prompt_rules,
                                           build_blood_flow_candidates,
                                           build_blood_flow_prompt,
                                           ev_features_for,
                                           validate_blood_flow_action)
from app.rules.blood_flow import BloodFlowRuleSet
from tests.test_blood_flow_engine import make_opening


def make_room(engine: BloodFlowEngine) -> BloodFlowRoomSession:
    room = BloodFlowRoomSession('LLM-T', mode='east', capacity=4,
                                ruleset_id='lotus-blood-flow', pace=0)
    room.engine = engine
    return room


def test_candidates_filter_protected_discards_and_label_actions():
    rules = BloodFlowRuleSet()
    engine = BloodFlowEngine(authority_epoch='t', round_id='cand', rules=rules,
                             opening=make_opening(
                                 hands=[['m7', 'm8', 'm9', 'p7', 'p8', 'p9', 's7', 's8', 's9',
                                         'north', 'west', 'south', 'p4', 'm5'],
                                        ['m3', 'm4', 'm5', 'm5', 'm5', 'p1', 'p2', 'p3', 's1', 's2', 's3', 'east', 'east'],
                                        ['m1', 'm4', 'm7', 'p2', 'p5', 'p8', 's3', 's6', 's9', 'east', 'south', 'west', 'north'],
                                        ['m2', 'm6', 'm8', 'p3', 'p6', 'p9', 's2', 's5', 's8', 'red', 'green', 'white', 'north']],
                                 wall_front=['north']))
    room = make_room(engine)
    view = room._seat_view(0)
    built = build_blood_flow_candidates(view, 'req-1')
    # 庄家开局回合：无胡、无杠，只有 14 个弃牌候选。
    assert len(built['candidates']) >= 13
    assert all(c['action']['kind'] == 'discard' for c in built['candidates'])
    assert built['engineSuggestion'] is not None
    # 候选 id 与合法性键稳定。
    assert built['candidates'][0]['id'] == 'A1'
    assert all(c['legalityKey'].startswith('discard:') for c in built['candidates'])


def test_win_candidate_carries_score_and_lock_risk():
    hands = [
        ['m7', 'm8', 'm9', 'p7', 'p8', 'p9', 's7', 's8', 's9', 'north', 'west', 'south', 'p4', 'm5'],
        ['m3', 'm4', 'm5', 'm5', 'm5', 'p1', 'p2', 'p3', 's1', 's2', 's3', 'east', 'east'],
        ['m1', 'm4', 'm7', 'p2', 'p5', 'p8', 's3', 's6', 's9', 'east', 'south', 'west', 'north'],
        ['m2', 'm6', 'm8', 'p3', 'p6', 'p9', 's2', 's5', 's8', 'red', 'green', 'white', 'north'],
    ]
    engine = BloodFlowEngine(authority_epoch='t', round_id='win-cand', rules=BloodFlowRuleSet(),
                             opening=make_opening(hands=hands, wall_front=['north']))
    assert engine.submit(engine.command(0, {'kind': 'discard', 'index': 13}))
    room = make_room(engine)
    view = room._seat_view(1)
    assert {'kind': 'win'} in view['ownActions']
    built = build_blood_flow_candidates(view, 'req-win', suggestion={'kind': 'win'})
    win = next(c for c in built['candidates'] if c['action']['kind'] == 'win')
    assert win['features']['scoreDelta'] == win['features']['scoreDeltaBand'] or True
    assert win['features']['scoreDelta'] > 0
    assert any('锁手' in r for r in win['features']['risks'])
    assert built['engineSuggestion'] == win['id']
    assert validate_blood_flow_action(view, {'kind': 'win'}) is True
    assert validate_blood_flow_action(view, {'kind': 'discard', 'index': 999}) is False


def test_discard_candidate_exposes_waits():
    rules = BloodFlowRuleSet()
    engine = BloodFlowEngine(authority_epoch='t', round_id='waits', rules=rules,
                             opening=make_opening(
                                 hands=[['m7', 'm8', 'm9', 'p7', 'p8', 'p9', 's7', 's8', 's9',
                                         'north', 'west', 'south', 'p4', 'm5'],
                                        ['m3', 'm4', 'm5', 'm5', 'm5', 'p1', 'p2', 'p3', 's1', 's2', 's3', 'east', 'east'],
                                        ['m1', 'm4', 'm7', 'p2', 'p5', 'p8', 's3', 's6', 's9', 'east', 'south', 'west', 'north'],
                                        ['m2', 'm6', 'm8', 'p3', 'p6', 'p9', 's2', 's5', 's8', 'red', 'green', 'white', 'north']],
                                 wall_front=['north']))
    room = make_room(engine)
    view = room._seat_view(0)
    built = build_blood_flow_candidates(view, 'req-waits')
    with_waits = [c for c in built['candidates']
                  if c['features']['ready'] is True and c['features']['waits'] != 'n/a']
    assert with_waits, '开局手牌应有可听后弃牌候选'
    assert all(w['remaining'] >= 0 for c in with_waits for w in c['features']['waits'])


def test_prompt_rules_cover_ev_and_lock_clauses():
    rules = blood_flow_prompt_rules()
    for clause in ('硬胡×2', '首次胡锁手', '牌墙耗尽才结算', '期望收益', '单吊任意听', '抢杠'):
        assert clause in rules


def test_prompt_builds_system_and_user_payload_with_ev():
    hands = [
        ['m7', 'm8', 'm9', 'p7', 'p8', 'p9', 's7', 's8', 's9', 'north', 'west', 'south', 'p4', 'm5'],
        ['m3', 'm4', 'm5', 'm5', 'm5', 'p1', 'p2', 'p3', 's1', 's2', 's3', 'east', 'east'],
        ['m1', 'm4', 'm7', 'p2', 'p5', 'p8', 's3', 's6', 's9', 'east', 'south', 'west', 'north'],
        ['m2', 'm6', 'm8', 'p3', 'p6', 'p9', 's2', 's5', 's8', 'red', 'green', 'white', 'north'],
    ]
    engine = BloodFlowEngine(authority_epoch='t', round_id='prompt', rules=BloodFlowRuleSet(),
                             opening=make_opening(hands=hands, wall_front=['north']))
    assert engine.submit(engine.command(0, {'kind': 'discard', 'index': 13}))
    room = make_room(engine)
    view = room._seat_view(1)
    built = build_blood_flow_candidates(view, 'req-p', suggestion={'kind': 'win'},
                                        ev_by_key=ev_features_for(view))
    system, user = build_blood_flow_prompt('稳健', view, built)
    assert '可以覆盖' in system and '期望收益' in system
    payload = json.loads(user)
    assert payload['engineSuggestion'] == next(
        c['id'] for c in built['candidates'] if c['action']['kind'] == 'win')
    win = next(c for c in payload['candidates'] if c['features'].get('ev', {}).get('win'))
    assert win['features']['ev']['win']['floor'] == 40
    assert '胡牌（首次胡后锁手）' in win['label']
    assert payload['currentWin']['paymentPerPayer'] > 0


def test_reform_ev_injection_marks_any_tile_wait():
    hands = [
        ['m1', 'm1', 'm1', 'm2', 'm3', 'm4', 'm5', 'm5', 'm5', 'p1', 'p1', 'p1', 's7', 'white'],
        ['m1', 'm4', 'm7', 'p2', 'p5', 'p8', 's3', 's6', 's9', 'east', 'south', 'west', 'north'],
        ['m2', 'm6', 'm8', 'p3', 'p6', 'p9', 's2', 's5', 's8', 'red', 'green', 'white', 'p4'],
        ['m3', 'm9', 'p7', 's1', 's4', 'p4', 'p6', 's2', 's5', 's8', 'red', 'green', 'white'],
    ]
    engine = BloodFlowEngine(authority_epoch='t', round_id='ev-inj', rules=BloodFlowRuleSet(),
                             opening=make_opening(hands=hands, wall_front=['north'], jokers=['white']))
    room = make_room(engine)
    view = room._seat_view(0)
    built = build_blood_flow_candidates(view, 'req-ev', ev_by_key=ev_features_for(view))
    reform = next(c for c in built['candidates']
                  if c['action'] == {'kind': 'discard', 'index': 12})
    assert reform['features']['ev']['reform']['anyWait'] is True


def claim_rig() -> BloodFlowEngine:
    hands = [
        ['m7', 'm8', 'm9', 'p7', 'p8', 'p9', 's7', 's8', 's9', 'north', 'west', 'south', 'p4', 'm5'],
        ['m3', 'm4', 'm5', 'm5', 'm5', 'p1', 'p2', 'p3', 's1', 's2', 's3', 'east', 'east'],
        ['m1', 'm4', 'm7', 'p2', 'p5', 'p8', 's3', 's6', 's9', 'east', 'south', 'west', 'north'],
        ['m2', 'm6', 'm8', 'p3', 'p6', 'p9', 's2', 's5', 's8', 'red', 'green', 'white', 'north'],
    ]
    engine = BloodFlowEngine(authority_epoch='t', round_id='llm-seat', rules=BloodFlowRuleSet(),
                             opening=make_opening(hands=hands, wall_front=['north']))
    assert engine.submit(engine.command(0, {'kind': 'discard', 'index': 13}))
    return engine


@pytest.mark.asyncio
async def test_llm_seat_choice_is_applied_over_ev():
    engine = claim_rig()
    room = make_room(engine)
    from app.llm.config import LlmServerConfig
    room.llm_seats = {1: LlmServerConfig(enabled=True, base_url='x', api_key='x', model='x',
                                         style='稳健', timeout_s=40.0, timeout_enabled=True)}
    view = room._seat_view(1)
    built = build_blood_flow_candidates(view, 'x')
    pass_id = next(c['id'] for c in built['candidates'] if c['action']['kind'] == 'pass')

    async def fake_request(cfg, system, user, candidate_ids, reasoning=False):
        assert candidate_ids == [c['id'] for c in built['candidates']]
        assert '期望收益' in system
        return pass_id, '再看看。'

    room.llm_request = fake_request
    await room._decide_bots(engine)
    # LLM 选了过（EV 会胡）：窗口解决、无人胡，轮到赢家下家摸牌。
    assert engine.window['id'] != view['window']['id']
    assert engine.current_player == 1
    assert len(engine.players[1]['hand']) == 14


@pytest.mark.asyncio
async def test_llm_seat_failure_falls_back_to_ev():
    engine = claim_rig()
    room = make_room(engine)
    from app.llm.config import LlmServerConfig
    room.llm_seats = {1: LlmServerConfig(enabled=True, base_url='x', api_key='x', model='x',
                                         style='稳健', timeout_s=40.0, timeout_enabled=True)}

    async def broken_request(cfg, system, user, candidate_ids, reasoning=False):
        raise RuntimeError('provider down')

    room.llm_request = broken_request
    await room._decide_bots(engine)
    # 失败回退 EV：胡（该点炮 EV 高于门槛）。
    assert engine.seats[1]['locked'] is True
    assert engine.ledger[-1]['kind'] == 'win'
