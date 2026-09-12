"""血流规则配置 —— 对应 src/game/variants/lotus/bloodFlow/config.ts 的 BLOOD_FLOW_CONFIG。"""

from dataclasses import dataclass, field
from typing import Literal

from app.core.blood_flow.types import PatternDefinition
# 16 番型 + 特殊手（权重与排除关系与前端 config.ts 完全一致）。
# 2026-09-12 重平衡：对齐广东麻将番型表的相对比例，顶端单独拉开；同步把单家封顶 64 → 128。
PATTERNS: dict[str, PatternDefinition] = {
    'big-three-dragons': PatternDefinition('big-three-dragons', '大三元', 32),
    'big-four-winds': PatternDefinition('big-four-winds', '大四喜', 32, ('all-triplets',)),
    'thirteenOrphans': PatternDefinition('thirteenOrphans', '十三幺', 32),
    'nine-gates': PatternDefinition('nine-gates', '九莲宝灯', 32, ('pure-suit',)),
    'four-kongs': PatternDefinition('four-kongs', '四杠', 32, ('three-kongs', 'all-triplets')),
    'all-honors': PatternDefinition('all-honors', '字一色', 24),
    'pure-terminals': PatternDefinition('pure-terminals', '清幺九', 24, ('all-triplets',)),
    'all-green': PatternDefinition('all-green', '绿一色', 24),
    'little-three-dragons': PatternDefinition('little-three-dragons', '小三元', 16),
    'little-four-winds': PatternDefinition('little-four-winds', '小四喜', 16),
    'four-concealed-triplets': PatternDefinition('four-concealed-triplets', '四暗刻', 16, ('three-concealed-triplets', 'all-triplets')),
    'luxury-seven-pairs': PatternDefinition('luxury-seven-pairs', '豪华七对', 16, ('sevenPairs',)),
    'mixed-terminals': PatternDefinition('mixed-terminals', '混幺九', 12, ('all-triplets',)),
    'three-kongs': PatternDefinition('three-kongs', '三杠', 12),
    'pure-suit': PatternDefinition('pure-suit', '清一色', 8),
    'sevenPairs': PatternDefinition('sevenPairs', '七对', 6),
    'three-concealed-triplets': PatternDefinition('three-concealed-triplets', '三暗刻', 6),
    'qiXing': PatternDefinition('qiXing', '七星十三烂', 6),
    'mixed-suit': PatternDefinition('mixed-suit', '混一色', 4),
    'all-triplets': PatternDefinition('all-triplets', '碰碰胡', 4),
    'shiSanLan': PatternDefinition('shiSanLan', '十三烂', 2),
    'pinghu': PatternDefinition('pinghu', '平胡', 1),
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
    max_multiplier_per_payer: int = 128
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


@dataclass(frozen=True)
class DefensePolicyConfig:
    """兜/弃政策阈值（v3）—— 对齐前端 config.ts 的 DefensePolicyConfig。

    规则来自用户定稿（2026-09-12）：
      ① 只要能在本巡转成**精吊任意听**就继续走（锁手后每张摸到的牌都能胡、永不弃牌）；
      ② 未听牌且可达听口过窄 → 立即弃胡：只打最小赔付张、停吃碰杠；
      ③ 我方上限不低于对手已知/推断的牌型倍率 → 可以赌（继续进攻）。
    """
    # 触发「兜」的最低对手威胁档（3 = 十六倍级 / 门清大牌）。
    fold_threat_tier: int = 3
    # 我方上限认定：番型方向接近度 ≥ 该值才算「真有机会做成」。
    ceiling_progress: float = 0.35
    # 我方上限认定：该方向的番型倍率权重下限（与对手对比用）。
    ceiling_weight_floor: float = 4
    # 兜牌硬约束：'hard' = 候选层撤掉吃碰杠 + 弃牌只留安全档（引擎与 LLM 共用同一份候选）；
    # 'off' = 只在引擎侧选择最小赔付张，候选不收敛。
    mode: str = 'hard'
    # 兜牌时允许的弃牌安全档容差（0 = 只留放炮成本最小档）。
    fold_discard_tolerance: float = 0


# 兜/弃政策默认值（v3；与前端 BLOOD_FLOW_DEFENSE 同名同值）。
BLOOD_FLOW_DEFENSE = DefensePolicyConfig()


@dataclass(frozen=True)
class BloodFlowAiConfig:
    """血流本地 AI 贪婪 EV 参数（对齐前端 config.ts BLOOD_FLOW_AI）。"""
    strategy: str = 'ev'
    minimum_first_payment: int = 0
    self_draw_weight: int = 6
    first_win_floor_early: int = 40
    first_win_floor_mid: int = 20
    first_win_floor_late: int = 10
    late_game_wall_count: int = 15
    early_game_wall_count: int = 40
    potential_floor: float = 2.0
    reform_gain_ratio: float = 1.2
    chain_horizon: int = 8
    safety_cost_none: float = 0.25
    safety_cost_one: float = 0.1
    safety_cost_safe: float = 0.0
    # 对手牌型（大牌）风险定价：'off' = 只看公开张数的旧口径，
    # 'tier' = 档位版（见 app/core/opponent_pattern_risk.py，前端同源 shared/ai/opponentPatternRisk.ts）。
    opponent_pattern_risk: str = 'tier'
    # 档位倍率：1 = 平胡量级；4/16/32 ≈ 混一色 / 清一色 / 十六倍级硬胡点炮的单家赔付量级。
    risk_factor_tier1: float = 4
    risk_factor_tier2: float = 16
    risk_factor_tier3: float = 32
    # 染手（花色集中）嫌疑对手：非嫌疑花色牌的系数。
    risk_off_suit_factor: float = 0.5
    # 兜/弃政策阈值（v3）。
    defense: DefensePolicyConfig = BLOOD_FLOW_DEFENSE
    llm_ev_features: bool = True


BLOOD_FLOW_AI = BloodFlowAiConfig()

# 计时（毫秒）：对齐经典联机（非血流）与前端 config.ts。
BLOOD_FLOW_TIMING: dict[str, int] = {
    'normalDecisionMs': 15_000,
    'remoteDecisionMs': 12_000,   # 联机回合决策窗口：对齐经典房间 turn_timeout=12s
    'winBeatMs': 450,
    'recoveryGraceMs': 12_000,
}

# 真人联机房间的视觉节奏（毫秒）：以单机血流为基准逐项对齐——前端引擎 engine.ts
# 消费共享 PACE_MS，机器人思考是 useBloodFlowGame.schedule 的统一 650ms（不分窗口类型）。
# 不再对齐经典联机的以下专属档位：claim 500 / after_kong 550 分档、真人碰/明杠 350
# 缩短（skipDrawPengDelay 等）、多响抢杠 betweenRobKongs 450（单机引擎无此停顿）。
BLOOD_FLOW_PACE: dict[str, int] = {
    'aiThink': 650,                # 机器人决策前统一停顿（LLM 座位也先等再发请求，同单机 actBot）
    'afterDraw': 450,              # 摸牌到出牌窗口（对齐单机 PACE_MS.afterDraw）
    'afterDiscardToNextTurn': 450,  # 弃牌到下家
    'afterClaimPeng': 650,          # 碰/吃后（单机不分真人/AI）
    'afterClaimGang': 550,          # 明杠后（单机不分真人/AI）
    'afterKongSettle': 600,         # 暗杠/补杠/乱风杠后
    'beforeRobKong': 650,           # 补杠到抢杠窗口
    # 胡牌表现：血流本地 bloodFlowWinTiming（经典单胡无此档位，取本地为准）
    'winEffectBase': 1815,
    'winEffectLarge': 2600,
    'winEffectTop': 2900,
    'winEffectDeduct': 620,
    'winEffectTail': 1200,
    'winHandoffMargin': 100,
    'multiWinIntro': 1500,
}
