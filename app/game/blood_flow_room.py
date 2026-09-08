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

from app.core.blood_flow.config import BLOOD_FLOW_CONFIG, BLOOD_FLOW_PACE, BLOOD_FLOW_TIMING
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
        # 节奏：经典房间注入的是经典节奏表（dict）→ 血流用自带节奏表；0 = 测试/无节奏。
        if isinstance(pace, dict):
            self.pace: dict[str, int] = dict(BLOOD_FLOW_PACE)
        else:
            self.pace = dict(BLOOD_FLOW_PACE) if pace else {}
        # 联机决策窗口对齐前端 remoteDecisionMs（含网络余量）。
        self.decision_ms = BLOOD_FLOW_TIMING['remoteDecisionMs']
        # 真人回合倒计时（墙钟毫秒；无真人待决策的窗口为 0 → 前端不显示读秒）。
        self._window_deadline_ms = 0
        # 一次性公告（抢杠胡等）。
        self._announcement: Optional[dict] = None
        self._announcement_id = 0
        # 开局动画：每局第一份快照带骰点，等在线真人回执 opening_done 再开打（兜底超时）。
        self.opening_timeout = 60.0
        self._round_opening: Optional[dict] = None
        self._opening_round: Optional[int] = None
        self._opening_confirmations: set[int] = set()
        # 局间过场：结算后等在线真人回执 continue 再开下一局（兜底超时）。
        self.continue_timeout = 60.0
        self._continue_confirmations: set[int] = set()
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
                # 开局动画：首份快照携带骰点，随后清空（后续快照不带，避免重复触发动画）。
                self._round_opening = {
                    'firstDice': list(engine.dice or [1, 1]),
                    'secondDice': list(engine.second_dice or [1, 1]),
                }
                self._opening_round = round_index
                self._opening_confirmations = set()
                self._continue_confirmations = set()
                self.broadcast_snapshot()
                self._round_opening = None
                await self._wait_for_openings()
                await self._play_round(engine)
                self.scores = list(engine.result['endingScores'])
                self.round_result = engine.result
                # 结算快照：引擎仍在位，view 带 public.roundResult（前端据此弹结算面板）。
                self.broadcast_snapshot()
                if round_index + 1 < total:
                    await self._wait_for_continues()
                self.engine = None
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
                # AI 思考停顿：出牌/杠后补摸/碰杠响应分档（对齐经典 AI_DELAYS）。
                if engine.window['kind'] != 'turn':
                    think_key = 'aiThinkClaim'
                elif engine.kong_bloom:
                    think_key = 'aiThinkKong'
                else:
                    think_key = 'aiThinkTurn'
                await self._pace(think_key)
                action = _bot_policy(self, engine, seat)
            if action is not None and engine.window and engine.window['id'] == window['id'] \
                    and engine.window['decisions'][seat] is None:
                engine.submit(engine.command(seat, action))

        await asyncio.gather(*(resolve(s) for s in pending))

    async def _pace(self, key: str) -> None:
        """节奏停顿（毫秒）；pace 为 0（测试）时直接返回。"""
        if not self.pace:
            return
        delay = self.pace.get(key, 0)
        if delay:
            await asyncio.sleep(delay / 1000)

    def _win_pause_ms(self, batch: dict) -> int:
        """胡牌表现时长：对齐前端 bloodFlowWinTiming(tier).duration + 多响引言 + 交接余量。"""
        tier = 0
        for record in batch['winners']:
            weights = [item.weight for item in record['score'].items] or [1]
            weight = max(weights)
            tier = max(tier, 3 if weight >= 16 else 2 if weight >= 8 else 1 if weight >= 4 else 0)
        if tier >= 2:
            base = self.pace.get('winEffectTop' if tier == 3 else 'winEffectLarge', 2600)
            duration = base - self.pace.get('winEffectDeduct', 620) + self.pace.get('winEffectTail', 1200)
        else:
            duration = self.pace.get('winEffectBase', 1815) + self.pace.get('winEffectTail', 1200)
        multi = self.pace.get('multiWinIntro', 0) \
            if batch['source']['kind'] == 'discard' and len(batch['winners']) > 1 else 0
        # 多响抢杠（补杠被抢）额外再停一轮，对齐经典 betweenRobKongs 的「逐家」读感。
        rob_multi = self.pace.get('betweenRobKongs', 0) \
            if batch['source']['kind'] == 'added-kong' and len(batch['winners']) > 1 else 0
        return duration + multi + rob_multi + self.pace.get('winHandoffMargin', 0)

    def _step_delay_ms(self, engine: BloodFlowEngine, prev_win_count: int,
                       prev_discard_count: int, prev_action_count: int) -> int:
        """按刚发生的动作选择停顿（对齐经典 PLAY_PACE：胡 > 杠 > 碰吃 > 弃牌），毫秒。

        用各流水计数差判断「刚发生了什么」：动作流水（碰/杠/胡）、弃牌流水、赢批次。
        """
        if not self.pace:
            return 0
        wins = [e['batch'] for e in engine.ledger if e['kind'] == 'win']
        if len(wins) > prev_win_count:
            return self._win_pause_ms(wins[-1])
        # 补杠窗口刚打开（尚未裁决）：抢杠前停顿。
        if engine.window is not None and engine.window['source']['kind'] == 'added-kong' \
                and len(engine.actions) == prev_action_count:
            return self.pace.get('beforeRobKong', 0)
        if len(engine.actions) > prev_action_count:
            latest = engine.actions[-1]
            if latest['type'] == 'discard-gang':
                return self.pace.get('afterClaimGangHuman' if self._human_seat(latest['actorIndex'])
                                    else 'afterClaimGang', 0)
            if latest['type'] in ('concealed-gang', 'added-gang', 'wind-kong'):
                return self.pace.get('afterKongSettle', 0)
            if latest['type'] in ('peng', 'chi'):
                if latest['type'] == 'peng' and self._human_seat(latest['actorIndex']):
                    return self.pace.get('afterClaimPengHuman', 0)
                return self.pace.get('afterClaimPeng', 0)
        if len(engine.discard_actions) > prev_discard_count:
            return self.pace.get('afterDiscardToNextTurn', 0)
        return 0

    def _human_pending(self, window: Optional[dict]) -> bool:
        return bool(window) and any(window['options'][s] and window['decisions'][s] is None
                                    and self._human_seat(s) for s in SEATS)

    def _set_rob_announcement(self, engine: BloodFlowEngine, prev_win_count: int) -> None:
        """抢杠胡公告（对齐经典『{name} 抢杠胡』红字公告）。"""
        wins = [e['batch'] for e in engine.ledger if e['kind'] == 'win']
        if len(wins) <= prev_win_count:
            return
        batch = wins[-1]
        if batch['source']['kind'] != 'added-kong':
            return
        winner = batch['winners'][0]['winner']
        seat = self.seats[winner]
        name = seat.nickname if seat is not None else f'玩家{winner + 1}'
        self._announcement_id += 1
        self._announcement = {'text': f'{name} 抢杠胡', 'tone': 'red', 'id': self._announcement_id}

    async def _play_round(self, engine: BloodFlowEngine) -> None:
        while not engine.result and not self.closed:
            deadline = time.monotonic() + self.decision_ms / 1000
            last_broadcast: Optional[str] = None
            while not engine.result and not self.closed:
                prev_win_count = sum(1 for e in engine.ledger if e['kind'] == 'win')
                prev_discard_count = len(engine.discard_actions)
                prev_action_count = len(engine.actions)
                prev_window_id = engine.window['id'] if engine.window else None
                # 非真人席位持续决策（逐席重读窗口：提交可能解决旧窗口并开新窗口）。
                await self._decide_bots(engine)
                if engine.window is None or window_complete(engine.window):
                    break
                if engine.window and engine.window['id'] != last_broadcast:
                    # 真人待决策的窗口：从「窗口真正出现」起重新计时，避免机器人耗时吃掉真人时间。
                    if self._human_pending(engine.window):
                        deadline = time.monotonic() + self.decision_ms / 1000
                        self._window_deadline_ms = int(time.time() * 1000) + self.decision_ms
                    # 抢杠胡公告：本窗口若刚结算了抢杠赢家，随快照下发一次。
                    self._set_rob_announcement(engine, prev_win_count)
                    self.broadcast_snapshot()
                    self._announcement = None  # 公告一次性展示
                    last_broadcast = engine.window['id']
                if time.monotonic() >= deadline:
                    engine.expire()
                    break
                # 步进节奏：窗口已推进才停顿，按刚发生的动作给表现留时间（胡牌含多响引言）。
                if engine.window['id'] != prev_window_id:
                    delay_ms = self._step_delay_ms(engine, prev_win_count, prev_discard_count, prev_action_count)
                    if delay_ms:
                        await asyncio.sleep(delay_ms / 1000)
                await asyncio.sleep(0.05)
            self._window_deadline_ms = 0
            self.broadcast_snapshot()

    # ── 客户端消息 ──

    async def _wait_for_openings(self) -> None:
        """开局就绪屏障：等在线真人回执 opening_done 再开打，兜底超时防卡死。"""
        humans = [s for s in range(self.capacity)
                  if self.seats[s] is not None and self.seats[s].controller.connected]
        if not humans:
            return
        deadline = time.monotonic() + self.opening_timeout
        while not self.closed:
            pending = [s for s in humans if s not in self._opening_confirmations
                       and self.seats[s] is not None and self.seats[s].controller.connected]
            if not pending:
                return
            if time.monotonic() >= deadline:
                logger.bind(room_id=self.room_id).warning(
                    f'开局动画等待超时，直接开打 pending={pending}')
                return
            await asyncio.sleep(0.05)

    def _confirm_opening(self, seat: int, round_: Optional[int]) -> tuple[bool, str]:
        """客户端「opening_done」：标记本局开局动画完成（局号 1-based）。"""
        if self._opening_round is None:
            return True, ''
        if round_ is not None and round_ != self._opening_round + 1:
            return True, ''  # 过期回执直接忽略（不报错，避免客户端噪声）
        self._opening_confirmations.add(seat)
        return True, ''

    async def _wait_for_continues(self) -> None:
        """局间过场屏障：等在线真人确认「下一局」再开新局，兜底超时防卡死。"""
        humans = [s for s in range(self.capacity)
                  if self.seats[s] is not None and self.seats[s].controller.connected]
        if not humans:
            return
        deadline = time.monotonic() + self.continue_timeout
        while not self.closed:
            pending = [s for s in humans if s not in self._continue_confirmations
                       and self.seats[s] is not None and self.seats[s].controller.connected]
            if not pending:
                return
            if time.monotonic() >= deadline:
                logger.bind(room_id=self.room_id).warning(
                    f'局间等待超时，直接开下一局 pending={pending}')
                return
            await asyncio.sleep(0.05)

    def _confirm_continue(self, seat: int, round_: Optional[int]) -> tuple[bool, str]:
        """客户端「continue」：确认进入下一局。

        按「当前局号」接纳（不依赖屏障是否已建立）：结算快照会广播多次，
        客户端可能早于屏障建立就回执，去重后不会重发。
        """
        if round_ is not None and round_ != self.round_index + 1:
            return True, ''
        self._continue_confirmations.add(seat)
        return True, ''

    def handle_client_message(self, seat: int, message: dict) -> tuple[bool, str]:
        if message.get('kind') == 'opening_done':
            return self._confirm_opening(seat, message.get('round'))
        if message.get('kind') == 'continue':
            return self._confirm_continue(seat, message.get('round'))
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
            # 座位元数据来自房间（昵称/头像/角色/人类或 AI），而不是引擎占位名。
            state = self.seats[index]
            is_llm = index in self.llm_seats
            players.append({
                'name': state.nickname if state is not None else p['name'],
                'avatar': state.avatar if state is not None else '',
                'characterId': state.character_id if state is not None else 'deepseek',
                'playerKind': 'human' if state is not None else ('llm' if is_llm else 'bot'),
                'isLlm': is_llm,
                'score': p['score'], 'seat': p['seat'],
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
                # 真人待决策的窗口下发墙钟截止时间（前端读秒）；否则 0（JSON 不接受 Infinity）。
                'deadlineAt': self._window_deadline_ms if self._human_pending(window) else 0,
                'opensAt': window['opensAt'],
                'source': dict(window['source']),
            },
            'ownActions': [] if window is None or window['decisions'][seat] is not None
            else [dict(a) for a in window['options'][seat]],
            'ownScore': self._public_score(engine.evaluation[seat].score)
            if seat in engine.evaluation and window is not None and window['decisions'][seat] is None else None,
            'waitingSeats': [s for s in SEATS if window is not None and window['options'][s]
                             and window['decisions'][s] is None],
            'public': self._serializable_public(engine.public_state()),
            # 动作流水与最近弃牌：驱动前端动作字/语音、弃牌音效与牌名播报。
            'actionEvents': [dict(a) for a in engine.actions],
            'lastDiscardAction': dict(engine.discard_actions[-1]) if engine.discard_actions else None,
            'announcement': self._announcement,
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
            # 局号对玩家 1-based（东1局…）；回执按同一口径校验。
            'round': self.round_index + 1,
            'mode': self.mode,
            'dealer': self.round_index % 4,
            'opening': self._round_opening,
            'matchFinished': self.match_finished,
            'roundResult': self._serializable_result(self.round_result) if self.round_result else None,
        }

    def broadcast_snapshot(self) -> None:
        for seat in range(self.capacity):
            state = self.seats[seat]
            if state is None or not state.controller.connected:
                continue
            self.conn.send_to_seat_nowait(seat, self.snapshot_for(seat))
