"""幺筒引擎：开局/原报/出牌/整局推进测试。"""

from app.core.yaotong.engine import (
    StartOptions,
    act,
    continue_game,
    create_game,
    self_options,
)
from app.core.yaotong.types import Command

WALL = [tile for tile in range(27) for _ in range(4)]


def new_game(dealer=0):
    return create_game(StartOptions(wall=list(WALL), dice=(1, 2), dealer=dealer))


def test_create_game_deals_thirteen_each_and_waits_for_original():
    state = new_game()
    assert [len(player.hand) for player in state.players] == [13, 13, 13, 13]
    assert state.phase == 'original'
    assert state.current == state.dealer == 0
    assert len(state.wall) == 56
    assert state.serial == 1
    assert state.events[0].kind == 'start'
    assert state.dealer_round == 1
    assert state.hand_number == 1


def test_original_decisions_trigger_dealer_draw():
    state = new_game()
    for seat in range(4):
        assert act(state, seat, Command(type='original', declare=False)) is True
    assert state.phase == 'discard'
    assert state.current == 0
    assert len(state.players[0].hand) == 14
    assert state.players[0].drawn is not None
    assert len(state.wall) == 55
    assert state.events[-1].kind == 'draw'


def test_discard_moves_to_next_turn_or_response():
    state = new_game()
    for seat in range(4):
        act(state, seat, Command(type='original', declare=False))
    index = len(state.players[0].hand) - 1
    tile = state.players[0].hand[index]
    assert act(state, 0, Command(type='discard', index=index)) is True
    assert state.players[0].river[-1].tile == tile
    assert state.players[0].discards == 1
    assert state.players[0].drawn is None
    # 无人可响应时直接推进到下一家摸牌；有人可响应时停在 response。
    assert state.phase in ('response', 'discard')
    if state.phase == 'discard':
        assert state.current == 1
        assert len(state.players[1].hand) == 14


def autoplay_step(state) -> bool:
    if state.phase == 'settled':
        return False
    if state.phase == 'original':
        seat = next(i for i, decided in enumerate(state.original_decisions) if not decided)
        return act(state, seat, Command(type='original', declare=False))
    if state.phase == 'discard':
        seat = state.current
        options = self_options(state, seat)
        if options.hu:
            return act(state, seat, Command(type='hu'))
        return act(state, seat, Command(
            type='discard', index=len(state.players[seat].hand) - 1,
        ))
    if state.phase == 'response':
        pending = state.pending
        assert pending is not None
        for seat in range(4):
            if seat in pending.options and seat not in pending.choices:
                if pending.options[seat].hu:
                    return act(state, seat, Command(type='respond', action='hu'))
                return act(state, seat, Command(type='respond', action='pass'))
    return False


def test_full_hand_reaches_settlement_with_zero_sum():
    state = new_game()
    steps = 0
    while state.phase != 'settled' and steps < 2000:
        assert autoplay_step(state) is True
        steps += 1
    assert state.phase == 'settled'
    assert state.result is not None
    assert sum(state.result.deltas) == 0
    assert sum(player.score for player in state.players) == 0
    assert len(state.events) <= 80
    assert steps < 2000


def test_continue_game_preserves_scores_and_advances_round():
    state = new_game()
    steps = 0
    while state.phase != 'settled' and steps < 2000:
        autoplay_step(state)
        steps += 1
    assert state.result is not None
    if state.result.finished:
        return
    scores = [player.score for player in state.players]
    next_state = continue_game(state, StartOptions(
        wall=list(WALL), dice=(1, 2),
    ))
    assert next_state.hand_number == state.hand_number + 1
    assert [player.score for player in next_state.players] == scores
    assert next_state.phase == 'original'
