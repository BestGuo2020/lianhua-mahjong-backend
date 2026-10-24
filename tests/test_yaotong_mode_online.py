"""从房间输入边界验证新手保护、老手包牌及四座位的一致性。"""
from copy import deepcopy

import pytest

from app.core.yaotong.engine import act, continue_game
from app.core.yaotong.mode import action_protection
from app.core.yaotong.types import Command, Meld, Player
from app.game.yaotong_room import YaotongRoomSession
from tests.test_yaotong_online_regressions import table, tiles


CASES = [
    'peng-then-discard', 'discard-then-peng', 'passed-self', 'passed-discard',
    'kill-self-discard', 'kill-self-kong', 'kill-discard-pass', 'hot-pass', 'rob-pass',
    'false-hu', 'alias-passed-hu', 'alias-mandatory-pass', 'defer-mandatory-pass',
]


def scenario(name, actor=0, prior_pass=None):
    state = table()
    source = (actor + 1) % 4
    state.current = actor
    action = {'type': 'discard', 'index': 13}
    reason = ''
    if name == 'peng-then-discard':
        state.players[actor] = Player(name='玩家', hand=tiles('147m 258p 369s 2468s 1p'), draws=2, chicken_peng=[9])
        reason = '同巡碰鸡后打鸡'
    elif name in ('passed-self', 'alias-passed-hu'):
        state.players[actor] = Player(name='玩家', hand=tiles('234567m 234s 66p 789s'), draws=2, drawn=26, passed_hu=45)
        action = {'type': 'hu' if name == 'passed-self' else 'declareHu'}
        reason = '违反过手胡限制'
    elif name.startswith('kill-self') or name == 'alias-mandatory-pass':
        state.players[actor] = Player(name='玩家', hand=tiles('111123m 456789s 22p'), draws=2, drawn=10)
        state.players[source].declaration = 'ready'
        if name == 'kill-self-kong':
            action = {'type': 'kong', 'tile': 0}
        elif name == 'alias-mandatory-pass':
            action = {'type': 'passHu'}
        reason = '可杀报却放弃胡牌'
    elif name == 'false-hu':
        state.players[actor].hand.append(26)
        state.players[actor].drawn = 26
        action = {'type': 'declareHu'}
        reason = '炸胡'
    else:
        state.current = source
        tile = 26
        action = {'type': 'respond', 'action': 'pass'}
        if name == 'discard-then-peng':
            tile = 18
            state.players[actor] = Player(name='玩家', hand=tiles('11s 147m 258p 369s 24s'), draws=2, chicken_discard=[18])
            action['action'] = 'peng'
            reason = '同巡打鸡后碰鸡'
        elif name == 'passed-discard':
            state.players[actor] = Player(name='玩家', hand=tiles('234567m 234s 66p 78s'), draws=2,
                                          declaration='ready', passed_hu=45)
            action['action'] = 'hu'
            reason = '违反过手胡限制'
        elif name in ('hot-pass', 'rob-pass'):
            tile = 14
            state.players[actor] = Player(name='玩家', hand=tiles('234m 456789s 6p 45p 1p'), draws=2)
            reason = '手中有幺筒却未抢杠／接热炮'
        else:
            state.players[actor] = Player(name='玩家', hand=tiles('234567m 234s 66p 78s'), draws=2,
                                          declaration='ready')
            state.players[source].declaration = 'ready'
            reason = '可杀报却放弃胡牌'
            if name == 'defer-mandatory-pass':
                action['deferHu'] = True
        if prior_pass is not None:
            state.players[actor].passed_hu = prior_pass
            state.players[actor].passed_shape = prior_pass
        if name == 'rob-pass':
            state.players[source] = Player(name='出牌者', hand=tiles('6p 147s 258p 369s 2p'), draws=2,
                                           drawn=tile, melds=[Meld(tile=tile, kind='peng', from_seat=(actor + 2) % 4)])
            assert act(state, source, Command(type='kong', tile=tile))
        else:
            state.players[source].hand.append(tile)
            state.players[source].drawn = tile
            state.players[source].after_kong = name == 'hot-pass'
            assert act(state, source, Command(type='discard', index=13))
    assert state.result is None
    return state, action, reason


