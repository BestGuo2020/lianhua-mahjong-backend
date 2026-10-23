"""幺筒联机协议：yt_snapshot 组装与 action 解码。

协议 v0 见 docs/幺筒麻将联机方案.md §6。牌在协议里统一用 m1/p1/s1；
引擎内部仍是 0..26。
"""

from __future__ import annotations

from typing import Optional

from app.core.yaotong.engine import self_options
from app.core.yaotong.tiles import from_tile_type, to_tile_type
from app.core.yaotong.types import (
    ActionOptions,
    Command,
    Discard,
    GameEvent,
    GameState,
    HandValue,
    Meld,
    Pending,
    Player,
    Settlement,
)

_ACTION_TYPES = {
    'original', 'discard', 'respond', 'hu', 'declareHu', 'passHu', 'kong',
}
_RESPONSE_ACTIONS = {'hu', 'peng', 'kong', 'pass'}


def _tile(value) -> Optional[str]:
    return None if value is None else to_tile_type(value)


def command_from_action(action: dict) -> Command:
    if not isinstance(action, dict):
        raise ValueError('INVALID_ACTION')
    command_type = action.get('type')
    if command_type not in _ACTION_TYPES:
        raise ValueError('INVALID_ACTION')

    declare = action.get('declare')
    if declare is not None and not isinstance(declare, bool):
        raise ValueError('INVALID_ACTION')
    index = action.get('index')
    if index is not None and (
        not isinstance(index, int) or isinstance(index, bool) or index < 0
    ):
        raise ValueError('INVALID_ACTION')
    response = action.get('action')
    if response is not None and response not in _RESPONSE_ACTIONS:
        raise ValueError('INVALID_ACTION')
    plan = action.get('plan')
    if plan is not None and not isinstance(plan, str):
        raise ValueError('INVALID_ACTION')
    defer_hu = action.get('deferHu')
    if defer_hu is not None and not isinstance(defer_hu, bool):
        raise ValueError('INVALID_ACTION')

    tile = action.get('tile')
    if tile is not None:
        if isinstance(tile, str):
            tile = from_tile_type(tile)
        if not isinstance(tile, int) or isinstance(tile, bool) \
                or not 0 <= tile <= 26:
            raise ValueError('INVALID_ACTION')

    return Command(
        type=command_type,
        declare=declare,
        index=index,
        action=response,
        plan=plan,
        defer_hu=defer_hu,
        tile=tile,
    )


def options_view(options: ActionOptions) -> dict:
    return {
        'hu': options.hu,
        'peng': options.peng,
        'kong': options.kong,
        'restrictedHu': options.restricted_hu,
        'restrictedPeng': options.restricted_peng,
        'mandatory': options.mandatory,
        'h': options.h,
        'shapePoints': options.shape_points,
    }


def meld_view(meld: Meld) -> dict:
    return {
        'tile': to_tile_type(meld.tile),
        'kind': meld.kind,
        'from': meld.from_seat,
        'pre': meld.pre,
        'chicken': meld.chicken,
        'responsibility': meld.responsibility,
    }


def discard_view(discard: Discard) -> dict:
    return {
        'tile': to_tile_type(discard.tile),
        'chicken': discard.chicken,
        'stage': discard.stage,
        'id': discard.id,
        'firstChicken': discard.first_chicken,
        'followChicken': discard.follow_chicken,
        'pre': discard.pre,
    }


def player_view(player: Player, seat: int, self_seat: int,
                room_seat=None) -> dict:
    is_self = seat == self_seat
    return {
        'seat': seat,
        'name': room_seat.nickname if room_seat is not None else player.name,
        'avatar': room_seat.avatar if room_seat is not None else '',
        'characterId': (
            room_seat.character_id if room_seat is not None else 'deepseek'
        ),
        'playerKind': 'human' if room_seat is not None else 'bot',
        'isLlm': False,
        'score': player.score,
        'hand': [to_tile_type(tile) for tile in player.hand] if is_self else [],
        'concealedTileCount': len(player.hand),
        'melds': [meld_view(meld) for meld in player.melds],
        'river': [discard_view(discard) for discard in player.river],
        'declaration': player.declaration,
        'draws': player.draws,
        'discards': player.discards,
        'drawn': _tile(player.drawn) if is_self else None,
        'afterKong': player.after_kong,
        'burned': player.burned,
        'passedHu': _tile(player.passed_hu) if is_self else None,
        'chickenPeng': [to_tile_type(tile) for tile in player.chicken_peng]
        if is_self else [],
        'chickenDiscard': [to_tile_type(tile) for tile in player.chicken_discard]
        if is_self else [],
    }


def event_view(event: GameEvent) -> dict:
    return {
        'id': event.id,
        'seat': event.seat,
        'kind': event.kind,
        'text': event.text,
        'afterKong': event.after_kong,
        'tile': _tile(event.tile),
        'chicken': event.chicken,
        'rush': event.rush,
    }


