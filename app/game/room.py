"""房间会话 —— 座位管理 / 重进码 / 开局驱动 / AI 托管 / 事件广播

单个 RoomSession = 一个房间码对应的内存态：GameManager + 座位表 + 连接表。
对应开发计划 §4.3 的重连/断线流程（Phase 6 起 REST 接管生命周期）：
- REST join → join_or_rejoin 占座并签发重进码（写 room_seats / players 落库）
- WS 重连 → resume_by_code 按重进码恢复原座位（无重进码不占座）
- 真人占座用 RemotePlayer（挂起等待 WS 动作）；空座位用 AIPlayer 补位
- REST start → 所有已占（真人）座位 ready 后开局，独立 game_task 驱动整场
- 断线 → on_disconnect → RemotePlayer 立即 AI 托管；重连 → on_connect 归还控制权
- 落库钩子：storage 注入时，开局/每局结算/终局分别写 matches/round_results/rooms

WSEvents 是 GameManager 的 GameEvents 真实实现：表动作/分数/公告广播到房间。
"""

import asyncio
import os
import secrets
import time
from typing import Optional

from app.game.manager import GameManager, PLAYER_SEED
from app.game.player import AI_DELAYS, AIPlayer
from app.game.remote_player import RemotePlayer
from app.ws.manager import ConnectionManager


class RoomError(Exception):
    """房间层业务错误（错误码直接作为 rejoin_err / error 消息的 code 返回）。"""


# ─── 房间限时（Phase 8：创建起 60 分钟）────
# 非对局中超过限时 → 自动解散；对局中超过限时 → 等对局结束自动释放。
# 可用环境变量覆盖（秒）。
ROOM_LIFETIME = float(os.environ.get('ROOM_LIFETIME', str(60 * 60)))


def _make_rejoin_code() -> str:
    """8 位重进码：4+4 随机 hex，大写带连字符（如 'K7Q3-M9XP'）。"""
    return f'{secrets.token_hex(2).upper()}-{secrets.token_hex(2).upper()}'


class SeatState:
    """座位会话元数据：真人占座后的身份 / 重进码 / 控制器 / 准备态。"""

    __slots__ = ('seat', 'nickname', 'rejoin_code', 'player_id', 'controller', 'connected_at', 'ready')

    def __init__(self, seat: int, nickname: str, rejoin_code: str, controller: RemotePlayer,
                 player_id: Optional[str] = None):
        self.seat = seat
        self.nickname = nickname
        self.rejoin_code = rejoin_code
        self.player_id = player_id
        self.controller = controller
        self.connected_at: Optional[float] = None
        self.ready = False


class WSEvents:
    """GameEvents 真实广播实现：表动作/分数/公告 → 房间内所有连接。"""

    def __init__(self, room: 'RoomSession'):
        self.room = room
        self._id = 0

    def _next_id(self) -> int:
        self._id += 1
        return self._id

    def show_table_action(self, type_, actor_index, source_index, tile, meld_index) -> None:
        self.room.conn.broadcast({
            'kind': 'table_action',
            'event': {
                'id': self._next_id(),
                'type': type_,
                'actorIndex': actor_index,
                'sourceIndex': source_index,
                'tile': tile,
                'meldIndex': meld_index,
            },
        })

    def show_score_flow(self, deltas) -> None:
        self.room.conn.broadcast({'kind': 'score_flow', 'deltas': deltas})

    def announce(self, text, tone='gold', id=None) -> None:
        # id 来自 manager._announce 的 _id_counter：客户端按 id 去重，避免重复弹出
        self.room.conn.broadcast({'kind': 'announcement', 'text': text, 'tone': tone, 'id': id})

    def play_sound(self, name, volume=None) -> None:
        # 音效由客户端依据 table_action / score_flow 事件自行播放，服务端不推送
        pass

    async def play_sound_and_wait(self, name, volume=None) -> None:
        # 服务端无 UI 等待，直接返回
        pass

    def snapshot(self) -> None:
        # 状态变更后广播全量快照：per-seat 差异化（本人手牌可见，他座隐藏）
        self.room.broadcast_snapshot()

    def round_start(self, match_started, round_, dealer, honba, dice) -> None:
        # 每局开局广播：客户端据此播放开局序列（对局开始 + 骰子）
        self.room.conn.broadcast({
            'kind': 'round_start',
            'matchStarted': match_started,
            'round': round_,
            'dealer': dealer,
            'honba': honba,
            'dice': dice,
        })

    async def wait_for_opening(self) -> None:
        # 开局就绪屏障：等所有在线真人客户端发牌动画结束（opening_done）再开始首回合
        await self.room._wait_for_opening()


