"""幺筒牌与牌墙 —— 对应 src/game/variants/yaotong/tiles.ts。

牌用 0..26 的整数表示：0..8 万、9..17 筒、18..26 条。
"""

from __future__ import annotations

import random as random_module
from typing import Callable, Iterable

# 幺筒：9 = 一筒
YAO = 9
# 固定鸡：一筒、八筒、一条
FIXED = (0, 16, 18)
# 27 种序数牌，每种 4 张 = 108 张
TILES = tuple(range(27))
WALL_SIZE = 108
# 拆墙起点：四方牌墙的墩起点（按座位顺序）
WALL_SIDE_STARTS = (0, 28, 54, 82)

SUIT_LETTERS = ('m', 'p', 's')
SUIT_NAMES = ('万', '筒', '条')
RANK_NAMES = '一二三四五六七八九'

RandomFn = Callable[[], float]


def is_tile(tile: object) -> bool:
    return isinstance(tile, int) and not isinstance(tile, bool) and 0 <= tile < 27


def is_fixed(tile: int) -> bool:
    return tile in FIXED


def is_chicken(tile: int) -> bool:
    return tile == YAO or is_fixed(tile)


def tile_name(tile: int) -> str:
    if not is_tile(tile):
        raise ValueError(f'INVALID_TILE:{tile}')
    return RANK_NAMES[tile % 9] + SUIT_NAMES[tile // 9]


def next_tile(tile: int) -> int:
    """同花色内循环：九万 → 一万，九筒 → 一筒，九条 → 一条。"""
    return (tile // 9) * 9 + ((tile + 1) % 9)


def tile_file(tile: int) -> str:
    return f'{tile % 9 + 1}{SUIT_LETTERS[tile // 9]}.png'


def sort_hand(hand: list[int]) -> list[int]:
    """与 TS 的 sortHand 一样就地升序排序并返回同一个列表。"""
    hand.sort()
    return hand


def counts_of(hand: Iterable[int]) -> list[int]:
    counts = [0] * 27
    for tile in hand:
        counts[tile] += 1
    return counts


def shuffled_wall(rng: RandomFn | None = None) -> list[int]:
    """Fisher-Yates，与 TS 的 shuffledWall 完全同序。"""
    random_fn = rng or random_module.random
    wall = [tile for tile in TILES for _ in range(4)]
    for index in range(len(wall) - 1, 0, -1):
        swap = int(random_fn() * (index + 1))
        wall[index], wall[swap] = wall[swap], wall[index]
    return wall


def break_wall(wall: list[int], dice: tuple[int, int] | list[int],
               dealer: int) -> dict:
    """按骰子/庄家拆墙，返回旋转后的牌墙、方位和保留墩数。

    口径与 TS 一致：side 是相对庄家的方位，reserved 是较小骰点。
    """
    side = (dealer + ((dice[0] + dice[1] - 1) % 4)) % 4
    reserved = min(dice)
    offset = (WALL_SIDE_STARTS[side] + reserved * 2) % WALL_SIZE
    return {
        'wall': [*wall[offset:], *wall[:offset]],
        'side': side,
        'reserved': reserved,
    }


# ── 协议互转：引擎内部 int ↔ 前端 TileType（m1/p1/s1）──────────

def to_tile_type(tile: int) -> str:
    if not is_tile(tile):
        raise ValueError(f'INVALID_TILE:{tile}')
    return f'{SUIT_LETTERS[tile // 9]}{tile % 9 + 1}'


def from_tile_type(tile: str) -> int:
    if not isinstance(tile, str) or len(tile) != 2:
        raise ValueError(f'INVALID_TILE_TYPE:{tile!r}')
    suit = SUIT_LETTERS.index(tile[0]) if tile[0] in SUIT_LETTERS else -1
    rank = int(tile[1]) if tile[1].isdigit() else 0
    if suit < 0 or not 1 <= rank <= 9:
        raise ValueError(f'INVALID_TILE_TYPE:{tile!r}')
    return suit * 9 + rank - 1
