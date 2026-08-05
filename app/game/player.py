"""玩家控制器 —— 从 src/game/playerController.ts 翻译

PlayerController 接口 + AIPlayer 实现。「人类 vs AI 解耦」的关键抽象：
useGame 的 HumanController 在联网版中被 RemotePlayer 替代（Phase 5 接入 WebSocket）。

- TurnContext / ClaimContext / RobKongContext → Pydantic 模型（服务端→客户端的状态快照）
- TurnAction / ClaimAction / RobKongAction → 客户端→服务端的动作指令（dict，网络协议）
- AIPlayer：复用 core/ai.py，request_* 去掉思考延迟、直接决策（延迟可配置为 0 以加速模拟）
"""

import asyncio
from typing import Optional, Protocol

from pydantic import BaseModel, Field

from app.models.game import Meld, TileType
from app.core.actions import remove_matches
from app.core.ai import choose_discard_index, decide_claim, decide_rob_kong, decide_turn


# ─── 上下文（服务端 → 控制器 / 客户端的只读快照）──────────────

class TurnContext(BaseModel):
    """回合决策上下文"""
    hand: list[TileType]
    melds: list[Meld]
    exposedMelds: int
    kongBloom: bool
    skipDraw: bool
    afterKong: bool


class ClaimContext(BaseModel):
    """吃碰杠响应上下文"""
    hand: list[TileType]
    canGang: bool
    tile: TileType
    from_: int = Field(alias='from')
    model_config = {'populate_by_name': True}


class RobKongContext(BaseModel):
    """抢杠响应上下文"""
    tile: TileType
    from_: int = Field(alias='from')
    hand: list[TileType]
    exposedMelds: int
    model_config = {'populate_by_name': True}


# ─── PlayerController 协议 ─────────────────────────────────

class PlayerController(Protocol):
    """控制器接口：GameManager 只依赖此接口，不区分人类/AI。"""

    async def request_turn(self, ctx: TurnContext) -> dict: ...
    async def request_claim(self, ctx: ClaimContext) -> dict: ...
    async def request_rob_kong(self, ctx: RobKongContext) -> str: ...
    def on_discarded(self) -> None: ...
    def reset(self) -> None: ...


# ─── AI 控制器 ─────────────────────────────────────────────

# 对齐前端 src/game/playerController.ts 的 AI_DELAYS（人类正常思考速度）：
# 真人联机房间的 AI 摸牌/出牌（turn 650ms）、杠后补摸再决策（after_kong 550ms）、
# 吃碰杠响应（claim 500ms）都保持与本地 AI 一致的节奏，避免 AI 瞬移显得机械。
# 测试路径默认不注入（AIPlayer 默认全 0），保持即用即答。
AI_DELAYS = {'turn': 650, 'after_kong': 550, 'claim': 500}


def _map_turn_decision(decision: dict) -> dict:
    """把 ai.py 的 TurnDecision 映射为控制器层 TurnAction（语义一致）。"""
    kind = decision['kind']
    if kind == 'win':
        return {'kind': 'win'}
    if kind == 'added-kong':
        return {'kind': 'added-kong', 'meldIndex': decision['meldIndex']}
    if kind == 'concealed-kong':
        return {'kind': 'concealed-kong', 'tile': decision['tile']}
    return {'kind': 'discard', 'handIndex': decision['handIndex']}


class AIPlayer:
    """AI 玩家控制器：封装 AI 决策的时序编排与纯函数调用。"""

    def __init__(self, delays: Optional[dict] = None, random=None):
        self.delays = delays or {'turn': 0, 'after_kong': 0, 'claim': 0}
        self._random = random

    async def request_turn(self, ctx: TurnContext) -> dict:
        ms = self.delays['after_kong'] if ctx.afterKong else self.delays['turn']
        if ms:
            await asyncio.sleep(ms / 1000)
        view = {
            'hand': list(ctx.hand),
            'melds': ctx.melds,
            'exposedMelds': ctx.exposedMelds,
            'kongBloom': ctx.kongBloom,
        }
        return _map_turn_decision(decide_turn(view))

    async def request_claim(self, ctx: ClaimContext) -> dict:
        ms = self.delays['claim']
        if ms:
            await asyncio.sleep(ms / 1000)
        decision = decide_claim({'hand': list(ctx.hand), 'canGang': ctx.canGang})
        if decision == 'gang':
            return {'kind': 'gang'}
        if decision == 'peng':
            # 预计算碰后弃牌索引，实现 AI 的单次碰+出牌闭环
            after_peng = remove_matches(list(ctx.hand), ctx.tile, 2)
            if not after_peng:
                # 碰后无牌可打（手牌恰好只剩这 2 张）：真实规则下不能碰，
                # 否则出牌阶段手牌为空导致对局停滞。
                return {'kind': 'pass'}
            discard_index = choose_discard_index(after_peng, self._random)
            return {'kind': 'peng', 'discardIndex': discard_index}
        return {'kind': 'pass'}

    async def request_rob_kong(self, ctx: RobKongContext) -> str:
        # 抢杠决策无需额外延迟
        return decide_rob_kong({
            'hand': list(ctx.hand),
            'exposedMelds': ctx.exposedMelds,
            'tile': ctx.tile,
            'from': ctx.from_,
        })

    def on_discarded(self) -> None:
        pass

    def reset(self) -> None:
        pass


# ─── 人类远程玩家（Phase 5 实现于 remote_player.py）────────

# RemotePlayer 已随 Phase 5 迁移到 app/game/remote_player.py（接入 WebSocket 通道，
# 含超时代打 / 断线托管 / 重连归还）。这里延迟导入并重导出，保持 `from
# app.game.player import RemotePlayer` 兼容。底部导入避免与 remote_player 的
# 上行 import 形成循环。
try:
    from app.game.remote_player import RemotePlayer  # noqa: E402,F401
except ImportError:  # pragma: no cover - 仅当 remote_player 尚未实现时
    pass
