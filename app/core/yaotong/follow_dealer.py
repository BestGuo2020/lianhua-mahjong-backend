"""首圈跟庄；同前端 followDealer.ts。"""
from app.core.yaotong.tiles import YAO, is_fixed
from app.core.yaotong.types import GameState, Tile


def follow_dealer_base(tile: Tile) -> int:
    return 170 if tile == YAO else 15 if is_fixed(tile) else 5


def complete_follow_dealer(state: GameState) -> Tile | None:
    if state.players[state.dealer].follow_dealer:
        return None
    order = [state.players[(state.dealer + i) % 4] for i in range(4)]
    if any(p.discards != 1 or len(p.river) != 1 or p.melds for p in order):
        return None
    tile = order[0].river[0].tile
    if any(p.river[0].tile != tile or (i > 0 and p.river[0].id <= order[i - 1].river[0].id)
           for i, p in enumerate(order)):
        return None
    for player in order:
        player.river[0].followed_dealer = True
        player.river[0].chicken = 0
    state.players[state.dealer].follow_dealer = True
    return tile
