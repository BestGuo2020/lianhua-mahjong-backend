import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from app.core.yaotong.ai import choose_action
from app.core.yaotong.tiles import tile_name
from app.core.yaotong.types import Command, GameEvent, GameState, Meld, Player
from app.game import yaotong_room
from app.game.yaotong_room import YaotongRoomSession
from app.llm import yaotong_agent
from app.llm.config import LlmProvider, LlmServerConfig


@pytest.fixture
def room():
    session = YaotongRoomSession('AGENT1', llm_enabled=True, yaotong_mode='hard')
    session.join_or_rejoin('玩家')
    players = [Player(f'玩家{seat}', hand=[8, 8, 8]) for seat in range(3)]
    players.append(Player(
        '幺筒 Agent', hand=[0, 1, 1, 4, 7, 10, 12, 14, 20, 22, 26],
        melds=[Meld(0, 'peng', from_seat=2)], chicken_peng=[0],
    ))
    # 牌墙耗尽，碰出的第四张鸡可以继续留手，但本回合已经不能补杠。
    session.state = GameState(players=players, wall=[], current=3, phase='discard')
    session.llm_seats[3] = yaotong_agent.YaotongDiscardAgent(LlmServerConfig())
    return session


async def test_room_assigns_explicit_and_default_models_to_every_empty_seat(monkeypatch):
    providers = {
        'default': LlmProvider('default', base_url='https://api.deepseek.com/v1', model='default-model'),
        'first': LlmProvider('first', model='first-model', nickname='千问牌友', avatar_folder='qwen'),
        'second': LlmProvider('second', model='second-model', nickname='GPT牌友', provider_type='openai'),
    }
    monkeypatch.setattr(yaotong_room, 'load_llm_providers', lambda: providers)
    room = YaotongRoomSession('MULTI1', llm_enabled=True)
    seat, _, _ = room.join_or_rejoin('房主')
    room.ready_seat(seat, True)
    room.set_reserved_seat(1, 'first', '高冷')
    room.set_reserved_seat(3, 'first')
    await room.start(llm_seats=[{'seat': 3, 'providerId': 'second'}])
    try:
        assert {seat: agent.config.model for seat, agent in room.llm_seats.items()} == {
            1: 'first-model', 2: 'default-model', 3: 'second-model',
        }
        assert room.llm_seats[1].config.style == '高冷'
        players = room.snapshot_for(0)['view']['players']
        assert [player['playerKind'] for player in players] == ['human', 'llm', 'llm', 'llm']
        assert players[0]['name'] == '房主'
        assert [(player['name'], player['avatar'], player['characterId']) for player in players[1:]] == [
            ('千问牌友（高冷）', 'img/llm/qwen/llm-avatar-gaoleng.png', 'qwen'),
            ('大肥鱼（稳健）', 'img/llm/deepseek/llm-avatar-wenjian.png', 'deepseek'),
            ('GPT牌友（稳健）', 'img/llm/gpt/llm-avatar-wenjian.png', 'gpt'),
        ]
        assert [player.name for player in room.state.players] == [player['name'] for player in players]
    finally:
        room.close()
        await asyncio.gather(room._drive_task, return_exceptions=True)


async def test_agent_executes_only_a_legal_candidate_from_its_own_view(room, monkeypatch):
    player = room.state.players[3]
    selected = None
    agent_view = room.snapshot_for(0)['view']['players'][3]
    assert agent_view['playerKind'] == 'llm' and agent_view['isLlm'] is True
    assert agent_view['hand'] == []

    async def choose_other(config, system, user, candidate_ids, *, retry_parse):
        nonlocal selected
        payload = json.loads(user)
        assert payload['hand'] == [tile_name(tile) for tile in player.hand]
        assert payload['wallCount'] == 0 and 'wall' not in payload
        assert all('hand' not in other for other in payload['table'])
        tiles = [candidate['tile'] for candidate in payload['candidates']]
        assert len(tiles) == len(set(tiles)) and tile_name(0) not in tiles
        assert not retry_parse
        assert candidate_ids == [candidate['id'] for candidate in payload['candidates']]
        choice = next(candidate for candidate in payload['candidates']
                      if candidate['id'] != payload['engineSuggestion'])
        selected = choice['tile']
        return choice['id'], ''

    monkeypatch.setattr(yaotong_agent, 'request_llm_decision', choose_other)
    await room._act_ai(3)
    assert selected is not None
    assert tile_name(player.river[-1].tile) == selected


