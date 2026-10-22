"""幺筒牌/牌墙的移植测试。"""

import pytest

from app.core.yaotong import tiles


class LCG:
    """与后续 TS 夹具共用的确定性随机源（数值乘同余）。"""

    def __init__(self, seed: int):
        self.state = seed

    def __call__(self) -> float:
        self.state = (self.state * 1664525 + 1013904223) % (2 ** 32)
        return self.state / (2 ** 32)


def test_tiles_constants():
    assert len(tiles.TILES) == 27
    assert tiles.TILES[0] == 0 and tiles.TILES[-1] == 26
    assert tiles.FIXED == (0, 16, 18)
    assert tiles.YAO == 9
    assert tiles.WALL_SIZE == 108


def test_chicken_and_fixed():
    assert tiles.is_fixed(0)
    assert tiles.is_fixed(16)
    assert tiles.is_fixed(18)
    assert not tiles.is_fixed(1)
    for tile in (0, 9, 16, 18):
        assert tiles.is_chicken(tile)
    for tile in (1, 8, 10, 15, 17, 19, 26):
        assert not tiles.is_chicken(tile)


@pytest.mark.parametrize('tile,name', [
    (0, '一万'), (8, '九万'), (9, '一筒'), (17, '九筒'),
    (18, '一条'), (26, '九条'),
])
def test_tile_name(tile, name):
    assert tiles.tile_name(tile) == name


def test_next_tile_wraps_inside_suit():
    assert tiles.next_tile(0) == 1
    assert tiles.next_tile(8) == 0
    assert tiles.next_tile(9) == 10
    assert tiles.next_tile(17) == 9
    assert tiles.next_tile(18) == 19
    assert tiles.next_tile(26) == 18


def test_tile_file():
    assert tiles.tile_file(0) == '1m.png'
    assert tiles.tile_file(8) == '9m.png'
    assert tiles.tile_file(9) == '1p.png'
    assert tiles.tile_file(18) == '1s.png'
    assert tiles.tile_file(26) == '9s.png'


def test_sort_hand_is_in_place_like_ts():
    hand = [26, 0, 16, 9]
    result = tiles.sort_hand(hand)
    assert result is hand
    assert hand == [0, 9, 16, 26]


def test_counts_of():
    counts = tiles.counts_of([0, 0, 9, 26, 26, 26])
    assert counts[0] == 2
    assert counts[9] == 1
    assert counts[26] == 3
    assert sum(counts) == 6
    assert len(counts) == 27


def test_shuffled_wall_is_108_and_four_of_each():
    wall = tiles.shuffled_wall(LCG(20261023))
    assert len(wall) == 108
    assert tiles.counts_of(wall) == [4] * 27


def test_shuffled_wall_is_deterministic_with_same_rng():
    first = tiles.shuffled_wall(LCG(7))
    second = tiles.shuffled_wall(LCG(7))
    other = tiles.shuffled_wall(LCG(8))
    assert first == second
    assert first != other


def test_break_wall_side_offset_and_preservation():
    wall = tiles.shuffled_wall(LCG(1))
    result = tiles.break_wall(wall, (3, 5), dealer=0)
    # side = (0 + (3+5-1)%4) % 4 = 3; reserved = 3; offset = 82 + 6 = 88
    assert result['side'] == 3
    assert result['reserved'] == 3
    assert result['wall'][0] == wall[88]
    assert result['wall'][-1] == wall[87]
    assert sorted(result['wall']) == sorted(wall)


def test_break_wall_relative_to_dealer():
    wall = tiles.shuffled_wall(LCG(1))
    result = tiles.break_wall(wall, (3, 5), dealer=1)
    # side = (1 + 3) % 4 = 0; offset = 0 + 6 = 6
    assert result['side'] == 0
    assert result['reserved'] == 3
    assert result['wall'][0] == wall[6]


def test_break_wall_reserved_uses_smaller_die():
    wall = list(range(108))
    assert tiles.break_wall(wall, (6, 2), dealer=0)['reserved'] == 2


def test_tile_type_roundtrip():
    assert tiles.to_tile_type(0) == 'm1'
    assert tiles.to_tile_type(9) == 'p1'
    assert tiles.to_tile_type(18) == 's1'
    assert tiles.to_tile_type(26) == 's9'
    for tile in tiles.TILES:
        assert tiles.from_tile_type(tiles.to_tile_type(tile)) == tile


@pytest.mark.parametrize('value', ['m0', 'm10', 'x1', 'm', '', None])
def test_from_tile_type_rejects_invalid(value):
    with pytest.raises(ValueError):
        tiles.from_tile_type(value)


def test_invalid_tile_helpers_raise():
    with pytest.raises(ValueError):
        tiles.tile_name(27)
    with pytest.raises(ValueError):
        tiles.to_tile_type(-1)
