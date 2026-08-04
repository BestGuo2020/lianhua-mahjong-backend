"""远程玩家控制器 —— 把 HumanController 接到 WebSocket 通道

RemotePlayer 在 GameManager 视角中就是「人类玩家」：request_turn / request_claim /
request_rob_kong 通过 ConnectionManager 下发请求并 await 用户动作（asyncio.Future）。

防卡死策略（对应产品决策：超时代打 + 断线托管）：
- 超时：单回合 12s（可注入），asyncio.wait_for 超时 → AIPlayer 自动代打
- 断线：on_disconnect → 投递 _DISCONNECTED 哨兵 → 立即 AI 代打，不等超时
- 重连：set_connected(True) → 后续请求重新等待真人动作，控制权归还
- 过期动作：请求已结算/超时后再来的客户端动作一律拒绝（STALE_ACTION）

动作校验：按当前请求类型（turn/claim/rob_kong）白名单校验客户端「意图」，
非法 / 过期动作返回错误码，由 WS 层回传客户端。
"""

import asyncio
from typing import Optional

from app.game.player import AIPlayer, ClaimContext, RobKongContext, TurnContext

# on_disconnect 投递给 pending future 的哨兵：收到后走 AI 代打
_DISCONNECTED = object()


class RemotePlayer:
    """人类远程玩家控制器。"""

    def __init__(self, seat: int, conn, timeout: float = 12.0):
        self.seat = seat
        self.conn = conn          # ConnectionManager（按座位路由出站）
        self.timeout = timeout    # 单回合超时秒数（超时 AI 代打）
        self._ai = AIPlayer()
        self._pending: Optional[asyncio.Future] = None
        self._pending_kind: Optional[str] = None   # 'turn' | 'claim' | 'rob_kong'
        self._last_ctx = None     # 最近一次请求上下文（added-kong 需查 melds）
        self.connected = False

    # ── 连接状态 ─────────────────────────────────────────

    def set_connected(self, connected: bool) -> None:
        """WS 层在连接建立/断开时调用。断开即把 pending 请求改为 AI 代打。"""
        self.connected = connected
        if not connected:
            self._disconnect_pending()

    # ── GameManager 侧：发起请求并等待动作 ─────────────────

    async def request_turn(self, ctx: TurnContext) -> dict:
        self._last_ctx = ctx
        if not self.connected:
            return await self._ai.request_turn(ctx)
        await self.conn.send_to_seat(
            self.seat, {'kind': 'turn_request', 'ctx': ctx.model_dump(by_alias=True)})
        return await self._wait('turn', ctx)

    async def request_claim(self, ctx: ClaimContext) -> dict:
        self._last_ctx = ctx
        if not self.connected:
            return await self._ai.request_claim(ctx)
        await self.conn.send_to_seat(
            self.seat, {'kind': 'claim_request', 'ctx': ctx.model_dump(by_alias=True)})
        return await self._wait('claim', ctx)

    async def request_rob_kong(self, ctx: RobKongContext) -> str:
        self._last_ctx = ctx
        if not self.connected:
            return await self._ai.request_rob_kong(ctx)
        await self.conn.send_to_seat(
            self.seat, {'kind': 'rob_kong_request', 'ctx': ctx.model_dump(by_alias=True)})
        return await self._wait('rob_kong', ctx)

    async def _wait(self, kind: str, ctx):
        """挂起等待客户端动作；超时 / 断线 → AI 代打。"""
        future = asyncio.get_event_loop().create_future()
        self._pending = future
        self._pending_kind = kind
        try:
            result = await asyncio.wait_for(future, self.timeout)
            self._pending = None
            self._pending_kind = None
            if result is _DISCONNECTED:
                return await self._fallback(kind, ctx)
            return result
        except asyncio.TimeoutError:
            self._pending = None
            self._pending_kind = None
            return await self._fallback(kind, ctx)

    async def _fallback(self, kind: str, ctx):
        """超时 / 断线自动代打：复用 AIPlayer 的决策（含碰后无牌可打 → pass 的修复）。"""
        if kind == 'turn':
            return await self._ai.request_turn(ctx)
        if kind == 'claim':
            return await self._ai.request_claim(ctx)
        return await self._ai.request_rob_kong(ctx)

    # ── WS 层侧：收到客户端动作 ──────────────────────────

    def handle_action(self, message: dict) -> tuple[bool, str]:
        """投递客户端动作。返回 (是否受理, 错误码)。"""
        if self._pending is None:
            return False, 'STALE_ACTION'
        action, err = self._validate(message)
        if action is None:
            return False, err
        future = self._pending
        self._pending = None
        self._pending_kind = None
        if not future.done():
            future.set_result(action)
        return True, ''

    def _validate(self, message: dict) -> tuple[Optional[object], str]:
        """按当前请求类型校验并规整客户端动作。返回 (action, 错误码)。"""
        kind = self._pending_kind
        mtype = message.get('type')

        if kind == 'turn':
            if mtype == 'discard':
                hi = message.get('handIndex')
                if not isinstance(hi, int) or hi < 0:
                    return None, 'INVALID_ACTION'
                return {'kind': 'discard', 'handIndex': hi}, ''
            if mtype == 'hu':
                return {'kind': 'win'}, ''
            if mtype == 'gang':
                gk = message.get('kind')
                if gk == 'concealed':
                    tile = message.get('tile')
                    if not tile:
                        return None, 'INVALID_ACTION'
                    return {'kind': 'concealed-kong', 'tile': tile}, ''
                if gk == 'added':
                    tile = message.get('tile')
                    meld_index = self._find_peng_meld_index(tile)
                    if meld_index < 0:
                        return None, 'INVALID_ACTION'
                    return {'kind': 'added-kong', 'meldIndex': meld_index}, ''
                return None, 'INVALID_ACTION'
            return None, 'INVALID_ACTION'

        if kind == 'claim':
            if mtype == 'claim':
                a = message.get('action')
                if a in ('peng', 'gang', 'pass'):
                    return {'kind': a}, ''
                return None, 'INVALID_ACTION'
            if mtype == 'pass':
                return {'kind': 'pass'}, ''
            return None, 'INVALID_ACTION'

        if kind == 'rob_kong':
            if mtype == 'hu':
                return 'win', ''
            if mtype == 'pass':
                return 'pass', ''
            return None, 'INVALID_ACTION'

        return None, 'INVALID_ACTION'

    def _find_peng_meld_index(self, tile) -> int:
        """补杠时由牌找碰副露索引（客户端只发牌，服务端定位副露）。"""
        ctx = self._last_ctx
        if ctx is None:
            return -1
        for i, meld in enumerate(ctx.melds):
            if meld.type == 'peng' and meld.tile == tile:
                return i
        return -1

    def _disconnect_pending(self) -> None:
        future = self._pending
        self._pending = None
        self._pending_kind = None
        if future is not None and not future.done():
            future.set_result(_DISCONNECTED)

    # ── PlayerController 其余协议方法 ────────────────────

    def on_discarded(self) -> None:
        pass

    def reset(self) -> None:
        self._disconnect_pending()
