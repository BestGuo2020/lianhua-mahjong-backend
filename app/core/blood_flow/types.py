"""血流数据类型 —— 对应 src/game/variants/lotus/patterns/types.ts 与 bloodFlow/types.ts。"""

from dataclasses import dataclass, field
from typing import Literal, Optional

from app.models.game import TileType

RegularPatternId = Literal[
    'pure-suit', 'mixed-suit', 'all-triplets',
    'little-three-dragons', 'big-three-dragons',
    'little-four-winds', 'big-four-winds', 'nine-gates',
    'all-green', 'pure-terminals', 'mixed-terminals',
    'three-concealed-triplets', 'four-concealed-triplets',
    'all-honors', 'three-kongs', 'four-kongs',
]
SpecialPatternId = Literal['pinghu', 'sevenPairs', 'shiSanLan', 'qiXing', 'thirteenOrphans']
PatternId = str  # 保持宽松以便共享夹具 JSON 直接使用
HandShape = Literal['standard', 'sevenPairs', 'shiSanLan', 'qiXing', 'thirteenOrphans']
WinSource = Literal['discard', 'self-draw', 'robbed-kong', 'kong-bloom']


@dataclass(frozen=True)
class PatternDefinition:
    id: str
    label: str
    weight: int
    excludes: tuple[str, ...] = ()


@dataclass(frozen=True)
class TileAssignment:
    input_index: int
    physical: TileType
    represented: TileType


@dataclass(frozen=True)
class DecomposedGroup:
    kind: Literal['pair', 'sequence', 'triplet', 'kong', 'wind-kong']
    tiles: tuple[TileType, ...]
    concealed: bool
    # origin: {'kind': 'hand', 'inputIndexes': [...]} | {'kind': 'meld', 'meldIndex': int}
    origin: dict


@dataclass(frozen=True)
class WinningDecomposition:
    shape: HandShape
    groups: tuple[DecomposedGroup, ...]
    assignments: tuple[TileAssignment, ...]
    winning_tile_group_index: Optional[int]
    natural: bool


@dataclass(frozen=True)
class ScoringItem:
    id: str
    label: str
    weight: int


@dataclass(frozen=True)
class ExcludedPattern:
    id: str
    included_by: str


@dataclass(frozen=True)
class WinEvaluationInput:
    """concealed 不含胡牌张；外部胡时 winning_tile 失去万能身份。"""
    concealed: tuple[TileType, ...]
    melds: tuple[dict, ...]
    winning_tile: TileType
    source: WinSource
    jokers: tuple[TileType, ...]
    opening: Optional[Literal['heaven', 'earth']]


@dataclass(frozen=True)
class PublicWinScore:
    items: tuple[ScoringItem, ...]
    excluded: tuple[ExcludedPattern, ...]
    hard_win: bool
    source: WinSource
    opening: Optional[Literal['heaven', 'earth']]
    pattern_multiplier: int
    event_multiplier: int
    opening_applied: bool
    uncapped_multiplier: int
    final_multiplier: int
    capped: bool
    payment_per_payer: int


@dataclass(frozen=True)
class WinEvaluation:
    rule_version: str
    decomposition: WinningDecomposition
    natural_evidence: dict
    score: PublicWinScore


@dataclass(frozen=True)
class SourceTileEvent:
    id: str
    tile: TileType
    seat: int
    kind: Literal['draw', 'discard', 'added-kong']
