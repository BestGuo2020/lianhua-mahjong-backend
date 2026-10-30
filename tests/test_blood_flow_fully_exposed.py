import json
import os
from pathlib import Path

import pytest

from app.core.blood_flow.ai import _certain_patterns, pattern_potentials
from app.core.blood_flow.evaluate import evaluate_waits, win_input_from_dict
from app.core.blood_flow.pattern_reachability import can_develop_pattern_with_melds
from app.llm.blood_flow_candidates import blood_flow_prompt_rules

FIXTURES = Path(os.environ.get('BLOOD_FLOW_FIXTURES',
    Path(__file__).absolute().parents[2] / 'src/game/variants/lotus/patterns/fixtures'))
CASES = {case['id']: case['input'] for case in json.loads(
    (FIXTURES / 'fullyExposedHand.json').read_text(encoding='utf-8'))['cases']}
ID = 'fully-exposed-hand'


def test_all_joker_self_draw_waits_score_twelve_but_never_allow_discard():
    waits = evaluate_waits(win_input_from_dict(CASES['twelve-joker-self-draw']))
    assert len(waits) == 34
    for wait in waits:
        assert wait['discard'] is None
        assert any(item.id == ID and item.label == '十二落抬' and item.weight == 2
                   for item in wait['selfDraw'].items)
    rules = blood_flow_prompt_rules()
    assert '十二落抬（2番' in rules
    assert '含暗杠或风杠不算' in rules


@pytest.mark.parametrize('model', ['off', 'ev'])
def test_ai_includes_completed_bonus(model):
    case = CASES['twelve-joker-self-draw']
    assert {'id': ID, 'weight': 2, 'progress': 1, 'score': 2} in pattern_potentials(
        case['concealed'], case['melds'], case['jokers'], model)
    assert ID in _certain_patterns([*case['concealed'], case['winningTile']],
                                  case['melds'], case['jokers'], model)


@pytest.mark.parametrize('name', ['twelve-concealed-kong-excluded',
    'twelve-wind-kong-excluded', 'twelve-pending-added-kong-not-counted'])
def test_ai_excludes_impossible_melds(name):
    case = CASES[name]
    assert not can_develop_pattern_with_melds(ID, case['melds'])
    assert ID not in {p['id'] for p in pattern_potentials(
        case['concealed'], case['melds'], case['jokers'], 'ev')}
    assert ID not in _certain_patterns([*case['concealed'], case['winningTile']],
                                      case['melds'], case['jokers'], 'ev')
