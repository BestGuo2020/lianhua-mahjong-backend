"""幺筒「已胡但未确认」窗口 —— 对应 src/game/variants/yaotong/huWindow.ts。

TS 用 WeakMap 按 GameState 身份存快照；Python 把快照挂在 state 的动态属性上，
避免全局 id 表泄漏。快照是深拷贝，调用方可以安全读取。
"""

from __future__ import annotations

from copy import deepcopy
from typing import Optional

from app.core.yaotong.types import GameState, Seat

_WINDOW_ATTR = '_hu_windows'


def hu_window_seats(state: GameState) -> list[Seat]:
    return list(state.__dict__.get(_WINDOW_ATTR, {}))


def retain_hu_window(state: GameState, seat: Seat) -> None:
    windows = state.__dict__.pop(_WINDOW_ATTR, {})
    snapshot = deepcopy(state)
    snapshot.phase = 'response'
    if snapshot.pending is not None:
        for waiting in [*windows, seat]:
            snapshot.pending.choices.pop(waiting, None)
    # 多人可能同时暂留同一张弃牌；每个座位共享最新响应结果的独立快照。
    for waiting in [*windows, seat]:
        windows[waiting] = deepcopy(snapshot)
    state.__dict__[_WINDOW_ATTR] = windows


def hu_window(state: GameState, seat: Seat) -> Optional[GameState]:
    if state.phase == 'settled':
        return None
    return state.__dict__.get(_WINDOW_ATTR, {}).get(seat)


def take_hu_window(state: GameState, seat: Optional[Seat] = None):
    windows = state.__dict__.get(_WINDOW_ATTR, {})
    if not windows:
        return None
    if seat is None:
        seat = next(iter(windows))
    snapshot = windows.pop(seat, None)
    if snapshot is not None:
        for remaining in windows.values():
            if remaining.pending is not None:
                remaining.pending.choices[seat] = 'pass'
    return (seat, snapshot) if snapshot is not None else None


def clear_hu_windows(state: GameState) -> None:
    state.__dict__.pop(_WINDOW_ATTR, None)
