"""第二版番种表（2026-09-12）新增牌型的正反例 + 杠加成 —— 逐条对应前端
`src/game/variants/lotus/patterns/midTierPatterns.test.ts`。

共享夹具 golden.json 只覆盖旧 16 番种（前端也把新番种挪到该 TS 测试里断言），因此 Python 侧
在这里做同样的正反例镜像：同一批输入必须给出同一批 items / 同一个 kong_bonus。
"""

from app.core.blood_flow.config import BLOOD_FLOW_CONFIG, KONG_BONUS
from app.core.blood_flow.evaluate import evaluate_win, win_input_from_dict


def _evaluate(concealed, winning_tile, source='discard', jokers=(), melds=()):
    return evaluate_win(win_input_from_dict({
        'concealed': list(concealed), 'melds': list(melds), 'winningTile': winning_tile,
        'source': source, 'jokers': list(jokers), 'opening': None,
    }), BLOOD_FLOW_CONFIG)


def _ids(concealed, winning_tile, source='discard', jokers=()):
    result = _evaluate(concealed, winning_tile, source, jokers)
    return [item.id for item in result.score.items] if result else None


def _kong(meld_type: str, tile: str) -> dict:
    return {'type': meld_type, 'tile': tile, 'tiles': [tile] * 4}


# ── 断幺九 / 全带幺 ──

def test_all_simples_positive_and_negative():
    # 234 567 + 234 567 + 88（万/筒混）→ 断幺九
    assert 'all-simples' in _ids(['m2', 'm3', 'm4', 'm5', 'm6', 'm7',
                                  'p2', 'p3', 'p4', 'p5', 'p6', 'p7', 'p8'], 'p8')
    # 带 1 万 → 不成立
    assert 'all-simples' not in _ids(['m1', 'm2', 'm3', 'm4', 'm5', 'm6',
                                      'p2', 'p3', 'p4', 'p5', 'p6', 'p7', 'p8'], 'p8')


def test_all_with_terminals_positive_and_negative():
    # 123 789 123 + 东东东 + 99（每副都带幺）
    assert 'all-with-terminals' in _ids(['m1', 'm2', 'm3', 'm7', 'm8', 'm9',
                                         'p1', 'p2', 'p3', 'east', 'east', 'east', 'p9'], 'p9')
    # 把一组换成 456 → 不成立
    assert 'all-with-terminals' not in _ids(['m1', 'm2', 'm3', 'm7', 'm8', 'm9',
                                             'p4', 'p5', 'p6', 'east', 'east', 'east', 'p9'], 'p9')


# ── 一色步高 / 清龙 ──

def test_one_suit_three_steps_positive_and_negative():
    # 123 234 345 + 碰碰式对子
    assert 'one-suit-three-steps' in _ids(['m1', 'm2', 'm3', 'm2', 'm3', 'm4', 'm3', 'm4', 'm5',
                                           's5', 's5', 's5', 's9'], 's9')
    # 123 345 567（起始 1/3/5，不连续）
    assert 'one-suit-three-steps' not in _ids(['m1', 'm2', 'm3', 'm3', 'm4', 'm5', 'm5', 'm6', 'm7',
                                               's5', 's5', 's5', 's9'], 's9')


def test_one_suit_four_steps_covers_three_steps():
    items = _ids(['m1', 'm2', 'm3', 'm2', 'm3', 'm4', 'm3', 'm4', 'm5', 'm4', 'm5', 'm6', 's9'], 's9')
    assert 'one-suit-four-steps' in items
    assert 'one-suit-three-steps' not in items


def test_pure_straight_positive():
    assert 'pure-straight' in _ids(['m1', 'm2', 'm3', 'm4', 'm5', 'm6', 'm7', 'm8', 'm9',
                                    's5', 's5', 's5', 's9'], 's9')


# ── 一色节高 ──

def test_one_suit_three_joints_positive_and_negative():
    assert 'one-suit-three-joints' in _ids(['m2', 'm2', 'm2', 'm3', 'm3', 'm3', 'm4', 'm4', 'm4',
                                            's5', 's5', 's5', 's9'], 's9')
    assert 'one-suit-three-joints' not in _ids(['m2', 'm2', 'm2', 'm4', 'm4', 'm4', 'm6', 'm6', 'm6',
                                                's5', 's5', 's5', 's9'], 's9')


