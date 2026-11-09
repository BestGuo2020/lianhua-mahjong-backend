"""幺筒房间会话 —— 座位/重进码/开局/驱动/快照。

与经典 RoomSession、BloodFlowRoomSession 保持同一套 REST/WS 鸭子契约；
对局规则全部走 app.core.yaotong.engine，MVP 只做真人 + 服务端 AI 补位。
"""

from __future__ import annotations

import asyncio
import math
import secrets
import time
from dataclasses import dataclass, field
from threading import Lock
from typing import Callable, Optional

from loguru import logger

from app.core.yaotong.ai import choose_action
from app.core.yaotong.engine import (
    StartOptions,
    FlowStep,
    act,
    continue_game,
    create_game,
    self_options,
)
from app.core.yaotong.hands import waits
from app.core.yaotong.mode import action_protection, protection_reason
from app.core.yaotong.tiles import YAO
from app.core.yaotong.types import Command, GameState
from app.game.room import (
    ROOM_LIFETIME, RoomError, _LLM_DRAW_LINES, _LLM_LOSS_LINES, _LLM_WIN_LINES,
    _make_rejoin_code, room_registry,
)
from app.game.yaotong_protocol import command_from_action, settlement_view, snapshot_for
from app.llm.config import llm_server_available, load_llm_providers
from app.llm.decision_speech import decision_speech, reasoning_status_speech
from app.llm.speech_policy import LlmSpeechPolicy
from app.llm.yaotong_agent import YaotongDiscardAgent, YaotongDiscardDecision
from app.llm.yaotong_speech import chicken_claim_speech, discard_speech
from app.tts.service import get_tts_service
from app.ws.manager import ConnectionManager

# 对齐前端 src/game/core/local/localGameConfig.ts 的 PACE_MS。
PACE_MS_AFTER_CLAIM_GANG = 0.55   # 明杠补牌前
PACE_MS_AFTER_KONG_SETTLE = 0.6   # 补杠结算/补牌前

# 与前端幺筒 quickChat.ts / emotes.ts 的固定资源 ID 对齐。
QUICK_CHAT_IDS = frozenset({
    'follow', 'listen', 'draw', 'hurry', 'praise', 'think',
    'hello', 'stay', 'let-draw', 'feed-peng', 'hurt', 'oops-hu',
})
EMOTE_IDS = frozenset({'tomato', 'egg', 'pan', 'banana', 'kiss', 'like'})
QUICK_CHAT_GAP = 0.7
QUICK_CHAT_REPEAT = 5.0
EMOTE_GAP = 0.35
LLM_THINKING_CUE_DELAY = 0.8
LLM_MELD_ACTION_KINDS = {
    ('peng', '碰'): 'peng', ('kong-intent', '宣告补杠'): 'added-kong',
    ('kong', '明杠'): 'gang', ('kong', '暗杠'): 'concealed-kong',
    ('kong', '转弯杠'): 'concealed-kong', ('kong', '存杠'): 'concealed-kong',
}


@dataclass
class _Controller:
    connected: bool = False


@dataclass
class _Seat:
    seat: int
    nickname: str
    rejoin_code: str
    player_id: str
    character_id: str = 'deepseek'
    avatar: str = ''
    ready: bool = False
    connected_at: float = 0.0
    controller: _Controller = field(default_factory=_Controller)


@dataclass
class _Transition:
    state: GameState
    step: FlowStep
    resume: Callable[[], None]
    deadline: float


@dataclass
class _Decision:
    key: tuple
    deadline: float
    paused_remaining: Optional[float] = None


