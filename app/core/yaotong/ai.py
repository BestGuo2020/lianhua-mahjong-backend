"""幺筒服务端补位 AI —— 从 src/game/variants/yaotong/ai.ts 1:1 移植。

只使用本家手牌和公开信息；返回 None 表示当前没有该座位的动作。
"""

from __future__ import annotations

from typing import Optional

from app.core.yaotong.engine import kong_options, self_options
from app.core.yaotong.hands import discard_waits, is_untouched_first_draw, waits
from app.core.yaotong.tiles import YAO, counts_of, is_chicken
from app.core.yaotong.types import Command, GameState, Seat


def choose_action(state: GameState, seat: Seat) -> Optional[Command]:
    player = state.players[seat]

    if state.phase == 'original' and not state.original_decisions[seat]:
        return Command(type='original', declare=bool(waits(player)))

    if state.phase == 'response':
        pending = state.pending
        if pending is None:
            return None
        options = pending.options.get(seat)
        if options is None or pending.choices.get(seat):
            return None
        if options.hu and not options.restricted_hu:
            return Command(type='respond', action='hu')
        if options.kong:
            return Command(type='respond', action='kong')
        if (options.peng and not options.restricted_peng
                and pending.tile != YAO):
            # 不主动碰成「下一张只能打鸡」的必然违规。
            rest = list(player.hand)
            for _ in range(2):
                try:
                    rest.remove(pending.tile)
                except ValueError:
                    break
            if any(
                tile not in player.chicken_peng
                and (not is_chicken(pending.tile) or tile != pending.tile)
                for tile in rest
            ):
                return Command(type='respond', action='peng')
        return Command(type='respond', action='pass')

    if state.phase != 'discard' or state.current != seat:
        return None

    hu = self_options(state, seat)
    if hu.hu and not hu.restricted_hu:
        return Command(type='hu')

    kongs = kong_options(state, seat)
    if kongs:
        return Command(type='kong', tile=kongs[0])

    if player.declaration != 'none':
        return Command(type='discard', index=len(player.hand) - 1)

    legal = [
        {'tile': tile, 'index': index}
        for index, tile in enumerate(player.hand)
        if tile not in player.chicken_peng
    ]
    available = legal or [
        {'tile': tile, 'index': index}
        for index, tile in enumerate(player.hand)
    ]
    available_indexes = {entry['index'] for entry in available}
    listening = [
        option for option in discard_waits(player)
        if option['index'] in available_indexes
    ]
    if listening:
        public_counts = counts_of([
            *player.hand,
            *[
                tile
                for other in state.players
                for tile in [
                    *(discard.tile for discard in other.river),
                    *[
                        meld.tile
                        for meld in other.melds
                        for _ in range(3 if meld.kind == 'peng' else 4)
                    ],
                ]
            ],
        ])

        def wait_value(option: dict) -> int:
            return sum(
                max(0, 4 - public_counts[wait.tile])
                for wait in option['waits']
            )

        listening.sort(key=wait_value, reverse=True)
        return Command(
            type='discard',
            index=listening[0]['index'],
            declare=is_untouched_first_draw(player),
        )

    counts = counts_of(player.hand)

    def keep(tile: int) -> int:
        if tile == YAO:
            return 100
        score = counts[tile] * 8 + (2 if is_chicken(tile) else 0)
        for step in (-2, -1, 1, 2):
            neighbor = tile + step
            if neighbor // 9 == tile // 9:
                score += counts[neighbor] * (4 if abs(step) == 1 else 2)
        return score

    available.sort(key=lambda entry: keep(entry['tile']))
    return Command(type='discard', index=available[0]['index'])
