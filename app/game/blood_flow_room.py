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
from app.game.room import RoomError
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
                 pace: int = 0, llm_enabled: bool = False, llm_request_fn=None):
        self.room_id = room_id
        self.mode = mode
        self.capacity = capacity
        self.player_count = capacity
        self.ruleset_id = ruleset_id
        self.table_theme = 'jade'
        # 经典房间 pace 是 dict；血流只用整数毫秒节奏（dict 一律视为 0/测试节奏）。
        self.pace = pace if isinstance(pace, int) else 0
        self.decision_ms = 15_000
        self.status = 'lobby'
        self.creator_seat: Optional[int] = None
        self.lifetime = 600
        self.llm_enabled = bool(llm_enabled)
        self.effective_llm_enabled = False
        self.llm_available = False
        # LLM 席位：seat → LlmProviderConfig（baseUrl/apiKey/model/style/timeoutMs/...）。
        self.llm_seats: dict[int, dict] = {}
        self.llm_request = llm_request_fn
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
                       player_id: Optional[str] = None, character_id: str = 'deepseek',
                       avatar: str = ''):
        """与 RoomSession 同契约：返回 (seat, is_rejoin, state)；重进码恢复原座位。"""
        if rejoin_code:
            seat, state = self.resume_by_code(rejoin_code)
            state.nickname = nickname or state.nickname
            return seat, True, state
        for state in self.seats:
            if state is not None and player_id is not None and state.player_id == player_id:
                return state.seat, True, state
        seat = next((s for s in range(self.capacity) if self.seats[s] is None), None)
        if seat is None:
            raise RoomError('ROOM_FULL')
        code = _make_rejoin_code()
        state = _Seat(seat, nickname, code, player_id, character_id)
        state.avatar = avatar
        self.seats[seat] = state
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
            raise RoomError('SEAT_EMPTY')
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

    async def start(self, llm_seats: Optional[list] = None, default_provider: Optional[str] = None) -> None:
        """开局（与 RoomSession 同契约）：所有已占座位 ready 后触发。"""
        if self.match_started:
            return
        if not all(state.ready for state in self.seats if state is not None):
            raise RoomError('NOT_ALL_READY')
        self._resolve_llm_seats(llm_seats or [], default_provider)
        self.match_started = True
        self.status = 'playing'
        self._drive_task = asyncio.ensure_future(self._drive())

    def _resolve_llm_seats(self, llm_seats: list, default_provider: Optional[str]) -> None:
        """服务端供应商解析：每席位 providerId（空用默认）→ LlmProviderConfig 字典。"""
        from app.llm.config import default_provider_id, load_llm_providers, llm_server_available
        self.llm_available = llm_server_available()
        providers = load_llm_providers()
        self.llm_seats = {}
        if not providers:
            return
        fallback_id = default_provider or default_provider_id()
        for entry in llm_seats:
            seat = entry.get('seat')
            provider_id = (entry.get('providerId') or '').strip().lower() or fallback_id
            provider = providers.get(provider_id)
            if provider is None or not isinstance(seat, int) or not 0 <= seat < 4:
                continue
            self.llm_seats[seat] = provider.to_config((entry.get('style') or '').strip())
        self.effective_llm_enabled = bool(self.llm_seats)

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
            self.status = 'finished'
            self.broadcast_snapshot()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # pragma: no cover - 防御性兜底
            logger.bind(room_id=self.room_id).exception(f'血流房间驱动异常 {exc}')
            self.conn.broadcast({'kind': 'error', 'code': 'INTERNAL_ERROR'})
        finally:
            self.engine = None

    async def _llm_action(self, engine: BloodFlowEngine, seat: int) -> Optional[dict]:
        """LLM 席位决策：候选 + 提示词 → 真实模型；失败/超时/非法选择返回 None（调用方回退 EV）。"""
        if self.llm_request is None:
            from app.llm.client import request_llm_decision
            self.llm_request = request_llm_decision
        cfg = self.llm_seats[seat]
        try:
            from app.core.blood_flow.ai import decide_blood_flow_action_ev
            from app.core.blood_flow.config import BLOOD_FLOW_AI
            from app.llm.blood_flow_candidates import (build_blood_flow_candidates,
                                                       build_blood_flow_prompt, ev_features_for)
            view = self._seat_view(seat)
            request_id = f'llm/{self.room_id}/{engine.round_id}/{engine.window["id"]}/{seat}'
            suggestion = decide_blood_flow_action_ev(view, BLOOD_FLOW_AI)
            built = build_blood_flow_candidates(view, request_id, suggestion=suggestion,
                                                ev_by_key=ev_features_for(view))
            candidate_ids = [c['id'] for c in built['candidates']]
            if not candidate_ids:
                return None
            system, user = build_blood_flow_prompt(cfg.style, view, built)
            timeout_ms = max(2_000, min(int(cfg.timeout_s * 1000), self.decision_ms - 500))
            choice, _message = await asyncio.wait_for(
                self.llm_request(cfg, system, user, candidate_ids, False),
                timeout=timeout_ms / 1000)
            match = next((c for c in built['candidates'] if c['id'] == choice), None)
            return match['action'] if match is not None else None
        except Exception as exc:  # noqa: BLE001 - 任何失败都回退 EV，不阻塞对局
            logger.bind(room_id=self.room_id, seat=seat).warning(f'LLM 决策失败回退 EV：{exc}')
            return None

    async def _decide_bots(self, engine: BloodFlowEngine) -> None:
        """非真人席位决策：LLM 席位走模型（并行），其余走 EV；提交前重验窗口。"""
        window = engine.window
        if not window:
            return
        pending = [s for s in SEATS if window['options'][s] and window['decisions'][s] is None
                   and not self._human_seat(s)]
        if not pending:
            return

        async def resolve(seat: int) -> None:
            action: Optional[dict] = None
            if seat in self.llm_seats:
                action = await self._llm_action(engine, seat)
            if action is None:
                action = _bot_policy(self, engine, seat)
            if action is not None and engine.window and engine.window['id'] == window['id'] \
                    and engine.window['decisions'][seat] is None:
                engine.submit(engine.command(seat, action))

        await asyncio.gather(*(resolve(s) for s in pending))

    async def _play_round(self, engine: BloodFlowEngine) -> None:
        while not engine.result and not self.closed:
            deadline = time.monotonic() + self.decision_ms / 1000
            last_broadcast: Optional[str] = None
            while not engine.result and not self.closed:
                # 非真人席位持续决策（逐席重读窗口：提交可能解决旧窗口并开新窗口）。
                await self._decide_bots(engine)
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

    def _serializable_public(self, public: dict) -> dict:
        """public 快照深转换：批次与局末账本里的 PublicWinScore 转 JSON 字典。"""
        batches = []
        for batch in public.get('batches', []):
            clean = dict(batch)
            clean['winners'] = [{**dict(w), 'score': self._public_score(w['score'])}
                                for w in batch['winners']]
            batches.append(clean)
        result = public.get('roundResult')
        clean_result = None
        if result:
            clean_result = dict(result)
            ledger = []
            for entry in result['ledger']:
                if entry['kind'] == 'win':
                    clean_entry = dict(entry)
                    clean_batch = dict(entry['batch'])
                    clean_batch['winners'] = [
                        {**dict(w), 'score': self._public_score(w['score'])}
                        for w in entry['batch']['winners']]
                    clean_entry['batch'] = clean_batch
                    ledger.append(clean_entry)
                else:
                    ledger.append(dict(entry))
            clean_result['ledger'] = ledger
        return {
            'ruleVersion': public['ruleVersion'], 'roundId': public['roundId'],
            'status': public['status'], 'seats': public['seats'],
            'batches': batches, 'roundResult': clean_result,
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
            'public': self._serializable_public(engine.public_state()),
            'actionEvents': [], 'lastDiscardAction': None,
            'kongEvents': [e for e in engine.ledger if e['kind'] == 'kong'],
        }
        return view

    def _serializable_result(self, result: dict) -> dict:
        """局末结果深转换（ledger 批次里的 PublicWinScore → JSON 字典）。"""
        return self._serializable_public({
            'ruleVersion': result['ruleVersion'], 'roundId': result['roundId'],
            'status': 'settled', 'seats': result.get('seats', []), 'batches': [],
            'roundResult': result,
        })['roundResult']

    def snapshot_for(self, seat: int) -> dict:
        """单座全量快照（game_ws 重进握手用；形状同 broadcast）。"""
        return {
            'kind': 'bf_snapshot',
            'view': self._seat_view(seat),
            'round': self.round_index,
            'mode': self.mode,
            'dealer': self.round_index % 4,
            'matchFinished': self.match_finished,
            'roundResult': self._serializable_result(self.round_result) if self.round_result else None,
        }

    def broadcast_snapshot(self) -> None:
        for seat in range(self.capacity):
            state = self.seats[seat]
            if state is None or not state.controller.connected:
                continue
            self.conn.send_to_seat_nowait(seat, self.snapshot_for(seat))
