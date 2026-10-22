"""幺筒引擎与 TypeScript 的同种子金标对拍。

fixture 由前端 engine.ts 用同一副牌/骰子/庄家和同一套自动策略生成；
Python 重跑后逐步比较状态与终局。更新规则时两端必须一起重新生成。
"""

import json
from pathlib import Path

from app.core.yaotong.engine import (
    StartOptions,
    act,
    create_game,
    self_options,
)
from app.core.yaotong.types import Command

WALL = [tile for tile in range(27) for _ in range(4)]
FIXTURE = Path(__file__).parent / 'fixtures' / 'yaotong_engine_golden.json'


def norm_player(player):
    return {
        'name': player.name,
        'hand': list(player.hand),
        'melds': [
            {'tile': meld.tile, 'kind': meld.kind, 'from': meld.from_seat,
             'pre': meld.pre, 'chicken': meld.chicken,
             'responsibility': meld.responsibility}
            for meld in player.melds
        ],
        'river': [
            {'tile': discard.tile, 'chicken': discard.chicken,
             'stage': discard.stage, 'id': discard.id,
             'firstChicken': discard.first_chicken,
             'followChicken': discard.follow_chicken, 'pre': discard.pre}
            for discard in player.river
        ],
        'score': player.score, 'declaration': player.declaration,
        'draws': player.draws, 'discards': player.discards,
        'drawn': player.drawn, 'afterKong': player.after_kong,
        'burned': player.burned, 'passedHu': player.passed_hu,
        'passedShape': player.passed_shape,
        'chickenPeng': list(player.chicken_peng),
        'chickenDiscard': list(player.chicken_discard),
    }


def norm_pending(pending):
    if pending is None:
        return None
    return {
        'kind': pending.kind, 'tile': pending.tile, 'from': pending.from_seat,
        'discard': None if pending.discard is None else {
            'tile': pending.discard.tile, 'chicken': pending.discard.chicken,
            'stage': pending.discard.stage, 'id': pending.discard.id,
            'firstChicken': pending.discard.first_chicken,
            'followChicken': pending.discard.follow_chicken,
            'pre': pending.discard.pre,
        },
        'options': {
            str(seat): {
                'hu': options.hu, 'peng': options.peng, 'kong': options.kong,
                'restrictedHu': options.restricted_hu,
                'restrictedPeng': options.restricted_peng,
                'mandatory': options.mandatory, 'h': options.h,
                'shapePoints': options.shape_points,
            }
            for seat, options in pending.options.items()
        },
        'choices': {str(seat): choice for seat, choice in pending.choices.items()},
        'huPlans': {str(seat): plan for seat, plan in pending.hu_plans.items()},
        'deferredHu': pending.deferred_hu,
    }


def norm_result(result):
    if result is None:
        return None
    return {
        'kind': result.kind, 'winners': list(result.winners),
        'source': result.source, 'winKind': result.win_kind,
        'tile': result.tile, 'offender': result.offender,
        'reason': result.reason, 'indicator': result.indicator,
        'chicken': result.chicken, 'ready': list(result.ready),
        'deltas': list(result.deltas), 'nextDealer': result.next_dealer,
        'finished': result.finished,
        'transfers': [
            {'from': transfer.from_seat, 'to': transfer.to,
             'account': transfer.account, 'base': transfer.base,
             'multiplier': transfer.multiplier, 'amount': transfer.amount,
             'reason': transfer.reason}
            for transfer in result.transfers
        ],
        'values': {
            str(seat): {
                'h': value.h,
                'patterns': [[pattern.name, pattern.points] for pattern in value.patterns],
                'naturalYaos': value.natural_yaos,
                'winningYaoNatural': value.winning_yao_natural,
                'wildcards': value.wildcards,
                'resolvedHand': value.resolved_hand,
            }
            for seat, value in result.values.items()
        },
    }


def norm_state(state):
    return {
        'players': [norm_player(player) for player in state.players],
        'wall': list(state.wall),
        'wallCount': len(state.wall),
        'dice': list(state.dice),
        'wallSide': state.wall_side,
        'reservedStacks': state.reserved_stacks,
        'dealer': state.dealer, 'current': state.current,
        'phase': state.phase,
        'originalDecisions': list(state.original_decisions),
        'pending': norm_pending(state.pending),
        'result': norm_result(state.result),
        'firstFixedDiscard': list(state.first_fixed_discard),
        'firstFixedPeng': state.first_fixed_peng,
        'firstYaoDiscard': state.first_yao_discard,
        'seenChickenDiscards': list(state.seen_chicken_discards),
        'waitRemainingMs': list(state.wait_remaining_ms),
        'serial': state.serial, 'matchLength': state.match_length,
        'dealerRound': state.dealer_round, 'handNumber': state.hand_number,
        'repeats': state.repeats,
    }


def step(state):
    if state.phase == 'settled':
        return False
    if state.phase == 'original':
        seat = next(
            index for index, decided in enumerate(state.original_decisions)
            if not decided
        )
        return act(state, seat, Command(type='original', declare=False))
    if state.phase == 'discard':
        seat = state.current
        if self_options(state, seat).hu:
            return act(state, seat, Command(type='hu'))
        return act(state, seat, Command(
            type='discard', index=len(state.players[seat].hand) - 1,
        ))
    if state.phase == 'response':
        pending = state.pending
        for seat in range(4):
            if seat in pending.options and seat not in pending.choices:
                if pending.options[seat].hu:
                    return act(state, seat, Command(type='respond', action='hu'))
                return act(state, seat, Command(type='respond', action='pass'))
    return False


def diff(left, right, path=''):
    if type(left) is not type(right):
        return f'{path}: type {type(left).__name__} != {type(right).__name__}'
    if isinstance(left, dict):
        if set(left) != set(right):
            return f'{path}: keys {set(left) ^ set(right)}'
        for key in left:
            found = diff(left[key], right[key], f'{path}.{key}')
            if found:
                return found
        return ''
    if isinstance(left, list):
        if len(left) != len(right):
            return f'{path}: len {len(left)} != {len(right)}'
        for index, (left_item, right_item) in enumerate(zip(left, right)):
            found = diff(left_item, right_item, f'{path}[{index}]')
            if found:
                return found
        return ''
    if left != right:
        return f'{path}: {left!r} != {right!r}'
    return ''


def test_engine_matches_typescript_golden():
    golden = json.loads(FIXTURE.read_text(encoding='utf-8'))
    state = create_game(StartOptions(wall=list(WALL), dice=(1, 2), dealer=0))
    trace = []
    steps = 0
    while state.phase != 'settled' and steps < 2000:
        assert step(state) is True
        trace.append({
            'step': steps, 'phase': state.phase, 'current': state.current,
            'wallCount': len(state.wall), 'serial': state.serial,
            'hands': [len(player.hand) for player in state.players],
            'scores': [player.score for player in state.players],
        })
        steps += 1

    assert steps == golden['steps'], diff(trace, golden['trace'], 'trace')
    assert diff(trace, golden['trace'], 'trace') == ''
    assert diff(norm_state(state), golden['final'], 'final') == ''
    assert state.result is not None
    assert sum(state.result.deltas) == 0
