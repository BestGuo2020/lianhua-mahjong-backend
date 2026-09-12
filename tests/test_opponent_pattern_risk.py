"""对手牌型（大牌）风险定价的单元测试 —— 与 TS 测试一一对应。

参照 `src/game/shared/ai/opponentPatternRisk.test.ts`；核心断言：只用公共信息，
且无信号时结果与旧口径逐位一致。
"""

from app.core.blood_flow.config import BLOOD_FLOW_AI
from app.core.opponent_pattern_risk import (OPPONENT_RISK, max_opponent_risk_tier,
                                            opponent_pattern_exposure,
                                            opponent_pattern_feature,
                                            opponent_risk_profiles,
                                            opponent_threat_score, suit_of_tile,
                                            tuning_of)


def meld(tile: str, type_: str = 'peng') -> dict:
    return {'type': type_, 'tile': tile, 'tiles': [tile, tile, tile]}


QUIET = {'discards': [], 'melds': []}


def discards(count: int, tile: str) -> list[str]:
    return [tile] * count


def exposure_of(profiles, visible=()):
    return opponent_pattern_exposure(profiles, list(visible))


def test_default_tuning_matches_blood_flow_config():
    assert OPPONENT_RISK.factor_tier1 == 4
    assert OPPONENT_RISK.factor_tier2 == 16
    assert OPPONENT_RISK.factor_tier3 == 32
    assert OPPONENT_RISK.off_suit_factor == 0.5
    assert OPPONENT_RISK.exposure_unit == 40
    assert (OPPONENT_RISK.safety_cost_none, OPPONENT_RISK.safety_cost_one,
            OPPONENT_RISK.safety_cost_safe) == (0.25, 0.1, 0)
    assert OPPONENT_RISK.locked_tier == 2
    assert OPPONENT_RISK.late_game_wall_count == BLOOD_FLOW_AI.late_game_wall_count == 15
    assert OPPONENT_RISK.late_threat_wall_count == 24
    assert (BLOOD_FLOW_AI.risk_factor_tier1, BLOOD_FLOW_AI.risk_factor_tier2,
            BLOOD_FLOW_AI.risk_factor_tier3) == (4, 16, 32)
    assert BLOOD_FLOW_AI.risk_off_suit_factor == 0.5
    assert BLOOD_FLOW_AI.opponent_pattern_risk == 'tier'


def test_tuning_of_is_partial_override():
    tuned = tuning_of({'factor_tier2': 8})
    assert tuned.factor_tier2 == 8
    assert tuned.factor_tier1 == OPPONENT_RISK.factor_tier1
    assert tuned.exposure_unit == OPPONENT_RISK.exposure_unit
    assert tuning_of() is OPPONENT_RISK
    assert tuning_of({}) is OPPONENT_RISK


def test_suit_of_tile_only_for_suited_tiles():
    assert suit_of_tile('m1') == 'm'
    assert suit_of_tile('p9') == 'p'
    assert suit_of_tile('s5') == 's'
    assert suit_of_tile('east') is None
    assert suit_of_tile('red') is None
    assert suit_of_tile('white') is None


def test_no_public_signal_is_bit_identical_to_legacy_ladder():
    profiles = opponent_risk_profiles([QUIET, QUIET, QUIET], 60)
    assert [p.tier for p in profiles] == [0, 0, 0]
    assert all(p.factor == 1 and p.signals == [] for p in profiles)
    exposure = exposure_of(profiles, ['p3', 'p3', 's1'])
    assert exposure('p3') == 0        # 公开 ≥2 张
    assert exposure('s1') == 4        # 公开 1 张
    assert exposure('m5') == 10       # 生张


def test_empty_profile_table_is_bit_identical_to_legacy_ladder():
    exposure = exposure_of([], ['p3', 'p3', 's1'])
    assert exposure('p3') == 0
    assert exposure('s1') == 4
    assert exposure('m5') == 10


def test_two_same_suit_melds_suspect_flush_tier2():
    opponent = {'discards': ['m1', 'm2'], 'melds': [meld('p4'), meld('p7')]}
    profiles = opponent_risk_profiles([opponent], 60)
    assert profiles[0].tier == 2
    assert '副露染手嫌疑' in profiles[0].signals
    assert profiles[0].suspect_suit == 'p'
    exposure = exposure_of(profiles)
    assert exposure('p3') == 160        # 40 × 16 × 0.25
    assert exposure('m5') == 80         # 非嫌疑花色 × 0.5
    safe = exposure_of(profiles, ['p3', 'p3'])
    assert safe('p3') == 0              # 现物 / 公开 ≥2 张仍是 0


def test_three_melds_and_dragon_pairs_raise_tier_step_by_step():
    three_melds = opponent_risk_profiles(
        [{'discards': [], 'melds': [meld('m2'), meld('p3'), meld('s4')]}], 60)
    assert three_melds[0].tier == 2
    assert '副露3组' in three_melds[0].signals
    two_dragons = opponent_risk_profiles(
        [{'discards': [], 'melds': [meld('red'), meld('green')]}], 60)
    assert two_dragons[0].tier == 2
    assert '副露含两组箭牌' in two_dragons[0].signals
    three_dragons = opponent_risk_profiles(
        [{'discards': [], 'melds': [meld('red'), meld('green'), meld('white')]}], 60)
    assert three_dragons[0].tier == 3
    assert three_dragons[0].factor == OPPONENT_RISK.factor_tier3
    three_winds = opponent_risk_profiles(
        [{'discards': [], 'melds': [meld('east'), meld('south'), meld('west')]}], 60)
    assert three_winds[0].tier == 3


