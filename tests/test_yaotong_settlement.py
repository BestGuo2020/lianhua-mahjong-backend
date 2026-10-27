"""幺筒结算/鸡分/听牌信息测试。"""

import pytest

from app.core.yaotong.settlement import (
    Ending,
    chicken_breakdown,
    chicken_value,
    hu_gain,
    hu_plan_key,
    legal_hu_plans,
    next_dealer,
    offered_hu_plans,
    ready_info,
    settle,
)
from app.core.yaotong.types import Discard, GameState, Player, WinContext

NO_CHICKEN = [1, 2, 3, 4, 5, 6, 10, 11, 12, 13, 14, 15, 26, 26]
NO_CHICKEN_13 = NO_CHICKEN[:-1]


def make_state(winner_hand=None, **kwargs):
    players = [Player(name=f'P{i}') for i in range(4)]
    if winner_hand is not None:
        players[0] = Player(name='P0', hand=list(winner_hand))
    state = GameState(players=players, wall=kwargs.pop('wall', []), **kwargs)
    return state


def test_hu_plan_key_matches_ts_json_shape():
    value = legal_hu_plans(Player(name='P', hand=list(NO_CHICKEN)),
                           WinContext(kind='self', tile=26))[0]
    assert hu_plan_key(value) == '[["素胡"],0,false]'


def test_ready_info_for_thirteen_tiles():
    info = ready_info(Player(name='P', hand=list(NO_CHICKEN_13)))
    assert info['ready'] is True
    assert info['price'] >= 5
    assert info['natural'] >= 0
    assert info['values']


def test_chicken_breakdown_fixed_yao_and_flipped_multiplier():
    player = Player(name='P', hand=[0, 0, 16, 18])
    terms = chicken_breakdown(player, 2, flipped=0)
    by_label = {item.label: item for item in terms}
    assert by_label['一万·底鸡'].count == 2
    assert by_label['一万·底鸡'].multiplier == 3
    assert by_label['一万·底鸡'].amount == 12
    assert by_label['八筒·底鸡'].amount == 2
    assert by_label['一条·底鸡'].amount == 2
    assert by_label['一筒·本牌'].amount == 20
    assert chicken_value(player, 2, flipped=0) == 36
    assert chicken_value(player, 2, flipped=0, exposed_only=True) == 0


def test_chicken_breakdown_river_terms():
    player = Player(name='P', hand=[])
    player.river = [Discard(tile=9, chicken=10, stage='first', id=1)]
    terms = chicken_breakdown(player, 0, flipped=None)
    assert any(item.label == '一筒·首冲' and item.amount == 10 for item in terms)


def test_hu_gain_includes_chicken_and_responsibility():
    player = Player(name='P', hand=[])
    value = legal_hu_plans(Player(name='P', hand=list(NO_CHICKEN)),
                           WinContext(kind='self', tile=26))[0]
    assert hu_gain(player, value, payers=3, chicken=None, responsibility=0) == 75
    assert hu_gain(player, value, payers=3, chicken=9, responsibility=5) == 75


def test_settle_self_draw_no_chicken():
    state = make_state(NO_CHICKEN, wall=[8], dice=(1, 1), dealer=0,
                       dealer_round=0, match_length=4)
    result = settle(state, Ending(kind='win', winners=[0], win_kind='self', tile=26))
    assert state.wall == []
    assert result.kind == 'win'
    assert result.winners == [0]
    assert result.indicator == 8
    assert result.chicken == 0
    assert result.values[0].h == 25
    assert result.deltas[0] == 150
    assert result.deltas[1:] == [-50, -50, -50]
    assert sum(result.deltas) == 0
    assert result.next_dealer == 0
    assert result.finished is False


def test_settle_self_draw_with_chicken_in_river_is_not_su_hu():
    """素胡＝什么都没有：牌河里的固定鸡或翻牌鸡都不再按素胡结算（截图手牌）。"""
    hand = [5, 6, 7, 11, 11, 11, 12, 13, 13, 14, 14, 15, 22, 22]
    for river_tile in (18, 25):  # 一条（固定鸡）、八条（指示牌七条 → 翻牌鸡）
        state = make_state(hand, wall=[24], dice=(2, 3), dealer=0,
                           dealer_round=0, match_length=4)
        state.players[0].river = [
            Discard(tile=river_tile, chicken=4, stage='first', id=1),
        ]
        result = settle(state, Ending(kind='win', winners=[0], win_kind='self', tile=13))
        assert result.chicken == 25
        assert [(p.name, p.points) for p in result.values[0].patterns] == [('硬胡', 10)]
        assert result.values[0].h == 15
        assert [t.amount for t in result.transfers if t.account == '胡牌分'] == [15, 15, 15]


def test_settle_discard_win_with_ready_and_kill_report():
    state = make_state(NO_CHICKEN, wall=[8], dice=(2, 3), dealer=0,
                       dealer_round=0, match_length=4)
    state.players[0].declaration = 'ready'
    state.players[1].declaration = 'ready'
    result = settle(state, Ending(
        kind='win', winners=[0], source=1, win_kind='discard', tile=26,
    ))
    assert result.deltas[0] == 55
    assert result.deltas[1] == -55
    assert any(t.account == '杀报加分' and t.amount == 20 for t in result.transfers)
    assert result.next_dealer == 0


def test_settle_penalty():
    state = make_state(wall=[], dice=(2, 3), dealer=0)
    result = settle(state, Ending(
        kind='penalty', offender=1, reason='炸胡',
    ))
    assert result.kind == 'penalty'
    assert result.deltas == [45, -135, 45, 45]
    assert result.next_dealer == 0
    assert all(t.account == '一般包牌赔付' for t in result.transfers)


def test_settle_draw_pays_ready_players():
    state = make_state(wall=[], dice=(2, 3), dealer=0)
    state.players[0] = Player(name='P0', hand=list(NO_CHICKEN_13))
    result = settle(state, Ending(kind='draw'))
    price = ready_info(state.players[0])['price']
    assert result.deltas[0] == price * 3
    assert result.deltas[1:] == [-price, -price, -price]
    assert result.reason == '牌墙摸空，荒庄赔听'
    assert result.chicken is None


def test_next_dealer():
    state = make_state(wall=[], dealer=0)
    state.players[2] = Player(name='P2', hand=list(NO_CHICKEN))
    one = settle(state, Ending(kind='win', winners=[2], win_kind='self', tile=26))
    assert next_dealer(state, one) == 2

    state2 = make_state(wall=[], dealer=0)
    penalty = settle(state2, Ending(kind='penalty', offender=1, reason='x'))
    assert next_dealer(state2, penalty) == 0


def test_offered_hu_plans_requires_yao_in_hand():
    state = make_state(NO_CHICKEN, wall=[8], dealer=0)
    state.phase = 'discard'
    state.current = 0
    state.players[0].drawn = 26
    assert offered_hu_plans(state, 0) == []


def test_legal_hu_plans_and_context_restriction():
    player = Player(name='P', hand=list(NO_CHICKEN), passed_hu=30)
    values = legal_hu_plans(player, WinContext(kind='self', tile=26))
    # passAllows 只放行 >=45；素胡 h=25、硬胡 h=15 都被过手限制。
    assert values == []


@pytest.mark.parametrize('hand', [NO_CHICKEN[:0], NO_CHICKEN[:10]])
def test_ready_info_invalid_length(hand):
    info = ready_info(Player(name='P', hand=list(hand)))
    assert info['ready'] is False
    assert info['price'] == 5