def room_for(name, mode, actor=0):
    room = YaotongRoomSession('MODE-AUDIT', yaotong_mode=mode)
    for seat in range(4):
        room.join_or_rejoin(f'P{seat}', player_id=f'p{seat}')
        room.on_connect(seat)
    room.state, action, reason = scenario(name, actor)
    room.status = 'playing'
    return room, action, reason


@pytest.mark.parametrize('mode', ['easy', 'hard'])
@pytest.mark.parametrize('actor', range(4))
@pytest.mark.parametrize('name', CASES)
def test_mode_boundary_in_every_seat(name, actor, mode):
    room, action, reason = room_for(name, mode, actor)
    before = deepcopy(room.state)
    ok, error = room.handle_client_message(actor, {'kind': 'action', 'action': action})
    if mode == 'easy':
        assert (ok, error) == (False, 'PROTECTED_ACTION')
        assert room.state == before
    else:
        assert ok, error
        result = room.state.result
        assert result is not None
        assert result.kind == 'penalty'
        assert result.offender == actor
        assert reason in result.reason
        assert result.deltas[actor] <= -135
        assert all(delta >= 45 for seat, delta in enumerate(result.deltas) if seat != actor)
        assert sum(result.deltas) == 0
        assert len(result.transfers) == 3
        assert all(item.account == '一般包牌赔付' for item in result.transfers)
        assert result.next_dealer == before.dealer
        next_state = continue_game(room.state)
        assert next_state.dealer == before.dealer
        assert next_state.hand_number == before.hand_number + 1
        for viewer in range(4):
            snapshot = room.snapshot_for(viewer)
            assert snapshot['roundResult']['offender'] == actor
            assert all(player['hand'] for player in snapshot['view']['players'])


@pytest.mark.parametrize('mode', ['easy', 'hard'])
@pytest.mark.parametrize('path', ['timeout', 'auto', 'disconnect'])
@pytest.mark.parametrize('name', ['peng-then-discard', 'discard-then-peng', 'passed-self',
                                  'kill-self-discard', 'kill-self-kong', 'kill-discard-pass', 'hot-pass', 'rob-pass'])
@pytest.mark.asyncio
async def test_automatic_control_avoids_involuntary_penalties(name, path, mode):
    room, _, _ = room_for(name, mode, actor=1)
    room.turn_timeout = 0
    serial = room.state.serial
    try:
        if path == 'auto':
            room.handle_client_message(1, {'kind': 'auto', 'enabled': True})
        elif path == 'disconnect':
            room.on_disconnect(1)
        # 老手仅胡窗口不走 20 秒操作超时，下面另测观察期与胡口关闭的判罚。
        if path == 'timeout' and room._human_automatic_command(1) is not None:
            await room._human_or_ai(1)
            if name == 'rob-pass':
                assert room.state.result.kind == 'penalty'
                assert room.state.result.offender == 1
            else:
                assert room.state.result is None
            return
        else:
            await room._human_or_ai(1)
        assert room.state.result is None or room.state.result.kind != 'penalty'
        assert room.state.serial > serial
        assert not room._forced_finishing
    finally:
        room.close()


@pytest.mark.parametrize('mode', ['easy', 'hard'])
def test_illegal_basic_moves_are_rejected_without_penalty(mode):
    room, _, _ = room_for('false-hu', mode, actor=2)
    before = deepcopy(room.state)
    for action in ({'type': 'discard', 'index': 999}, {'type': 'kong', 'tile': 0},
                   {'type': 'respond', 'action': 'peng'}):
        assert room.handle_client_message(2, {'kind': 'action', 'action': action}) == (False, 'INVALID_ACTION')
        assert room.state == before


