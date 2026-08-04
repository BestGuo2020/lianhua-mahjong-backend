"""整局模拟 —— 对照 src/game/useGame.sim.test.ts 翻译

4 个 AIPlayer 自动跑完东风场 4 局，验证：
- AI 闭环能稳定打完，不卡死、不抛错
- 分数守恒：4 名玩家起始各 1000，任意时刻总和应为 4000
"""

import asyncio

import pytest

from app.game.manager import GameManager
from app.game.player import AIPlayer


async def play_one_match(max_steps: int = 8000) -> dict:
    """打满一整场「东风场」：0 号座位也是 AIPlayer，纯 AI 对局。"""
    manager = GameManager(mode='east', controllers=[AIPlayer() for _ in range(4)])
    await manager.start_game('east')
    settled_rounds: list[int] = []
    steps = 0
    while steps < max_steps:
        steps += 1
        if manager.match_finished or manager.phase == 'finished':
            break
        if manager.phase == 'settled':
            settled_rounds.append(manager.round)
            await manager.next_round()
            continue
        if manager.phase == 'lobby':
            break
        await asyncio.sleep(0)
    return {
        'finished': manager.match_finished or manager.phase == 'finished',
        'steps': steps,
        'phase': manager.phase,
        'round': manager.round,
        'settled_rounds': settled_rounds,
        'scores': [p.score for p in manager.players],
    }


class TestFullEastMatch:
    """对应 useGame.sim.test.ts '整局模拟：东风场自动打完'"""

    @pytest.mark.asyncio
    async def test_three_matches_complete_without_error(self):
        """连续 3 场对局都能打完且无异常"""
        for _ in range(3):
            result = await play_one_match()
            # 每场必须完整打到「finished」，证明 AI 闭环不会卡死
            assert result['finished'] is True
            # 每场至少完成一局才证明闭环在运转
            assert len(result['settled_rounds']) > 0
            # 分数守恒：4 名玩家起始各 1000，任意时刻总和应为 4000
            assert sum(result['scores']) == 4000

    @pytest.mark.asyncio
    async def test_single_match_within_steps_no_infinite_loop(self):
        """单人场在固定步数内能到达结算或打完，不出死循环"""
        result = await play_one_match()
        assert result['steps'] < 8000
        assert result['phase'] in ('finished', 'settled')


class TestGameManagerState:
    """GameManager 状态机的额外验证"""

    @pytest.mark.asyncio
    async def test_four_rounds_settled(self):
        """东风场 4 个局号都产生结算，最后结束在 round 4（连庄时同局号多结算一次）"""
        result = await play_one_match()
        # 东风场 4 局：round 1-4 每个局号至少结算一次；连庄时同一局号可结算多次
        assert {1, 2, 3, 4} <= set(result['settled_rounds'])
        assert len(result['settled_rounds']) >= 4
        # 第 4 局结算后推进到 round 5 触发 finished（东风场最后一局为 4）
        assert result['round'] >= 4

    @pytest.mark.asyncio
    async def test_dealer_advances(self):
        """每局结束后庄位轮转（庄家胡牌连庄时保持不变）"""
        manager = GameManager(mode='east', controllers=[AIPlayer() for _ in range(4)])
        await manager.start_game('east')
        assert manager.phase == 'settled'
        # 记录第 1 局的结果与庄位（next_round 会覆盖 result 为第 2 局）
        first_result = manager.result
        dealer_before = manager.dealer
        await manager.next_round()
        # 庄家胡 → 连庄不变；闲家胡/流局 → 庄位 +1（与 TS 端 advanceMatchState 一致）
        if not first_result.get('draw') and first_result.get('winnerIndex') == dealer_before:
            assert manager.dealer == dealer_before
        else:
            assert manager.dealer == (dealer_before + 1) % 4
