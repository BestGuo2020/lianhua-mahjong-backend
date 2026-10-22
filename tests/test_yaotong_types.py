"""幺筒数据类型的默认值与形状测试。"""

from app.core.yaotong.types import (
    ActionOptions,
    Command,
    Discard,
    GameEvent,
    GameState,
    Meld,
    Pending,
    Player,
    Settlement,
    Transfer,
)


def test_player_default_factories_are_independent():
    first = Player(name='A')
    second = Player(name='B')
    first.hand.append(0)
    first.chicken_peng.append(16)
    assert second.hand == []
    assert second.chicken_peng == []
    assert first.drawn is None
    assert first.declaration == 'none'


def test_game_state_defaults():
    state = GameState(players=[Player(name='A')], wall=[])
    assert state.phase == 'original'
    assert state.original_decisions == [False] * 4
    assert state.wait_remaining_ms == [15000] * 4
    assert state.match_length == 4
    assert state.pending is None
    state.original_decisions[0] = True
    assert GameState(players=[], wall=[]).original_decisions[0] is False


def test_meld_discard_and_pending_shapes():
    meld = Meld(tile=9, kind='peng', from_seat=2, chicken=1, responsibility=0)
    discard = Discard(tile=9, chicken=1, stage='first', id=3, first_chicken=True)
    pending = Pending(kind='discard', tile=9, from_seat=1, discard=discard)
    pending.options[0] = ActionOptions(hu=True, h=3)
    pending.choices[1] = 'pass'
    assert meld.from_seat == 2
    assert discard.first_chicken
    assert pending.options[0].hu
    assert pending.choices[1] == 'pass'


def test_transfer_and_settlement_minimal():
    transfer = Transfer(from_seat=1, to=0, account='鸡分', base=2, multiplier=1,
                        amount=2, reason='冲鸡')
    result = Settlement(
        kind='win', winners=[0], source=1, reason='点炮', deltas=[2, -2, 0, 0],
        next_dealer=0, finished=False, transfers=[transfer],
    )
    assert result.transfers[0].account == '鸡分'
    assert result.values == {}
    assert result.ready == []


def test_event_and_command():
    event = GameEvent(id=1, seat=0, text='碰', kind='peng', tile=9)
    command = Command(type='discard', index=3, declare=False)
    assert event.kind == 'peng'
    assert command.type == 'discard'
    assert command.index == 3