class YaotongRoomSession:
    """单房间会话。状态机由 engine.act 推进，本类只负责网络与节奏。"""

    def __init__(
        self,
        room_id: str,
        mode: str = 'east',
        capacity: int = 4,
        ruleset_id: str = 'yaotong',
        storage=None,
        pace=None,
        llm_enabled: bool = False,
        match_length: int = 4,
        yaotong_mode: str = 'easy',
    ):
        self.room_id = room_id
        self.mode = mode
        self.capacity = 4
        self.player_count = 4
        self.ruleset_id = ruleset_id
        self.match_length = match_length
        self.yaotong_mode = yaotong_mode if yaotong_mode in ('easy', 'hard') else 'easy'
        self.storage = storage
        self.llm_enabled = bool(llm_enabled)
        self.llm_seats: dict[int, YaotongDiscardAgent] = {}
        self._llm_speech = LlmSpeechPolicy()
        self._llm_message_seq = 0
        self._llm_event_serial = 0
        self._llm_audio_tasks: dict[asyncio.Task, bool] = {}
        self.status = 'lobby'
        self.creator_seat: Optional[int] = None
        self.seats: list[Optional[_Seat]] = [None] * 4
        self.reserved_seats: dict[int, dict[str, str]] = {}
        self._seat_lock = Lock()
        self.table_theme = 'jade'
        self.lifetime = ROOM_LIFETIME
        self.created_at = time.monotonic()
        self.deadline = self.created_at + ROOM_LIFETIME
        self.conn = ConnectionManager()

        self.state: Optional[GameState] = None
        self.match_id: Optional[str] = None
        self._queued_rounds: set[tuple[str, int]] = set()
        self._round_writes: list[tuple[str, dict]] = []
        self.match_started = False
        self.match_finished = False
        self.closed = False
        self.match_started_at: Optional[float] = None

        self._drive_task: Optional[asyncio.Task] = None
        self._forced_finishing = False
        self._rejoin_attempts: dict[str, list[float]] = {}
        self._opening: Optional[dict] = None
        self._opening_round = 0
        self._opening_confirmations: set[int] = set()
        self._round_speech_sent = False
        self._round_speech_confirmations: set[int] = set()
        self._continue_confirmations: set[int] = set()
        self._quick_chat_sent: dict[int, tuple[str, float]] = {}
        self._emote_sent: dict[int, float] = {}
        self.auto_seats: set[int] = set()
        self._wake = asyncio.Event()
        self._action_epoch = 0
        self._reaction_key = None
        self._reaction_deadline = 0.0

        self.turn_timeout = 20.0
        self.opening_timeout = 60.0
        self.round_speech_timeout = 60.0
        self.continue_timeout = 20.0
        # 真人房间注入 PACE 时按单机 AI_DELAYS/YAOTONG_AI_DISCARD_MS 停顿；
        # 测试/无头房间默认 0（仍可用 ai_delay 覆盖）。
        self.ai_delay = 1.0 if pace else 0.0
        # 操作窗口按座位同时计时；“等”暂停剩余时长，不能重置整段 20 秒。
        self._decisions: dict[int, _Decision] = {}
        self._holds: dict[int, dict] = {}
        self._hold_tasks: dict[int, asyncio.Task] = {}
        self._transition: Optional[_Transition] = None
        self.game_factory = create_game

    # ── 限时 ─────────────────────────────────────────────

    def is_past_deadline(self, now: Optional[float] = None) -> bool:
        return (now if now is not None else time.monotonic()) >= self.deadline

    def is_expired(self, now: Optional[float] = None) -> bool:
        if self.status == 'playing':
            return False
        return self.is_past_deadline(now)

    # ── 座位 / 重进码 ────────────────────────────────────

    def join_or_rejoin(self, nickname: str, rejoin_code: Optional[str] = None,
                       player_id: Optional[str] = None,
                       character_id: str = 'deepseek', avatar: str = ''):
        with self._seat_lock:
            if rejoin_code:
                seat, state = self.resume_by_code(rejoin_code)
                state.nickname = nickname or state.nickname
                return seat, True, state
            if player_id is not None:
                for state in self.seats:
                    if state is not None and state.player_id == player_id:
                        return state.seat, True, state
            if self.status == 'playing':
                raise RoomError('ROOM_CLOSED')
            seat = next((index for index, state in enumerate(self.seats)
                         if state is None and index not in self.reserved_seats), None)
            if seat is None:
                raise RoomError('SEATS_RESERVED' if self.reserved_seats else 'ROOM_FULL')
            state = _Seat(
                seat=seat,
                nickname=nickname,
                rejoin_code=_make_rejoin_code(),
                player_id=player_id or f'yaotong-{secrets.token_hex(4)}',
                character_id=character_id,
                avatar=avatar or '',
            )
            self.seats[seat] = state
            self.auto_seats.discard(seat)
            if self.creator_seat is None:
                self.creator_seat = seat
            return seat, False, state

    def resume_by_code(self, rejoin_code: str):
        for state in self.seats:
            if state is not None and state.rejoin_code == rejoin_code:
                return state.seat, state
        raise RoomError('REJOIN_CODE_INVALID')

    def check_rejoin_rate(self, rejoin_code: str) -> bool:
        now = time.monotonic()
        attempts = [
            stamp for stamp in self._rejoin_attempts.get(rejoin_code, [])
            if now - stamp < 30
        ]
        self._rejoin_attempts[rejoin_code] = attempts
        return len(attempts) < 5

    def reset_rejoin_rate(self, rejoin_code: str) -> None:
        self._rejoin_attempts.pop(rejoin_code, None)

    def release_seat(self, seat: int, rejoin_code: Optional[str] = None) -> None:
        with self._seat_lock:
            state = self.seats[seat]
            if state is not None and (
                rejoin_code is None or state.rejoin_code == rejoin_code
            ):
                self.seats[seat] = None
                self._action_epoch += 1
                if seat in self._holds:
                    self._release_hold(seat)
                self._wake.set()
                self.auto_seats.discard(seat)
                self.conn.unregister(seat)
                if self.creator_seat == seat:
                    self._transfer_creator()

    def _transfer_creator(self) -> None:
        self.creator_seat = next(
            (state.seat for state in self.seats if state is not None), None)

    def ready_seat(self, seat: int, ready: Optional[bool] = None) -> bool:
        state = self.seats[seat]
        if state is None:
            return False
        state.ready = ready if ready is not None else not state.ready
        return state.ready

    def set_character(self, seat: int, character_id: str) -> str:
        state = self.seats[seat]
        if state is None:
            raise RoomError('SEAT_EMPTY')
        state.character_id = character_id
        return character_id

    def has_humans(self) -> bool:
        return any(state is not None for state in self.seats)

    @property
    def llm_available(self) -> bool:
        return llm_server_available()

    @property
    def effective_llm_enabled(self) -> bool:
        return self.llm_enabled and self.llm_available

    def set_reserved_seat(self, seat: int, provider_id: Optional[str],
                          style: Optional[str] = None) -> None:
        with self._seat_lock:
            if self.status not in ('lobby', 'finished'):
                raise RoomError('ROOM_CLOSED')
            if not 0 <= seat < self.player_count:
                raise RoomError('INVALID_SEAT')
            if self.seats[seat] is not None:
                raise RoomError('SEAT_OCCUPIED')
            if provider_id:
                self.reserved_seats[seat] = {'providerId': provider_id, 'style': style or ''}
            else:
                self.reserved_seats.pop(seat, None)

    def on_connect(self, seat: int) -> None:
        state = self.seats[seat]
        if state is not None:
            state.controller.connected = True
            state.connected_at = time.monotonic()
        self._action_epoch += 1
        self._wake.set()

    def on_disconnect(self, seat: int) -> None:
        self._action_epoch += 1
        state = self.seats[seat]
        if state is not None:
            state.controller.connected = False
        # 暂离/断线结束本次等待；已用额度不返还。唤醒驱动，让 AI 立即接管。
        if seat in self._holds:
            self._release_hold(seat)
        self._wake.set()

    def _human_seat(self, seat: int) -> bool:
        state = self.seats[seat]
        return (
            state is not None and state.controller.connected
            and seat not in self.auto_seats
        )

    def _discard_agent(self, seat: int) -> Optional[YaotongDiscardAgent]:
        return self.llm_seats.get(seat) if self.seats[seat] is None else None

    def _connected_human_seats(self) -> list[int]:
        # 托管只交出操作权，在线玩家仍须看到完整开局与结算演出。
        return [seat for seat, state in enumerate(self.seats)
                if state is not None and state.controller.connected]

    @property
    def opening(self) -> Optional[dict]:
        return self._opening

    @property
    def round_speech_pending(self) -> Optional[bool]:
        result = self.state.result if self.state is not None else None
        if not self.llm_seats or result is None or result.kind not in ('win', 'draw'):
            return None
        return not self._round_speech_sent and not self._forced_finishing

    def continuation(self) -> Optional[dict]:
        if self.state is None or self.state.result is None:
            return None
        required = self._connected_human_seats()
        return {
            'requiredSeats': required,
            'readySeats': [
                seat for seat in required
                if seat in self._continue_confirmations
            ],
        }

    # ── 快照 / 广播 ─────────────────────────────────────

    def snapshot_for(self, seat: int) -> dict:
        return snapshot_for(self, seat)

    def broadcast_snapshot(self) -> None:
        for seat in range(4):
            state = self.seats[seat]
            if state is None or not state.controller.connected:
                continue
            self.conn.send_to_seat_nowait(seat, self.snapshot_for(seat))
        self._broadcast_llm_action_events()

    # ── 开局 ────────────────────────────────────────────

    async def start(self, llm_seats: Optional[list] = None,
                    default_provider: Optional[str] = None) -> None:
        if self._drive_task is not None and not self._drive_task.done():
            raise RoomError('ALREADY_STARTED')
        with self._seat_lock:
            if self.status not in ('lobby', 'finished'):
                raise RoomError('ROOM_CLOSED')
            if not any(state is not None for state in self.seats):
                raise RoomError('ROOM_EMPTY')
            if not all(state.ready for state in self.seats if state is not None):
                raise RoomError('NOT_ALL_READY')
            agents = self._resolve_llm_seats(llm_seats or [], default_provider)
            self._release_all_holds()
            self._transition = None
            self.state = self.game_factory(StartOptions(length=self.match_length))
            self.llm_seats = agents
            self._llm_speech.reset()
            self._llm_message_seq = 0
            self._apply_names()
            self.match_started = True
            self.match_finished = False
            self.status = 'playing'
        self.match_started_at = time.monotonic()
        self._forced_finishing = False
        self.match_id = secrets.token_hex(8)
        self._prepare_opening()
        self._continue_confirmations = set()
        self._action_epoch = 0
        self._wake = asyncio.Event()
        self._persist_match_start()
        self._drive_task = asyncio.ensure_future(self._drive())

    def _resolve_llm_seats(self, llm_seats: list,
                           default_provider: Optional[str]) -> dict[int, YaotongDiscardAgent]:
        """已入座的真人继续控制本座；其余座位优先用指定模型，再用默认模型。"""
        providers = load_llm_providers() if self.llm_enabled else {}
        if not providers:
            if llm_seats:
                raise RoomError('LLM_NOT_ENABLED')
            return {}

        default = providers.get(default_provider or 'default') or next(iter(providers.values()))
        selections = self.reserved_seats | {entry['seat']: entry for entry in llm_seats}
        agents = {}
        for seat, state in enumerate(self.seats):
            if state is not None:
                continue
            selection = selections.get(seat)
            provider = providers.get(selection['providerId']) if selection else default
            if provider is not None:
                style = selection.get('style') if selection else None
                agents[seat] = YaotongDiscardAgent(
                    provider.to_config(style), nickname=provider.nickname,
                    avatar_folder=provider.avatar_folder,
                )
        return agents

    def _prepare_opening(self) -> None:
        self._opening = {
            'dice': list(self.state.dice),
            'wallSide': self.state.wall_side,
            'reservedStacks': self.state.reserved_stacks,
        }
        self._opening_round = self.state.hand_number
        self._opening_confirmations = set()
        self._round_speech_sent = False
        self._round_speech_confirmations.clear()
        self._llm_event_serial = self.state.serial
        self._reaction_key = None
        self._quick_chat_sent.clear()
        self._emote_sent.clear()

    def _apply_names(self) -> None:
        if self.state is None:
            return
        for index, player in enumerate(self.state.players):
            state = self.seats[index]
            if state is not None:
                player.name = state.nickname
            elif index in self.llm_seats:
                player.name = self.llm_seats[index].nickname

    # ── 驱动 ────────────────────────────────────────────

    async def _drive(self) -> None:
        try:
            self.broadcast_snapshot()
            await self._wait_for_openings()
            self._opening = None
            self.broadcast_snapshot()
            while not self.match_finished:
                state = self.state
                if state is None:
                    break
                if state.phase == 'settled':
                    self._cancel_llm_audio(keep_result=True)
                    self._release_all_holds()
                    await self._persist_round()
                    self.broadcast_snapshot()
                    if self.round_speech_pending:
                        pending_win_audio = tuple(self._llm_audio_tasks)
                        try:
                            await self._announce_llm_round_reactions(state, pending_win_audio)
                        except Exception:
                            logger.bind(room_id=self.room_id).exception('幺筒局末感言发送失败')
                        self._round_speech_sent = True
                        self.broadcast_snapshot()
                        # 合成与发送结束后，仍需等待在线真人客户端播完语音。
                        await self._wait_for_confirmations(
                            self._round_speech_confirmations, self.round_speech_timeout)
                    if self.closed or self.match_finished:
                        break
                    if state.result is not None and state.result.finished:
                        break
                    await self._wait_for_continues()
                    if self.closed or self.match_finished:
                        break
                    self._cancel_llm_audio()
                    self._continue_confirmations = set()
                    self.state = continue_game(state)
                    self._apply_names()
                    self._prepare_opening()
                    self.broadcast_snapshot()
                    await self._wait_for_openings()
                    self._opening = None
                    self.broadcast_snapshot()
                    continue
                await self._drive_step()
            self.match_finished = True
            self.status = 'finished'
            self.broadcast_snapshot()
            self._broadcast_match_finished()
            await self._persist_round()
            self._persist_match_end()
            if self.is_past_deadline():
                room_registry.remove(self.room_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            self.status = 'error'
            self.conn.broadcast({'kind': 'error', 'code': 'INTERNAL_ERROR'})
            logger.bind(room_id=self.room_id).exception('幺筒房间驱动异常')
        finally:
            self._cancel_llm_audio(keep_result=self.status == 'finished' and not self._forced_finishing)

    async def _drive_step(self) -> None:
        state = self.state
        if state is None:
            return
        self._sync_decisions()
        if self._holds:
            # 任一座位点“等”时暂停整桌推进；等额度耗尽或玩家胡/过/取消后恢复。
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), 0.5)
            except asyncio.TimeoutError:
                pass
            return
        if self._transition is not None:
            await self._resume_transition()
            return
        if state.phase == 'original':
            automatic = [seat for seat in range(4) if not state.original_decisions[seat]
                         and (not self._human_seat(seat) or not waits(state.players[seat]))]
            if automatic:
                epoch = self._action_epoch
                await self._wait_pace(0.5 * self.ai_delay)
                if self.state is not state or self._holds or self._action_epoch != epoch:
                    return
                # 与单机相同：各 AI 在同一个 500ms 原报窗口决策，不逐家追加半秒。
                for seat in automatic:
                    if state.phase != 'original':
                        break
                    command = (Command(type='original', declare=False) if self._human_seat(seat)
                               else choose_action(state, seat))
                    if command is not None and self._apply_command(seat, command):
                        self._action_epoch += 1
                        self.broadcast_snapshot()
                return
            seat = next(
                (index for index, decided in enumerate(state.original_decisions)
                 if not decided),
                None,
            )
            if seat is None:
                await asyncio.sleep(0.01)
                return
            await self._human_or_ai(seat)
            return
        if state.phase == 'discard':
            await self._human_or_ai(state.current)
            return
        if state.phase == 'response':
            pending = state.pending
            if pending is None:
                await asyncio.sleep(0.01)
                return
            awaiting = [seat for seat in range(4) if seat in pending.options and seat not in pending.choices]
            automatic = [seat for seat in awaiting if not self._human_seat(seat)]
            automatic += [seat for seat in awaiting if self._human_seat(seat)
                          and self._human_automatic_command(seat) is not None]
            seat = next(iter(automatic or awaiting), None)
            if seat is None:
                await asyncio.sleep(0.01)
                return
            await self._human_or_ai(seat)
            return
        await asyncio.sleep(0.01)

    async def _wait_pace(self, seconds: float) -> None:
        if seconds <= 0:
            return
        self._wake.clear()
        try:
            await asyncio.wait_for(self._wake.wait(), seconds)
        except asyncio.TimeoutError:
            pass

    def _flow(self, step: FlowStep, resume: Callable[[], None]) -> None:
        state = self.state
        if step == 'after-peng' and not self._human_seat(state.current):
            resume()
            return
        reaction = self._reaction_remaining()
        delays = {
            'first-draw': 0.65 * self.ai_delay,
            'next-draw': max(reaction, 0.45 * self.ai_delay),
            'before-claim': reaction,
            'after-peng': 0.35 * self.ai_delay,
            'after-open-kong': PACE_MS_AFTER_CLAIM_GANG * self.ai_delay,
            'before-rob': max(reaction, 2.0 * self.ai_delay),
            'between-robbers': 0.45 * self.ai_delay,
            'after-added-kong': PACE_MS_AFTER_KONG_SETTLE * self.ai_delay,
            'rob-win': 0.45 * self.ai_delay,
        }
        delay = delays[step]
        if delay <= 0:
            resume()
            return
        self._transition = _Transition(state, step, resume, time.monotonic() + delay)

    async def _resume_transition(self) -> None:
        transition = self._transition
        if transition.state is not self.state:
            self._transition = None
            return
        await self._wait_pace(max(0.0, transition.deadline - time.monotonic()))
        if (self.closed or self._holds or self._transition is not transition
                or transition.state is not self.state or time.monotonic() < transition.deadline):
            return
        self._transition = None
        transition.resume()
        self._action_epoch += 1
        self._reaction_remaining()
        self.broadcast_snapshot()

    def _apply_command(self, seat: int, command: Command) -> bool:
        previous = self._transition
        accepted = act(self.state, seat, command, self._flow)
        if self._transition is previous and self.state.phase != 'transition':
            self._transition = None
        return accepted

    def _ai_think_seconds(self, state: GameState, seat: int) -> float:
        """单机 AI 节奏：原报 500ms、出牌 1000ms、响应 500ms（抢杠 0）。"""
        if not self.ai_delay:
            return 0.0
        if state.phase == 'original':
            base = 0.5
        elif state.phase == 'discard':
            # 杠后补摸的那一张等 2s 再打；普通出牌仍 1s。
            base = 2.0 if getattr(state.players[seat], 'after_kong', False) else 1.0
        elif state.phase == 'response':
            pending = state.pending
            base = 0.0 if pending is not None and pending.kind == 'rob' else 0.5
        else:
            base = 0.45
        return base * self.ai_delay

    def _decision_key(self, seat: int) -> Optional[tuple]:
        state = self.state
        if state is None or self.closed or self.match_finished or self._opening is not None:
            return None
        if self.seats[seat] is None:
            if self._discard_agent(seat) is None or state.phase != 'discard':
                return None
        elif self._human_automatic_command(seat) is not None:
            return None
        base = (id(state), state.phase, seat)
        player = state.players[seat]
        if state.phase == 'original' and not state.original_decisions[seat] and waits(player):
            return base
        if state.phase == 'discard' and state.current == seat:
            # 过胡/重连/托管消息不构成新出牌窗口；实际摸牌、碰杠后才重新计时。
            return (*base, player.draws, player.discards, tuple(player.hand),
                    tuple((meld.kind, meld.tile) for meld in player.melds))
        pending = state.pending
        if (state.phase == 'response' and pending is not None
                and seat in pending.options and seat not in pending.choices):
            return (*base, id(pending))
        return None

    def _sync_decisions(self) -> None:
        now = time.monotonic()
        for seat in range(4):
            key = self._decision_key(seat)
            decision = self._decisions.get(seat)
            if key is None or (decision is not None and decision.key != key):
                self._decisions.pop(seat, None)
                decision = None
            if key is None:
                continue
            if decision is None and (self._human_seat(seat) or self._discard_agent(seat) is not None):
                decision = _Decision(key, now + self.turn_timeout)
                self._decisions[seat] = decision
            if decision is None:
                continue
            if self._holds and decision.paused_remaining is None:
                decision.paused_remaining = max(0.0, decision.deadline - now)
            elif not self._holds and decision.paused_remaining is not None:
                decision.deadline = now + decision.paused_remaining
                decision.paused_remaining = None

    def decision_view(self, seat: int) -> dict:
        self._sync_decisions()
        decision = self._decisions.get(seat) if self._human_seat(seat) else None
        if decision is None:
            return {'decisionDeadlineAt': 0, 'decisionRemainingMs': 0, 'decisionPaused': False}
        paused = decision.paused_remaining is not None
        remaining = (decision.paused_remaining if paused
                     else max(0.0, decision.deadline - time.monotonic()))
        return {
            'decisionDeadlineAt': 0 if paused else int((time.time() + remaining) * 1000),
            'decisionRemainingMs': math.ceil(remaining * 1000),
            'decisionPaused': paused,
        }

    async def _human_or_ai(self, seat: int) -> None:
        if self._human_seat(seat):
            if self._holds:
                while self._holds:
                    await asyncio.sleep(0.2)
                return
            automatic = self._human_automatic_command(seat)
            if automatic is not None:
                delay = (self._reaction_remaining()
                         if automatic.type == 'respond'
                         else 0.65 * self.ai_delay)
                await self._act_ai(seat, command=automatic, delay=delay)
                return
            self._sync_decisions()
            decision = self._decisions.get(seat)
            if decision is None:
                return
            timeout = max(0.0, decision.deadline - time.monotonic())
            acted = await self._wait_for_action(seat, timeout)
            if acted:
                return
            if self._holds:
                # 等开始后本函数让出主线；等结束由 _drive_step 重新评估状态，
                # 避免拿着旧决策窗口继续等或直接代打。
                while self._holds:
                    await asyncio.sleep(0.2)
                return
            # 已经给足真人操作时间；沿用安全 AI 决策，不再附加 AI 思考停顿。
            await self._act_ai(seat, delay=0)
            return
        await self._act_ai(seat)

    async def _wait_for_action(self, seat: int, timeout: float) -> bool:
        start_epoch = self._action_epoch
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            if not self._human_seat(seat):
                return True
            if self._holds:
                return False
            if self._action_epoch != start_epoch:
                return True
            self._wake.clear()
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                break
            try:
                await asyncio.wait_for(
                    self._wake.wait(), max(0.01, min(remaining, 1.0)))
            except asyncio.TimeoutError:
                pass
            if self._action_epoch != start_epoch:
                return True
            if self.state is None or self.state.phase == 'settled':
                return True
        return False

    def _reaction_remaining(self) -> float:
        """从实际弃牌/补杠事件起计时，多位玩家共享同一个观察窗口。"""
        state = self.state
        if state is None:
            return 0.0
        event = next((event for event in reversed(state.events)
                      if event.kind in ('discard', 'kong-intent')), None)
        key = (id(state), event.id if event else id(state.pending))
        if self._reaction_key != key:
            self._reaction_key = key
            extended = (state.pending is not None and state.pending.kind in ('rob', 'hot')) \
                or (event is not None and (event.tile == YAO or event.after_kong
                                          or event.kind == 'kong-intent'))
            self._reaction_deadline = time.monotonic() + (2.0 if extended else 1.0) * self.ai_delay
        return max(0.0, self._reaction_deadline - time.monotonic())

    def _human_automatic_command(self, seat: int) -> Optional[Command]:
        state = self.state
        if state is None:
            return None
        player = state.players[seat]
        if state.phase == 'discard' and state.current == seat \
                and player.declaration != 'none' and player.drawn is not None:
            options = self_options(state, seat)
            protection = action_protection(state, seat)
            index = len(player.hand) - 1
            if (not options.hu or options.restricted_hu) \
                    and not protection_reason(protection, Command(type='discard', index=index)):
                return Command(type='discard', index=index)
        pending = state.pending
        if self.yaotong_mode == 'hard' and state.phase == 'response' and pending is not None:
            options = pending.options.get(seat)
            if options and options.hu and not options.peng and not options.kong \
                    and seat not in pending.choices:
                return Command(type='respond', action='pass', defer_hu=(
                    pending.kind != 'rob' and 'hu' not in pending.choices.values()))
        return None

    async def _act_ai(self, seat: int, *, command: Optional[Command] = None,
                      delay: Optional[float] = None) -> None:
        state = self.state
        if state is None:
            return
        epoch = self._action_epoch
        was_human = self._human_seat(seat)

        def current_window() -> bool:
            return (not self.closed and not self.match_finished and not self._holds
                    and self.state is state
                    and self._action_epoch == epoch and self._human_seat(seat) == was_human
                    and state.phase != 'settled')

        clock = None
        if self._discard_agent(seat) is not None and state.phase == 'discard':
            self._sync_decisions()
            clock = self._decisions.get(seat)
        if delay is None:
            delay = self._ai_think_seconds(state, seat)
        if clock is not None:
            delay = min(delay, max(0.0, clock.deadline - time.monotonic()))
        await self._wait_pace(delay)
        # 等待期间可能重连、取消托管、真人操作或换局。
        if not current_window():
            return
        command = command or choose_action(state, seat)
        decision = None
        agent = self._discard_agent(seat)
        if agent is not None and clock is not None and command is not None:
            remaining = clock.deadline - time.monotonic()
            if remaining > 0:
                with logger.contextualize(
                        room_id=self.room_id, seat=seat, llm_ruleset=self.ruleset_id,
                        llm_round=state.hand_number, llm_window='discard',
                        llm_request_id=f'{self.match_id}:{state.hand_number}:{epoch}:{seat}'):
                    try:
                        decision = await self._choose_discard_with_thinking(
                            agent, state, seat, command, remaining, current_window)
                    except asyncio.TimeoutError:
                        logger.info('幺筒 Agent 出牌时间到，使用规则 AI')
                if not current_window():
                    return
                if time.monotonic() >= clock.deadline:
                    decision = None
        before_action = state.serial
        agent_played = decision is not None and self._apply_command(seat, decision.command)
        ok = agent_played
        if not ok:
            command = command or self._fallback_command(state, seat)
            ok = command is not None and self._apply_command(seat, command)
        if not ok:
            fallback = self._fallback_command(state, seat)
            if fallback is not None:
                ok = self._apply_command(seat, fallback)
        if not ok:
            logger.bind(room_id=self.room_id, seat=seat).error(
                '幺筒 AI 动作被引擎拒绝，强制收尾')
            self.force_finish('AI_ACTION_FAILED')
            return
        self._action_epoch += 1
        self._reaction_remaining()
        self.broadcast_snapshot()
        if agent_played and decision is not None and agent is not None and state.result is None:
            river = state.players[seat].river
            if river and river[-1].id > before_action:
                text = discard_speech(
                    decision.message, river[-1], agent.config.style, agent.requests - 1,
                    is_dealer=seat == state.dealer,
                )
                self._broadcast_llm_commentary(state, seat, text)

    def _llm_scope(self, state: GameState, seat: int) -> dict:
        return {'matchId': self.match_id or self.room_id, 'round': state.hand_number, 'seat': seat}

    async def _choose_discard_with_thinking(
        self, agent: YaotongDiscardAgent, state: GameState, seat: int, command: Command,
        timeout: float, current_window: Callable[[], bool],
    ) -> YaotongDiscardDecision | None:
        request_count = agent.requests
        status = None

        async def announce_thinking() -> None:
            nonlocal status
            await asyncio.sleep(LLM_THINKING_CUE_DELAY)
            if not current_window() or agent.requests == request_count or not self.conn.connected_seats:
                return
            scope = self._llm_scope(state, seat)
            text = reasoning_status_speech(agent.config.style, request_count)
            self._llm_message_seq += 1
            message_id = self._llm_message_seq
            status = {'kind': 'llm_status', 'messageId': message_id, **scope}
            self.conn.broadcast({**status, 'active': True, 'text': text})
            await self._synthesize_llm_audio(state, {
                **scope, 'priority': 'normal', 'purpose': 'commentary',
                'speechSource': 'fixed-line', 'actionKind': 'discard',
            }, message_id, text, agent.config.style, agent.config.provider_id, is_current=current_window)

        thinking = asyncio.create_task(announce_thinking())
        try:
            return await asyncio.wait_for(agent.choose_discard(state, seat, command), timeout)
        finally:
            thinking.cancel()
            if status is not None:
                self.conn.broadcast({**status, 'active': False})

    async def _announce_llm_round_reactions(
        self, state: GameState, pending_win_audio: tuple[asyncio.Task, ...],
    ) -> None:
        result = state.result
        if result is None or result.kind not in ('win', 'draw'):
            return
        # 胡牌动作语音先入播放队列，感言在结算期间按座位顺序补上。
        await asyncio.gather(*pending_win_audio, return_exceptions=True)
        llm_seats = [seat for seat in self.llm_seats if self._discard_agent(seat) is not None]
        anchor = next((winner for winner in result.winners if winner in llm_seats), 0)
        order = sorted(llm_seats, key=lambda seat: (seat not in result.winners, (seat - anchor) % 4))
        win_type = {'self': 'self-draw', 'rob': 'robbed-kong-win'}.get(result.win_kind, 'discard-win')
        sequence = self._llm_message_seq
        for index, seat in enumerate(order):
            if self.state is not state or self.closed or self._forced_finishing or not self.conn.connected_seats:
                return
            config = self.llm_seats[seat].config
            lines = _LLM_DRAW_LINES if result.kind == 'draw' else (
                _LLM_WIN_LINES[win_type] if seat in result.winners else _LLM_LOSS_LINES)
            variants = lines.get(config.style, lines['稳健'])
            text = variants[(sequence + index) % len(variants)]
            self._llm_message_seq += 1
            await self._synthesize_llm_audio(state, {
                **self._llm_scope(state, seat), 'priority': 'important',
                'purpose': 'round-reaction', 'speechSource': 'fixed-line',
            }, self._llm_message_seq, text, config.style, config.provider_id)

    def _broadcast_llm_action_events(self) -> None:
        state = self.state
        if state is None or not self.llm_seats or state.serial <= self._llm_event_serial:
            return
        previous = self._llm_event_serial
        self._llm_event_serial = state.serial
        if not self.conn.connected_seats or self.closed or self.match_finished:
            return
        for event in state.events:
            if event.id <= previous:
                continue
            if event.kind == 'hu' and state.result is not None:
                text = {'self': '自摸！', 'rob': '抢杠胡！', 'hot': '接热炮！'}.get(
                    state.result.win_kind, '我胡了！')
                for seat in state.result.winners:
                    if self._discard_agent(seat) is not None:
                        self._broadcast_llm_commentary(state, seat, text, action_kind='win')
                continue
            agent = self._discard_agent(event.seat)
            if agent is None or state.result is not None:
                continue
            if event.kind == 'ready':
                original = state.players[event.seat].declaration == 'original'
                self._broadcast_llm_commentary(
                    state, event.seat, '我原报。' if original else '我报听了。',
                    action_kind='original-ready' if original else 'ready')
                continue
            # 补杠在宣告时说一次，即使后来被抢杠，也不在结算补杠时重播。
            action_kind = LLM_MELD_ACTION_KINDS.get((event.kind, event.text))
            if action_kind:
                text = chicken_claim_speech(state, event, action_kind, agent.config.style)
                if text is None:
                    text = decision_speech({'kind': action_kind}, agent.config.style, event.id)
                self._broadcast_llm_commentary(
                    state, event.seat, text.replace('大明杠', '杠'), action_kind=action_kind)

    def _broadcast_llm_commentary(self, state: GameState, seat: int, text: str, *,
                                  action_kind: str = 'discard') -> None:
        config = self.llm_seats[seat].config
        action = action_kind != 'discard'
        priority = 'important' if action else 'normal'
        if not self.conn.connected_seats or not self._llm_speech.admit(
                seat, config.style, priority, mandatory=config.style == '话痨'):
            return
        self._llm_message_seq += 1
        message_id = self._llm_message_seq
        scope = {
            **self._llm_scope(state, seat),
            'priority': priority, 'purpose': 'action' if action else 'commentary',
            'speechSource': 'fixed-line' if action else 'model-message', 'actionKind': action_kind,
        }
        self.conn.broadcast({'kind': 'llm_message', 'id': message_id, 'text': text, **scope})
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        task = loop.create_task(self._synthesize_llm_audio(
            state, scope, message_id, text, config.style, config.provider_id))
        self._track_llm_audio_task(task, keep_result=action_kind == 'win')

    def _track_llm_audio_task(self, task: asyncio.Task, *, keep_result: bool) -> None:
        self._llm_audio_tasks[task] = keep_result
        task.add_done_callback(lambda done: self._llm_audio_tasks.pop(done, None))

    async def _synthesize_llm_audio(self, state: GameState, scope: dict, message_id: int,
                                     text: str, style: str, provider_id: str,
                                     *, is_current: Optional[Callable[[], bool]] = None) -> None:
        try:
            audio = await asyncio.wait_for(get_tts_service().ensure_audio(text, style, provider_id), 4.0)
        except Exception:
            audio = None
        if (self.closed or self._forced_finishing or self.state is not state
                or (is_current is not None and not is_current())):
            return
        round_reaction = scope['purpose'] == 'round-reaction'
        if not round_reaction and scope.get('actionKind') != 'win' and (self.match_finished or state.result is not None):
            return
        if round_reaction:
            self.conn.broadcast({
                'kind': 'llm_message', 'id': message_id, 'text': text, 'hasAudio': audio is not None, **scope,
            })
        if audio is None:
            if scope['purpose'] == 'action':
                self.conn.broadcast({
                    'kind': 'llm_message', 'id': message_id, 'text': text, 'hasAudio': False, **scope,
                })
            return
        self.conn.broadcast({
            'kind': 'llm_audio', 'messageId': message_id, 'audioUrl': audio.audio_url,
            'cached': audio.cached, **scope,
        })

    def _cancel_llm_audio(self, *, keep_result: bool = False) -> None:
        for task, is_result in tuple(self._llm_audio_tasks.items()):
            if keep_result and is_result:
                continue
            if not task.done() and not task.get_loop().is_closed():
                task.get_loop().call_soon_threadsafe(task.cancel)
            self._llm_audio_tasks.pop(task, None)

    # ── 老手“等”：每小局每个座位独立累计 15 秒 ─────────────

    def hold_remaining_ms(self, seat: int) -> Optional[int]:
        entry = self._holds.get(seat)
        if entry is None:
            return None
        elapsed = (time.monotonic() - entry['started_at']) * 1000
        return max(0, int(entry['remaining'] - elapsed))

    def _start_hold(self, seat: int) -> tuple[bool, str]:
        if self.yaotong_mode != 'hard':
            return False, 'HOLD_NOT_AVAILABLE'
        if self.seats[seat] is None or self.state is None \
                or self.state.phase == 'settled':
            return False, 'STALE_HOLD'
        if seat in self.auto_seats:
            return False, 'AUTO_SEAT'
        if seat in self._holds:
            return True, ''
        budget = self.state.wait_remaining_ms[seat]
        if budget <= 0:
            return False, 'NO_HOLD_BUDGET'
        self._holds[seat] = {
            'started_at': time.monotonic(),
            'remaining': budget,
        }
        self._action_epoch += 1
        self._hold_tasks[seat] = asyncio.create_task(self._expire_hold(seat))
        self._wake.set()
        self.broadcast_snapshot()
        return True, ''

    def _release_hold(self, seat: int, *, expired: bool = False) -> None:
        entry = self._holds.pop(seat, None)
        if entry is None:
            return  # 重复/迟到的释放不重开真人决策窗口，也不打断 AI 的出牌计时。
        self._action_epoch += 1
        task = self._hold_tasks.pop(seat, None)
        if task is not None and not task.done() and not expired:
            task.cancel()
        if entry is not None and self.state is not None:
            if expired:
                self.state.wait_remaining_ms[seat] = 0
            else:
                elapsed = (time.monotonic() - entry['started_at']) * 1000
                self.state.wait_remaining_ms[seat] = max(
                    0, int(entry['remaining'] - elapsed))
        self._wake.set()
        self.broadcast_snapshot()

    async def _expire_hold(self, seat: int) -> None:
        entry = self._holds.get(seat)
        if entry is None:
            return
        await asyncio.sleep(max(0.0, entry['remaining'] / 1000))
        if self._holds.get(seat) is entry:
            self._release_hold(seat, expired=True)

    def _release_all_holds(self) -> None:
        for seat in list(self._holds):
            task = self._hold_tasks.pop(seat, None)
            if task is not None and not task.done():
                task.cancel()
        self._holds.clear()

    @staticmethod
    def _fallback_command(state: GameState, seat: int) -> Optional[Command]:
        if state.phase == 'original':
            return Command(type='original', declare=False)
        if state.phase == 'response':
            return Command(type='respond', action='pass')
        if state.phase == 'discard' and state.current == seat:
            return Command(
                type='discard', index=len(state.players[seat].hand) - 1)
        return None

    async def _wait_for_openings(self) -> None:
        required = self._connected_human_seats()
        if not required:
            return
        deadline = asyncio.get_running_loop().time() + self.opening_timeout
        while asyncio.get_running_loop().time() < deadline:
            required = self._connected_human_seats()
            if not required or all(
                seat in self._opening_confirmations for seat in required
            ):
                return
            self._wake.clear()
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                return
            try:
                await asyncio.wait_for(
                    self._wake.wait(), max(0.01, min(remaining, 1.0)))
            except asyncio.TimeoutError:
                pass

    async def _wait_for_continues(self) -> None:
        await self._wait_for_confirmations(self._continue_confirmations, self.continue_timeout)

    async def _wait_for_confirmations(self, confirmations: set[int], timeout: float) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while not self.closed and not self.match_finished:
            if all(seat in confirmations for seat in self._connected_human_seats()):
                return
            self._wake.clear()
            remaining = deadline - loop.time()
            if remaining <= 0:
                return
            try:
                await asyncio.wait_for(
                    self._wake.wait(), max(0.01, min(remaining, 1.0)))
            except asyncio.TimeoutError:
                pass

    # ── 客户端消息 ──────────────────────────────────────

    def _handle_interaction(self, seat: int, kind: str, message: dict) -> tuple[bool, str]:
        state = self.state
        hand_number = message.get('round')
        if (self.closed or self.status != 'playing' or state is None or state.phase == 'settled'
                or self.match_id is None or message.get('matchId') != self.match_id
                or type(hand_number) is not int or hand_number != state.hand_number):
            return False, 'STALE_INTERACTION'
        if self.seats[seat] is None:
            return False, 'NOT_HUMAN_SEAT'

        event = {'kind': kind, 'matchId': self.match_id, 'round': state.hand_number, 'fromSeat': seat}
        now = time.monotonic()
        if kind == 'quick_chat':
            phrase_id = message.get('phraseId')
            if not isinstance(phrase_id, str) or phrase_id not in QUICK_CHAT_IDS:
                return False, 'INVALID_INTERACTION'
            previous, last_at = self._quick_chat_sent.get(seat, ('', -math.inf))
            elapsed = now - last_at
            if elapsed < QUICK_CHAT_GAP or (phrase_id == previous and elapsed < QUICK_CHAT_REPEAT):
                return False, 'INTERACTION_RATE_LIMITED'
            self._quick_chat_sent[seat] = (phrase_id, now)
            event['phraseId'] = phrase_id
        else:
            emote = message.get('emote')
            target = message.get('targetSeat')
            if (not isinstance(emote, str) or emote not in EMOTE_IDS
                    or type(target) is not int or not 0 <= target < self.player_count or target == seat):
                return False, 'INVALID_INTERACTION'
            if now - self._emote_sent.get(seat, -math.inf) < EMOTE_GAP:
                return False, 'INTERACTION_RATE_LIMITED'
            self._emote_sent[seat] = now
            event.update(emote=emote, targetSeat=target)

        self.conn.broadcast(event)
        return True, ''

    def handle_client_message(self, seat: int, message: dict) -> tuple[bool, str]:
        if message.get('type') == 'ping' or message.get('kind') == 'ping':
            self.conn.send_to_seat_nowait(seat, {'kind': 'pong'})
            return True, ''
        kind = message.get('kind')
        if kind in ('quick_chat', 'emote'):
            return self._handle_interaction(seat, kind, message)
        if message.get('kind') == 'auto':
            enabled = message.get('enabled')
            if not isinstance(enabled, bool):
                return False, 'INVALID_AUTO'
            if self.seats[seat] is None:
                return False, 'NOT_HUMAN_SEAT'
            if enabled:
                self.auto_seats.add(seat)
            else:
                self.auto_seats.discard(seat)
            self._action_epoch += 1
            if seat in self._holds:
                self._release_hold(seat)
            self._wake.set()
            self.broadcast_snapshot()
            return True, ''
        if message.get('kind') == 'opening_done':
            if self.seats[seat] is None:
                return False, 'NOT_HUMAN_SEAT'
            if self.state is None or message.get('round') != self.state.hand_number:
                return False, 'STALE_ACTION'
            self._opening_confirmations.add(seat)
            self._wake.set()
            return True, ''
        if kind == 'round_speech_done':
            if self.seats[seat] is None:
                return False, 'NOT_HUMAN_SEAT'
            if (not self._round_speech_sent or self.state is None or self.state.result is None
                    or type(message.get('round')) is not int or message['round'] != self.state.hand_number
                    or message.get('matchId') != (self.match_id or self.room_id)):
                return False, 'STALE_ACTION'
            self._round_speech_confirmations.add(seat)
            self._wake.set()
            return True, ''
        if message.get('kind') == 'continue':
            if self.seats[seat] is None:
                return False, 'NOT_HUMAN_SEAT'
            if self.state is None or self.state.result is None or self.state.result.finished \
                    or message.get('round') != self.state.hand_number:
                return False, 'STALE_ACTION'
            if 'matchId' in message and message['matchId'] != (self.match_id or self.room_id):
                return False, 'STALE_ACTION'
            self._continue_confirmations.add(seat)
            # 旧客户端只有“继续”回执。提前确认也只能在台词发完后推进。
            if 'matchId' not in message:
                self._round_speech_confirmations.add(seat)
            self._wake.set()
            self.broadcast_snapshot()
            return True, ''
        if message.get('kind') == 'hold':
            holding = message.get('holding')
            if not isinstance(holding, bool):
                return False, 'INVALID_HOLD'
            if holding:
                return self._start_hold(seat)
            self._release_hold(seat)
            return True, ''
        if message.get('kind') != 'action':
            return False, 'INVALID_MESSAGE'
        if self.state is None or self.state.phase == 'settled':
            return False, 'STALE_ACTION'
        if self._opening is not None:
            return False, 'STALE_ACTION'
        if self._holds and seat not in self._holds:
            return False, 'TABLE_HELD'
        try:
            command = command_from_action(message.get('action'))
        except ValueError:
            return False, 'INVALID_ACTION'
        # 延长胡口只由房间驱动决定。真人主动点“过”始终按放弃处理，
        # 不能通过客户端标记绕过必须胡/过手判定。
        command.defer_hu = False
        if seat in self._holds:
            if command.type not in ('declareHu', 'passHu'):
                return False, 'HOLD_ACTION_NOT_ALLOWED'
            self._release_hold(seat)
        if self.yaotong_mode == 'easy':
            # 新手模式服务端执行同一套输入拦截；客户端按钮已隐藏，这里防绕过。
            if command.type in ('declareHu', 'passHu'):
                return False, 'PROTECTED_ACTION'
            reason = protection_reason(
                action_protection(self.state, seat), command)
            if reason:
                return False, 'PROTECTED_ACTION'
        if not self._apply_command(seat, command):
            return False, 'INVALID_ACTION'
        self._action_epoch += 1
        if (self.state.pending is not None and self.state.phase == 'response'
                and (command.type == 'declareHu' or command.action == 'hu')):
            # 延长胡口恢复后，其他真人仍可对同一张牌确认一炮多响。
            self._reaction_key = None
        self._reaction_remaining()
        self._wake.set()
        self.broadcast_snapshot()
        return True, ''

    # ── 收尾 ────────────────────────────────────────────

    def close(self) -> None:
        self._transition = None
        self.closed = True
        self._cancel_llm_audio()
        self._release_all_holds()
        self.conn.broadcast({'kind': 'room_closed'})
        try:
            current = asyncio.current_task()
        except RuntimeError:
            current = None
        if (self._drive_task is not None and not self._drive_task.done()
                and current is not self._drive_task):
            self._drive_task.cancel()
        if self.storage is not None:
            try:
                self.storage.update_room_status(self.room_id, 'closed')
            except Exception:
                logger.bind(room_id=self.room_id).exception('房间关闭落库失败')

    def force_finish(self, reason: str = 'stalled', *,
                     cancel_task: bool = True) -> bool:
        if self._forced_finishing or self.status != 'playing':
            return False
        self._transition = None
        self._release_all_holds()
        self._forced_finishing = True
        self.status = 'finished'
        self.match_finished = True
        self._cancel_llm_audio()
        if (cancel_task and self._drive_task is not None
                and not self._drive_task.done()):
            self._drive_task.cancel()
        self.broadcast_snapshot()
        logger.bind(room_id=self.room_id).warning(
            f"幺筒对局被强制收尾 reason={reason}")
        return True

    async def finalize_forced(self) -> None:
        await self._persist_round()
        if self.storage is not None and self.match_id is not None:
            try:
                self.storage.update_room_status(self.room_id, 'finished')
            except Exception:
                logger.bind(room_id=self.room_id).exception('强制收尾落库失败')
        if self.is_past_deadline():
            room_registry.remove(self.room_id)

    # ── 落库 ────────────────────────────────────────────

    async def _persist_round(self) -> None:
        if self.storage is None or self.match_id is None:
            return
        state = self.state
        if state is not None and state.phase == 'settled' and state.result is not None:
            key = (self.match_id, state.hand_number)
            if key not in self._queued_rounds:
                self._round_writes.append((self.match_id, {
                    'round': state.hand_number,
                    'rulesetId': self.ruleset_id,
                    **settlement_view(state.result),
                }))
                self._queued_rounds.add(key)
        # 按序补写：数据库抖动不阻断牌局，下一次结算或收尾时重试。
        while self._round_writes:
            match_id, data = self._round_writes[0]
            try:
                await asyncio.to_thread(self.storage.insert_round_result, match_id, data)
            except Exception:
                logger.bind(room_id=self.room_id).exception('幺筒结算落库失败，待下次补写')
                return
            self._round_writes.pop(0)

    def _persist_match_start(self) -> None:
        if self.storage is None:
            return
        players = [
            {
                'seat': seat,
                'player_id': state.player_id if state is not None else None,
                'nickname': (
                    state.nickname if state is not None
                    else self.llm_seats[seat].nickname if seat in self.llm_seats else f'AI-{seat}'
                ),
            }
            for seat, state in enumerate(self.seats)
        ]
        try:
            self.match_id = self.storage.create_match(
                self.room_id, self.mode, self.ruleset_id, self.match_length)
            self.storage.update_room_status(self.room_id, 'playing')
            self.storage.upsert_match_players(self.match_id, players)
        except Exception:
            logger.bind(room_id=self.room_id).exception('幺筒开局落库失败')

    def _broadcast_match_finished(self) -> None:
        final_scores = [] if self.state is None else [
            {'seat': index, 'name': player.name, 'score': player.score}
            for index, player in enumerate(self.state.players)
        ]
        self.conn.broadcast({
            'kind': 'match_finished',
            'roomId': self.room_id,
            'mode': self.mode,
            'rulesetId': 'yaotong',
            'matchLength': self.match_length,
            'finalScores': final_scores,
        })

    def _persist_match_end(self) -> None:
        if self.storage is None or self.match_id is None or self.state is None:
            return
        try:
            self.storage.finish_match(
                self.match_id,
                [
                    {'seat': index, 'name': player.name, 'score': player.score}
                    for index, player in enumerate(self.state.players)
                ],
            )
            self.storage.update_room_status(self.room_id, 'finished')
        except Exception:
            logger.bind(room_id=self.room_id).exception('幺筒终局落库失败')