@pytest.mark.parametrize('timeout', [False, True], ids=['invalid-choice', 'timeout'])
async def test_agent_failure_uses_the_rule_ai_discard(room, monkeypatch, timeout):
    player = room.state.players[3]
    expected = player.hand[choose_action(room.state, 3).index]
    cancelled = False

    async def fail(*args, **kwargs):
        nonlocal cancelled
        if timeout:
            try:
                await asyncio.Event().wait()
            finally:
                cancelled = True
        return 'A0', ''

    room.turn_timeout = 0.05
    monkeypatch.setattr(yaotong_agent, 'request_llm_decision', fail)
    await room._act_ai(3)
    assert room.llm_seats[3].requests == 1
    assert player.river[-1].tile == expected
    assert cancelled == timeout


@pytest.mark.parametrize('model_seconds', [6.0, 14.0], ids=['before-deadline', 'at-deadline'])
async def test_agent_uses_remaining_room_countdown(room, monkeypatch, model_seconds):
    now = 100.0
    monkeypatch.setattr(yaotong_room, 'time', SimpleNamespace(monotonic=lambda: now))
    room._sync_decisions()
    now += 4.0
    player = room.state.players[3]
    rule_tile = player.hand[choose_action(room.state, 3).index]
    selected_tile = None
    timeouts = []

    async def advance_pace(seconds):
        nonlocal now
        now += seconds

    async def wait_for_decision(awaitable, timeout):
        timeouts.append(timeout)
        return await awaitable

    async def request(config, system, user, candidate_ids, **kwargs):
        nonlocal now, selected_tile
        payload = json.loads(user)
        candidate = next(c for c in payload['candidates'] if c['id'] != payload['engineSuggestion'])
        selected_tile = candidate['tile']
        now += model_seconds
        return candidate['id'], ''

    monkeypatch.setattr(room, '_wait_pace', advance_pace)
    monkeypatch.setattr(yaotong_room.asyncio, 'wait_for', wait_for_decision)
    monkeypatch.setattr(yaotong_agent, 'request_llm_decision', request)
    await room._act_ai(3, delay=2.0)

    assert timeouts == [14.0]
    assert selected_tile is not None
    assert tile_name(player.river[-1].tile) == (
        selected_tile if model_seconds < 14 else tile_name(rule_tile))


async def test_agent_skips_request_when_countdown_has_expired(room):
    player = room.state.players[3]
    rule_tile = player.hand[choose_action(room.state, 3).index]
    room.turn_timeout = 0

    await room._act_ai(3, delay=2.0)

    assert room.llm_seats[3].requests == 0
    assert player.river[-1].tile == rule_tile


@pytest.mark.parametrize('finish_match', [False, True], ids=['rejoin', 'finish'])
async def test_agent_ignores_a_result_after_the_room_window_changes(room, monkeypatch, finish_match):
    async def change_window(config, system, user, candidate_ids, **kwargs):
        if finish_match:
            room.status = 'playing'
            room.force_finish(cancel_task=False)
        else:
            room.on_connect(0)
        return candidate_ids[0], ''

    monkeypatch.setattr(yaotong_agent, 'request_llm_decision', change_window)
    await room._act_ai(3)
    assert room.llm_seats[3].requests == 1
    assert room.state.players[3].river == []


async def test_agent_leaves_other_actions_and_locked_hands_to_rule_ai(room):
    agent = room.llm_seats[3]
    for command in (
        Command(type='original', declare=True),
        Command(type='hu'),
        Command(type='kong', tile=0),
        Command(type='respond', action='peng'),
        Command(type='discard', index=1, declare=True),
    ):
        assert await agent.choose_discard(room.state, 3, command) is None
    room.state.players[3].declaration = 'ready'
    assert await agent.choose_discard(room.state, 3, Command(type='discard', index=10)) is None
    assert agent.requests == 0


@pytest.mark.parametrize(('reply', 'expected'), [
    ('  先走一步。你们继续。  ', '先走一步。'),
    ('手里有两张一筒。', '这张先走。'),
])
async def test_agent_comments_after_discard_and_synthesizes_in_background(room, monkeypatch, reply, expected):
    player = room.state.players[3]
    player.melds = []
    player.chicken_peng = []
    player.hand = [1, 1, 4, 5, 6, 10, 11, 14, 17, 19, 20, 22, 24, 26]
    player.drawn = 26
    room.state.wall = [3, 7, 9, 12, 16, 21, 23, 25]
    room.on_connect(0)
    queue = asyncio.Queue()
    room.conn.register(0, queue, None)
    release_audio = asyncio.Event()
    audio_url = f'/api/tts/audio/{"a" * 64}.mp3'

    async def request(config, system, user, candidate_ids, **kwargs):
        return candidate_ids[0], reply

    async def synthesize(text, style, provider_id):
        assert text == expected
        assert style == room.llm_seats[3].config.style
        await release_audio.wait()
        return SimpleNamespace(audio_url=audio_url, cached=True)

    monkeypatch.setattr(yaotong_agent, 'request_llm_decision', request)
    monkeypatch.setattr(yaotong_room, 'get_tts_service', lambda: SimpleNamespace(ensure_audio=synthesize))
    await room._act_ai(3)
    assert player.river
    assert queue.get_nowait()['kind'] == 'yt_snapshot'
    speech = queue.get_nowait()
    assert speech['kind'] == 'llm_message' and speech['seat'] == 3 and speech['text'] == expected
    assert queue.empty()

    release_audio.set()
    await asyncio.gather(*room._llm_audio_tasks)
    audio = queue.get_nowait()
    assert audio['kind'] == 'llm_audio' and audio['audioUrl'] == audio_url
    assert (audio['messageId'], audio['matchId'], audio['round']) == (
        speech['id'], speech['matchId'], speech['round'],
    )


