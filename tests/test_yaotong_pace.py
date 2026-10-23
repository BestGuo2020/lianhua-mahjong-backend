"""Compare actual room events with the same timeline exercised by the Vue local game."""
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
import time

import pytest

from app.core.yaotong.engine import StartOptions, create_game, observe_game_events
from app.core.yaotong.types import Command, Meld, Player
import app.game.yaotong_room as room_module
from app.game.yaotong_room import YaotongRoomSession

CASES = json.loads((Path(__file__).parent / 'fixtures' / 'yaotong_pace_golden.json').read_text(encoding='utf-8'))


def tiles(text):
    return ['mps'.index(part[-1]) * 9 + int(n) - 1 for part in text.split() for n in part[:-1]]


def state_for(scenario):
    state = create_game(StartOptions(dealer=0, dice=(2, 3)))
    state.phase = scenario.get('phase', 'discard')
    state.current = scenario['actor']
    state.wall = [2, 5, 8, 11, 14, 17, 21, 25, 26]
    state.players = [Player(name=f'P{seat}', hand=tiles('147m 258p 369s 2468s'), draws=2, discards=1)
                     for seat in range(4)]
    for seat, data in scenario['players'].items():
        player = state.players[int(seat)]
        player.hand = tiles(data['hand'])
        player.drawn = data.get('drawn')
        player.after_kong = data.get('afterKong', False)
        player.declaration = data.get('declaration', 'none')
        if 'peng' in data:
            player.melds = [Meld(tile=data['peng'], kind='peng', from_seat=3)]
    return state


@pytest.mark.asyncio
@pytest.mark.parametrize('scenario', CASES, ids=lambda case: case['name'])
async def test_room_event_timeline_matches_local_game(monkeypatch, scenario):
    clock = [0.0]
    monkeypatch.setattr(room_module, 'time', SimpleNamespace(monotonic=lambda: clock[0], time=time.time))
    room = YaotongRoomSession('PACE', pace={'enabled': True})
    room.join_or_rejoin('本家', player_id='p0')
    room.on_connect(0)
    room.state = state_for(scenario)

    async def advance(seconds):
        clock[0] += seconds
        await asyncio.sleep(0)

    monkeypatch.setattr(room, '_wait_pace', advance)
    events = []
    snapshots = []
    observe_game_events(room.state, lambda state, event: events.append([round(clock[0] * 1000), event.kind, event.seat]))
    monkeypatch.setattr(room, 'broadcast_snapshot', lambda: snapshots.append(room.snapshot_for(0)))
    if 'command' in scenario:
        ok, error = room.handle_client_message(scenario['actor'], {'kind': 'action', 'action': scenario['command']})
        assert ok, error
    for _ in range(30):
        if len(events) >= len(scenario['events']):
            break
        await room._drive_step()
    assert events == scenario['events']
    if scenario['name'] == 'open-kong':
        intermediate = next(snapshot for snapshot in snapshots if snapshot['flowStep'] == 'after-open-kong')
        assert intermediate['view']['players'][1]['concealedTileCount'] == 10
        assert intermediate['view']['wallCount'] == 9
    if scenario['name'] == 'added-kong':
        assert any(snapshot['flowStep'] == 'before-rob' for snapshot in snapshots)
        intermediate = next(snapshot for snapshot in snapshots if snapshot['flowStep'] == 'after-added-kong')
        assert intermediate['view']['players'][1]['concealedTileCount'] == 10
        assert intermediate['view']['wallCount'] == 9
    room.close()


def test_timeline_fixture_matches_frontend_when_both_checkouts_are_present():
    frontend = Path(__file__).resolve().parents[2] / 'tests' / 'fixtures' / 'yaotong-pace.json'
    if frontend.exists():
        assert json.loads(frontend.read_text(encoding='utf-8')) == CASES
