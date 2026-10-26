"""幺筒可胡形、听牌与过手限制 —— 对应 src/game/variants/yaotong/hands.ts。

纯函数；牌用 0..26 整数。形状缓存跨调用复用，但调用方不得修改返回的 groups。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Iterable, Optional

from app.core.yaotong.tiles import TILES, YAO, counts_of, is_chicken
from app.core.yaotong.types import (
    HandValue,
    Pattern,
    Player,
    Tile,
    Wait,
    WinContext,
)


@dataclass
class _Shape:
    suits: int
    pungs: bool
    dragons: int
    groups: list[list[Tile]]


def _suit_bit(tile: Tile) -> int:
    return 1 << (tile // 9)


def _one_suit(bits: int) -> bool:
    return bits != 0 and (bits & (bits - 1)) == 0


_SHAPE_CACHE: dict[str, list[_Shape]] = {}
_WAIT_CACHE: dict[tuple, list[Wait]] = {}


def is_untouched_first_draw(player: Player) -> bool:
    """首摸且未动过手：既用于首报资格，也用于天胡/地胡。"""
    return (
        player.draws == 1
        and player.drawn is not None
        and player.discards == 0
        and not player.melds
        and not player.after_kong
    )


def _unique(shapes: list[_Shape]) -> list[_Shape]:
    result: dict[tuple, _Shape] = {}
    for shape in shapes:
        flat = [tile for group in shape.groups for tile in group]
        key = (shape.suits, shape.pungs, shape.dragons, tuple(counts_of(flat)))
        result[key] = shape
    return list(result.values())


def _standard(counts: list[int], wild: int, melds: int, pair: bool) -> list[_Shape]:
    key = f"{''.join(str(count) for count in counts)}:{wild}:{melds}:{int(pair)}"
    cached = _SHAPE_CACHE.get(key)
    if cached is not None:
        return cached

    first = next((index for index, count in enumerate(counts) if count > 0), -1)
    if first < 0:
        if wild != melds * 3 + (2 if pair else 0):
            result: list[_Shape] = []
        elif wild == 0:
            result = [_Shape(suits=0, pungs=True, dragons=0, groups=[])]
        else:
            result = []
            for tile in TILES:
                groups = [[tile, tile, tile] for _ in range(melds)]
                if pair:
                    groups.append([tile, tile])
                result.append(_Shape(
                    suits=_suit_bit(tile), pungs=True, dragons=0, groups=groups,
                ))
    else:
        result = []

        def append(group: list[Tile], consumed: list[Tile], spent: int,
                   is_pair: bool, pung: bool) -> None:
            if spent > wild:
                return
            remaining = list(counts)
            for tile in consumed:
                remaining[tile] -= 1
            tails = _standard(
                remaining,
                wild - spent,
                melds - (0 if is_pair else 1),
                pair and not is_pair,
            )
            for tail in tails:
                result.append(_Shape(
                    suits=tail.suits | _suit_bit(first),
                    pungs=tail.pungs and pung,
                    dragons=0,
                    groups=[group, *tail.groups],
                ))

        if pair:
            for used in range(1, min(2, counts[first]) + 1):
                append([first, first], [first] * used, 2 - used, True, True)

        if melds > 0:
            for used in range(1, min(3, counts[first]) + 1):
                append([first, first, first], [first] * used, 3 - used, False, True)

            base = (first // 9) * 9
            for start in range(max(base, first - 2), min(first, base + 6) + 1):
                group = [start, start + 1, start + 2]
                others = [tile for tile in group if tile != first]
                # 即使自然牌够，也可能需要把某张当成万能牌，才能枚举出七对/顺子替代解。
                for mask in range(4):
                    consumed = [first]
                    spent = 0
                    possible = True
                    for index, tile in enumerate(others):
                        if mask & (1 << index):
                            spent += 1
                        elif counts[tile]:
                            consumed.append(tile)
                        else:
                            possible = False
                            break
                    if possible:
                        append(group, consumed, spent, False, False)

        result = _unique(result)

    if len(_SHAPE_CACHE) > 35_000:
        _SHAPE_CACHE.clear()
    _SHAPE_CACHE[key] = result
    return result


def _pairs(counts: list[int], wild: int) -> list[_Shape]:
    needed = 0
    groups: list[list[Tile]] = []
    for tile in TILES:
        if not counts[tile]:
            continue
        needed += counts[tile] % 2
        for _ in range((counts[tile] + 1) // 2):
            groups.append([tile, tile])

    if needed > wild or (wild - needed) % 2:
        return []
    extra = (wild - needed) // 2
    result: list[_Shape] = []

    def complete(current: list[list[Tile]], left: int, start: int = 0) -> None:
        flat = [tile for group in current for tile in group]
        resolved = counts_of(flat)
        if not left:
            suits = 0
            for group in current:
                suits |= _suit_bit(group[0])
            result.append(_Shape(
                suits=suits,
                pungs=False,
                dragons=sum(1 for count in resolved if count == 4),
                groups=current,
            ))
            return
        for tile in range(start, 27):
            if resolved[tile] <= 2:
                complete([*current, [tile, tile]], left - 1, tile)

    complete(groups, extra)
    return result


def _meld_tiles(player: Player) -> list[Tile]:
    tiles: list[Tile] = []
    for meld in player.melds:
        tiles.extend([meld.tile] * (4 if meld.kind != 'peng' else 3))
    return tiles


def _structural_values(player: Player, ctx: WinContext) -> list[HandValue]:
    hand = player.hand
    if len(hand) != 14 - len(player.melds) * 3:
        return []
    if any(not isinstance(tile, int) or isinstance(tile, bool) or tile < 0 or tile > 26
           for tile in hand):
        return []

    raw = counts_of(hand)
    physical = counts_of([*hand, *_meld_tiles(player)])
    if any(count > 4 for count in physical):
        return []

    yao_count = raw[YAO]
    exposed_suits = 0
    for meld in player.melds:
        exposed_suits |= _suit_bit(meld.tile)

    result: list[HandValue] = []

    def add(shape: _Shape, natural: int, wildcards: int,
            structure: str, forced_winning_natural: Optional[bool] = None) -> None:
        patterns: list[Pattern] = []
        if structure == 'pairs':
            dragons = min(shape.dragons, 3)
            patterns.append(Pattern(
                ['七小对', '龙七对', '双龙七', '三龙七'][dragons],
                [20, 40, 80, 120][dragons],
            ))
        elif structure == 'earth':
            dragons = min(shape.dragons, 2)
            patterns.append(Pattern(
                ['地龙', '双地龙', '三地龙'][dragons],
                [40, 80, 120][dragons],
            ))
        else:
            if shape.pungs:
                patterns.append(Pattern('碰碰胡', 10))
            if len(player.melds) == 4:
                patterns.append(Pattern('金钩钓', 20))

        if _one_suit(shape.suits | exposed_suits):
            patterns.append(Pattern('清一色', 20))

        # big 只统计牌型/清一色：硬胡、报听、抢杠、天胡等场景番不参与「小胡不能点炮」判定。
        big = len(patterns) > 0
        if wildcards == 0:
            patterns.append(Pattern('硬胡', 10))
        if player.declaration != 'none':
            patterns.append(Pattern(
                '原报' if player.declaration == 'original' else '报听',
                40 if player.declaration == 'original' else 20,
            ))
        if ctx.kind in ('rob', 'hot'):
            patterns.append(Pattern('抢杠' if ctx.kind == 'rob' else '热炮', 20))
        if ctx.kind == 'self' and ctx.is_dealer is not None and is_untouched_first_draw(player):
            patterns.append(Pattern('天胡' if ctx.is_dealer else '地胡', 40))

        has_kong = any(meld.kind != 'peng' for meld in player.melds)
        if (ctx.kind == 'discard' and not big and not has_kong and not player.follow_dealer
                and player.declaration == 'none'):
            return

        pattern_points = sum(pattern.points for pattern in patterns)
        if ctx.tile != YAO:
            uses = [False]
        elif forced_winning_natural is not None:
            uses = [forced_winning_natural]
        else:
            uses = ([True] if natural > 0 else []) + ([False] if wildcards > 0 else [])

        resolved_hand = [tile for group in shape.groups for tile in group]
        if structure == 'earth':
            resolved_hand.append(player.melds[0].tile)

        for winning_yao_natural in uses:
            value = HandValue(
                h=5 + pattern_points,
                pattern_points=pattern_points,
                patterns=patterns,
                natural_yaos=natural,
                winning_yao_natural=winning_yao_natural,
                wildcards=wildcards,
                big=big,
                structure=structure,  # type: ignore[arg-type]
                groups=shape.groups,
                resolved_hand=resolved_hand,
            )
            result.append(value)

            used = [*hand, *[meld.tile for meld in player.melds for _ in range(3)]]
            if (ctx.kind == 'self' and not player.melds and not has_kong
                    and not ctx.kills_report
                    and not any(is_chicken(tile) or tile == ctx.chicken for tile in used)):
                result.append(replace(
                    value,
                    h=25,
                    pattern_points=20,
                    patterns=[Pattern('素胡', 20)],
                ))

    for natural in range(yao_count + 1):
        counts = list(raw)
        counts[YAO] = natural
        wild = yao_count - natural
        for shape in _standard(counts, wild, 4 - len(player.melds), True):
            add(shape, natural, wild, 'standard')
        if not player.melds:
            for shape in _pairs(counts, wild):
                add(shape, natural, wild, 'pairs')

    # 地龙：仅一副碰；胡入张接到该碰上，剩余十张拆作五对。
    if (len(player.melds) == 1 and player.melds[0].kind == 'peng'
            and (ctx.tile == player.melds[0].tile or ctx.tile == YAO)):
        remaining = list(raw)
        if remaining[ctx.tile] > 0:
            remaining[ctx.tile] -= 1
            winning_wild = ctx.tile == YAO and player.melds[0].tile != YAO
            for natural in range(remaining[YAO] + 1):
                counts = list(remaining)
                counts[YAO] = natural
                wild = remaining[YAO] - natural
                for shape in _pairs(counts, wild):
                    add(
                        shape,
                        natural + (1 if ctx.tile == YAO and not winning_wild else 0),
                        wild + (1 if winning_wild else 0),
                        'earth',
                        ctx.tile == YAO and not winning_wild,
                    )

    dedup: dict[tuple, HandValue] = {}
    for value in result:
        key = (
            value.h,
            value.natural_yaos,
            value.winning_yao_natural,
            tuple(pattern.name for pattern in value.patterns),
            tuple(counts_of(value.resolved_hand or [])),
        )
        dedup[key] = value
    return sorted(dedup.values(), key=lambda value: (-value.h, -value.natural_yaos))


evaluate_hand = _structural_values


def waits(player: Player) -> list[Wait]:
    if len(player.hand) != 13 - len(player.melds) * 3:
        return []
    key = (
        tuple(sorted(player.hand)),
        tuple((meld.tile, meld.kind) for meld in player.melds),
        player.declaration,
    )
    cached = _WAIT_CACHE.get(key)
    if cached is not None:
        return cached

    physical = counts_of([*player.hand, *_meld_tiles(player)])
    found: list[Wait] = []
    for tile in TILES:
        if physical[tile] >= 4:
            continue
        values = evaluate_hand(
            replace(player, hand=[*player.hand, tile]),
            WinContext(kind='ready', tile=tile),
        )
        if values:
            found.append(Wait(tile=tile, values=values))

    if len(_WAIT_CACHE) > 5_000:
        _WAIT_CACHE.clear()
    _WAIT_CACHE[key] = found
    return found


def discard_waits(player: Player) -> list[dict]:
    if len(player.hand) != 14 - len(player.melds) * 3:
        return []
    result: list[dict] = []
    for index in range(len(player.hand)):
        remaining = [*player.hand[:index], *player.hand[index + 1:]]
        tile_waits = waits(replace(player, hand=remaining))
        if tile_waits:
            result.append({'index': index, 'waits': tile_waits})
    return result


_SCENE_PATTERNS = frozenset({
    '抢杠', '热炮', '天胡', '地胡', '天胡／地胡', '报听', '原报', '素胡',
})


def shape_points(value: HandValue) -> int:
    return sum(
        pattern.points for pattern in value.patterns
        if pattern.name not in _SCENE_PATTERNS
    )


def pass_allows(player: Player, h: int, value: Optional[HandValue] = None) -> bool:
    if player.passed_hu is None:
        return True
    return (
        h >= 45
        and h > player.passed_hu
        and (player.passed_shape is None
             or (value is not None and shape_points(value) > player.passed_shape))
    )


def context_allows(player: Player, context: WinContext, value: HandValue) -> bool:
    """抢杠/热炮不受普通过手限制。"""
    return context.kind in ('rob', 'hot') or pass_allows(player, value.h, value)


def hu_opportunity(player: Player, context: WinContext) -> dict:
    values = evaluate_hand(player, context)
    legal = [value for value in values if context_allows(player, context, value)]
    pool = legal or values
    return {
        'hu': bool(values),
        'restricted': bool(values) and not legal,
        'h': max([0, *[value.h for value in pool]]),
        'shape_points': max([0, *[shape_points(value) for value in legal]]),
    }