async def test_agent_audio_from_a_previous_hand_is_dropped(room, monkeypatch):
    queue = asyncio.Queue()
    room.conn.register(0, queue, None)
    release_audio = asyncio.Event()

    async def synthesize(*_):
        await release_audio.wait()
        return SimpleNamespace(audio_url='/api/tts/audio/test.mp3', cached=True)

    monkeypatch.setattr(yaotong_room, 'get_tts_service', lambda: SimpleNamespace(ensure_audio=synthesize))
    room._broadcast_llm_commentary(room.state, 3, '这张先走。')
    assert queue.get_nowait()['kind'] == 'llm_message'
    room.state = replace(room.state, hand_number=room.state.hand_number + 1)
    release_audio.set()
    await asyncio.gather(*room._llm_audio_tasks)
    assert queue.empty()


@pytest.mark.parametrize('audio_ready', [True, False], ids=['during-request', 'after-request'])
async def test_slow_agent_request_clears_its_thinking_cue_after_the_decision(room, monkeypatch, audio_ready):
    room.on_connect(0)
    queue = asyncio.Queue()
    room.conn.register(0, queue, None)
    release_model = asyncio.Event()
    release_audio = asyncio.Event()
    tts_started = asyncio.Event()
    config = room.llm_seats[3].config
    thinking_text = yaotong_room.reasoning_status_speech(config.style)

    async def request(config, system, user, candidate_ids, **kwargs):
        await release_model.wait()
        return candidate_ids[0], ''

    async def synthesize(text, style, provider_id):
        if text == thinking_text:
            assert (style, provider_id) == (config.style, config.provider_id)
            tts_started.set()
            await release_audio.wait()
        return SimpleNamespace(audio_url='/api/tts/audio/test.mp3', cached=True)

    monkeypatch.setattr(yaotong_room, 'LLM_THINKING_CUE_DELAY', 0)
    monkeypatch.setattr(yaotong_agent, 'request_llm_decision', request)
    monkeypatch.setattr(yaotong_room, 'get_tts_service', lambda: SimpleNamespace(ensure_audio=synthesize))
    turn = asyncio.create_task(room._act_ai(3, delay=0))
    try:
        status = await asyncio.wait_for(queue.get(), 1)
        assert (status['kind'], status['seat'], status['active'], status['text']) == (
            'llm_status', 3, True, thinking_text,
        )
        assert room.llm_seats[3].requests == 1
        await asyncio.wait_for(tts_started.wait(), 1)
        if audio_ready:
            release_audio.set()
            audio = await asyncio.wait_for(queue.get(), 1)
            assert (audio['kind'], audio['messageId'], audio['purpose']) == (
                'llm_audio', status['messageId'], 'commentary',
            )
    finally:
        release_model.set()
        await turn
    release_audio.set()
    await asyncio.gather(*room._llm_audio_tasks)
    stopped = queue.get_nowait()
    assert stopped['kind'] == 'llm_status' and stopped['active'] is False
    assert (stopped['messageId'], stopped['matchId'], stopped['round']) == (
        status['messageId'], status['matchId'], status['round'],
    )
    assert room.state.players[3].river
    assert all(message.get('messageId') != status['messageId']
               for message in (queue.get_nowait() for _ in range(queue.qsize())))