def hand_value_view(value: HandValue) -> dict:
    return {
        'h': value.h,
        'patternPoints': value.pattern_points,
        'patterns': [
            {'name': pattern.name, 'points': pattern.points}
            for pattern in value.patterns
        ],
        'naturalYaos': value.natural_yaos,
        'winningYaoNatural': value.winning_yao_natural,
        'wildcards': value.wildcards,
        'big': value.big,
        'structure': value.structure,
        'groups': [[to_tile_type(tile) for tile in group] for group in value.groups],
        'resolvedHand': (
            [to_tile_type(tile) for tile in value.resolved_hand]
            if value.resolved_hand is not None else None
        ),
    }


def settlement_view(result: Settlement) -> dict:
    return {
        'kind': result.kind,
        'winners': list(result.winners),
        'source': result.source,
        'winKind': result.win_kind,
        'tile': _tile(result.tile),
        'offender': result.offender,
        'reason': result.reason,
        'indicator': _tile(result.indicator),
        'chicken': _tile(result.chicken),
        'ready': list(result.ready),
        'deltas': list(result.deltas),
        'nextDealer': result.next_dealer,
        'finished': result.finished,
        'transfers': [
            {
                'from': transfer.from_seat,
                'to': transfer.to,
                'account': transfer.account,
                'base': transfer.base,
                'multiplier': transfer.multiplier,
                'amount': transfer.amount,
                'reason': transfer.reason,
            }
            for transfer in result.transfers
        ],
        'values': {
            str(seat): hand_value_view(value)
            for seat, value in result.values.items()
        },
    }


def _pending_view(pending: Pending, seat: int) -> dict:
    return {
        'kind': pending.kind,
        'tile': to_tile_type(pending.tile),
        'from': pending.from_seat,
        'discard': (
            discard_view(pending.discard)
            if pending.discard is not None else None
        ),
        'choices': {
            str(choice_seat): choice
            for choice_seat, choice in pending.choices.items()
        },
        'waitingSeats': [
            waiting for waiting, options in pending.options.items()
            if waiting not in pending.choices
        ],
    }


def _self_options(state: GameState, seat: int) -> Optional[dict]:
    if state.phase == 'discard' and state.current == seat:
        return options_view(self_options(state, seat))
    if state.phase == 'response' and state.pending is not None:
        options = state.pending.options.get(seat)
        if options is not None and seat not in state.pending.choices:
            return options_view(options)
    return None


def snapshot_view(room, seat: int) -> dict:
    state = room.state
    if state is None:
        return {
            'seat': seat,
            'phase': 'lobby',
            'players': [],
            'ownOptions': None,
            'pending': None,
            'events': [],
            'wallCount': 0,
        }
    players = [
        player_view(player, index, seat, room.seats[index])
        for index, player in enumerate(state.players)
    ]
    wait_remaining = list(state.wait_remaining_ms)
    for hold_seat in getattr(room, '_holds', {}):
        remaining = room.hold_remaining_ms(hold_seat)
        if remaining is not None:
            wait_remaining[hold_seat] = remaining
    return {
        'seat': seat,
        'phase': state.phase,
        'dealer': state.dealer,
        'current': state.current,
        'serial': state.serial,
        'wallCount': len(state.wall),
        'dice': list(state.dice),
        'wallSide': state.wall_side,
        'reservedStacks': state.reserved_stacks,
        'matchLength': state.match_length,
        'dealerRound': state.dealer_round,
        'handNumber': state.hand_number,
        'repeats': state.repeats,
        'players': players,
        'originalDecisions': list(state.original_decisions),
        'ownOriginalPending': (
            state.phase == 'original' and not state.original_decisions[seat]
        ),
        'pending': (
            _pending_view(state.pending, seat)
            if state.pending is not None else None
        ),
        'ownOptions': _self_options(state, seat),
        'events': [event_view(event) for event in state.events],
        'firstFixedDiscard': [
            to_tile_type(tile) for tile in state.first_fixed_discard
        ],
        'firstFixedPeng': state.first_fixed_peng,
        'firstYaoDiscard': state.first_yao_discard,
        'seenChickenDiscards': [
            to_tile_type(tile) for tile in state.seen_chicken_discards
        ],
        'waitRemainingMs': wait_remaining,
    }


def snapshot_for(room, seat: int) -> dict:
    state = room.state
    result = state.result if state is not None else None
    continuation = room.continuation()
    return {
        'kind': 'yt_snapshot',
        'roomId': room.room_id,
        'matchId': room.match_id,
        'rulesetId': 'yaotong',
        'seat': seat,
        'round': state.hand_number if state is not None else 0,
        'dealer': state.dealer if state is not None else 0,
        'matchLength': room.match_length,
        'yaotongMode': getattr(room, 'yaotong_mode', 'easy'),
        'holdSeats': sorted(getattr(room, '_holds', {}).keys()),
        'decisionDeadlineAt': (
            int(room._decision_deadline * 1000)
            if getattr(room, '_decision_deadline', None) else 0
        ),
        'matchFinished': bool(
            getattr(room, 'match_finished', False)
            or (result is not None and result.finished)
        ),
        'opening': room.opening,
        'roundResult': settlement_view(result) if result is not None else None,
        **({'continuation': continuation} if continuation else {}),
        'view': snapshot_view(room, seat),
    }
