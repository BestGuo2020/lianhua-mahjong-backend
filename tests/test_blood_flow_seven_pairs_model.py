"""七对潜力模型 v2 后端镜像测试 —— 对齐前端 sevenPairsModel.test.ts。

规则/口径（2026-09-13 用户定案，引擎实测见 tmp/luxury-seven-pairs-analysis.test.ts）：
  · 精牌能把刻子/对子补成四张 → 豪华七对不必等第四张（3 张实体 + 1 精即成立）；
  · 旧口径 `pairs + min(singles, jokers)` 会丢掉多余精牌，导致"精越多、豪华七对越确定、估值反而越低"；
  · 新口径按引擎 is_seven_pairs 记账（剩余精牌两两成对），并新增豪华七对（12 番）方向。

最后一段是**跨语言护栏**：潜力合计与豪华七对进度必须与前端逐位一致。
"""

import pytest

from app.core.blood_flow.ai import (_luxury_seven_pairs_progress, _quad_availability,
                                    _seven_pairs_account, _seven_pairs_potential,
                                    _seven_pairs_progress, estimate_win_income,
                                    pattern_potential_total, pattern_potentials)
from app.core.blood_flow.config import BLOOD_FLOW_CONFIG

JOKERS = ['red']
WILD = ['red', 'white']

# H1：实体刻子 + 5 对（手上无精）——引擎实测听 m4 与翻精，两者都是豪华七对。
H1 = ['m4', 'm4', 'm4', 'm3', 'm3', 'm5', 'm5', 's5', 's5', 'p6', 'p6', 'east', 'east']
# H2：实体四张 + 4 对 + 单张。
H2 = ['m4', 'm4', 'm4', 'm4', 'm3', 'm3', 's5', 's5', 'p6', 'p6', 'east', 'east', 'south']
# H3：刻子 + 3 对 + 2 精 + 2 散（用户例子型）。
H3 = ['m3', 'm3', 'm4', 'm4', 'm4', 's5', 's5', 'p6', 'p6', 'red', 'red', 'm7', 'm9']
# H4：5 对 + 3 精 —— 引擎实测 34 种全听（29 种豪华七对 + 5 种断幺九+四暗刻）。
H4 = ['m3', 'm3', 's5', 's5', 'p6', 'p6', 'm7', 'm7', 'p8', 'p8', 'red', 'red', 'red']
# H5：刻子 + 4 对 + 1 精。
H5 = ['m4', 'm4', 'm4', 'm3', 'm3', 's5', 's5', 'p6', 'p6', 'm7', 'm7', 'east', 'red']
# H6：6 对 + 1 散张（真·普通七对，对照）。
H6 = ['m3', 'm3', 's5', 's5', 'p6', 'p6', 'm7', 'm7', 'p8', 'p8', 's9', 's9', 'east']


def direction(hand, pattern_id, model):
    return next((d for d in pattern_potentials(hand, [], JOKERS, model) if d['id'] == pattern_id), None)


def test_account_matches_engine_pairs_and_singles():
    account = _seven_pairs_account(H1, WILD)
    assert account['pairs'] == 6  # 5 对 + 刻子算 1 对
    assert account['singles'] == 1  # 刻子还留 1 张单（不是白拿一对）
    assert account['jokers'] == 0
    assert account['effectivePairs'] == 6


def test_surplus_jokers_are_counted():
    account = _seven_pairs_account(H4, WILD)
    assert (account['pairs'], account['singles'], account['jokers']) == (5, 0, 3)
    assert account['effectivePairs'] == 6  # 两张精自成一对，剩 1 张等补单张
    assert _seven_pairs_potential(H4, WILD) == 20  # 旧口径只有 5/7
    assert _seven_pairs_progress(H4, WILD) == pytest.approx(6 / 7, abs=1e-9)


def test_quad_availability_ladder():
    assert _quad_availability(['m4', 'm4', 'm4', 'm3', 'm3', 's5', 's5', 'p6', 'p6', 'red'], WILD) == 1
    assert _quad_availability(H4, WILD) == 1
    assert _quad_availability(H1, WILD) == pytest.approx(0.6, abs=1e-9)
    assert _quad_availability(H6, WILD) == pytest.approx(0.2, abs=1e-9)


