import pytest
from app.core.yaotong.engine import act, continue_game
from app.core.yaotong.follow_dealer import complete_follow_dealer
from app.core.yaotong.hands import hu_opportunity
from app.core.yaotong.settlement import Ending, chicken_value, settle
from app.core.yaotong.types import Command, Discard, GameState, Meld, Player, WinContext
from app.game.yaotong_protocol import player_view


def first_circle(tile, dealer=0):
    players = [Player(name=str(i), hand=[1, 4, 7, 10, 13, 16, 19, 22, 25, 20, 23, 24, 26], draws=2)
               for i in range(4)]
    for seat, player in enumerate(players):
        offset = (seat - dealer) % 4
        player.hand = [t for t in player.hand if t != tile]
        player.discards = int(offset < 3)
        if offset < 3:
            player.river = [Discard(tile, 30, 'pre', offset + 1)]
    current = (dealer + 3) % 4
    players[current].hand.append(tile)
    return GameState(players=players, wall=[], dice=(2, 3), dealer=dealer, current=current,
                     phase='discard', serial=3, dealer_round=1, hand_number=1)


@pytest.mark.parametrize('tile,base', [(13, 5), (18, 15), (0, 15), (16, 15), (9, 170)])
def test_follow_pays_three_opponents_and_resets_next_hand(tile, base):
    state = first_circle(tile, 2)
    player = state.players[state.current]
    assert act(state, state.current, Command(type='discard', index=len(player.hand) - 1))
    while state.pending:
        seat = next(s for s in state.pending.options if s not in state.pending.choices)
        assert act(state, seat, Command(type='respond', action='pass'))
    assert state.players[2].follow_dealer
    assert all(p.river[0].followed_dealer and p.river[0].chicken == 0 for p in state.players)
    payments = [t for t in state.result.transfers if t.account == '跟庄赔付']
    assert [(t.from_seat, t.to, t.base, t.amount) for t in payments] == [(2, i, base, base) for i in (0, 1, 3)]
    assert sum(state.result.deltas) == 0
    view = player_view(state.players[2], 2, 0)
    assert view['followDealer'] and view['river'][0]['followedDealer']
    assert complete_follow_dealer(state) is None
    assert all(not p.follow_dealer and not p.river for p in continue_game(state).players)


@pytest.mark.parametrize('reason', ['different', 'second', 'meld', 'claimed', 'order'])
def test_does_not_follow_interrupted_or_different_discards(reason):
    state = first_circle(13)
    state.players[3].discards = 1
    state.players[3].river = [Discard(13, 0, 'normal', 4)]
    p = state.players[1]
    if reason == 'different': p.river[0].tile = 14
    if reason == 'second': p.discards = 2
    if reason == 'meld': p.melds = [Meld(2, 'turn')]
    if reason == 'claimed': p.river = []
    if reason == 'order': p.river[0].id = 5
    assert complete_follow_dealer(state) is None


def test_passport_adds_no_points_and_does_not_bypass_passed_hu():
    player = Player('庄家', hand=[1, 2, 3, 4, 5, 6, 19, 20, 21, 14, 14, 24, 25, 26], draws=2)
    ctx = WinContext('discard', 26)
    assert not hu_opportunity(player, ctx)['hu']
    player.follow_dealer = True
    option = hu_opportunity(player, ctx)
    assert option['hu'] and option['h'] == 15
    player.passed_hu = option['h']
    player.passed_shape = option['shape_points']
    assert hu_opportunity(player, ctx)['restricted']


@pytest.mark.parametrize('deferred', [False, True])
def test_fourth_tile_won_including_retained_window_does_not_complete_follow(deferred):
    state = first_circle(13)
    state.wall = [7, 11]
    state.players[1].hand = [1, 2, 3, 4, 5, 6, 19, 20, 21, 14, 14, 11, 12]
    state.players[1].declaration = 'ready'
    assert act(state, 3, Command(type='discard', index=len(state.players[3].hand) - 1))
    assert state.pending.options[1].hu
    assert act(state, 1, Command(type='respond', action='pass' if deferred else 'hu', defer_hu=deferred))
    while state.pending and state.phase == 'response':
        seat = next(s for s in state.pending.options if s not in state.pending.choices)
        assert act(state, seat, Command(type='respond', action='pass'))
    if deferred:
        assert state.players[0].follow_dealer
        assert act(state, 1, Command(type='declareHu'))
    assert state.result.kind == 'win'
    assert not state.players[0].follow_dealer
    assert not any(t.account == '跟庄赔付' for t in state.result.transfers)


def test_normal_win_after_follow_does_not_rescore_flipped_follow_tile():
    state = first_circle(13)
    state.players[3].discards = 1
    state.players[3].river = [Discard(13, 0, 'normal', 4)]
    complete_follow_dealer(state)
    for p in state.players:
        p.hand = []
    state.players[0].hand = [1, 2, 3, 4, 5, 6, 19, 20, 21, 14, 14, 24, 25, 26]
    state.wall = [12]
    result = settle(state, Ending('win', winners=[0], source=1, win_kind='discard', tile=26))
    assert result.chicken == 13
    assert len([t for t in result.transfers if t.account == '跟庄赔付']) == 3
    assert not any(t.account == '鸡分' for t in result.transfers)
    assert sum(result.deltas) == 0


@pytest.mark.parametrize('tile,base', [(13, 5), (18, 15), (9, 170)])
def test_follow_tiles_never_score_again_and_payment_survives_penalty(tile, base):
    state = first_circle(tile)
    state.players[3].discards = 1
    state.players[3].river = [Discard(tile, 30, 'pre', 4)]
    assert complete_follow_dealer(state) == tile
    for player in state.players:
        player.hand = []
        assert chicken_value(player, 0, tile) == 0
        assert chicken_value(player, 0, tile, True) == 0
    state.dice = (3, 3)
    result = settle(state, Ending('penalty', offender=1))
    assert [t.amount for t in result.transfers if t.account == '跟庄赔付'] == [base * 2] * 3
