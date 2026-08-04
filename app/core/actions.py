"""动作执行层 —— 从 src/game/actions.ts 翻译

共享的「牌面执行」层：用户与 AI 走同一套物理操作（移除手牌、组成副露、
消掉弃牌、结算分数、播报动画/音效），避免两处并行实现逐渐漂移。

决策（做什么）在 ai.py，回合编排（谁继续、何时继续）留在 GameManager，
这里只负责「把某个动作在牌桌上执行掉」。
"""

from typing import Optional, Protocol

from app.models.game import GamePlayer, Meld, TileType
from app.core.rules import apply_kong_score


class ActionContext(Protocol):
    """执行层依赖的最小上下文：由 GameManager 注入可变状态与表现副作用。

    在联网版中，show_table_action / show_score_flow 内部改为 WebSocket 广播。
    """

    players: list[GamePlayer]
    current_player: object  # 带 .value 的可变引用

    def show_table_action(self, type_: str, actor_index: int, source_index: Optional[int],
                          tile: TileType, meld_index: int) -> None: ...

    def show_score_flow(self, deltas: list[dict]) -> None: ...

    def play_sound(self, name: str, volume: Optional[float] = None) -> None: ...


def remove_matches(hand: list[TileType], tile: TileType, amount: int) -> list[TileType]:
    """移除指定张数的牌（返回新列表，不修改原数组）"""
    nxt = list(hand)
    for _ in range(amount):
        nxt.remove(tile)
    return nxt


def remove_last_discard(discards: list[TileType], tile: TileType) -> None:
    """消除弃牌堆最后一张（碰/杠后使用）；末张不匹配则不动"""
    if discards and discards[-1] == tile:
        discards.pop()


def perform_peng(ctx: ActionContext, player_index: int, tile: TileType, from_: int) -> None:
    """碰：拿掉弃牌、手牌移除 2 张、组成碰副露，轮到本家，播报动画与音效。"""
    player = ctx.players[player_index]
    player.drawnTileIndex = -1
    remove_last_discard(ctx.players[from_].discards, tile)
    player.hand = remove_matches(player.hand, tile, 2)
    player.melds.append(Meld(type='peng', tile=tile, from_=from_, tiles=[tile, tile, tile]))
    ctx.current_player.value = player_index  # type: ignore[attr-defined]
    ctx.show_table_action('peng', player_index, from_, tile, len(player.melds) - 1)
    ctx.play_sound('peng.mp3')


def perform_discard_gang(ctx: ActionContext, player_index: int, tile: TileType, from_: int) -> None:
    """点杠（吃他家弃牌的杠）：拿掉弃牌、手牌移除 3 张、组成杠副露、
    结算杠分，轮到本家，播报动画与音效。后续补摸由调用方负责。"""
    player = ctx.players[player_index]
    player.drawnTileIndex = -1
    remove_last_discard(ctx.players[from_].discards, tile)
    player.hand = remove_matches(player.hand, tile, 3)
    player.melds.append(Meld(type='gang', tile=tile, from_=from_, tiles=[tile, tile, tile, tile]))
    score_deltas = apply_kong_score(ctx.players, player_index, 'discard', from_)
    ctx.current_player.value = player_index  # type: ignore[attr-defined]
    ctx.show_table_action('discard-gang', player_index, from_, tile, len(player.melds) - 1)
    ctx.show_score_flow(score_deltas)
    ctx.play_sound('gang.mp3')
