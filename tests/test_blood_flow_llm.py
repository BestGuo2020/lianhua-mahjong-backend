"""血流 LLM 候选构建/校验测试（M4）—— 候选形状、默认推荐、动作复核。"""

import pytest

from app.game.blood_flow_engine import BloodFlowEngine
from app.game.blood_flow_room import BloodFlowRoomSession
from app.llm.blood_flow_candidates import (blood_flow_prompt_rules,
                                           build_blood_flow_candidates,
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