def build_snapshot(room: 'RoomSession', seat: int) -> dict:
    """构造全量 state_snapshot：对请求座位隐藏其他玩家手牌（防作弊）。"""
    mgr = room.manager
    players = []
    if mgr is not None:
        for p in mgr.players:
            data = p.model_dump(mode='json', by_alias=True)
            # 结算快照亮出全部手牌（局已结束，赢牌翻牌需要展示三家手牌）；
            # 进行中只对本人显示手牌，他人用 null 占位（防作弊）。
            reveal = mgr.phase == 'settled' and mgr.result is not None
            if p.seat != seat and not reveal:
                data['hand'] = [None] * len(data['hand'])
            players.append(data)
    return {
        'kind': 'state_snapshot',
        'roomId': room.room_id,
        'mode': room.mode,
        'phase': mgr.phase if mgr else room.status,
        'round': mgr.round if mgr else 1,
        'dealer': mgr.dealer if mgr else 0,
        'honba': mgr.honba if mgr else 0,
        'dice': mgr.dice if mgr else [1, 1],
        'wallCount': len(mgr.wall) if mgr else 0,
        'currentPlayer': mgr.current_player if mgr else -1,
        'players': players,
        'seat': seat,
        'result': mgr.result if mgr else None,
        'announcement': mgr.announcement if mgr else None,
        'matchFinished': bool(mgr.match_finished) if mgr else False,
        'lastDiscard': mgr.last_discard if mgr else None,
        'winPresentation': mgr.win_presentation if mgr else None,
        'winningPlayerIndex': mgr.winning_player_index if mgr else -1,
    }


