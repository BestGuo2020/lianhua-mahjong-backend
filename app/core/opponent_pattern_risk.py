"""对手牌型（大牌）风险的公共信息估算 —— 档位版。

前端同源实现见 ``src/game/shared/ai/opponentPatternRisk.ts``；本文件是逐位等价的 Python 镜像，
数值、档位规则与信号中文文案（会进入 prompt）必须与 TS 保持一致。

目的：把「点炮给在做大牌的对手」从「与番型无关的常数价格」改成按公共证据分档的价格。
只用公共牌：对手牌河、副露明细、已胡次数 / 锁手、墙余、公开牌池。
严禁读取对手暗手：本模块不接收、不推断任何未公开手牌（bloodFlowSeatView 只向本家暴露 hand）。
纯函数、确定性、无 IO；未提供任何信号时结果与旧口径逐位一致（见 opponent_pattern_exposure）。
"""

from dataclasses import dataclass, field
import re
from typing import Callable, Optional, Sequence

OpponentRiskTier = int          # 0 | 1 | 2 | 3
SuitKey = str                   # 'm' | 'p' | 's'
Tile = str                      # 内部牌面（'m1' / 'east' / 'red' / 'white' …）

_SUIT_TILE = re.compile(r'^([mps])[1-9]$')
_DRAGONS: tuple[Tile, ...] = ('red', 'green', 'white')
_WINDS: tuple[Tile, ...] = ('east', 'south', 'west', 'north')
_SUIT_ORDER: tuple[SuitKey, ...] = ('m', 'p', 's')
_SUIT_LABELS: dict[SuitKey, str] = {'m': '万', 'p': '筒', 's': '条'}


@dataclass(frozen=True)
class OpponentRiskTuning:
    """风险模块调参（默认值 = 前端 OPPONENT_RISK）。"""

    # 档位倍率：1 = 平胡量级，4/16/32 ≈ 混一色 / 清一色 / 十六倍级硬胡点炮。
    factor_tier1: float = 4
    factor_tier2: float = 16
    factor_tier3: float = 32
    # 染手（花色集中）嫌疑对手：非嫌疑花色牌的系数。
    off_suit_factor: float = 0.5
    # 与既有 safetyCostLadder 一致的基准单价（点）。
    exposure_unit: float = 40
    # 公开张数档位（0 张 / 1 张 / ≥2 张）。
    safety_cost_none: float = 0.25
    safety_cost_one: float = 0.1
    safety_cost_safe: float = 0
    # 锁手家的倍率档（已胡仍在听，现物不再享受折扣）。
    locked_tier: OpponentRiskTier = 2
    # 残局墙余阈值（沿用 BLOOD_FLOW_AI.lateGameWallCount）。
    late_game_wall_count: int = 15
    # 残局提速墙余阈值（原 estimateOpponentThreat 的 24）。
    late_threat_wall_count: int = 24
    # 档位 → 中文标签（1/2/3）。
    tier_labels: dict[int, str] = field(
        default_factory=lambda: {1: '低', 2: '中', 3: '高'})


OPPONENT_RISK = OpponentRiskTuning()

# 调参字段名（tuning_of 用；与前端 OpponentRiskTuning 一一对应）。
TUNING_FIELDS: tuple[str, ...] = (
    'factor_tier1', 'factor_tier2', 'factor_tier3', 'off_suit_factor', 'exposure_unit',
    'safety_cost_none', 'safety_cost_one', 'safety_cost_safe', 'locked_tier',
    'late_game_wall_count', 'late_threat_wall_count', 'tier_labels',
)

RISK_TIER_LABELS: dict[int, str] = {1: '低', 2: '中', 3: '高'}


def tuning_of(partial=None) -> OpponentRiskTuning:
    """部分覆盖 → 完整调参表（前端 ``tuningOf``）。"""
    if not partial:
        return OPPONENT_RISK
    if isinstance(partial, OpponentRiskTuning):
        return partial
    values = {**{name: getattr(OPPONENT_RISK, name) for name in TUNING_FIELDS}, **partial}
    return OpponentRiskTuning(**values)


def suit_of_tile(tile: Tile) -> Optional[SuitKey]:
    matched = _SUIT_TILE.match(tile) if tile else None
    return matched.group(1) if matched else None


def _factor_for(tier: OpponentRiskTier, tuning: OpponentRiskTuning) -> float:
    if tier == 3:
        return tuning.factor_tier3
    if tier == 2:
        return tuning.factor_tier2
    if tier == 1:
        return tuning.factor_tier1
    return 1


def _meld_attr(meld, name: str, default=None):
    if isinstance(meld, dict):
        return meld.get(name, default)
    return getattr(meld, name, default)