def test_one_suit_four_joints_covers_three_joints_and_all_triplets():
    items = _ids(['m2', 'm2', 'm2', 'm3', 'm3', 'm3', 'm4', 'm4', 'm4', 'm5', 'm5', 'm5', 'm9'], 'm9')
    assert 'one-suit-four-joints' in items
    assert 'one-suit-three-joints' not in items
    assert 'all-triplets' not in items


# ── 门清（仅标准四面子一将型生效）──

def test_concealed_hand_standard_only():
    # 标准型无副露成立
    assert 'concealed-hand' in _ids(['m1', 'm2', 'm3', 'm4', 'm5', 'm6',
                                     'p2', 'p3', 'p4', 'p5', 'p6', 'p7', 'p8'], 'p8')
    # 七对虽是门清结构，但按“仅标准型生效”不计门清
    pairs = _ids(['m1', 'm1', 'm2', 'm2', 'm3', 'm3', 'p4', 'p4', 'p5', 'p5', 's6', 's6', 's7'], 's7')
    assert 'sevenPairs' in pairs
    assert 'concealed-hand' not in pairs
    # 十三烂同理
    scattered = _ids(['m1', 'm4', 'm7', 'p1', 'p4', 'p7', 's1', 's4', 's7',
                      'east', 'south', 'west', 'north'], 'red')
    assert 'concealed-hand' not in (scattered or [])


# ── 杠加成 ──

def test_kong_bonus_weights_and_pattern_double_counting():
    assert KONG_BONUS == {'exposed': 1, 'concealed': 2, 'wind': 2}
    assert BLOOD_FLOW_CONFIG.kong_bonus == {'exposed': 1, 'concealed': 2, 'wind': 2}
    # 无杠：基础倍率 = 1 + Σ(w-1)
    plain = _evaluate(['m2', 'm3', 'm4', 'm5', 'm6', 'm7',
                       'p2', 'p3', 'p4', 'p5', 'p6', 'p7', 'p8'], 'p8')
    assert plain.score.kong_bonus == 0
    assert plain.score.pattern_multiplier == 1 + sum(i.weight - 1 for i in plain.score.items)
    # 两个明杠 + 一个暗杠 → 同时成三杠番种 → 按「不重复计算」口径加成归零
    three = _evaluate(['m4', 'm5', 'm6', 'east'],
                      'east', melds=[_kong('gang', 'm1'), _kong('gang', 's3'), _kong('angang', 'p2')])
    assert 'three-kongs' in [i.id for i in three.score.items]
    assert three.score.kong_bonus == 0
    assert three.score.pattern_multiplier == 1 + sum(i.weight - 1 for i in three.score.items)
    # 只有两副杠（不成三杠/四杠番种）→ 加成照计：1(明) + 2(暗) = 3
    two = _evaluate(['m4', 'm5', 'm6', 'p7', 'p8', 'p9', 'east'],
                    'east', melds=[_kong('gang', 'm1'), _kong('angang', 'p2')])
    assert 'three-kongs' not in [i.id for i in two.score.items]
    assert two.score.kong_bonus == 3
    # 四杠：同样归零，且只覆盖三杠（可与碰碰胡叠加）
    four = _evaluate(['east'], 'east', melds=[_kong('gang', 'm1'), _kong('angang', 'p2'),
                                              _kong('gang', 's3'), _kong('gang', 'red')])
    four_ids = [i.id for i in four.score.items]
    assert 'four-kongs' in four_ids and 'all-triplets' in four_ids and 'three-kongs' not in four_ids
    assert four.score.kong_bonus == 0
    # 风杠按 +2（风杠 2 + 明杠 1 = 3）
    wind_meld = {'type': 'angang', 'tile': 'east',
                 'tiles': ['east', 'south', 'west', 'north'], 'windKong': True}
    wind = _evaluate(['m4', 'm5', 'm6', 'p7', 'p8', 'p9', 'east'], 'east',
                     melds=[wind_meld, _kong('gang', 'm1')])
    assert wind is not None
    assert wind.score.kong_bonus == 3


def test_short_kong_hand_is_not_a_win():
    """前端同一条守卫断言：该输入不构成和牌（13 张）→ 两端都必须返回 None。"""
    wind_meld = {'type': 'angang', 'tile': 'east',
                 'tiles': ['east', 'south', 'west', 'north'], 'windKong': True}
    assert _evaluate(['m4', 'm5', 'm6', 'east'], 'east',
                     melds=[wind_meld, _kong('gang', 'm1')]) is None