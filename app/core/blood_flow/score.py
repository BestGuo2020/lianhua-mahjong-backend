"""血流计分 —— 对应 patterns/score.ts。"""

from .config import BLOOD_FLOW_CONFIG, BloodFlowConfig
from .types import ExcludedPattern, PublicWinScore, ScoringItem, WinSource


def score_patterns(patterns: list[str], natural: bool, source: WinSource,
                   opening: str | None = None, config: BloodFlowConfig = BLOOD_FLOW_CONFIG) -> PublicWinScore:
    ids = sorted(set(patterns))
    excluded: list[ExcludedPattern] = []
    for pid in ids:
        included_by = next((other for other in ids if pid in config.patterns[other].excludes), None)
        if included_by is not None:
            excluded.append(ExcludedPattern(id=pid, included_by=included_by))
    excluded_ids = {e.id for e in excluded}
    items = [ScoringItem(id=pid, label=config.patterns[pid].label, weight=config.patterns[pid].weight)
             for pid in ids if pid not in excluded_ids]
    pattern_multiplier = 1 + sum(p.weight - 1 for p in items)
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
