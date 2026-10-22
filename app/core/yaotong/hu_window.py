"""幺筒「已胡但未确认」窗口 —— 对应 src/game/variants/yaotong/huWindow.ts。

TS 用 WeakMap 按 GameState 身份存快照；Python 把快照挂在 state 的动态属性上，
避免全局 id 表泄漏。快照是深拷贝，调用方可以安全读取。
"""

from __future__ import annotations

from copy import deepcopy
from typing import Optional

from app.core.yaotong.types import GameState, Seat

_WINDOW_ATTR = '_hu_window'


def retain_hu_window(state: GameState, seat: Seat) -> None:
    state.__dict__.pop(_WINDOW_ATTR, None)
    snapshot = deepcopy(state)
    snapshot.phase = 'response'
    if snapshot.pending is not None:
        snapshot.pending.choices.pop(seat, None)
    state.__dict__[_WINDOW_ATTR] = (seat, snapshot)


def hu_window(state: GameState, seat: Seat) -> Optional[GameState]:
    entry = state.__dict__.get(_WINDOW_ATTR)
    if entry is None or entry[0] != seat or state.phase == 'settled':
        return None
    return entry[1]


def take_hu_window(state: GameState):
    return state.__dict__.pop(_WINDOW_ATTR, None)
