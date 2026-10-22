"""幺筒新手/老手模式：输入拦截与 AI 节奏对齐单机。"""

from app.core.yaotong.engine import StartOptions, create_game
from app.core.yaotong.mode import action_protection, protection_reason
from app.core.yaotong.types import Command, GameState, Pending, Player
from app.game.yaotong_room import YaotongRoomSession

WALL = [tile for tile in range(27) for _ in range(4)]


def state_with_chicken_hand() -> GameState:
    players = [Player(name='P0', hand=[1, 1, 2, 3, 4, 5, 6, 10, 11, 12, 13, 14, 15, 26])
               for _ in range(4)]
    players[0].chicken_peng = [1]
    return GameState(players=players, wall=[], phase='discard', current=0)


def test_new_player_protection_blocks_chicken_discard():
    state = state_with_chicken_hand()
    protection = action_protection(state, 0)
    assert 0 in protection['discards']  # 手里两张 2m，至少一张被打鸡限制
    assert protection_reason(
        protection, Command(type='discard', index=0)) == '本巡已碰鸡，系统已拦截打鸡'


def test_room_easy_mode_rejects_protected_action_server_side():
    room = YaotongRoomSession('YT-MODE', yaotong_mode='easy')
    room.state = state_with_chicken_hand()
    ok, error = room.handle_client_message(0, {
        'kind': 'action',
        'action': {'type': 'discard', 'index': 0},
    })
    assert ok is False
    assert error == 'PROTECTED_ACTION'


def test_ai_think_seconds_match_single_player_constants():
    room = YaotongRoomSession('YT-PACE', pace={'play': 1})
    state = create_game(StartOptions(wall=list(WALL), dice=(1, 2), dealer=0))

    state.phase = 'original'
    assert room._ai_think_seconds(state) == 0.5
    state.phase = 'discard'
    assert room._ai_think_seconds(state) == 1.0
    state.phase = 'response'
    state.pending = Pending(kind='discard', tile=1, from_seat=1)
    assert room._ai_think_seconds(state) == 0.5
    state.pending = Pending(kind='rob', tile=1, from_seat=1)
    assert room._ai_think_seconds(state) == 0.0

    room.ai_delay = 0
    assert room._ai_think_seconds(state) == 0.0


def test_room_remembers_mode():
    assert YaotongRoomSession('YT-E', yaotong_mode='easy').yaotong_mode == 'easy'
    assert YaotongRoomSession('YT-H', yaotong_mode='hard').yaotong_mode == 'hard'
    assert YaotongRoomSession('YT-X', yaotong_mode='invalid').yaotong_mode == 'easy'
