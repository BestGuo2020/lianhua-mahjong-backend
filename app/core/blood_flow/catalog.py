"""血流番型匹配 —— 对应 patterns/catalog.ts。"""

from app.models.game import TileType

from .types import WinningDecomposition

WINDS: tuple[TileType, ...] = ('east', 'south', 'west', 'north')
DRAGONS: tuple[TileType, ...] = ('red', 'green', 'white')


def is_honor(tile: TileType) -> bool:
    return len(tile) != 2


def is_terminal(tile: TileType) -> bool:
    return len(tile) == 2 and tile[1] in ('1', '9')


def has_consecutive_run(numbers: list[int], length: int) -> bool:
    """一组数字里是否存在 length 个连续整数（用于一色三步高/四步高、节高系列）。"""
    unique = sorted(set(numbers))
    streak = 1 if unique else 0
    for index in range(1, len(unique)):
        streak = streak + 1 if unique[index] == unique[index - 1] + 1 else 1
        if streak >= length:
            return True
    return streak >= length


def _by_suit(groups) -> dict[str, list[int]]:
    """同花色分组：{花色: [每组首张的数字]}（字牌跳过，与 TS bySuit 同义）。"""
    result: dict[str, list[int]] = {}
    for group in groups:
        tile = group.tiles[0]
        if is_honor(tile):
            continue
        result.setdefault(tile[0], []).append(int(tile[1]))
    return result


def match_patterns(hand: WinningDecomposition) -> list[str]:
    if hand.shape not in ('standard', 'sevenPairs'):
        return [hand.shape]
    result: list[str] = ['sevenPairs'] if hand.shape == 'sevenPairs' else []
    tiles = [t for g in hand.groups for t in g.tiles]
    suits = {t[0] for t in tiles if not is_honor(t)}
    honors = any(is_honor(t) for t in tiles)
    # 豪华七对：七对里含"四张相同"。2026-09-12 用户定案：**允许精牌替补**凑成那四张
    # （tiles 是 represented 牌面，精牌顶替后计入），因此不再要求整手全自然（hand.natural）。
    # 好处：不必真的摸到 4 张实体同牌，也不必为了它放弃开杠——豪华七对因此可达。
    if hand.shape == 'sevenPairs' and any(tiles.count(t) >= 4 for t in tiles):
        result.append('luxury-seven-pairs')
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
    sequences = [g for g in melds if g.kind == 'sequence']
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
    # —— 2026-09-12 第二版番种表新增 ——
    # 断幺九：全部为 2~8 数牌。
    if all(not is_honor(t) and not is_terminal(t) for t in tiles):
        result.append('all-simples')
    # 全带幺：每副面子与将牌都含幺九或字牌（允许 123 / 789 这类含幺的顺子）。
    if all(any(is_honor(t) or is_terminal(t) for t in g.tiles) for g in hand.groups):
        result.append('all-with-terminals')
    # 一色步高 / 清龙：同花色顺子的起始数字关系。
    for starts in _by_suit(sequences).values():
        if has_consecutive_run(starts, 4):
            result.append('one-suit-four-steps')
        elif has_consecutive_run(starts, 3):
            result.append('one-suit-three-steps')
        if all(start in starts for start in (1, 4, 7)):
            result.append('pure-straight')
    # 一色节高：同花色刻子/杠的数字连续。
    for numbers in _by_suit(triplets).values():
        if has_consecutive_run(numbers, 4):
            result.append('one-suit-four-joints')
        elif has_consecutive_run(numbers, 3):
            result.append('one-suit-three-joints')
    # 门清平胡是**兜底本体**（方案B，2026-09-12 用户定案）：标准四面子一将、未副露、且不满足任何其他番种时，
    # 取代鸡胡作为兜底；**不与任何主体番种叠加**。七对/十三幺/十三烂/七星等特殊结构在函数开头已提前返回。
    if result:
        return result
    concealed_hand = all(g.origin.get('kind') == 'hand' for g in hand.groups)
    return ['concealed-hand'] if concealed_hand else ['pinghu']