@dataclass
class MeldFacts:
    groups: int
    suit_counts: dict[SuitKey, int]
    dominant_suit: Optional[SuitKey]
    max_share: float
    dragon_groups: int
    wind_groups: int
    honor_groups: int


def meld_facts(melds: Optional[Sequence]) -> MeldFacts:
    """副露明细统计：组数、花色集中度、箭/风/字成组数（只读公开副露）。"""
    suit_counts: dict[SuitKey, int] = {}
    groups = dragon_groups = wind_groups = honor_groups = 0
    for meld in melds or []:
        if _meld_attr(meld, 'type') == 'flower':
            continue
        tiles = list(_meld_attr(meld, 'tiles', None) or [])
        groups += 1
        tile = _meld_attr(meld, 'tile', None)
        if not tile and tiles:
            tile = tiles[0]
        if tile in _DRAGONS:
            dragon_groups += 1
            honor_groups += 1
            continue
        if tile in _WINDS:
            wind_groups += 1
            honor_groups += 1
            continue
        for item in tiles:
            suit = suit_of_tile(item)
            if suit:
                suit_counts[suit] = suit_counts.get(suit, 0) + 1
    suited_total = sum(suit_counts.values())
    dominant_suit: Optional[SuitKey] = None
    max_count = 0
    for suit, count in suit_counts.items():
        if count > max_count:
            max_count = count
            dominant_suit = suit
    return MeldFacts(
        groups=groups, suit_counts=suit_counts, dominant_suit=dominant_suit,
        max_share=(max_count / suited_total) if suited_total else 0,
        dragon_groups=dragon_groups, wind_groups=wind_groups, honor_groups=honor_groups,
    )


def _suit_discard_counts(discards: Sequence[Tile]) -> dict[SuitKey, int]:
    counts: dict[SuitKey, int] = {}
    for tile in discards or []:
        suit = suit_of_tile(tile)
        if suit:
            counts[suit] = counts.get(suit, 0) + 1
    return counts


def _weakest_suit(counts: dict[SuitKey, int]) -> Optional[SuitKey]:
    weakest: Optional[SuitKey] = None
    least = float('inf')
    for suit in _SUIT_ORDER:
        count = counts.get(suit, 0)
        if count < least:
            least = count
            weakest = suit
    return weakest


@dataclass
class OpponentRiskProfile:
    """逐家风险档。``index`` 为传入数组下标；调用方负责映射到座位 / 相对方位。"""
    index: int
    tier: OpponentRiskTier
    factor: float
    signals: list[str]
    suspect_suit: Optional[SuitKey]
    locked: bool


def opponent_risk_profiles(opponents: Optional[Sequence[dict]] = None,
                           wall_count: Optional[int] = None,
                           tuning=None) -> list[OpponentRiskProfile]:
    """逐家估算对手牌型风险档。所有信号都来自公共牌；``tier`` 取各项信号的最大值。

    档位含义：1 = 弱信号（门清染手 / 残局快听 / 半染手），2 = 染手或三副露或已锁手仍在听，
    3 = 三元 / 四喜系或十六倍级嫌疑。
    """
    resolved = tuning_of(tuning)
    wall = 99 if wall_count is None else wall_count
    profiles: list[OpponentRiskProfile] = []
    for index, opponent in enumerate(opponents or []):
        facts = meld_facts(_meld_attr(opponent, 'melds', None))
        discards = list(_meld_attr(opponent, 'discards', None) or [])
        signals: list[str] = []
        tier = 0
        suspect_suit: Optional[SuitKey] = None

        def raise_tier(next_tier: int, signal: Optional[str] = None) -> None:
            nonlocal tier
            if next_tier > tier:
                tier = next_tier
            if signal:
                signals.append(signal)

        if facts.dragon_groups >= 3:
            raise_tier(3, '副露含三组箭牌')
        elif facts.dragon_groups == 2:
            raise_tier(2, '副露含两组箭牌')
        if facts.wind_groups >= 3:
            raise_tier(3, '副露含三组风牌')
        elif facts.wind_groups == 2:
            raise_tier(2, '副露含两组风牌')
        if facts.honor_groups >= 3:
            raise_tier(2, '副露字牌成组')
        if facts.groups >= 3:
            raise_tier(2, f'副露{facts.groups}组')
        if facts.groups >= 2 and facts.max_share >= 0.75:
            raise_tier(2, '副露染手嫌疑')
            suspect_suit = facts.dominant_suit
        elif facts.groups >= 2 and facts.max_share >= 0.5:
            raise_tier(1, '副露半染手')
            suspect_suit = facts.dominant_suit
        if facts.groups >= 2 and 1 <= len(discards) <= 7 and wall > resolved.late_threat_wall_count:
            raise_tier(1, '副露少牌河快听')
        if facts.groups == 0 and len(discards) >= 8:
            counts = _suit_discard_counts(discards)
            weakest = _weakest_suit(counts)
            if weakest and counts.get(weakest, 0) <= 1:
                raise_tier(1, f'牌河未见{_SUIT_LABELS[weakest]}')
                if suspect_suit is None:
                    suspect_suit = weakest
        if wall <= resolved.late_game_wall_count and 1 <= len(discards) <= 7:
            raise_tier(1, '残局少牌河')
        win_count = _meld_attr(opponent, 'winCount', 0) or 0
        locked = bool(_meld_attr(opponent, 'locked', False) and win_count > 0)
        if locked:
            raise_tier(resolved.locked_tier, f'已胡{win_count}次仍听')
        profiles.append(OpponentRiskProfile(
            index=index, tier=tier, factor=_factor_for(tier, resolved),
            signals=list(dict.fromkeys(signals)), suspect_suit=suspect_suit, locked=locked,
        ))
    return profiles


