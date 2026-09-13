"""血流计分 —— 对应 patterns/score.ts。"""

from .config import BLOOD_FLOW_CONFIG, KONG_BONUS, BloodFlowConfig
from .types import ExcludedPattern, KongCounts, PublicWinScore, ScoringItem, WinSource

# 三杠/四杠本身就是“把杠算进去”的番种：命中其一则每副杠的加成归零，避免重复奖励同一结构。
KONG_PATTERN_IDS = frozenset(('three-kongs', 'four-kongs'))


def kong_bonus_of(counts: KongCounts, config: BloodFlowConfig = BLOOD_FLOW_CONFIG) -> int:
    """杠加成（对应 TS 的 kongBonusOf）：明杠 ×1、暗杠/风杠 ×2，权重取自规则配置。"""
    bonus = getattr(config, 'kong_bonus', None) or KONG_BONUS
    return counts.exposed * bonus['exposed'] + counts.concealed * bonus['concealed'] \
        + counts.wind * bonus['wind']


def score_patterns(patterns: list[str], natural: bool, source: WinSource,
                   opening: str | None = None, config: BloodFlowConfig = BLOOD_FLOW_CONFIG,
                   kongs: KongCounts | None = None) -> PublicWinScore:
    ids = sorted(set(patterns))
    excluded: list[ExcludedPattern] = []
    for pid in ids:
        included_by = next((other for other in ids if pid in config.patterns[other].excludes), None)
        if included_by is not None:
            excluded.append(ExcludedPattern(id=pid, included_by=included_by))
    excluded_ids = {e.id for e in excluded}
    items = [ScoringItem(id=pid, label=config.patterns[pid].label, weight=config.patterns[pid].weight)
             for pid in ids if pid not in excluded_ids]
    # 三杠/四杠不再叠加每副杠的加成（不重复计算）。
    kong_pattern_scored = any(item.id in KONG_PATTERN_IDS for item in items)
    kong_bonus = 0 if kong_pattern_scored else kong_bonus_of(kongs or KongCounts(), config)
    pattern_multiplier = 1 + sum(p.weight - 1 for p in items) + kong_bonus
    event_multiplier = config.event_multipliers[source]
    ordinary = pattern_multiplier * event_multiplier
    opening_applied = opening is not None and ordinary < config.opening_minimum_multiplier
    uncapped = (config.opening_minimum_multiplier if opening_applied else ordinary) \
        * (config.hard_win_multiplier if natural else 1)
    final = min(uncapped, config.max_multiplier_per_payer)
    return PublicWinScore(
        items=tuple(items), excluded=tuple(excluded), hard_win=natural, source=source,
        opening=opening, pattern_multiplier=pattern_multiplier, event_multiplier=event_multiplier,
        opening_applied=opening_applied, uncapped_multiplier=uncapped, final_multiplier=final,
        capped=uncapped > final, payment_per_payer=config.base_points * final,
        kong_bonus=kong_bonus,
    )


def compare_scores(a: PublicWinScore, b: PublicWinScore) -> int:
    """Negative means a wins. Never lend the natural flag to another decomposition."""
    if b.payment_per_payer != a.payment_per_payer:
        return b.payment_per_payer - a.payment_per_payer
    if int(b.hard_win) != int(a.hard_win):
        return int(b.hard_win) - int(a.hard_win)
    sa = ','.join(p.id for p in a.items)
    sb = ','.join(p.id for p in b.items)
    return -1 if sa < sb else (1 if sa > sb else 0)