class RoomSession:
    """单房间会话：座位 / 重进码 / 开局驱动 / 断线托管。

    capacity 决定座位数与玩家数（2/3/4 人桌）。开局由 REST start 显式触发
    （Phase 6），不再按真人到齐自动开局。
    """

    def __init__(self, room_id: str, mode: str = 'east', capacity: int = 4,
                 turn_timeout: float = 12.0, random=None, storage=None,
                 pace: Optional[dict] = None):
        self.room_id = room_id
        self.mode = mode
        # capacity = 真人座位上限（2/3/4）；麻将桌固定 4 人，空位由 AI 补足
        self.capacity = capacity
        self.player_count = 4
        self.turn_timeout = turn_timeout
        self._random = random
        self.storage = storage  # 可选 app.storage.db.Storage；为 None 时纯内存态（测试/单机）
        self.pace = pace  # 视觉节奏注入；None → GameManager 默认 0（测试/单机即用即答）
        self.status = 'lobby'  # lobby / playing / finished / error / closed
        self.seats: list[Optional[SeatState]] = [None] * self.player_count
        # 创建者座位（REST 创建房间后第一个 join 者）；创建者离房后转移给下一座位
        self.creator_seat: Optional[int] = None
        # 限时：创建起 60 分钟（deadline）。非对局中到期清扫回收；对局中到期由 _drive 在对局结束释放
        self.lifetime = ROOM_LIFETIME
        self.created_at: float = time.monotonic()
        self.deadline: float = self.created_at + ROOM_LIFETIME
        # 重进码握手限速：30s 窗口内最多 5 次（成功 resume 后清零，正常重连不受影响）
        self._rejoin_attempts: dict[str, list[float]] = {}
        self._rejoin_window = 30.0
        self._rejoin_limit = 5
        self.conn = ConnectionManager()
        self.manager: Optional[GameManager] = None
        self.game_task: Optional[asyncio.Task] = None
        self.match_id: Optional[str] = None  # 落库用；storage 为 None 时保持 None
        # 结算确认屏障：一局结算后等所有已连真人确认（客户端「继续」按钮）再推进下一局。
        # 兜底超时防止某客户端完全不响应导致整场卡死；正常流程客户端 10s 倒计时自动确认。
        self._continue_timeout = 20.0
        self._continue: Optional[dict] = None
        self._continue_event: Optional[asyncio.Event] = None
        # 开局就绪屏障：等所有在线真人客户端发牌动画结束（opening_done）再开始首回合。
        # 消除固定 openingDelay 在慢设备上的「服务端抢跑」；兜底超时防客户端不响应卡死。
        self._opening_timeout = 15.0
        self._opening: Optional[dict] = None
        self._opening_event: Optional[asyncio.Event] = None

    # ── 限时（60 分钟）─────────────────────────────────

    def is_past_deadline(self, now: Optional[float] = None) -> bool:
        """是否已超过 60 分钟限时。"""
        return (now if now is not None else time.monotonic()) >= self.deadline

    def is_expired(self, now: Optional[float] = None) -> bool:
        """房间是否可回收：对局中绝不回收（等对局结束自动释放）；
        超过限时（deadline）即回收，与是否有人在座/在线无关。"""
        if self.status == 'playing':
            return False
        return self.is_past_deadline(now)

    # ── 座位 / 重进码 ────────────────────────────────────

    def join_or_rejoin(self, nickname: str, rejoin_code: Optional[str] = None,
                       player_id: Optional[str] = None):
        """REST join：占第一个空座并签发重进码（is_rejoin=False）。失败抛 RoomError。

        真人占座受 capacity 上限约束（超出 → ROOM_FULL）；AI 座位不在此列。
        昵称查重：房间内已有同名玩家占座 → NICKNAME_TAKEN（重进码路径不受此限）。
        首个占座者为创建者（REST 创建房间后 creator 自己 join）。
        反赌博风控：player_id 命中黑名单 → BANNED（重进码路径在 resume_by_code 内查禁）。
        """
        if player_id and self.storage is not None and self.storage.is_banned('player', player_id):
            raise RoomError('BANNED')
        if rejoin_code:
            return self.resume_by_code(rejoin_code)
        if sum(1 for s in self.seats if s is not None) >= self.capacity:
            raise RoomError('ROOM_FULL')
        for state in self.seats:
            if state is not None and state.nickname == nickname:
                raise RoomError('NICKNAME_TAKEN')
        for seat, state in enumerate(self.seats):
            if state is None:
                # 断线/超时代打 AI 的思考速度在开局时由 _controllers 统一注入（_ai_delays）
                controller = RemotePlayer(seat, self.conn, timeout=self.turn_timeout)
                state = SeatState(seat, nickname, _make_rejoin_code(), controller,
                                  player_id=player_id)
                self.seats[seat] = state
                if self.creator_seat is None:
                    self.creator_seat = seat
                self._persist_seat(seat)
                return seat, False, state
        raise RoomError('ROOM_FULL')

    def resume_by_code(self, rejoin_code: str):
        """WS 重连：按重进码定位原座位。原会话仍在线 → ALREADY_CONNECTED。"""
        for seat, state in enumerate(self.seats):
            if state is not None and state.rejoin_code == rejoin_code:
                if self.storage is not None and state.player_id \
                        and self.storage.is_banned('player', state.player_id):
                    raise RoomError('BANNED')
                if state.controller.connected:
                    # 顶号尝试：原会话仍在线，拒绝（防双连接争抢同一座位）
                    raise RoomError('ALREADY_CONNECTED')
                return seat, state
        raise RoomError('INVALID_REJOIN_CODE')

    # ── 重进码握手限速 ───────────────────────────────────

    def check_rejoin_rate(self, rejoin_code: str) -> bool:
        """重进码握手限速：30s 窗口内最多 5 次。

        成功 resume 后由 reset_rejoin_rate 清零 → 正常断线重连的客户端不会因
        指数退避下的多次重试被锁；失败尝试（错误码 / 顶号）持续累积以触发限速。
        """
        now = time.monotonic()
        window = [t for t in self._rejoin_attempts.get(rejoin_code, [])
                  if now - t < self._rejoin_window]
        if len(window) >= self._rejoin_limit:
            self._rejoin_attempts[rejoin_code] = window
            return False
        window.append(now)
        self._rejoin_attempts[rejoin_code] = window
        return True

    def reset_rejoin_rate(self, rejoin_code: str) -> None:
        """成功恢复座位后清零该码的失败计数（正常重连不被限速）。"""
        self._rejoin_attempts.pop(rejoin_code, None)

    def release_seat(self, seat: int, rejoin_code: Optional[str] = None) -> None:
        """REST leave：释放座位。带 rejoin_code 时校验身份，防止误释放他人座位。"""
        state = self.seats[seat]
        if state is None:
            raise RoomError('SEAT_EMPTY')
        if rejoin_code is not None and rejoin_code != state.rejoin_code:
            raise RoomError('INVALID_REJOIN_CODE')
        state.controller.set_connected(False)  # 断开 pending，转 AI 代打
        self.seats[seat] = None
        self.conn.unregister(seat)
        if self.creator_seat == seat:
            self._transfer_creator()
        if self.storage is not None:
            self.storage.remove_room_seat(self.room_id, seat)

    def _transfer_creator(self) -> None:
        """创建者离房 → 房主转移给剩余座位中编号最小者；无人在座则置空。"""
        self.creator_seat = next(
            (s.seat for s in self.seats if s is not None), None)

    def ready_seat(self, seat: int, ready: Optional[bool] = None) -> bool:
        """REST ready：设置座位准备态（缺省 toggle）。返回新状态。"""
        state = self.seats[seat]
        if state is None:
            raise RoomError('SEAT_EMPTY')
        state.ready = not state.ready if ready is None else ready
        return state.ready

    # ── 连接生命周期 ─────────────────────────────────────

    def on_connect(self, seat: int) -> None:
        state = self.seats[seat]
        if state is not None:
            state.controller.set_connected(True)
            state.connected_at = time.time()

    def on_disconnect(self, seat: int) -> None:
        state = self.seats[seat]
        if state is not None:
            state.controller.set_connected(False)

    def broadcast_snapshot(self) -> None:
        """向所有在位连接广播 per-seat 快照（本人手牌可见，他座隐藏）。"""
        for seat in self.conn.connected_seats:
            self.conn.send_to_seat_nowait(seat, build_snapshot(self, seat))

    def handle_client_message(self, seat: int, message: dict) -> tuple[bool, str]:
        """客户端动作 → 投递给该座位控制器。返回 (是否受理, 错误码)。"""
        if message.get('type') == 'ping':
            # 回应 pong：客户端据此测 RTT → 信号质量显示（signal-N）
            self.conn.send_to_seat_nowait(seat, {'kind': 'pong'})
            return True, ''
        if message.get('type') == 'continue':
            return self._confirm_continue(seat)
        if message.get('type') == 'opening_done':
            return self._confirm_opening(seat)
        state = self.seats[seat]
        if state is None or not isinstance(state.controller, RemotePlayer):
            return False, 'NOT_HUMAN_SEAT'
        return state.controller.handle_action(message)

    # ── 结算确认屏障 ─────────────────────────────────────

    def _human_connected_seats(self) -> list[int]:
        """当前在线（WS 已连）的真人座位列表。断线座位由 AI 托管，不参与确认。"""
        return [s.seat for s in self.seats if s is not None and s.controller.connected]

    def _confirm_continue(self, seat: int) -> tuple[bool, str]:
        """客户端「继续」：把座位标记为已确认（仅在确认屏障激活时生效）。"""
        if self._continue is None:
            return True, ''   # 非结算期间：幂等忽略（结算窗早于到达的 continue 不算错）
        confirmed = self._continue['confirmed']
        if seat not in confirmed:
            confirmed.add(seat)
        if self._continue_event is not None:
            self._continue_event.set()
        return True, ''

    def _confirm_opening(self, seat: int) -> tuple[bool, str]:
        """客户端「opening_done」：标记该座位开局动画已完成（仅在开局就绪屏障激活时生效）。"""
        if self._opening is None:
            return True, ''   # 非开局等待期间：幂等忽略
        confirmed = self._opening['confirmed']
        if seat not in confirmed:
            confirmed.add(seat)
        if self._opening_event is not None:
            self._opening_event.set()
        return True, ''

    async def _wait_for_opening(self) -> None:
        """开局就绪屏障：等所有在线真人客户端发牌动画结束（opening_done）再开始首回合。

        测试路径（pace 为空）不等待：客户端不做开局动画，直接即用即答。
        兜底超时（_opening_timeout）防客户端完全不响应导致整场卡死。
        """
        if not self.pace:
            return
        seats = self._human_connected_seats()
        if not seats:
            return   # 全 AI / 全员断线 → 无需等待
        self._opening = {'deadline': time.monotonic() + self._opening_timeout, 'confirmed': set()}
        self._opening_event = asyncio.Event()
        try:
            while True:
                current = self._human_connected_seats()
                confirmed = self._opening['confirmed']
                # 无在线真人（全员断线）或所有在线真人都已就绪 → 开始首回合
                if not current or all(s in confirmed for s in current):
                    break
                remaining = self._opening['deadline'] - time.monotonic()
                if remaining <= 0:
                    break
                try:
                    await asyncio.wait_for(self._opening_event.wait(), timeout=remaining)
                    self._opening_event.clear()
                except asyncio.TimeoutError:
                    break
        finally:
            self._opening = None
            self._opening_event = None

    async def _wait_for_continue(self) -> None:
        """结算后等所有已连真人确认再推进。全部断线 / 无真人 → 直接通过。

        客户端在「继续」按钮显示后自动倒计时 10s 并发送 continue；此处的兜底
        超时（_continue_timeout）只防客户端完全不响应导致整场卡死。
        """
        seats = self._human_connected_seats()
        if not seats:
            return
        self._continue = {'deadline': time.monotonic() + self._continue_timeout, 'confirmed': set()}
        self._continue_event = asyncio.Event()
        self.conn.broadcast({'kind': 'continue_prompt', 'total': len(seats)})
        try:
            while True:
                current = self._human_connected_seats()
                confirmed = self._continue['confirmed']
                # 无在线真人（全员断线）或所有在线真人都已确认 → 推进
                if not current or all(s in confirmed for s in current):
                    break
                remaining = self._continue['deadline'] - time.monotonic()
                if remaining <= 0:
                    break
                try:
                    await asyncio.wait_for(self._continue_event.wait(), timeout=remaining)
                    self._continue_event.clear()
                except asyncio.TimeoutError:
                    break
        finally:
            self._continue = None
            self._continue_event = None

    # ── 开局驱动 ─────────────────────────────────────────

    async def start(self) -> None:
        """REST start：所有已占（真人）座位 ready 后开局，独立 game_task 驱动整场。

        必须 await（async）以便 game_task 创建在当前事件循环 —— 即 uvicorn 的
        事件循环，与 WS 处理器一致。否则跨循环入队/唤醒会死锁。
        对局已结束（game_task 完成、status=finished）的房间允许再开一局：旧
        manager / match 被新一场替换（旧 match 已落库为 finished 历史）。
        """
        if self.game_task is not None and not self.game_task.done():
            raise RoomError('ALREADY_STARTED')
        if self.status not in ('lobby', 'finished'):
            raise RoomError('ROOM_CLOSED')
        for seat, state in enumerate(self.seats):
            if state is not None and not state.ready:
                raise RoomError('NOT_ALL_READY')
        if not any(s is not None for s in self.seats):
            raise RoomError('ROOM_EMPTY')
        self.manager = GameManager(
            mode=self.mode,
            controllers=self._controllers(),
            player_seeds=self._seeds(),
            random=self._random,
            events=WSEvents(self),
            pace=self.pace,
        )
        self.status = 'playing'
        self.game_task = asyncio.create_task(self._drive())

    def _ai_delays(self) -> Optional[dict]:
        """真人联机房间（注入 PLAY_PACE）的 AI 用人类思考速度；测试路径保持即用即答。"""
        return AI_DELAYS if self.pace else None

    def _controllers(self) -> list:
        """装配控制器：空座位 AIPlayer；真人座位 RemotePlayer。

        AI 思考速度按「开局瞬间」的房间节奏注入（_ai_delays）：真人房间用
        AI_DELAYS 人类节奏，测试路径（pace 为空）保持即用即答。
        """
        ai_delays = self._ai_delays()
        controllers = []
        for seat in self.seats:
            if seat is not None:
                seat.controller.set_ai_delays(ai_delays)
                controllers.append(seat.controller)
            else:
                controllers.append(AIPlayer(delays=ai_delays))
        return controllers

    def _seeds(self) -> list:
        seeds = []
        for seat, state in enumerate(self.seats):
            if state is not None:
                seeds.append({'name': state.nickname, 'avatar': '', 'score': 1000})
            else:
                seeds.append(PLAYER_SEED[seat])
        return seeds

    # ── 落库（storage 注入时生效；纯内存态为空操作）─────────

    def _persist_seat(self, seat: int) -> None:
        """join 后写 room_seats / players 表（同步 sqlite 小操作，调用方在请求线程）。"""
        if self.storage is None:
            return
        state = self.seats[seat]
        self.storage.create_player(state.nickname)
        self.storage.upsert_room_seat(self.room_id, seat, state.nickname, state.rejoin_code,
                                      state.player_id)

    async def _persist_match_start(self) -> None:
        if self.storage is None:
            return
        self.match_id = await asyncio.to_thread(
            self.storage.create_match, self.room_id, self.mode)
        await asyncio.to_thread(self.storage.update_room_status, self.room_id, 'playing')
        # 记录参赛者身份（战绩真源；room_seats 离房即删，不能作为战绩依据）
        players = [
            {'seat': seat, 'player_id': state.player_id, 'nickname': state.nickname}
            if (state := self.seats[seat]) is not None else
            {'seat': seat, 'player_id': None, 'nickname': PLAYER_SEED[seat]['name']}
            for seat in range(len(self.seats))
        ]
        await asyncio.to_thread(self.storage.upsert_match_players, self.match_id, players)

    async def _persist_round(self, result: dict) -> None:
        if self.storage is None or self.match_id is None:
            return
        round_data = self._map_round_result(result)
        await asyncio.to_thread(self.storage.insert_round_result, self.match_id, round_data)

    async def _persist_match_end(self, final_scores: list) -> None:
        if self.storage is None:
            return
        if self.match_id is not None:
            await asyncio.to_thread(self.storage.finish_match, self.match_id, final_scores)
        await asyncio.to_thread(self.storage.update_room_status, self.room_id,
                                'finished' if self.status == 'finished' else self.status)

    @staticmethod
    def _map_round_result(result: dict) -> dict:
        """把 manager.make_round_result 的 result 映射为 round_results 表所需的单行 dict。"""
        changes = result.get('scoreChanges', [])
        return {
            'round': result.get('roundLabel', ''),
            'dealer': result.get('dealer', 0),
            'honba': result.get('honba', 0),
            'winner_index': result.get('winnerIndex'),
            'points': result.get('points'),
            'multiplier': result.get('multiplier'),
            'horse_hits': result.get('hits', 0),
            'horses': result.get('horses', []),
            'deltas': [{'playerIndex': c['playerIndex'], 'amount': c['delta']}
                       for c in changes],
            'scores_after': [{'playerIndex': c['playerIndex'], 'score': c['score']}
                             for c in changes],
            'opts': {k: result.get(k) for k in ('fourRed', 'kongBloom', 'robbedKong')},
            'draw': bool(result.get('draw')),
            'winner': result.get('winner'),
        }

    async def _drive(self) -> None:
        """整场对局驱动循环：开局 → 每局结算广播 → 推进 → 终局。"""
        try:
            await self._persist_match_start()
            await self.manager.start_game(self.mode)
            while not self.manager.match_finished:
                if self.manager.phase == 'settled':
                    self.conn.broadcast({'kind': 'hand_result', 'result': self.manager.result})
                    await self._persist_round(self.manager.result)
                    # 确认屏障：等所有在线真人点「继续」（10s 倒计时 / 兜底超时）再进下一局
                    await self._wait_for_continue()
                    await self.manager.next_round()
                elif self.manager.phase == 'lobby':
                    break
                else:
                    await asyncio.sleep(0)
            if self.manager.phase == 'finished':
                self.status = 'finished'
            final_scores = [
                {'seat': p.seat, 'name': p.name, 'score': p.score}
                for p in self.manager.players
            ]
            self.conn.broadcast({
                'kind': 'match_finished',
                'roomId': self.room_id,
                'mode': self.mode,
                'finalScores': final_scores,
            })
            await self._persist_match_end(final_scores)
            # 对局结束：解除各座位准备态（房间保留，房主可再开一局）。
            # 不回 lobby 状态，保留 finished 供记录/重连快照展示。
            for state in self.seats:
                if state is not None:
                    state.ready = False
            # 对局结束时已超过 60 分钟限时 → 自动释放房间（close 内会广播 room_closed）
            if self.is_past_deadline():
                room_registry.remove(self.room_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            self.status = 'error'
            self.conn.broadcast({'kind': 'error', 'code': 'INTERNAL_ERROR'})
            raise

    def close(self) -> None:
        """关闭房间：通知在位客户端后取消游戏任务，落库 closed 状态。

        _drive 在对局结束超时释放房间时会回调（当前任务即 game_task），
        此时跳过自我取消，避免对已近完成的整场驱动注入 CancelledError。
        无事件循环线程（REST 同步路由线程池 / 测试清理）里 current_task() 抛错，
        此时一律正常取消游戏任务。
        """
        self.status = 'closed'
        self.conn.broadcast({'kind': 'room_closed'})
        try:
            current = asyncio.current_task()
        except RuntimeError:
            current = None
        if self.game_task is not None and not self.game_task.done() \
                and current is not self.game_task:
            self.game_task.cancel()
        if self.storage is not None:
            self.storage.update_room_status(self.room_id, 'closed')


class RoomRegistry:
    """内存房间注册表（Phase 6 由 REST 层接管创建；当前供 WS 端点与测试使用）。

    限时清扫：get/create 时惰性清扫超过 60 分钟限时的房间（带节流）；
    对局中的房间不回收，由 _drive 在对局结束超时时释放。
    """

    def __init__(self) -> None:
        self._rooms: dict[str, RoomSession] = {}
        self._last_sweep = 0.0
        self._sweep_interval = 60.0   # 惰性清扫节流：至少间隔 60s 才真正遍历

    def create(self, room_id: str, **kwargs) -> RoomSession:
        self._maybe_sweep()
        if room_id in self._rooms:
            raise RoomError('ROOM_EXISTS')
        room = RoomSession(room_id, **kwargs)
        self._rooms[room_id] = room
        return room

    def get(self, room_id: str) -> Optional[RoomSession]:
        self._maybe_sweep()
        return self._rooms.get(room_id)

    def count(self) -> int:
        """当前在册房间数（房间数上限检查用）。"""
        return len(self._rooms)

    def _maybe_sweep(self) -> None:
        now = time.monotonic()
        if now - self._last_sweep < self._sweep_interval:
            return
        self._last_sweep = now
        self.sweep_expired(now)

    def sweep_expired(self, now: Optional[float] = None) -> list[str]:
        """回收所有过期的空房间，返回被回收的房间码列表（供测试直接触发）。"""
        now = now if now is not None else time.monotonic()
        expired = [rid for rid, room in list(self._rooms.items()) if room.is_expired(now)]
        for rid in expired:
            self.remove(rid)
        return expired

    def remove(self, room_id: str) -> None:
        room = self._rooms.pop(room_id, None)
        if room is not None:
            room.close()

    def clear(self) -> None:
        for room_id in list(self._rooms):
            self.remove(room_id)


# 共享房间注册表（Phase 6 起由 REST 层创建，WS 层读取；测试通过它注入/清理房间）
room_registry = RoomRegistry()
