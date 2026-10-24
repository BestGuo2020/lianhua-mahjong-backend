"""Zero-pace matches must share the event loop with sockets and cancellation."""

import asyncio
import random

import pytest

from app.game.manager import GameManager
from app.rules.lianhua import get_default_rule_set
from app.rules.lotus_legacy import LotusLegacyRuleSet


class ImmediateDiscardPlayer:
    def __init__(self):
        self.turns = 0
        self.on_first_turn = lambda: None

    async def request_turn(self, ctx):
        self.turns += 1
        if self.turns == 1:
            self.on_first_turn()
        return {'kind': 'discard', 'handIndex': 0}

    async def request_claim(self, ctx):
        return {'kind': 'pass'}

    async def request_rob_kong(self, ctx):
        return 'pass'

    def on_discarded(self):
        pass

    def reset(self):
        pass


@pytest.fixture(params=[get_default_rule_set, LotusLegacyRuleSet],
                ids=['classic', 'legacy'])
def zero_pace_game(request):
    player = ImmediateDiscardPlayer()
    manager = GameManager(
        controllers=[player] * 4,
        rule_set=request.param(),
        random=random.Random(42).random,
    )
    return manager, player


@pytest.mark.asyncio
async def test_zero_pace_match_services_callbacks_before_finishing(zero_pace_game):
    manager, player = zero_pace_game
    observed = []
    player.on_first_turn = lambda: asyncio.get_running_loop().call_soon(
        lambda: observed.append((player.turns, manager.phase)))

    await manager.start_game('east')
    await asyncio.sleep(0)

    assert manager.phase == 'settled'
    assert player.turns > 1
    assert observed and observed[0][0] == 1
    assert observed[0][1] != 'settled', 'Network callbacks waited for the entire hand'


@pytest.mark.asyncio
async def test_zero_pace_match_accepts_cancellation_between_actions(zero_pace_game):
    manager, player = zero_pace_game
    player.on_first_turn = lambda: asyncio.get_running_loop().call_soon(
        asyncio.current_task().cancel)

    game_task = asyncio.create_task(manager.start_game('east'))
    with pytest.raises(asyncio.CancelledError):
        await game_task

    assert player.turns == 1
    assert manager.phase != 'settled'