def max_opponent_risk_tier(profiles: Optional[Sequence[OpponentRiskProfile]]) -> OpponentRiskTier:
    best = 0
    for profile in profiles or []:
        if profile.tier > best:
            best = profile.tier
    return best


def _visible_counts(visible_tiles: Sequence[Tile]) -> dict[Tile, int]:
    counts: dict[Tile, int] = {}
    for tile in visible_tiles or []:
        counts[tile] = counts.get(tile, 0) + 1
    return counts


def opponent_pattern_exposure(profiles: Optional[Sequence[OpponentRiskProfile]],
                              visible_tiles: Sequence[Tile],
                              tuning=None) -> Callable[[Tile], float]:
    """每张牌的估算点炮赔付（点）。

    口径：一次弃牌最多被一家胡，所以取「权重最高的那一家」的赔付，而不是各家相加
    （相加会把三家都危险的局面高估 3 倍，也会破坏与旧口径的等价性）。

    与旧口径的等价性：无任何信号（全部 tier=0、无锁手）时权重恒为 1，退化为
    ``exposure_unit × ladder(公开张数)``，即改动前 ``safetyExposureFor`` 的逐位相同结果。
    """
    resolved = tuning_of(tuning)
    counts = _visible_counts(visible_tiles)
    active = list(profiles or [])

    def ladder_ratio(tile: Tile) -> float:
        count = counts.get(tile, 0)
        if count >= 2:
            return resolved.safety_cost_safe
        if count == 1:
            return resolved.safety_cost_one
        return resolved.safety_cost_none

    def exposure(tile: Tile) -> float:
        ladder = ladder_ratio(tile)
        if not active:
            return resolved.exposure_unit * ladder
        suit = suit_of_tile(tile)
        weight = 1
        best_is_locked = False
        for profile in active:
            # 已锁手的家可能停在单吊任意听（任何一张都能胡）：现物折扣与花色折扣都不适用。
            off_suit = (not profile.locked and profile.suspect_suit is not None
                        and suit != profile.suspect_suit)
            candidate = profile.factor * (resolved.off_suit_factor if off_suit else 1)
            if candidate > weight:
                weight = candidate
                best_is_locked = profile.locked
        # 已锁手的家仍然每巡在听（已胡仍付款）：现物 / 公开多张不再享受折扣。
        return resolved.exposure_unit * weight \
            * (resolved.safety_cost_none if best_is_locked else ladder)

    return exposure


@dataclass
class OpponentRiskFeature:
    """该张牌的对手风险特征（供候选特征 / prompt 使用）。"""
    tier: str
    payment: int
    signals: list[str]


def opponent_pattern_feature(profiles: Optional[Sequence[OpponentRiskProfile]],
                             visible_tiles: Sequence[Tile], tile: Tile,
                             tuning=None) -> Optional[OpponentRiskFeature]:
    """该张牌的对手风险特征；无信号返回 None（保持旧形状）。"""
    tier = max_opponent_risk_tier(profiles)
    if tier == 0:
        return None
    top = next((p for p in (profiles or []) if p.tier == tier), None)
    payment = round(opponent_pattern_exposure(profiles, visible_tiles, tuning)(tile))
    signals = list(top.signals[:3]) if top else []
    return OpponentRiskFeature(tier=RISK_TIER_LABELS[tier], payment=int(payment),
                               signals=signals)


def opponent_threat_score(profiles: Optional[Sequence[OpponentRiskProfile]],
                          wall_count: int) -> int:
    """深思门槛口径：把档位换算成 0～100 的公开威胁分（与原 estimateOpponentThreat 同量纲）。"""
    tier = max_opponent_risk_tier(profiles)
    base = 90 if tier == 3 else 70 if tier == 2 else 40 if tier == 1 else 0
    if not base:
        return 0
    late = 10 if wall_count <= OPPONENT_RISK.late_threat_wall_count else 0
    return min(100, base + late)
