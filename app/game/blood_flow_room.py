"""血流房间会话 —— 后端权威对局房间（M3）。

复用 RoomSession 的外部契约（REST 房间生命周期 + WS 重进），但驱动自己的
BloodFlowEngine：锁手/多响/抢杠/杠收付/牌墙耗尽结算。快照对齐前端
bloodFlowSeatView / network/protocol.ts 形状，前端 externalAuthority 直接消费。

本版不含观战、LLM 补位（M4）与对局落库；空座用本地规则策略代打。
"""

import asyncio
import secrets
import time
from typing import Optional

from loguru import logger

from app.core.blood_flow.config import BLOOD_FLOW_CONFIG
from app.game.blood_flow_engine import (SEATS, BloodFlowEngine, next_seat,
                                        window_complete)
from app.rules.blood_flow import BloodFlowRuleSet
from app.ws.manager import ConnectionManager


def _make_rejoin_code() -> str:
    return f'{secrets.token_hex(2).upper()}-{secrets.token_hex(2).upper()}'


class _Controller:
    """占位控制器：api/rooms.py 只读 connected。"""

    def __init__(self):
        self.connected = False


class _Seat:
    __slots__ = ('seat', 'nickname', 'rejoin_code', 'player_id', 'character_id',
                 'controller', 'connected_at', 'ready', 'avatar')

    def __init__(self, seat: int, nickname: str, rejoin_code: str,
                 player_id: Optional[str] = None, character_id: str = 'deepseek'):
        self.seat = seat
        self.nickname = nickname
        self.rejoin_code = rejoin_code
        self.player_id = player_id
        self.character_id = character_id
        self.controller = _Controller()
        self.connected_at: Optional[float] = None
        self.ready = False
        self.avatar = ''


class BloodFlowRoomError(Exception):
    pass


def _fallback_policy(engine: BloodFlowEngine, seat: int) -> dict:
    """兜底代打策略：锁手自动 + 见胡就胡 + 首张普通弃牌（EV 决策失败时使用）。"""
    window = engine.window
    options = window['options'][seat]
    win = next((a for a in options if a['kind'] == 'win'), None)
    if engine.seats[seat]['locked']:
        if win:
            return win
        return {'kind': 'discard', 'index': engine.players[seat]['drawnTileIndex']}
    if win:
        return win
    discards = [a for a in options if a['kind'] == 'discard']
    if discards:
        ordinary = [a for a in discards
                    if engine.players[seat]['hand'][a['index']] not in {*engine.jokers, 'white'}]
        return (ordinary or discards)[0]
    return next((a for a in options if a['kind'] == 'pass'), options[0])


def _bot_policy(room: 'BloodFlowRoomSession', engine: BloodFlowEngine, seat: int) -> dict:
    """空座代打：EV 策略翻译版（M4）；异常/空决策回退规则策略。"""
    try:
        from app.core.blood_flow.ai import decide_blood_flow_action_ev
        from app.core.blood_flow.config import BLOOD_FLOW_AI
        decision = decide_blood_flow_action_ev(room._seat_view(seat), BLOOD_FLOW_AI)
        if decision is not None and decision in engine.window['options'][seat]:
            return decision
    except Exception:
        logger.bind(room_id=room.room_id, seat=seat).warning('EV 代打异常，回退规则策略')
    return _fallback_policy(engine, seat)


