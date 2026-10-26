"""幺筒结算 —— 对应 src/game/variants/yaotong/settlement.ts。

所有转账在 ledger 里保留付款方/收款方/科目；骰子倍率只在这里应用一次。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from functools import cmp_to_key
from typing import Callable, Optional

from app.core.yaotong.hands import context_allows, evaluate_hand, waits
from app.core.yaotong.hu_window import hu_window
from app.core.yaotong.follow_dealer import follow_dealer_base
from app.core.yaotong.tiles import TILES, YAO, counts_of, is_fixed, next_tile, tile_name
from app.core.yaotong.types import (
    GameState,
    HandValue,
    Player,
    Seat,
    Settlement,
    SettlementKind,
    Tile,
    Transfer,
    WinContext,
    WinKind,
)


@dataclass
class Ending:
    kind: SettlementKind
    winners: list[Seat] = field(default_factory=list)
    source: Optional[Seat] = None
    win_kind: Optional[WinKind] = None
    tile: Optional[Tile] = None
    offender: Optional[Seat] = None
    reason: Optional[str] = None
    responsibility: int = 0
    hu_plans: dict[Seat, str] = field(default_factory=dict)


@dataclass
class ChickenTerm:
    label: str
    count: int
    unit: int
    multiplier: int
    amount: int


def ready_info(player: Player) -> dict:
    options = waits(player)
    values: list[tuple[HandValue, int]] = []
    for option in options:
        for value in option.values:
            natural = value.natural_yaos - (
                1 if option.tile == YAO and value.winning_yao_natural else 0
            )
            values.append((value, natural))
    if len(player.hand) == 14 - len(player.melds) * 3:
        tile = player.drawn if player.drawn is not None else player.hand[-1]
        values = [
            (value, value.natural_yaos)
            for value in evaluate_hand(player, WinContext(kind='ready', tile=tile))
        ]
    return {
        'ready': bool(values),
        'price': max([5, *[value.pattern_points for value, _ in values]]),
        'natural': max([0, *[natural for _, natural in values]]),
        'values': [value for value, _ in values],
    }


def ledger(
    dice: tuple[int, int] | list[int],
) -> tuple[list[Transfer], Callable[[Seat, Seat, str, int, str], None]]:
    transfers: list[Transfer] = []
    multiplier = 2 if dice[0] == dice[1] else 1

    def add(from_seat: Seat, to: Seat, account: str, base: int, reason: str) -> None:
        if base > 0 and from_seat != to:
            transfers.append(Transfer(
                from_seat=from_seat, to=to, account=account,  # type: ignore[arg-type]
                base=base, multiplier=multiplier,
                amount=base * multiplier, reason=reason,
            ))

    return transfers, add


def owned_tiles(player: Player, exposed_only: bool = False) -> list[Tile]:
    return [
        *([] if exposed_only else player.hand),
        *[meld.tile for meld in player.melds for _ in range(3 if meld.kind == 'peng' else 4)],
        *[discard.tile for discard in player.river if not discard.followed_dealer],
    ]


def chicken_breakdown(
    player: Player,
    natural_yaos: int,
    flipped: Optional[Tile],
    exposed_only: bool = False,
    value: Optional[HandValue] = None,
) -> list[ChickenTerm]:
    terms: dict[str, ChickenTerm] = {}

    def add(tile: Tile, label: str, count: int, unit: int) -> None:
        if not count or not unit:
            return
        multiplier = 3 if (
            not exposed_only and tile == flipped and (tile == YAO or is_fixed(tile))
        ) else 1
        name = f'{tile_name(tile)}·{label}'
        key = f'{name}/{unit}'
        previous = terms.get(key)
        quantity = count + (previous.count if previous is not None else 0)
        terms[key] = ChickenTerm(
            label=name, count=quantity, unit=unit, multiplier=multiplier,
            amount=quantity * unit * multiplier,
        )

    if not exposed_only:
        resolved = (
            value.resolved_hand
            if value is not None and value.resolved_hand is not None
            else player.hand
        )
        for tile in resolved:
            if is_fixed(tile):
                add(tile, '底鸡', 1, 2)
            elif tile == flipped and tile != YAO:
                add(tile, '翻牌鸡', 1, 1)
        add(YAO, '本牌', natural_yaos, 10)

    stage_labels = {
        'pre': '预冲', 'first': '首冲', 'later': '后冲',
        'hot': '杠冲', 'normal': '牌河',
    }
    for discard in player.river:
        if discard.followed_dealer:
            continue
        if discard.tile == YAO:
            add(discard.tile, stage_labels[discard.stage], 1, discard.chicken)
        elif is_fixed(discard.tile):
            add(discard.tile, '冲鸡' if discard.chicken == 4 else '牌河底鸡',
                1, discard.chicken)
        elif not exposed_only and discard.tile == flipped:
            add(discard.tile, '牌河翻鸡', 1, 1)

    meld_labels = {'open': '明杠', 'turn': '转弯杠', 'stored': '存杠', 'added': '补杠'}
    for meld in player.melds:
        if meld.tile == YAO or is_fixed(meld.tile):
            if meld.kind == 'peng':
                label = '首碰' if is_fixed(meld.tile) and meld.chicken == 8 else '碰'
            else:
                label = meld_labels[meld.kind]
            add(meld.tile, ('预冲' if meld.pre and meld.tile == YAO else '') + label,
                1, meld.chicken)
        elif not exposed_only and meld.tile == flipped:
            add(meld.tile, '副露翻鸡', 3 if meld.kind == 'peng' else 4, 1)

    if not exposed_only and flipped is not None \
            and counts_of(owned_tiles(player))[flipped] == 4:
        add(flipped, '四张额外一份', 1,
            10 if flipped == YAO else 2 if is_fixed(flipped) else 1)

    return list(terms.values())


def chicken_value(
    player: Player,
    natural_yaos: int,
    flipped: Optional[Tile],
    exposed_only: bool = False,
    value: Optional[HandValue] = None,
) -> int:
    return sum(
        item.amount for item in chicken_breakdown(
            player, natural_yaos, flipped, exposed_only, value,
        )
    )


def hu_gain(
    player: Player,
    value: HandValue,
    payers: int,
    chicken: Optional[Tile],
    responsibility: int = 0,
) -> int:
    return (
        value.h * payers
        + chicken_value(player, value.natural_yaos, chicken, False, value) * 3
        + (responsibility * 2 if value.winning_yao_natural and chicken == YAO else 0)
    )


def hu_plan_key(value: HandValue) -> str:
    return json.dumps(
        [[pattern.name for pattern in value.patterns],
         value.natural_yaos, value.winning_yao_natural],
        ensure_ascii=False, separators=(',', ':'),
    )


def legal_hu_plans(player: Player, context: WinContext) -> list[HandValue]:
    return [
        value for value in evaluate_hand(player, context)
        if context_allows(player, context, value)
    ]


def _by_pre_flip_gain(player: Player, payers: int, responsibility: int = 0):
    def compare(a: HandValue, b: HandValue) -> int:
        gain_diff = (
            hu_gain(player, b, payers, None, responsibility)
            - hu_gain(player, a, payers, None, responsibility)
        )
        return gain_diff if gain_diff else b.h - a.h
    return cmp_to_key(compare)


def offered_hu_plans(state: GameState, seat: Seat) -> list[dict]:
    window = hu_window(state, seat)
    state = window if window is not None else state
    self_turn = state.phase == 'discard' and state.current == seat
    pending = state.pending if state.phase in ('response', 'transition') else None
    if ((not self_turn
         and (pending is None or pending.from_seat == seat or seat in pending.choices))
            or not state.wall):
        return []

    tile = state.players[seat].drawn if self_turn else pending.tile  # type: ignore[union-attr]
    if tile is None:
        return []
    player = (
        state.players[seat]
        if self_turn
        else replace(state.players[seat], hand=[*state.players[seat].hand, tile])
    )
    if YAO not in player.hand:
        return []

    context = WinContext(
        kind='self' if self_turn else pending.kind,  # type: ignore[union-attr]
        tile=tile,
        is_dealer=seat == state.dealer,
        kills_report=(
            any(p.declaration != 'none' for i, p in enumerate(state.players) if i != seat)
            if self_turn
            else state.players[pending.from_seat].declaration != 'none'  # type: ignore[union-attr]
        ),
    )
    candidates = legal_hu_plans(player, context)
    if not any(value.wildcards > 0 for value in candidates):
        return []

    payers = 3 if self_turn else 1
    responsibility = (
        pending.discard.chicken
        if pending is not None and pending.discard is not None else 0
    )
    choices: dict[str, HandValue] = {}
    profiles: dict[str, list[float]] = {}
    for value in sorted(candidates, key=_by_pre_flip_gain(player, payers, responsibility)):
        key = hu_plan_key(value)
        if key not in choices:
            choices[key] = value
        previous = profiles.get(key)
        profiles[key] = [
            max(
                previous[index] if previous is not None else float('-inf'),
                hu_gain(player, value, payers, flip, responsibility),
            )
            for index, flip in enumerate([None, *TILES])
        ]

    entries = list(choices.items())
    viable: list[tuple[str, HandValue]] = []
    for index, (key, value) in enumerate(entries):
        dominated = False
        for other_index, (other, _) in enumerate(entries):
            if other == key:
                continue
            a = profiles[key]
            b = profiles[other]
            if (all(b[i] >= a[i] for i in range(len(a)))
                    and (any(b[i] > a[i] for i in range(len(a)))
                         or other_index < index)):
                dominated = True
                break
        if not dominated:
            viable.append((key, value))

    if len(viable) < 2:
        return []
    excluded = {'报听', '原报', '天胡', '地胡', '抢杠', '热炮'}
    return [
        {
            'key': key,
            'value': value,
            'label': '＋'.join(
                pattern.name for pattern in value.patterns
                if pattern.name not in excluded
            ) or '鸡胡',
        }
        for key, value in viable
    ]


def chicken_reason(
    player: Player,
    natural_yaos: int,
    flipped: Optional[Tile],
    exposed_only: bool = False,
    value: Optional[HandValue] = None,
) -> str:
    parts = []
    for item in chicken_breakdown(player, natural_yaos, flipped, exposed_only, value):
        calculation = ''
        if item.count > 1 or item.multiplier > 1:
            calculation = f'{item.count}×' if item.count > 1 else ''
            calculation += str(item.unit)
            if item.multiplier > 1:
                calculation += f'×{item.multiplier}'
            calculation += '='
        parts.append(f'{item.label} {calculation}{item.amount}分')
    return '；'.join(parts)


def next_dealer(state: GameState, result: Settlement) -> Seat:
    if result.kind == 'penalty':
        return state.dealer
    if len(result.winners) == 1:
        return result.winners[0]
    if len(result.winners) > 1:
        return result.source if result.source is not None else state.dealer
    not_ready = [seat for seat, ready in enumerate(result.ready) if not ready]
    return not_ready[0] if len(not_ready) == 1 else state.dealer


def settle(state: GameState, ending: Ending) -> Settlement:
    players = state.players
    winners = ending.winners or []
    source = ending.source

    contexts: dict[Seat, WinContext] = {}
    for winner in winners:
        if ending.win_kind is None or ending.tile is None:
            raise ValueError('INVALID_WIN_CONTEXT')
        killers = (
            [seat for seat in range(4) if seat != winner]
            if ending.win_kind == 'self'
            else ([source] if source is not None else [])
        )
        contexts[winner] = WinContext(
            kind=ending.win_kind,
            tile=ending.tile,
            is_dealer=winner == state.dealer,
            kills_report=any(
                players[seat].declaration != 'none' for seat in killers
                if seat is not None
            ),
        )

    declared_plans: dict[Seat, str] = {}
    for winner in winners:
        options = sorted(
            legal_hu_plans(players[winner], contexts[winner]),
            key=_by_pre_flip_gain(
                players[winner],
                3 if ending.win_kind == 'self' else 1,
                ending.responsibility,
            ),
        )
        selected = ending.hu_plans.get(winner)
        if selected is None:
            selected = hu_plan_key(options[0]) if options else None
        if selected is None or not any(hu_plan_key(value) == selected for value in options):
            raise ValueError('胡牌前确认的牌型不合法')
        declared_plans[winner] = selected

    indicator = state.wall.pop(0) if ending.kind == 'win' and state.wall else None
    chicken = next_tile(indicator) if indicator is not None else None
    info = [ready_info(player) for player in players]
    ready = [bool(info[seat]['ready'] or seat in winners) for seat in range(4)]
    values: dict[Seat, HandValue] = {}
    transfers, add = ledger(state.dice)

    def others(seat: Seat) -> list[Seat]:
        return [index for index in range(4) if index != seat]

    followed = next((d for d in players[state.dealer].river if d.followed_dealer), None)
    if players[state.dealer].follow_dealer and followed is not None:
        for to in others(state.dealer):
            add(state.dealer, to, '跟庄赔付', follow_dealer_base(followed.tile),
                f'首圈跟庄·{tile_name(followed.tile)}（跟牌不再计分）')

    if ending.kind == 'penalty':
        base = max([45, *[item['price'] for item in info if item['ready']]])
        offender = ending.offender
        if offender is not None:
            for to in others(offender):
                add(offender, to, '一般包牌赔付', base, ending.reason or '违反行为规则')
    else:
        for winner in winners:
            all_values = legal_hu_plans(
                players[winner],
                replace(contexts[winner], chicken=chicken),
            )
            matching = [
                value for value in all_values
                if hu_plan_key(value) == declared_plans[winner]
            ]
            candidates = matching if matching else all_values
            payers = others(winner) if ending.win_kind == 'self' else (
                [source] if source is not None else []
            )

            def gain(value: HandValue) -> int:
                return hu_gain(players[winner], value, len(payers),
                               chicken, ending.responsibility)

            def compare(a: HandValue, b: HandValue) -> int:
                gain_diff = gain(b) - gain(a)
                return gain_diff if gain_diff else b.h - a.h

            ordered = sorted(candidates, key=cmp_to_key(compare))
            if not ordered:
                raise ValueError('结算收到不合法的胡牌方案')
            value = ordered[0]
            values[winner] = value
            info[winner]['natural'] = value.natural_yaos

            for from_seat in payers:
                add(
                    from_seat, winner, '胡牌分', value.h,
                    '＋'.join(pattern.name for pattern in value.patterns) or '鸡胡',
                )
                if players[from_seat].declaration != 'none':
                    add(
                        from_seat, winner, '杀报加分',
                        40 if players[from_seat].declaration == 'original' else 20,
                        '杀原报' if players[from_seat].declaration == 'original' else '杀报听',
                    )
            if ending.tile == YAO and source is not None and ending.responsibility:
                add(
                    source, winner, '幺筒责任分',
                    ending.responsibility * (
                        3 if value.winning_yao_natural and chicken == YAO else 1
                    ),
                    '胡入幺筒作本牌' if value.winning_yao_natural else '胡入幺筒作癞子',
                )

        exposed = [chicken_value(player, 0, chicken, True) for player in players]
        if ending.kind == 'draw':
            for from_seat in range(4):
                if ready[from_seat]:
                    continue
                for to in range(4):
                    if not ready[to]:
                        continue
                    add(from_seat, to, '流局赔听款', info[to]['price'],
                        '赔听牌型价（不加基础5）')
                    add(
                        from_seat, to, '鸡分', exposed[from_seat],
                        f'未听赔露出鸡：{chicken_reason(players[from_seat], 0, chicken, True)}',
                    )
        else:
            collected = [
                0 if player.burned else chicken_value(
                    player, info[index]['natural'], chicken, False, values.get(index),
                )
                for index, player in enumerate(players)
            ]
            for owner in range(4):
                for other in others(owner):
                    if ready[owner]:
                        add(
                            other, owner, '鸡分', collected[owner],
                            chicken_reason(
                                players[owner], info[owner]['natural'],
                                chicken, False, values.get(owner),
                            ),
                        )
                    elif ready[other]:
                        add(
                            owner, other, '鸡分', exposed[owner],
                            f'未听赔露出鸡：{chicken_reason(players[owner], 0, chicken, True)}',
                        )

    for owner in range(4):
        player = players[owner]
        for meld in player.melds:
            if (ending.kind == 'win' and meld.responsibility
                    and meld.from_seat is not None):
                amount = meld.responsibility * (3 if chicken == YAO else 1)
                if ready[owner]:
                    add(meld.from_seat, owner, '幺筒责任分', amount, '碰杠责任继承')
                elif not ready[owner] and ready[meld.from_seat]:
                    add(owner, meld.from_seat, '幺筒责任分', amount, '未听，反赔对应责任家')

            if meld.kind in ('peng', 'stored'):
                continue
            sources = [meld.from_seat] if meld.kind == 'open' else others(owner)
            for from_seat in sources:
                if from_seat is None:
                    continue
                if not ready[owner] and not ready[from_seat]:
                    continue
                from_who = from_seat if ready[owner] else owner
                to_who = owner if ready[owner] else from_seat
                label = '' if ready[owner] else '未听反赔：'
                kong_label = {'open': '明杠', 'turn': '转弯杠', 'added': '补杠'}[meld.kind]
                add(
                    from_who, to_who, '杠分',
                    20 if meld.tile == YAO else 5,
                    f'{label}{tile_name(meld.tile)}{kong_label}',
                )

        if counts_of(owned_tiles(player))[YAO] == 4:
            bao = (
                owner in winners
                and source is not None
                and ending.tile == YAO
                and any(meld.tile == YAO and meld.kind == 'peng' for meld in player.melds)
            )
            if bao:
                add(source, owner, '见面礼', 300, '放炮家包三份见面礼')
            else:
                for from_seat in others(owner):
                    add(from_seat, owner, '见面礼', 100, '结算仍归本人四张幺筒')

    deltas = [0, 0, 0, 0]
    for transfer in transfers:
        deltas[transfer.from_seat] -= transfer.amount
        deltas[transfer.to] += transfer.amount

    result = Settlement(
        kind=ending.kind,
        winners=winners,
        source=source,
        reason=ending.reason if ending.reason is not None else (
            '牌墙摸空，荒庄赔听' if ending.kind == 'draw' else '胡牌结算'
        ),
        deltas=deltas,
        next_dealer=state.dealer,
        finished=False,
        win_kind=ending.win_kind,
        tile=ending.tile,
        offender=ending.offender,
        indicator=indicator,
        chicken=chicken,
        values=values,
        ready=ready,
        transfers=transfers,
    )
    result.next_dealer = next_dealer(state, result)
    result.finished = (
        result.next_dealer != state.dealer
        and state.dealer_round == state.match_length
    )
    return result
