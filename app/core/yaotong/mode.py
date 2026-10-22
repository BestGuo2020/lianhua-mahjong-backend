"""幺筒模式边界 —— 对应 src/game/variants/yaotong/mode.ts。

新手模式只拦截人类输入；服务端也执行同一套拦截，避免客户端被绕过。
老手模式不拦截，违规由引擎按规则包牌。
"""

from __future__ import annotations

from app.core.yaotong.engine import self_options
from app.core.yaotong.tiles import is_chicken
from app.core.yaotong.types import Command, GameState, Seat

NO_PROTECTION = {
    'hu': '', 'peng': '', 'kong': '', 'pass': '', 'discards': [],
}


def action_protection(state: GameState, seat: Seat = 0) -> dict:
    player = state.players[seat]
    response = (
        state.pending.options.get(seat)
        if state.phase == 'response' and state.pending is not None
        and seat not in state.pending.choices
        else None
    )
    own_turn = state.phase == 'discard' and state.current == seat
    options = response or self_options(state, seat)
    mandatory = '本次必须胡牌，系统已拦截其他操作' if options.mandatory else ''
    peng = '本巡已打鸡，系统已拦截碰鸡' if options.restricted_peng else ''
    if response is not None and response.peng and state.pending is not None:
        rest = list(player.hand)
        for _ in range(2):
            try:
                rest.remove(state.pending.tile)
            except ValueError:
                break
        if all(
            tile in player.chicken_peng
            or (is_chicken(state.pending.tile) and tile == state.pending.tile)
            for tile in rest
        ):
            peng = '碰牌后将只能打同种鸡，系统已拦截此次碰牌'
    return {
        'hu': '过手胡尚未解除，系统已拦截胡牌' if options.restricted_hu else '',
        'peng': mandatory or peng,
        'kong': mandatory,
        'pass': mandatory,
        'discards': [
            index for index, tile in enumerate(player.hand)
            if own_turn and (
                mandatory or tile in player.chicken_peng
            )
        ] if own_turn else [],
    }


def protection_reason(protection: dict, command: Command) -> str:
    if command.type == 'respond' and command.action is not None:
        return protection.get(command.action, '')
    if command.type in ('hu', 'kong'):
        return protection.get(command.type, '')
    if command.type == 'discard' and command.index is not None \
            and command.index in protection['discards']:
        return protection.get('pass') or '本巡已碰鸡，系统已拦截打鸡'
    return ''