class BloodFlowRoomSession:
    def __init__(self, room_id: str, mode: str = 'east', capacity: int = 4,
                 ruleset_id: str = 'lotus-blood-flow', storage=None,
                 pace: int = 0, llm_enabled: bool = False):
        self.room_id = room_id
        self.mode = mode
        self.capacity = capacity
        self.ruleset_id = ruleset_id
        self.table_theme = 'jade'
        self.pace = pace  # 每步间隔毫秒（真人房间注入节奏；测试 0）
        self.decision_ms = BLOOD_FLOW_CONFIG.base_points * 0 + 15_000
        self.seats: list[Optional[_Seat]] = [None] * 4
        self.conn = ConnectionManager()
        self.engine: Optional[BloodFlowEngine] = None
        self.scores = [BLOOD_FLOW_CONFIG.initial_score] * 4
        self.round_index = -1
        self.match_started = False
        self.match_finished = False
        self.closed = False
        self.created_at = time.monotonic()
        self._rejoin_attempts: dict[str, list[float]] = {}
        self._drive_task: Optional[asyncio.Task] = None
        self.round_result: Optional[dict] = None

    # ── 生命周期（api/rooms.py 契约） ──

    def is_past_deadline(self, now: Optional[float] = None) -> bool:
        return (now or time.monotonic()) - self.created_at > 10 * 60

    def is_expired(self, now: Optional[float] = None) -> bool:
        return not self.has_humans() and self.is_past_deadline(now)

    def join_or_rejoin(self, nickname: str, rejoin_code: Optional[str] = None,
                       player_id: Optional[str] = None, character_id: str = 'deepseek'):
        for state in self.seats:
            if state is not None and state.player_id == player_id and player_id is not None:
                return state
        if rejoin_code:
            return self.resume_by_code(rejoin_code)[1]
        seat = next((s for s in range(self.capacity) if self.seats[s] is None), None)
        if seat is None:
            raise BloodFlowRoomError('ROOM_FULL')
        code = _make_rejoin_code()
        state = _Seat(seat, nickname, code, player_id, character_id)
        self.seats[seat] = state
        return state

    def resume_by_code(self, rejoin_code: str):
        for state in self.seats:
            if state is not None and state.rejoin_code == rejoin_code:
                return state.seat, state
        raise BloodFlowRoomError('REJOIN_CODE_INVALID')

    def check_rejoin_rate(self, rejoin_code: str) -> bool:
        now = time.monotonic()
        attempts = [t for t in self._rejoin_attempts.get(rejoin_code, []) if now - t < 30]
        self._rejoin_attempts[rejoin_code] = attempts
        return len(attempts) < 5

    def reset_rejoin_rate(self, rejoin_code: str) -> None:
        self._rejoin_attempts.pop(rejoin_code, None)

    def release_seat(self, seat: int, rejoin_code: Optional[str] = None) -> None:
        state = self.seats[seat]
        if state is not None and (rejoin_code is None or state.rejoin_code == rejoin_code):
            self.seats[seat] = None
            self.conn.unregister(seat)

    def ready_seat(self, seat: int, ready: Optional[bool] = None) -> bool:
        state = self.seats[seat]
        if state is None:
            return False
        state.ready = ready if ready is not None else not state.ready
        return True

    def set_character(self, seat: int, character_id: str) -> str:
        state = self.seats[seat]
        if state is None:
            raise BloodFlowRoomError('SEAT_EMPTY')
        state.character_id = character_id
        return character_id

    def has_humans(self) -> bool:
        return any(state is not None for state in self.seats)

    def on_connect(self, seat: int) -> None:
        state = self.seats[seat]
        if state is None:
            return
        state.controller.connected = True
        state.connected_at = time.monotonic()

    def on_disconnect(self, seat: int) -> None:
        state = self.seats[seat]
        if state is not None:
            state.controller.connected = False

    def close(self) -> None:
        self.closed = True
        if self._drive_task is not None:
            self._drive_task.cancel()

    def _human_seat(self, seat: int) -> bool:
        state = self.seats[seat]
        return state is not None and state.controller.connected

    # ── 开局与驱动 ──

    def start(self, llm_seats: Optional[list] = None) -> None:
        if self.match_started:
            return
        if not all(state.ready for state in self.seats if state is not None):
            raise BloodFlowRoomError('NOT_ALL_READY')
        self.match_started = True
        self._drive_task = asyncio.ensure_future(self._drive())

    async def _drive(self) -> None:
        total = BLOOD_FLOW_CONFIG.rounds[self.mode]
        try:
            for round_index in range(total):
                self.round_index = round_index
                rules = BloodFlowRuleSet()
                engine = BloodFlowEngine(
                    authority_epoch=f'bf-{self.room_id}', round_id=f'round-{round_index}',
                    rules=rules, dealer=round_index % 4, scores=self.scores)
                self.engine = engine
                self.round_result = None
                self.broadcast_snapshot()
                await self._play_round(engine)
                self.scores = list(engine.result['endingScores'])
                self.round_result = engine.result
                self.engine = None
                self.broadcast_snapshot()
                if self.pace:
                    await asyncio.sleep(self.pace / 1000 * 3)
            self.match_finished = True
            self.broadcast_snapshot()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # pragma: no cover - 防御性兜底
            logger.bind(room_id=self.room_id).exception(f'血流房间驱动异常 {exc}')
            self.conn.broadcast({'kind': 'error', 'code': 'INTERNAL_ERROR'})
        finally:
            self.engine = None

    async def _play_round(self, engine: BloodFlowEngine) -> None:
        while not engine.result and not self.closed:
            deadline = time.monotonic() + self.decision_ms / 1000
            last_broadcast: Optional[str] = None
            while not engine.result and not self.closed:
                # 机器人席位持续提交（逐席重读窗口：提交可能解决旧窗口并开新窗口）。
                progressed = False
                for seat in SEATS:
                    window = engine.window
                    if window and window['options'][seat] and window['decisions'][seat] is None \
                            and not self._human_seat(seat):
                        if engine.submit(engine.command(seat, _bot_policy(self, engine, seat))):
                            progressed = True
                if engine.window is None or window_complete(engine.window):
                    break
                if engine.window and engine.window['id'] != last_broadcast:
                    self.broadcast_snapshot()
                    last_broadcast = engine.window['id']
                if time.monotonic() >= deadline:
                    engine.expire()
                    break
                await asyncio.sleep(0.05)
            self.broadcast_snapshot()
            if self.pace:
                await asyncio.sleep(self.pace / 1000)

    # ── 客户端消息 ──

    def handle_client_message(self, seat: int, message: dict) -> tuple[bool, str]:
        if message.get('kind') != 'action':
            return False, 'INVALID_MESSAGE'
        engine = self.engine
        if engine is None or engine.window is None:
            return False, 'STALE_ACTION'
        action = message.get('action')
        if not isinstance(action, dict) or message.get('windowId') != engine.window['id'] \
                or message.get('stateVersion') != engine.window['version']:
            return False, 'STALE_ACTION'
        if engine.window['decisions'][seat] is not None \
                or not any(a == action for a in engine.window['options'][seat]):
            return False, 'INVALID_ACTION'
        if not engine.submit(engine.command(seat, action)):
            return False, 'INVALID_ACTION'
        self.broadcast_snapshot()
        return True, ''

    # ── 快照（对齐前端 bloodFlowSeatView） ──

    def _public_score(self, score) -> dict:
        return {
            'items': [{'id': p.id, 'label': p.label, 'weight': p.weight} for p in score.items],
            'excluded': [{'id': e.id, 'includedBy': e.included_by} for e in score.excluded],
            'hardWin': score.hard_win, 'source': score.source, 'opening': score.opening,
            'patternMultiplier': score.pattern_multiplier, 'eventMultiplier': score.event_multiplier,
            'openingApplied': score.opening_applied, 'uncappedMultiplier': score.uncapped_multiplier,
            'finalMultiplier': score.final_multiplier, 'capped': score.capped,
            'paymentPerPayer': score.payment_per_payer,
        }

    def _seat_view(self, seat: int) -> dict:
        engine = self.engine
        if engine is None:
            return {}
        players = []
        for index, p in enumerate(engine.players):
            players.append({
                'name': p['name'], 'avatar': p['avatar'], 'score': p['score'], 'seat': p['seat'],
                'hand': list(p['hand']) if (index == seat or engine.result) else [],
                'concealedTileCount': len(p['hand']),
                'discards': list(p['discards']),
                'melds': [{'type': m['type'], 'tile': m['tile'], 'tiles': m['tiles'],
                           **({'from': m['from']} if m.get('from') is not None else {}),
                           **({'windKong': True} if m.get('windKong') else {}),
                           **({'added': True} if m.get('added') else {})} for m in p['melds']],
                'redCount': p['redCount'], 'drawnTileIndex': p['drawnTileIndex'],
            })
        window = engine.window
        view = {
            'authorityEpoch': engine.authority_epoch, 'roundId': engine.round_id,
            'version': engine.version, 'seat': seat,
            'players': players, 'currentPlayer': engine.current_player,
            'wallCount': len(engine.wall), 'headDrawn': engine.head_drawn,
            'flipTile': engine.flip_tiles[0], 'jokers': list(engine.jokers),
            'flipStack': engine.flip_stack, 'flipSeat': engine.flip_seat,
            'wallBreakIndex': engine.wall_break_index,
            'window': None if window is None else {
                'id': window['id'], 'version': window['version'], 'kind': window['kind'],
                'deadlineAt': window['deadlineAt'], 'opensAt': window['opensAt'],
                'source': dict(window['source']),
            },
            'ownActions': [] if window is None or window['decisions'][seat] is not None
            else [dict(a) for a in window['options'][seat]],
            'ownScore': self._public_score(engine.evaluation[seat].score)
            if seat in engine.evaluation and window is not None and window['decisions'][seat] is None else None,
            'waitingSeats': [s for s in SEATS if window is not None and window['options'][s]
                             and window['decisions'][s] is None],
            'public': engine.public_state(),
            'actionEvents': [], 'lastDiscardAction': None,
            'kongEvents': [e for e in engine.ledger if e['kind'] == 'kong'],
        }
        return view

    def broadcast_snapshot(self) -> None:
        for seat in range(self.capacity):
            state = self.seats[seat]
            if state is None or not state.controller.connected:
                continue
            self.conn.send_to_seat_nowait(seat, {
                'kind': 'bf_snapshot',
                'view': self._seat_view(seat),
                'round': self.round_index,
                'mode': self.mode,
                'dealer': self.round_index % 4,
                'matchFinished': self.match_finished,
                'roundResult': self.round_result,
            })
