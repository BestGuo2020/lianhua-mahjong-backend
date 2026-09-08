"""血流胡牌评估 —— 对应 patterns/evaluate.ts。"""

from app.core.tiles import TILE_TYPES

from .catalog import match_patterns
from .config import BLOOD_FLOW_CONFIG, BloodFlowConfig
from .decompose import validate_win_input, visit_decompositions
from .score import compare_scores, score_patterns
from .types import WinEvaluation, WinEvaluationInput


def win_input_from_dict(data: dict) -> WinEvaluationInput:
    """共享夹具 golden.json 的输入 → 内部类型（camelCase 键）。"""
    return WinEvaluationInput(
        concealed=tuple(data['concealed']),
        melds=tuple(data['melds']),
        winning_tile=data['winningTile'],
        source=data['source'],
        jokers=tuple(data.get('jokers') or []),
        opening=data.get('opening'),
    )


def evaluate_win(inp: WinEvaluationInput, config: BloodFlowConfig = BLOOD_FLOW_CONFIG) -> WinEvaluation | None:
    if validate_win_input(inp) is None:
        return None
    best: WinEvaluation | None = None

    def visit(decomposition) -> None:
        nonlocal best
        score = score_patterns(match_patterns(decomposition), decomposition.natural,
                               inp.source, inp.opening, config)
        if best is None or compare_scores(score, best.score) < 0:
            best = WinEvaluation(
                rule_version=config.version,
                decomposition=decomposition,
                natural_evidence={'allAssignmentsIdentity': decomposition.natural},
                score=score,
            )

    visit_decompositions(inp, visit)
    return best


def evaluate_waits(inp: WinEvaluationInput, config: BloodFlowConfig = BLOOD_FLOW_CONFIG) -> list[dict]:
    """听口：每张牌的 self-draw / discard 精确分（含任意听 34 种）。"""
    results: list[dict] = []
    for tile in TILE_TYPES:
        self_draw = evaluate_win(WinEvaluationInput(
            concealed=inp.concealed, melds=inp.melds, winning_tile=tile,
            source='self-draw', jokers=inp.jokers, opening=None,
        ), config)
        discard = evaluate_win(WinEvaluationInput(
            concealed=inp.concealed, melds=inp.melds, winning_tile=tile,
            source='discard', jokers=inp.jokers, opening=None,
        ), config)
        if self_draw or discard:
            results.append({
                'tile': tile,
                'selfDraw': self_draw.score if self_draw else None,
                'discard': discard.score if discard else None,
            })
    return results
