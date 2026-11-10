from types import SimpleNamespace

import pytest

from app.game.blood_flow_room import BloodFlowRoomSession
from app.game.room import RoomSession


@pytest.mark.parametrize('blood_flow', [False, True])
def test_lotus_rooms_scope_allowlist_and_rate_limit_table_interactions(blood_flow, monkeypatch):
    room = BloodFlowRoomSession('TABLE') if blood_flow else RoomSession('TABLE')
    room.status = 'playing'
    room.seats[1] = object()
    if blood_flow:
        room.engine = SimpleNamespace(result=None, round_id='first')
    else:
        room.manager = SimpleNamespace(round=1, honba=0, phase='thinking', match_finished=False)
    messages = []
    monkeypatch.setattr(room.conn, 'broadcast', messages.append)
    scope = room.table_interaction_scope()
    assert scope

    def send(seat, action, hand=scope):
        assert room.handle_client_message(seat, {
            'kind': 'table_interaction_request', 'scope': hand, 'fromSeat': 3, 'action': action,
        }) == (True, '')

    chat = {'type': 'quick_chat', 'phraseId': 'think'}
    send(0, chat)
    send(1, {'type': 'quick_chat', 'phraseId': 'follow'})
    send(1, {'type': 'emote', 'emote': 'like', 'targetSeat': 1})
    send(1, {'type': 'emote', 'emote': 'like', 'targetSeat': True})
    assert messages == []

    send(1, chat)
    send(1, chat)
    emote = {'type': 'emote', 'emote': 'like', 'targetSeat': 3}
    send(1, emote)
    send(1, emote)
    assert [event['action'] for event in messages] == [chat, emote]
    assert all(event['fromSeat'] == 1 and event['scope'] == scope for event in messages)

    if blood_flow:
        room.engine = SimpleNamespace(result=None, round_id='next')
    else:
        room.manager.honba = 1
    next_scope = room.table_interaction_scope()
    assert next_scope and next_scope != scope
    send(1, chat)
    assert len(messages) == 2
    send(1, chat, next_scope)
    assert messages[-1]['scope'] == next_scope
    assert messages[-1]['id'] == 3

    room.status = 'finished'
    assert room.table_interaction_scope() is None
    send(1, emote, next_scope)
    assert len(messages) == 3