def test_easy_peng_guard_prevents_a_hand_with_only_forbidden_discards():
    room, _, _ = room_for('discard-then-peng', 'easy')
    room.state.pending.options[0].restricted_peng = False
    room.state.players[0].hand = tiles('1111s')
    room.state.players[0].melds = [Meld(tile=tile, kind='peng') for tile in (0, 3, 6)]
    assert '只能打同种鸡' in action_protection(room.state)['peng']
    assert room.handle_client_message(0, {'kind': 'action', 'action': {'type': 'respond', 'action': 'peng'}}) == (False, 'PROTECTED_ACTION')


@pytest.mark.parametrize('mode', ['easy', 'hard'])
@pytest.mark.parametrize('name', ['hot-pass', 'rob-pass'])
def test_rob_and_hot_hu_override_previous_pass_in_both_modes(mode, name):
    room, _, _ = room_for(name, mode, actor=2)
    room.state, _, _ = scenario(name, actor=2, prior_pass=100)
    assert not room.state.pending.options[2].restricted_hu
    assert room.state.pending.options[2].mandatory
    assert room.handle_client_message(2, {'kind': 'action', 'action': {'type': 'respond', 'action': 'hu'}})[0]
    assert room.state.result.kind == 'win'


@pytest.mark.parametrize('mode', ['easy', 'hard'])
def test_different_chicken_and_genuinely_bigger_hand_are_not_overprotected(mode):
    room, command, _ = room_for('peng-then-discard', mode)
    room.state.players[0].chicken_peng = [18]  # 碰过一条，不禁止打幺筒。
    assert room.handle_client_message(0, {'kind': 'action', 'action': command})[0]
    assert room.state.result is None
    room, _, _ = room_for('passed-self', mode)
    room.state.players[0] = Player(name='玩家', hand=tiles('22233344455566m'), draws=2, drawn=5,
                                   passed_hu=10, passed_shape=10)
    assert room.handle_client_message(0, {'kind': 'action', 'action': {'type': 'hu'}})[0]
    assert room.state.result.kind == 'win'


@pytest.mark.parametrize('mode', ['easy', 'hard'])
def test_restricted_hu_is_not_mistaken_for_mandatory_kill(mode):
    room, _, _ = room_for('passed-self', mode)
    room.state.players[1].declaration = 'ready'
    assert not action_protection(room.state)['pass']
    assert room.handle_client_message(0, {'kind': 'action', 'action': {'type': 'discard', 'index': 13}})[0]
    assert room.state.result is None


@pytest.mark.asyncio
@pytest.mark.parametrize('name', ['kill-discard-pass', 'hot-pass'])
@pytest.mark.parametrize('decision', ['late-hu', 'explicit-pass', 'next-discard'])
async def test_expert_retained_mandatory_hu_is_judged_when_window_closes(name, decision):
    room, _, reason = room_for(name, 'hard', actor=1)
    try:
        await room._human_or_ai(1)
        assert room.state.result is None  # 观察期结束并不等于主动过胡。
        assert room.snapshot_for(1)['huWindow']['ownOptions']['mandatory']
        if decision == 'next-discard':
            seat = room.state.current
            action = {'type': 'discard', 'index': len(room.state.players[seat].hand) - 1}
        else:
            seat = 1
            assert room.handle_client_message(1, {'kind': 'hold', 'holding': True})[0]
            action = {'type': 'declareHu' if decision == 'late-hu' else 'passHu'}
        assert room.handle_client_message(seat, {'kind': 'action', 'action': action})[0]
        if decision == 'late-hu':
            assert room.state.result.kind == 'win'
            assert room.state.result.winners == [1]
        else:
            assert room.state.result.kind == 'penalty'
            assert room.state.result.offender == 1
            assert reason in room.state.result.reason
    finally:
        room.close()
