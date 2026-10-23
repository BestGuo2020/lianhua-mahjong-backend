"""幺筒房间会话 —— 座位/重进码/开局/驱动/快照。

与经典 RoomSession、BloodFlowRoomSession 保持同一套 REST/WS 鸭子契约；
对局规则全部走 app.core.yaotong.engine，MVP 只做真人 + 服务端 AI 补位。
"""

from __future__ import annotations

import asyncio
import secrets
import time
from dataclasses import dataclass, field
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
from app.game.room import ROOM_LIFETIME, RoomError, _make_rejoin_code, room_registry
from app.game.yaotong_protocol import command_from_action, snapshot_for
from app.ws.manager import ConnectionManager

# 对齐前端 src/game/core/local/localGameConfig.ts 的 PACE_MS。
PACE_MS_AFTER_CLAIM_GANG = 0.55   # 明杠补牌前
PACE_MS_AFTER_KONG_SETTLE = 0.6   # 补杠结算/补牌前


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
        self.llm_enabled = False
        self.status = 'lobby'
        self.creator_seat: Optional[int] = None
        self.seats: list[Optional[_Seat]] = [None] * 4
        self.reserved_seats: dict = {}
        self.table_theme = 'jade'
        self.lifetime = ROOM_LIFETIME
        self.created_at = time.monotonic()
        self.deadline = self.created_at + ROOM_LIFETIME
        self.conn = ConnectionManager()

        self.state: Optional[GameState] = None
        self.match_id: Optional[str] = None
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
        self._continue_confirmations: set[int] = set()
        self.auto_seats: set[int] = set()
        self._wake = asyncio.Event()
        self._action_epoch = 0
        self._reaction_key = None
        self._reaction_deadline = 0.0

        self.turn_timeout = 20.0
        self.opening_timeout = 60.0
        self.continue_timeout = 20.0
        # 真人房间注入 PACE 时按单机 AI_DELAYS/YAOTONG_AI_DISCARD_MS 停顿；
        # 测试/无头房间默认 0（仍可用 ai_delay 覆盖）。
        self.ai_delay = 1.0 if pace else 0.0
        # 老手“等”由客户端显式开始/结束；未点等时不自动代胡。
        # 这里只留超长兜底防止幽灵会话，真人掉线会立即转 AI。
        self.hard_fallback_timeout = 3600.0
        self._decision_deadline: Optional[float] = None
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
        seat = next(
            (index for index in range(4) if self.seats[index] is None), None)
        if seat is None:
            raise RoomError('ROOM_FULL')
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
        return False

    @property
    def effective_llm_enabled(self) -> bool:
        return False

    def set_reserved_seat(self, seat: int, provider_id: Optional[str],
                          style: Optional[str] = None) -> None:
        raise RoomError('LLM_NOT_ENABLED')

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

    def _connected_human_seats(self) -> list[int]:
        # 托管只交出操作权，在线玩家仍须看到完整开局与结算演出。
        return [seat for seat, state in enumerate(self.seats)
                if state is not None and state.controller.connected]

    @property
    def opening(self) -> Optional[dict]:
        return self._opening

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

    # ── 开局 ────────────────────────────────────────────

    async def start(self, llm_seats: Optional[list] = None,
                    default_provider: Optional[str] = None) -> None:
        if self._drive_task is not None and not self._drive_task.done():
            raise RoomError('ALREADY_STARTED')
        if self.status not in ('lobby', 'finished'):
            raise RoomError('ROOM_CLOSED')
        if not any(state is not None for state in self.seats):
            raise RoomError('ROOM_EMPTY')
        if not all(state.ready for state in self.seats if state is not None):
            raise RoomError('NOT_ALL_READY')
        if llm_seats:
            raise RoomError('LLM_NOT_ENABLED')

        self._release_all_holds()
        self._transition = None
        self.state = self.game_factory(StartOptions(length=self.match_length))
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

    def _prepare_opening(self) -> None:
        self._opening = {
            'dice': list(self.state.dice),
            'wallSide': self.state.wall_side,
            'reservedStacks': self.state.reserved_stacks,
        }
        self._opening_round = self.state.hand_number
        self._opening_confirmations = set()
        self._reaction_key = None

    def _apply_names(self) -> None:
        if self.state is None:
            return
        for index, player in enumerate(self.state.players):
            state = self.seats[index]
            if state is not None:
                player.name = state.nickname

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
                    self._release_all_holds()
                    self.broadcast_snapshot()
                    if state.result is not None and state.result.finished:
                        break
                    await self._wait_for_continues()
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
            self._persist_match_end()
            if self.is_past_deadline():
                room_registry.remove(self.room_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            self.status = 'error'
            self.conn.broadcast({'kind': 'error', 'code': 'INTERNAL_ERROR'})
            logger.bind(room_id=self.room_id).exception('幺筒房间驱动异常')

    async def _drive_step(self) -> None:
        state = self.state
        if state is None:
            return
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
            timeout = (
                self.hard_fallback_timeout
                if self.yaotong_mode == 'hard' else self.turn_timeout
            )
            self._decision_deadline = time.time() + timeout
            try:
                acted = await self._wait_for_action(seat, timeout)
            finally:
                self._decision_deadline = None
            if acted:
                return
            if self._holds:
                # 等开始后本函数让出主线；等结束由 _drive_step 重新评估状态，
                # 避免拿着旧决策窗口继续等或直接代打。
                while self._holds:
                    await asyncio.sleep(0.2)
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
        if delay is None:
            delay = self._ai_think_seconds(state, seat)
        await self._wait_pace(delay)
        # sleep 期间可能重连、取消托管、真人抢先操作或换局。旧任务只需丢弃。
        if (self.closed or self._holds or self.state is not state
                or self._action_epoch != epoch or self._human_seat(seat) != was_human
                or state.phase == 'settled'):
            return
        command = command or choose_action(state, seat)
        if command is None:
            command = self._fallback_command(state, seat)
        ok = self._apply_command(seat, command) if command is not None else False
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
        required = self._connected_human_seats()
        if not required:
            return
        deadline = asyncio.get_running_loop().time() + self.continue_timeout
        while asyncio.get_running_loop().time() < deadline:
            required = self._connected_human_seats()
            if not required or all(
                seat in self._continue_confirmations for seat in required
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

    # ── 客户端消息 ──────────────────────────────────────

    def handle_client_message(self, seat: int, message: dict) -> tuple[bool, str]:
        if message.get('type') == 'ping' or message.get('kind') == 'ping':
            self.conn.send_to_seat_nowait(seat, {'kind': 'pong'})
            return True, ''
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
        if message.get('kind') == 'continue':
            if self.seats[seat] is None:
                return False, 'NOT_HUMAN_SEAT'
            if self.state is None or self.state.result is None or self.state.result.finished \
                    or message.get('round') != self.state.hand_number:
                return False, 'STALE_ACTION'
            self._continue_confirmations.add(seat)
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
        if seat in self._holds:
            if command.type not in ('declareHu', 'passHu'):
                return False, 'HOLD_ACTION_NOT_ALLOWED'
            self._release_hold(seat)
        if self.yaotong_mode == 'easy':
            # 新手模式服务端执行同一套输入拦截；客户端按钮已隐藏，这里防绕过。
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
        if (cancel_task and self._drive_task is not None
                and not self._drive_task.done()):
            self._drive_task.cancel()
        self.broadcast_snapshot()
        logger.bind(room_id=self.room_id).warning(
            f"幺筒对局被强制收尾 reason={reason}")
        return True

    async def finalize_forced(self) -> None:
        if self.storage is not None and self.match_id is not None:
            try:
                self.storage.update_room_status(self.room_id, 'finished')
            except Exception:
                logger.bind(room_id=self.room_id).exception('强制收尾落库失败')
        if self.is_past_deadline():
            room_registry.remove(self.room_id)

    # ── 落库 ────────────────────────────────────────────

    def _persist_match_start(self) -> None:
        if self.storage is None:
            return
        players = [
            {
                'seat': seat,
                'player_id': state.player_id if state is not None else None,
                'nickname': (
                    state.nickname if state is not None
                    else f'AI-{seat}'
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