def test_off_model_has_no_luxury_direction_and_matches_legacy():
    for hand in (H1, H4, H6):
        directions = pattern_potentials(hand, [], JOKERS, 'off')
        assert not any(d['id'] == 'luxury-seven-pairs' for d in directions)
        seven = direction(hand, 'sevenPairs', 'off')
        assert seven['progress'] == pytest.approx(min(1.0, _seven_pairs_potential(hand, WILD) / 28), abs=1e-9)


def test_luxury_direction_outweighs_plain_seven_pairs():
    luxury = direction(H1, 'luxury-seven-pairs', 'ev')
    seven = direction(H1, 'sevenPairs', 'ev')
    assert luxury['weight'] == BLOOD_FLOW_CONFIG.patterns['luxury-seven-pairs'].weight
    assert luxury['progress'] == pytest.approx(_seven_pairs_progress(H1, WILD) * 0.6, abs=1e-9)
    assert luxury['score'] > seven['score']


def test_model_raises_valuation_for_joker_rich_pairs_hand():
    assert pattern_potential_total(H4, [], JOKERS, 'ev') > pattern_potential_total(H4, [], JOKERS, 'off')
    assert direction(H4, 'luxury-seven-pairs', 'ev')['progress'] == pytest.approx(6 / 7, abs=1e-9)


def test_plain_seven_pairs_gets_only_a_tiny_luxury_direction():
    luxury = direction(H6, 'luxury-seven-pairs', 'ev')
    assert luxury['progress'] < 0.25
    assert luxury['score'] < 1


def test_win_income_prices_luxury_seven_pairs():
    luxury_win = ['m4', 'm4', 'm4', 'red', 'm3', 'm3', 's5', 's5', 'p6', 'p6', 'm7', 'm7', 'east', 'east']
    plain_win = ['m3', 'm3', 's5', 's5', 'p6', 'p6', 'm7', 'm7', 'p8', 'p8', 's9', 's9', 'north', 'red']
    ev = estimate_win_income(luxury_win, [], JOKERS, 'self-draw', 'ev')
    off = estimate_win_income(luxury_win, [], JOKERS, 'self-draw', 'off')
    assert ev['multiplier'] == (BLOOD_FLOW_CONFIG.patterns['luxury-seven-pairs'].weight
                                * BLOOD_FLOW_CONFIG.event_multipliers['self-draw'])
    assert off['multiplier'] == (BLOOD_FLOW_CONFIG.patterns['sevenPairs'].weight
                                 * BLOOD_FLOW_CONFIG.event_multipliers['self-draw'])
    assert estimate_win_income(plain_win, [], JOKERS, 'self-draw', 'ev') == \
        estimate_win_income(plain_win, [], JOKERS, 'self-draw', 'off')


def test_luxury_progress_is_product_of_both_halves():
    assert _luxury_seven_pairs_progress(H1, WILD) == pytest.approx(
        _seven_pairs_progress(H1, WILD) * _quad_availability(H1, WILD), abs=1e-9)


@pytest.mark.parametrize('hand,off_total,ev_total,luxury_progress', [
    (H1, 11.523, 14.696, 0.514),
    (H2, 11.523, 20.339, 0.857),
    (H3, 58.036, 66.852, 0.857),
    (H4, 92.798, 102.512, 0.857),
    (H5, 23.109, 31.925, 0.857),
    (H6, 7.530, 7.883, 0.171),
])
def test_cross_language_guard(hand, off_total, ev_total, luxury_progress):
    """与前端的潜力合计/豪华七对进度逐位一致（tmp/luxury-seven-pairs-analysis.test.ts 打印同一组数字）。"""
    assert pattern_potential_total(hand, [], JOKERS, 'off') == pytest.approx(off_total, abs=0.001)
    assert pattern_potential_total(hand, [], JOKERS, 'ev') == pytest.approx(ev_total, abs=0.001)
    assert direction(hand, 'luxury-seven-pairs', 'ev')['progress'] == pytest.approx(luxury_progress, abs=0.001)