def test_concealed_big_hand_only_yields_weak_discard_signal():
    opponent = {'discards': [*discards(6, 'm1'), 'p2', 'p3', 'white', 'east'], 'melds': []}
    profiles = opponent_risk_profiles([opponent], 60)
    assert profiles[0].tier == 1
    assert any(signal.startswith('牌河未见') for signal in profiles[0].signals)
    assert profiles[0].suspect_suit == 's'
    assert exposure_of(profiles)('s5') == 40    # 40 × 4 × 0.25


def test_locked_opponent_loses_both_discounts():
    opponent = {'discards': ['m1'], 'melds': [meld('p4'), meld('p7')],
                'winCount': 12, 'locked': True}
    profiles = opponent_risk_profiles([opponent], 40)
    assert profiles[0].tier == 2
    assert profiles[0].locked is True
    assert '已胡12次仍听' in profiles[0].signals
    exposure = exposure_of(profiles, ['p4', 'p4'])
    assert exposure('p4') > 0
    assert exposure('p4') == exposure('m9')
    # 40 × 16 × 0.25：现物不再按 ladder 归零（生张同价），非嫌疑花色也不再 ×0.5。
    assert exposure('p4') == 160


def test_locked_without_wins_keeps_discounts():
    """只锁手、没胡过（winCount=0）不算「已胡仍听」：档位来自副露，折扣照旧。"""
    opponent = {'discards': [], 'melds': [meld('p4'), meld('p7')],
                'winCount': 0, 'locked': True}
    profiles = opponent_risk_profiles([opponent], 60)
    assert profiles[0].locked is False
    assert profiles[0].tier == 2
    exposure = exposure_of(profiles, ['p4', 'p4'])
    assert exposure('p4') == 0


def test_takes_the_highest_weight_single_opponent_not_the_sum():
    flush = {'discards': [], 'melds': [meld('p4'), meld('p7')]}
    profiles = opponent_risk_profiles([flush, flush, flush], 60)
    assert max_opponent_risk_tier(profiles) == 2
    assert exposure_of(profiles)('p3') == 160      # 相加会得到 480


def test_only_public_information_is_used():
    opponent = {'discards': ['m1', 'm2'], 'melds': [meld('s5'), meld('s8')]}
    assert opponent_risk_profiles([opponent], 60)[0].tier == 2


def test_half_flush_counts_every_tile_of_every_meld():
    """副露按 tiles 逐张计花色（chi 三张不同 / gang 四张），不是只看 meld.tile。"""
    opponent = {'discards': ['p1', 'p2', 'p3', 's1', 's2', 's3', 'east', 'east', 'east'],
                'melds': [{'type': 'chi', 'tile': 'p4', 'tiles': ['p3', 'p4', 'p5']},
                          {'type': 'gang', 'tile': 'm7', 'tiles': ['m7'] * 4}]}
    profiles = opponent_risk_profiles([opponent], 60)
    assert profiles[0].tier == 1
    assert profiles[0].signals == ['副露半染手']
    assert profiles[0].suspect_suit == 'm'
    exposure = exposure_of(profiles)
    assert exposure('m5') == 40     # 40 × 4 × 0.25（嫌疑花色）
    assert exposure('p5') == 20     # 40 × 4 × 0.5 × 0.25（非嫌疑花色）


def test_no_discards_never_raises_short_discard_signal():
    """`副露少牌河快听` 要求牌河至少有 1 张（与 TS 的 `discards.length >= 1` 守卫一致）。"""
    flush_profiles = opponent_risk_profiles(
        [{'discards': [], 'melds': [meld('p4'), meld('p7')]}], 60)
    feature = opponent_pattern_feature(flush_profiles, [], 'p3')
    assert (feature.tier, feature.payment) == ('中', 160)
    assert feature.signals == ['副露染手嫌疑']
    assert opponent_pattern_feature(flush_profiles, [], 'm3').payment == 80
    with_discard = opponent_risk_profiles(
        [{'discards': ['m1'], 'melds': [meld('p4'), meld('p7')]}], 60)
    assert with_discard[0].signals == ['副露染手嫌疑', '副露少牌河快听']


def test_candidate_feature_and_reasoning_threat_score():
    quiet_profiles = opponent_risk_profiles([QUIET], 60)
    assert opponent_pattern_feature(quiet_profiles, [], 'm5') is None
    flush_profiles = opponent_risk_profiles(
        [{'discards': [], 'melds': [meld('p4'), meld('p7')]}], 60)
    assert opponent_threat_score(quiet_profiles, 60) == 0
    assert opponent_threat_score(flush_profiles, 60) == 70
    assert opponent_threat_score(flush_profiles, 20) == 80
    dragons = opponent_risk_profiles(
        [{'discards': [], 'melds': [meld('red'), meld('green'), meld('white')]}], 60)
    assert opponent_threat_score(dragons, 60) == 90
    assert opponent_threat_score(dragons, 20) == 100


def test_feature_signals_are_capped_at_three_and_deduplicated():
    opponent = {'discards': [], 'melds': [meld('red'), meld('green'), meld('white')],
                'winCount': 3, 'locked': True}
    profiles = opponent_risk_profiles([opponent], 60)
    feature = opponent_pattern_feature(profiles, [], 'm5')
    assert len(feature.signals) == 3
    assert feature.signals == ['副露含三组箭牌', '副露字牌成组', '副露3组']


def test_deterministic_repeated_calls():
    opponent = {'discards': ['m1', 'm2'], 'melds': [meld('p4'), meld('p7')]}
    first = opponent_risk_profiles([opponent], 60)
    second = opponent_risk_profiles([opponent], 60)
    assert [(p.tier, p.factor, p.signals, p.suspect_suit, p.locked) for p in first] \
        == [(p.tier, p.factor, p.signals, p.suspect_suit, p.locked) for p in second]
