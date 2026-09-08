"""血流番型匹配 —— 对应 patterns/catalog.ts。"""

from app.models.game import TileType

from .types import WinningDecomposition

WINDS: tuple[TileType, ...] = ('east', 'south', 'west', 'north')
DRAGONS: tuple[TileType, ...] = ('red', 'green', 'white')


def is_honor(tile: TileType) -> bool:
    return len(tile) != 2


def is_terminal(tile: TileType) -> bool:
    return len(tile) == 2 and tile[1] in ('1', '9')


def match_patterns(hand: WinningDecomposition) -> list[str]:
    if hand.shape not in ('standard', 'sevenPairs'):
        return [hand.shape]
    result: list[str] = ['sevenPairs'] if hand.shape == 'sevenPairs' else []
    tiles = [t for g in hand.groups for t in g.tiles]
    suits = {t[0] for t in tiles if not is_honor(t)}
    honors = any(is_honor(t) for t in tiles)
    if len(suits) == 1:
        result.append('mixed-suit' if honors else 'pure-suit')
    if all(is_honor(t) for t in tiles):
        result.append('all-honors')
    if all(t in ('s2', 's3', 's4', 's6', 's8', 'green') for t in tiles):
        result.append('all-green')
    if hand.shape == 'sevenPairs':
        return result
    melds = [g for g in hand.groups if g.kind != 'pair']
    pair = next(g for g in hand.groups if g.kind == 'pair').tiles[0]
    triplets = [g for g in melds if g.kind in ('triplet', 'kong')]
    all_triplets = len(triplets) == 4
    if all_triplets:
        result.append('all-triplets')
    dragon_count = sum(1 for t in DRAGONS if any(g.tiles[0] == t for g in triplets))
    wind_count = sum(1 for t in WINDS if any(g.tiles[0] == t for g in triplets))
    if dragon_count == 3:
        result.append('big-three-dragons')
    if dragon_count == 2 and pair in DRAGONS and not any(g.tiles[0] == pair for g in triplets):
        result.append('little-three-dragons')
    if wind_count == 4:
        result.append('big-four-winds')
    if wind_count == 3 and pair in WINDS and not any(g.tiles[0] == pair for g in triplets):
        result.append('little-four-winds')
    if all_triplets and all(is_terminal(t) for t in tiles):
        result.append('pure-terminals')
    if all_triplets and honors and len(suits) > 0 and all(is_honor(t) or is_terminal(t) for t in tiles):
        result.append('mixed-terminals')
    concealed = sum(1 for g in triplets if g.concealed)
    if concealed >= 3:
        result.append('three-concealed-triplets')
    if concealed == 4:
        result.append('four-concealed-triplets')
    kongs = sum(1 for g in melds if g.kind == 'kong')
    if kongs >= 3:
        result.append('three-kongs')
    if kongs == 4:
        result.append('four-kongs')
    if len(suits) == 1 and not honors and all(g.origin.get('kind') == 'hand' for g in hand.groups):
        counts = [sum(1 for t in tiles if len(t) == 2 and int(t[1]) == n) for n in range(1, 10)]
        if all(count >= (3 if i in (0, 8) else 1) for i, count in enumerate(counts)):
            result.append('nine-gates')
    return result if result else ['pinghu']
