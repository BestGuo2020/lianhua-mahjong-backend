"""幺筒可胡形/听牌/过手限制测试。"""

from app.core.yaotong.hands import (
    context_allows,
    discard_waits,
    evaluate_hand,
    hu_opportunity,
    is_untouched_first_draw,
    pass_allows,
    shape_points,
    waits,
)
from app.core.yaotong.types import Discard, Player, WinContext

# 2m3m4m / 5m6m7m / 2p3p4p / 5p6p7p / 9s9s，不含鸡牌
NO_CHICKEN_HAND = [1, 2, 3, 4, 5, 6, 10, 11, 12, 13, 14, 15, 26, 26]
# 同型但把 9s9s 换成 1p1p（幺筒/鸡）
CHICKEN_HAND = [1, 2, 3, 4, 5, 6, 10, 11, 12, 13, 14, 15, 9, 9]


def player(hand, **kwargs):
    return Player(name='测试', hand=list(hand), **kwargs)


def test_is_untouched_first_draw():
    assert is_untouched_first_draw(player([], draws=1, drawn=1))
    assert not is_untouched_first_draw(player([], draws=2, drawn=1))
    assert not is_untouched_first_draw(player([], draws=1, drawn=None))
    assert not is_untouched_first_draw(player([], draws=1, discards=1, drawn=1))
    assert not is_untouched_first_draw(player([], draws=1, after_kong=True, drawn=1))


def test_standard_self_draw_has_plain_and_su_hu():
    values = evaluate_hand(player(NO_CHICKEN_HAND), WinContext(kind='self', tile=26))
    assert values
    assert max(value.h for value in values) == 25
    assert any(pattern.name == '素胡' for value in values for pattern in value.patterns)
    assert all(value.big is False for value in values)


def test_standard_discard_win_needs_big_or_kong_or_declaration():
    assert evaluate_hand(player(NO_CHICKEN_HAND), WinContext(kind='discard', tile=26)) == []

    ready = evaluate_hand(
        player(NO_CHICKEN_HAND, declaration='ready'),
        WinContext(kind='discard', tile=26),
    )
    assert ready
    assert any(pattern.name == '报听' for value in ready for pattern in value.patterns)
    assert max(value.h for value in ready) >= 35


def test_chicken_blocks_su_hu():
    values = evaluate_hand(player(CHICKEN_HAND), WinContext(kind='self', tile=9))
    assert values
    assert not any(pattern.name == '素胡' for value in values for pattern in value.patterns)
    assert max(value.h for value in values) == 15
    assert evaluate_hand(player(CHICKEN_HAND), WinContext(kind='discard', tile=9)) == []


def test_river_chicken_also_blocks_su_hu():
    """素胡＝什么都没有：手牌、副露和牌河都不含固定鸡、幺筒或翻牌鸡。"""
    def discard(tile, **kwargs):
        return Discard(tile=tile, chicken=0, stage='normal', id=1, **kwargs)

    def has_su_hu(river, chicken=None):
        values = evaluate_hand(
            player(NO_CHICKEN_HAND, river=list(river)),
            WinContext(kind='self', tile=26, chicken=chicken),
        )
        return any(pattern.name == '素胡' for value in values for pattern in value.patterns)

    assert has_su_hu([discard(7), discard(20)])
    # 固定鸡（一万/八筒/一条）、幺筒、翻牌鸡出现在牌河同样不能算素胡。
    for chicken_tile in (0, 16, 18, 9):
        assert not has_su_hu([discard(chicken_tile)])
    assert not has_su_hu([discard(20)], chicken=20)
    # 跟庄跟牌留在牌河展示但不参与计分，不否定素胡。
    assert has_su_hu([discard(18, followed_dealer=True)])


def test_waits_and_discard_waits():
    thirteen = NO_CHICKEN_HAND[:-1]
    found = waits(player(thirteen))
    tiles = {wait.tile for wait in found}
    # 9 = 幺筒（万能牌）也能把牌型补成胡；26 = 9s 对子。
    assert {9, 26} <= tiles

    options = discard_waits(player(NO_CHICKEN_HAND))
    assert any(entry['index'] in (12, 13)
               and {9, 26} <= {wait.tile for wait in entry['waits']}
               for entry in options)


def test_waits_returns_empty_for_wrong_hand_size():
    assert waits(player(NO_CHICKEN_HAND)) == []


def test_discard_waits_returns_empty_for_wrong_hand_size():
    assert discard_waits(player(NO_CHICKEN_HAND[:-1])) == []


def test_physical_tiles_cannot_exceed_four():
    hand = [1, 1, 1, 1, 1, 2, 3, 4, 5, 6, 7, 10, 11, 12]
    assert evaluate_hand(player(hand), WinContext(kind='self', tile=1)) == []


def test_shape_points_excludes_scene_patterns():
    value = evaluate_hand(player(NO_CHICKEN_HAND), WinContext(kind='self', tile=26))[0]
    assert shape_points(value) == 0

    ready = evaluate_hand(
        player(NO_CHICKEN_HAND, declaration='ready'),
        WinContext(kind='discard', tile=26),
    )[0]
    # 硬胡是结构番（不在场景番排除表里），报听才是场景番。
    assert shape_points(ready) == 10


def test_pass_allows():
    assert pass_allows(player([]), 10)
    assert not pass_allows(player([], passed_hu=30), 35)
    assert pass_allows(player([], passed_hu=30), 50)
    assert not pass_allows(player([], passed_hu=30, passed_shape=20), 50, None)


def test_context_allows_rob_and_hot_bypass_pass_restriction():
    value = evaluate_hand(player(NO_CHICKEN_HAND), WinContext(kind='self', tile=26))[0]
    restricted = player(NO_CHICKEN_HAND, passed_hu=30)
    assert not pass_allows(restricted, value.h, value)
    assert context_allows(restricted, WinContext(kind='rob', tile=26), value)
    assert context_allows(restricted, WinContext(kind='hot', tile=26), value)


def test_hu_opportunity_self_and_discard():
    self_op = hu_opportunity(player(NO_CHICKEN_HAND), WinContext(kind='self', tile=26))
    assert self_op['hu'] is True
    assert self_op['restricted'] is False
    assert self_op['h'] == 25
    # 合法解里既有素胡（0）也有硬胡（10），取最大结构分。
    assert self_op['shape_points'] == 10

    discard_op = hu_opportunity(player(NO_CHICKEN_HAND), WinContext(kind='discard', tile=26))
    assert discard_op['hu'] is False
    assert discard_op['restricted'] is False
    assert discard_op['h'] == 0


def test_hu_opportunity_restricted_by_passed_hu():
    restricted = player(NO_CHICKEN_HAND, passed_hu=30)
    op = hu_opportunity(restricted, WinContext(kind='self', tile=26))
    assert op['hu'] is True
    assert op['restricted'] is True
    assert op['h'] == 25
