"""幺筒服务端 AI 与前端 ai.ts 的同种子金标对拍。"""

import json
from pathlib import Path

from app.core.yaotong.ai import choose_action
from app.core.yaotong.engine import StartOptions, act, create_game

WALL = [tile for tile in range(27) for _ in range(4)]
FIXTURE = Path(__file__).parent / 'fixtures' / 'yaotong_ai_golden.json'


def norm_command(command):
    return {
        'type': command.type, 'index': command.index,
        'declare': command.declare, 'action': command.action,
        'plan': command.plan, 'tile': command.tile,
    }


def norm_final(state):
    result = state.result
    return {
        'phase': state.phase, 'serial': state.serial, 'steps': None,
        'scores': [player.score for player in state.players],
        'handCounts': [len(player.hand) for player in state.players],
        'riverCounts': [len(player.river) for player in state.players],
        'melds': [
            [[meld.tile, meld.kind] for meld in player.melds]
            for player in state.players
        ],
        'result': None if result is None else {
            'kind': result.kind, 'winners': list(result.winners),
            'source': result.source, 'winKind': result.win_kind,
            'tile': result.tile, 'reason': result.reason,
            'indicator': result.indicator, 'chicken': result.chicken,
            'deltas': list(result.deltas), 'nextDealer': result.next_dealer,
            'finished': result.finished,
            'transfers': [
                [t.from_seat, t.to, t.account, t.base, t.multiplier,
                 t.amount, t.reason]
                for t in result.transfers
            ],
            'values': {
                str(seat): {
                    'h': value.h,
                    'patterns': [[p.name, p.points] for p in value.patterns],
                    'naturalYaos': value.natural_yaos,
                    'winningYaoNatural': value.winning_yao_natural,
                    'wildcards': value.wildcards,
                }
                for seat, value in result.values.items()
            },
        },
    }


def diff(left, right, path=''):
    if type(left) is not type(right):
        return f'{path}: type'
    if isinstance(left, dict):
        if set(left) != set(right):
            return f'{path}: keys {set(left) ^ set(right)}'
        for key in left:
            found = diff(left[key], right[key], f'{path}.{key}')
            if found:
                return found
        return ''
    if isinstance(left, list):
        if len(left) != len(right):
            return f'{path}: len {len(left)} != {len(right)}'
        for index, (left_item, right_item) in enumerate(zip(left, right)):
            found = diff(left_item, right_item, f'{path}[{index}]')
            if found:
                return found
        return ''
    if left != right:
        return f'{path}: {left!r} != {right!r}'
    return ''


def test_ai_matches_typescript_golden():
    golden = json.loads(FIXTURE.read_text(encoding='utf-8'))
    state = create_game(StartOptions(wall=list(WALL), dice=(1, 2), dealer=0))
    trace = []
    steps = 0
    while state.phase != 'settled' and steps < 2000:
        seat = -1
        command = None
        if state.phase == 'original':
            seat = next(
                index for index, decided in enumerate(state.original_decisions)
                if not decided
            )
            command = choose_action(state, seat)
        elif state.phase == 'discard':
            seat = state.current
            command = choose_action(state, seat)
        elif state.phase == 'response':
            pending = state.pending
            for candidate in range(4):
                if (candidate in pending.options
                        and candidate not in pending.choices):
                    choice = choose_action(state, candidate)
                    if choice is not None:
                        seat = candidate
                        command = choice
                        break
        assert command is not None and seat >= 0
        ok = act(state, seat, command)
        trace.append({
            'step': steps, 'seat': seat,
            'command': norm_command(command), 'ok': ok,
        })
        assert ok is True
        steps += 1

    final = norm_final(state)
    final['steps'] = steps
    assert steps == golden['steps']
    assert diff(trace, golden['trace'], 'trace') == ''
    assert diff(final, golden['final'], 'final') == ''
    assert state.result is not None
    assert sum(state.result.deltas) == 0
