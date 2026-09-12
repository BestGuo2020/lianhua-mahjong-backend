"""对手牌型风险定价的前后端 golden fixture 对拍。

读同一份 JSON（前端 `src/game/llm/fixtures/opponent-risk.json`），逐 case 断言 Python 镜像
（app/core/opponent_pattern_risk.py）与 TS 实现（src/game/shared/ai/opponentPatternRisk.ts）
产出完全相同的 profiles / exposure / feature。TS 侧参照测试见
`src/game/llm/opponentRiskFixture.test.ts`。
"""

import json
import os
from pathlib import Path

import pytest

from app.core.opponent_pattern_risk import (opponent_pattern_exposure,
                                            opponent_pattern_feature,
                                            opponent_risk_profiles)

_FIXTURE_REL = Path('src') / 'game' / 'llm' / 'fixtures' / 'opponent-risk.json'


def _candidate_roots() -> list[Path]:
    """前端工作区根候选。

    正常布局下 backend/ 在前端工作区内，`parents[2]` 即工作区根；但当 backend 是
    `D:/PycharmProjects/linahua-mahjong-backend` 的 linked worktree 时（AGENTS.md 所述），
    `parents[2]` 指向 `D:/PycharmProjects`。因此再补充：环境变量、工作区会话路径、
    以及从本文件逐级向上的祖先目录。
    """
    roots: list[Path] = []
    for name in ('LOTUS_FRONTEND_ROOT', 'DSH_WORKSPACE_ROOT', 'WORKSPACE_ROOT'):
        value = os.environ.get(name)
        if value:
            roots.append(Path(value))
    session_jsonl = os.environ.get('DSH_SESSION_JSONL')
    if session_jsonl:
        # ...\.dsh\sessions\--D-vueprojects-lianhua_guangma--\<id>\session.jsonl.zstd
        # → D:\vueprojects\lianhua_guangma（会话路径各层深度可能变化，逐层找工作区 slug）
        for part in Path(session_jsonl).parts:
            if part.startswith('--') and part.endswith('--'):
                windows_path = part[2:-2].replace('-', '\\')
                drive, _, rest = windows_path.partition('\\')
                if len(drive) == 1 and rest:
                    roots.append(Path(f'{drive}:\\{rest}'))
                break
    here = Path(__file__).resolve()
    for parent in here.parents:
        roots.append(parent)
    roots.append(here.parents[2])
    seen: list[Path] = []
    for root in roots:
        if root not in seen:
            seen.append(root)
    return seen


def _fixture_path() -> Path:
    for root in _candidate_roots():
        candidate = root / _FIXTURE_REL
        if candidate.is_file():
            return candidate
    raise FileNotFoundError(
        f'未找到 {_FIXTURE_REL}（已尝试：{[str(root) for root in _candidate_roots()]}）')


FIXTURE = _fixture_path()

CASES = json.loads(FIXTURE.read_text(encoding='utf-8'))['cases']


def _profiles_of(case: dict):
    return opponent_risk_profiles(case['opponents'], case['wallCount'])


@pytest.mark.parametrize('case', CASES, ids=[case['id'] for case in CASES])
def test_fixture_case_matches_ts_expectation(case: dict):
    profiles = _profiles_of(case)
    summary = [{'tier': p.tier, 'factor': p.factor, 'signals': list(p.signals),
                'suspectSuit': p.suspect_suit, 'locked': p.locked} for p in profiles]
    assert summary == case['expectedProfiles'], f"{case['id']} profiles"

    visible = case['visibleTiles']
    exposure = opponent_pattern_exposure(profiles, visible)
    for tile, expected in case['expectedExposure'].items():
        assert exposure(tile) == pytest.approx(expected, rel=1e-9), \
            f"{case['id']} 赔付 {tile}"

    for tile, expected in case['expectedFeature'].items():
        actual = opponent_pattern_feature(profiles, visible, tile)
        if expected is None:
            assert actual is None, f"{case['id']} 特征 {tile} 应为 undefined/None"
            continue
        assert {'tier': actual.tier, 'payment': actual.payment, 'signals': actual.signals} == expected, \
            f"{case['id']} 特征 {tile}"


@pytest.mark.parametrize('case', CASES, ids=[case['id'] for case in CASES])
def test_fixture_payment_is_integer_points(case: dict):
    """赔付必须四舍五入成整数点（对齐 TS Math.round）。"""
    profiles = _profiles_of(case)
    exposure = opponent_pattern_exposure(profiles, case['visibleTiles'])
    for tile in case['visibleTiles']:
        assert float(exposure(tile)).is_integer(), f"{case['id']} 赔付 {tile} 非整数"


def test_fixture_ids_cover_the_required_scenarios():
    """四个 case 覆盖：无信号等价旧口径 / 染手+锁手 / 三组箭牌 tier3 / 半染手。"""
    assert [case['id'] for case in CASES] == \
        ['quiet', 'flush-and-locked', 'three-dragons', 'half-flush']
