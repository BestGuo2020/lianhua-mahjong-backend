"""血流规则配置 —— 对应 src/game/variants/lotus/bloodFlow/config.ts 的 BLOOD_FLOW_CONFIG。"""

from dataclasses import dataclass, field
from typing import Literal

from app.core.blood_flow.types import PatternDefinition

# 16 番型 + 特殊手（权重与排除关系与前端 config.ts 完全一致）。
PATTERNS: dict[str, PatternDefinition] = {
    'pure-suit': PatternDefinition('pure-suit', '清一色', 4),
    'mixed-suit': PatternDefinition('mixed-suit', '混一色', 2),
    'all-triplets': PatternDefinition('all-triplets', '碰碰胡', 2),
    'little-three-dragons': PatternDefinition('little-three-dragons', '小三元', 4),
    'big-three-dragons': PatternDefinition('big-three-dragons', '大三元', 8),
    'little-four-winds': PatternDefinition('little-four-winds', '小四喜', 8),
    'big-four-winds': PatternDefinition('big-four-winds', '大四喜', 16, ('all-triplets',)),
    'nine-gates': PatternDefinition('nine-gates', '九莲宝灯', 16, ('pure-suit',)),
    'all-green': PatternDefinition('all-green', '绿一色', 16),
    'pure-terminals': PatternDefinition('pure-terminals', '清幺九', 16, ('all-triplets',)),
    'mixed-terminals': PatternDefinition('mixed-terminals', '混幺九', 4, ('all-triplets',)),
    'three-concealed-triplets': PatternDefinition('three-concealed-triplets', '三暗刻', 4),
    'four-concealed-triplets': PatternDefinition('four-concealed-triplets', '四暗刻', 8, ('three-concealed-triplets', 'all-triplets')),
    'all-honors': PatternDefinition('all-honors', '字一色', 8),
    'three-kongs': PatternDefinition('three-kongs', '三杠', 8),
    'four-kongs': PatternDefinition('four-kongs', '四杠', 16, ('three-kongs', 'all-triplets')),
    'pinghu': PatternDefinition('pinghu', '平胡', 1),
    'sevenPairs': PatternDefinition('sevenPairs', '七对', 2),
    'shiSanLan': PatternDefinition('shiSanLan', '十三烂', 2),
    'qiXing': PatternDefinition('qiXing', '七星十三烂', 4),
    'thirteenOrphans': PatternDefinition('thirteenOrphans', '十三幺', 16),
}

EVENT_MULTIPLIERS: dict[str, int] = {'discard': 1, 'self-draw': 2, 'robbed-kong': 2, 'kong-bloom': 4}

KONG_PAYMENTS: dict[str, int] = {'discard': 1, 'added': 1, 'concealed': 2, 'wind': 2}

ROUNDS: dict[str, int] = {'east': 4, 'hanchan': 8}


@dataclass(frozen=True)
class BloodFlowConfig:
    id: str = 'lotus-blood-flow'
    version: str = 'lotus-blood-flow-v1'
    label: str = '莲花麻将·血流'
    base_points: int = 10
    initial_score: int = 2000
    max_multiplier_per_payer: int = 64
    hard_win_multiplier: int = 2
    patterns: dict[str, PatternDefinition] = field(default_factory=lambda: dict(PATTERNS))
    event_multipliers: dict[str, int] = field(default_factory=lambda: dict(EVENT_MULTIPLIERS))
    opening_minimum_multiplier: int = 8
    kong_payments: dict[str, int] = field(default_factory=lambda: dict(KONG_PAYMENTS))
    rounds: dict[str, int] = field(default_factory=lambda: dict(ROUNDS))
    lock_after_first_win: bool = True
    multiple_winners: bool = True
    allow_negative_scores: bool = True
    already_won_players_pay: bool = True
    dealer_multiplier: int = 1
    dealer_rotation: Literal['every-round'] = 'every-round'
    cross_window_pass_restriction: bool = False


BLOOD_FLOW_CONFIG = BloodFlowConfig()