@pytest.mark.parametrize('tts_result', ['audio', 'missing', 'error'])
async def test_public_llm_actions_use_persona_voice_once_per_event(room, monkeypatch, tts_result):
    room.on_connect(0)
    queue = asyncio.Queue()
    room.conn.register(0, queue, None)
    voices = []

    async def synthesize(text, style, provider_id):
        voices.append((text, style, provider_id))
        if tts_result == 'error':
            raise RuntimeError('TTS failed')
        if tts_result == 'audio':
            return SimpleNamespace(audio_url='/api/tts/audio/test.mp3', cached=True)
        return None

    monkeypatch.setattr(yaotong_room, 'get_tts_service', lambda: SimpleNamespace(ensure_audio=synthesize))
    room.state.players[3].declaration = 'ready'
    room.state.events = [
        GameEvent(1, 3, '碰', 'peng'),
        GameEvent(2, 3, '明杠', 'kong'),
        GameEvent(3, 3, '存杠', 'kong'),
        GameEvent(4, 3, '宣告补杠', 'kong-intent'),
        GameEvent(5, 3, '补杠', 'kong'),
        GameEvent(6, 3, '报听 · 锁定听口', 'ready'),
        GameEvent(7, 1, '碰', 'peng'),
    ]
    room.state.serial = 7
    room.broadcast_snapshot()
    room.broadcast_snapshot()
    await asyncio.gather(*room._llm_audio_tasks)
    messages = [queue.get_nowait() for _ in range(queue.qsize())]
    assert messages[0]['llmActionSpeech'] is True
    speech = [message for message in messages if message['kind'] == 'llm_message' and message.get('hasAudio') is not False]
    fallback = [message for message in messages if message['kind'] == 'llm_message' and message.get('hasAudio') is False]
    audio = [message for message in messages if message['kind'] == 'llm_audio']
    assert [message['actionKind'] for message in speech] == [
        'peng', 'gang', 'concealed-kong', 'added-kong', 'ready',
    ]
    assert all(message['seat'] == 3 and message['priority'] == 'important' for message in speech)
    assert all('大明杠' not in message['text'] for message in speech)
    speech_ids = {message['id'] for message in speech}
    if tts_result == 'audio':
        assert {message['messageId'] for message in audio} == speech_ids
        assert not fallback
    else:
        assert not audio
        assert {message['id'] for message in fallback} == speech_ids
    config = room.llm_seats[3].config
    assert set(voices) == {(message['text'], config.style, config.provider_id) for message in speech}
    assert room.llm_seats[3].requests == 0


@pytest.mark.parametrize('audio_available', [True, False])
async def test_llm_win_audio_precedes_round_reactions_after_the_match_settles(room, monkeypatch, audio_available):
    room.on_connect(0)
    room._opening_confirmations.add(0)
    room.llm_seats[2] = yaotong_agent.YaotongDiscardAgent(LlmServerConfig(style='高冷', provider_id='other'))
    queue = asyncio.Queue()
    room.conn.register(0, queue, None)
    player = room.state.players[3]
    player.melds = []
    player.hand = [1, 2, 3, 4, 5, 6, 19, 20, 21, 14, 14, 24, 25, 26]
    player.drawn = 26
    release_audio = asyncio.Event()
    voices = []

    async def synthesize(text, style, provider_id):
        voices.append((text, style, provider_id))
        await release_audio.wait()
        return SimpleNamespace(audio_url='/api/tts/audio/test.mp3', cached=True) if audio_available else None

    monkeypatch.setattr(yaotong_room, 'get_tts_service', lambda: SimpleNamespace(ensure_audio=synthesize))
    await room._act_ai(3, command=Command(type='hu'))
    assert queue.get_nowait()['roundResult']['winners'] == [3]
    speech = queue.get_nowait()
    assert (speech['kind'], speech['actionKind'], speech['text']) == ('llm_message', 'win', '自摸！')
    room.state.result = replace(room.state.result, finished=True)
    await asyncio.wait_for(room._drive(), 1)
    assert room.match_finished
    assert all(message.get('purpose') != 'round-reaction'
               for message in (queue.get_nowait() for _ in range(queue.qsize())))
    release_audio.set()
    await asyncio.gather(*room._llm_audio_tasks)
    audio = queue.get_nowait()
    if audio_available:
        assert audio['kind'] == 'llm_audio' and audio['messageId'] == speech['id']
    else:
        assert audio == {**speech, 'hasAudio': False}
    for seat, lines in ((3, yaotong_room._LLM_WIN_LINES['self-draw']), (2, yaotong_room._LLM_LOSS_LINES)):
        reaction = queue.get_nowait()
        config = room.llm_seats[seat].config
        assert (reaction['kind'], reaction['purpose'], reaction['seat'], reaction['hasAudio']) == (
            'llm_message', 'round-reaction', seat, audio_available,
        )
        assert reaction['text'] in lines[config.style]
        assert (reaction['text'], config.style, config.provider_id) in voices
        if audio_available:
            reaction_audio = queue.get_nowait()
            assert (reaction_audio['kind'], reaction_audio['messageId']) == ('llm_audio', reaction['id'])
    assert queue.empty()
