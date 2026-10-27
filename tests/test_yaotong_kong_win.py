"""杠开 +20：真实开杠/补摸/胡牌流程与场景边界。"""
import pytest

from app.core.yaotong.engine import act, self_options
from app.core.yaotong.hands import evaluate_hand, shape_points
from app.core.yaotong.settlement import ready_info
from app.core.yaotong.types import Command, GameState, Meld, Player, WinContext


def tiles(text):
    return [9 * 'mps'.index(part[-1]) + int(n) - 1 for part in text.split() for n in part[:-1]]


def kong_draw(kind, double=False):
    state = GameState(
        players=[Player(name=f'P{i}', hand=tiles('147m 258p 369s 2468s'), draws=2) for i in range(4)],
        wall=[8, 26], dice=(3, 3) if double else (2, 3), dealer=0, current=0, phase='discard')
    wait = '234m 234s 66p 78s'
    if kind == 'open':
        state.players[0] = Player('P0', hand=tiles(f'555m {wait}'), draws=2)
        state.current = 1
        state.players[1].hand.append(4)
        assert act(state, 1, Command(type='discard', index=13))
        assert act(state, 0, Command(type='respond', action='kong'))
    else:
        state.players[0] = Player('P0', hand=tiles(f'5m {wait}' if kind == 'added' else f'5555m {wait}'),
                                  drawn=25 if kind == 'stored' else 4, draws=2,
                                  melds=[Meld(4, 'peng', from_seat=1)] if kind == 'added' else [])
        assert act(state, 0, Command(type='kong', tile=4))
    while state.phase == 'response' and state.pending:
        seat = next(s for s in state.pending.options if s not in state.pending.choices)
        assert act(state, seat, Command(type='respond', action='pass'))
    assert state.players[0].melds[0].kind == kind
    assert state.players[0].drawn == 26
    return state


@pytest.mark.parametrize('kind', ['open', 'turn', 'stored', 'added'])
def test_all_kong_kinds_add_20_to_hu_and_keep_independent_kong_payment(kind):
    state = kong_draw(kind)
    assert self_options(state, 0).h == 35
    assert act(state, 0, Command(type='hu'))
    value = state.result.values[0]
    assert [(p.name, p.points) for p in value.patterns] == [('硬胡', 10), ('杠开', 20)]
    assert [t.amount for t in state.result.transfers if t.account == '胡牌分'] == [35, 35, 35]
    assert [t.amount for t in state.result.transfers if t.account == '杠分'] == (
        [] if kind == 'stored' else [5] if kind == 'open' else [5, 5, 5])
    assert sum(state.result.deltas) == 0
    assert shape_points(value) == 10


def test_kong_win_stacks_with_flush_hard_hu_and_dice_once():
    state = kong_draw('turn', double=True)
    state.players[0].hand = tiles('234678m 99m 123m')
    state.players[0].drawn = 2
    assert act(state, 0, Command(type='hu'))
    assert state.result.values[0].h == 55
    assert [t.amount for t in state.result.transfers if t.account == '胡牌分'] == [110, 110, 110]
    assert [t.amount for t in state.result.transfers if t.account == '杠分'] == [10, 10, 10]


def test_joker_kong_win_is_5_plus_20_without_hard_hu():
    state = kong_draw('turn')
    state.players[0].hand = tiles('234m 234s 66p 78s 1p')
    state.players[0].drawn = 9
    assert act(state, 0, Command(type='hu'))
    assert state.result.values[0].h == 25
    assert [(p.name, p.points) for p in state.result.values[0].patterns] == [('杠开', 20)]


@pytest.mark.parametrize('kind', ['discard', 'rob', 'hot', 'ready'])
def test_other_scenes_and_ready_price_never_borrow_kong_win(kind):
    state = kong_draw('turn')
    values = evaluate_hand(state.players[0], WinContext(kind, 26))
    assert values
    assert all(p.name != '杠开' for v in values for p in v.patterns)
    assert ready_info(state.players[0])['price'] == 10


def test_discard_ends_kong_win_window():
    state = kong_draw('turn')
    player = state.players[0]
    assert act(state, 0, Command(type='discard', index=player.hand.index(26)))
    assert not player.after_kong
    player.hand.append(26)
    player.drawn = 26
    player.passed_hu = None
    state.current = 0
    state.phase = 'discard'
    state.pending = None
    assert self_options(state, 0).h == 15


def test_multiple_kongs_still_count_kong_win_only_once():
    player = Player('P', hand=tiles('234m 66p 789s'), drawn=26, after_kong=True,
                    melds=[Meld(4, 'turn'), Meld(12, 'added')])
    value = evaluate_hand(player, WinContext('self', 26))[0]
    assert value.h == 35
    assert [(p.name, p.points) for p in value.patterns if p.name == '杠开'] == [('杠开', 20)]
