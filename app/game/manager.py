"""游戏状态机 —— 从 src/game/useGame.ts 翻译

GameManager 是房间内的权威状态机，房间内异步串行执行（asyncio 驱动）。
与 useGame 的差异：
- ref()/reactive()/computed() → 普通 Python 类属性
- later()/setTimeout → 可配置 PACE 延迟（默认 0 加速模拟），asyncio.sleep
- playSound/showTableAction/showScoreFlow/announce → GameEvents 广播（默认空实现，
  Phase 5 由 WebSocket 层注入）
- HumanController → RemotePlayer（Phase 5）
- 开局/和牌动画阶段（dealing/win-effect/revealing）保留为内部流转，
  由 begin_turn/end_game 同步推进到 settled，无需 UI 定时器

对外关键接口：
- start_game(mode) / next_round() / return_to_lobby()
- 状态：phase / players / wall / current_player / round / dealer / honba / result / match_finished

注意：TS 端用定时器调度回合（调用栈恒定浅），本实现用 async 递归串联整局
（begin_turn → discard_tile → begin_turn），深度 O(每局动作数) 约几千层，
单场对局结束即释放，因此提高递归限制以容纳整场对局的调用链。
"""

import asyncio
import sys
from types import SimpleNamespace
from typing import Optional, Protocol

# 容纳整场对局（东风 4 局 ≈ 数千次动作）的 async 递归调用链
sys.setrecursionlimit(10000)

from app.models.game import GamePlayer, Meld, TileType
from app.core.tiles import create_wall, shuffle, sort_tiles
from app.core.rules import (
    apply_kong_score,
    apply_win_score,
    can_rob_kong,
    draw_horses,
    matching_count,
    score_hand,
)
from app.core.actions import perform_discard_gang, perform_peng, remove_matches
from app.game.player import AIPlayer, ClaimContext, RobKongContext, TurnContext

# ─── 场次常量（对应 useGame.ts MATCH_HANDS / MATCH_NAMES）────────

MATCH_HANDS = {'east': 4, 'hanchan': 8}
MATCH_NAMES = {'east': '东风场', 'hanchan': '半庄场'}

# 默认玩家种子（对应 PLAYER_SEED）
PLAYER_SEED = [
    {'name': '北冥重生', 'avatar': 'avatars/lotus.svg', 'score': 1000},
    {'name': '南粤阿乐', 'avatar': 'avatars/ah-lok.svg', 'score': 1000},
    {'name': '西关十三姨', 'avatar': 'avatars/shisan.svg', 'score': 1000},
    {'name': '东山少爷', 'avatar': 'avatars/young-master.svg', 'score': 1000},
]

# 视觉节奏延迟（动画展示用；后端默认 0 以加速模拟，可注入覆盖）
DEFAULT_PACE = {
    'afterDiscardToNextTurn': 0,
    'afterClaimGang': 0,
    'afterClaimPeng': 0,
    'afterKongSettle': 0,
    'beforeRobKong': 0,
    'betweenRobKongs': 0,
    'skipDrawPengDelay': 0,
}

# 真人联机房间的视觉节奏（REST create 注入，对齐前端 PACE_MS：动画可读、AI 不瞬移）
PLAY_PACE = {
    'afterDiscardToNextTurn': 450,
    'afterClaimGang': 550,
    'afterClaimPeng': 650,
    'afterKongSettle': 600,
    'beforeRobKong': 650,
    'betweenRobKongs': 450,
    'skipDrawPengDelay': 350,
}


# ─── 表现副作用接口（Phase 5 由 WebSocket 层实现广播）────────

class GameEvents(Protocol):
    def show_table_action(self, type_: str, actor_index: int, source_index: Optional[int],
                          tile: TileType, meld_index: int) -> None: ...
    def show_score_flow(self, deltas: list[dict]) -> None: ...
    def announce(self, text: str, tone: str = 'gold', id: Optional[int] = None) -> None: ...
    def play_sound(self, name: str, volume: Optional[float] = None) -> None: ...
    async def play_sound_and_wait(self, name: str, volume: Optional[float] = None) -> None: ...
    def snapshot(self) -> None: ...


class NullEvents:
    """默认空实现：测试 / 无 WebSocket 时使用"""

    def show_table_action(self, *a, **k) -> None:
        pass

    def show_score_flow(self, *a, **k) -> None:
        pass

    def announce(self, *a, **k) -> None:
        pass

    def play_sound(self, *a, **k) -> None:
        pass

    async def play_sound_and_wait(self, *a, **k) -> None:
        pass

    def snapshot(self) -> None:
        pass


# ─── 场次推进（对应 useGame.ts advanceMatchState，纯函数）──────

def advance_match_state(*, round_, dealer, honba, match_type, result, scores=None, player_count=4) -> dict:
    """庄家连庄 + 本场累加 / 轮庄推进 → 终局判断。"""
    dealer_keeps_seat = not result.get('draw') and result.get('winnerIndex') == dealer
    if dealer_keeps_seat:
        next_state = {'round': round_, 'dealer': dealer, 'honba': honba + 1}
    else:
        next_state = {'round': round_ + 1, 'dealer': (dealer + 1) % player_count, 'honba': 0}
    return {
        **next_state,
        'finished': next_state['round'] > MATCH_HANDS[match_type],
    }


# ─── 和牌关键牌解析（对应 useGame.ts resolveWinTile）────────

def resolve_win_tile(winner: GamePlayer, options: Optional[dict] = None) -> TileType:
    """四红中 → red；否则 options.winTile → 刚摸到的牌 → 手牌末张。"""
    options = options or {}
    if options.get('fourRed'):
        return 'red'
    if options.get('winTile'):
        return options['winTile']
    if winner.drawnTileIndex >= 0:
        return winner.hand[winner.drawnTileIndex]
    return winner.hand[-1]


def structural_meld_count(player: GamePlayer) -> int:
    """公开副露数（结构性，不含花杠），用于胡牌判断。"""
    return sum(1 for meld in player.melds if meld.type != 'flower')


# ─── GameManager ───────────────────────────────────────────

class GameManager:
    """单房间游戏状态机。房间内所有流程串行 await 执行，无并发竞态。"""

    def __init__(self, mode: str = 'east', controllers: Optional[list] = None,
                 player_count: int = 4, random=None, events: Optional[GameEvents] = None,
                 pace: Optional[dict] = None, player_seeds: Optional[list] = None):
        self.match_type = mode
        self.player_count = player_count
        self.controllers = controllers or [AIPlayer() for _ in range(player_count)]
        # 玩家种子：联网房间用「座位昵称」覆盖默认种子（AI 座位保留 PLAYER_SEED）
        self.seeds = player_seeds or PLAYER_SEED
        self._random = random
        self.events: GameEvents = events or NullEvents()
        self.pace = {**DEFAULT_PACE, **(pace or {})}

        # 当前玩家可变引用 box：与 actions.py 的 ActionContext.current_player 共享
        self.phase = 'lobby'
        self.players: list[GamePlayer] = []
        self._cp_box = SimpleNamespace(value=-1)
        self._table_context = SimpleNamespace(
            players=self.players,
            current_player=self._cp_box,
            show_table_action=self.events.show_table_action,
            show_score_flow=self.events.show_score_flow,
            play_sound=self.events.play_sound,
        )
        self._id_counter = 0

        # 状态
        self.wall: list[TileType] = []
        self.kong_draw_player_index = -1
        self.selected_index = -1
        self.last_discard: Optional[dict] = None
        self.action_prompt: Optional[dict] = None
        self.pending_kong: Optional[dict] = None
        self.announcement: Optional[dict] = None
        self.result: Optional[dict] = None
        self.win_presentation: Optional[dict] = None
        self.winning_player_index = -1
        self.round = 1
        self.dealer = 0
        self.honba = 0
        self.match_finished = False
        self.user_drew_this_turn = False

    # ── current_player：property 读写共享 box，与 actions.py 保持一致 ──

    @property
    def current_player(self) -> int:
        return self._cp_box.value

    @current_player.setter
    def current_player(self, value: int) -> None:
        self._cp_box.value = value

    # ── 座位工具 ──

    def seat_distance(self, from_: int, to: int) -> int:
        return (to - from_ + len(self.players)) % len(self.players)

    # ── 表现副作用转发 ──

    def _show_table_action(self, type_, actor_index, source_index, tile, meld_index) -> None:
        self.events.show_table_action(type_, actor_index, source_index, tile, meld_index)

    def _show_score_flow(self, deltas) -> None:
        self.events.show_score_flow(deltas)

    def _announce(self, text, tone='gold') -> None:
        self._id_counter += 1
        self.announcement = {'text': text, 'tone': tone, 'id': self._id_counter}
        # id 随广播下发：客户端按 id 去重（公告随快照重复携带，不能重复弹出）
        self.events.announce(text, tone, id=self._id_counter)

    def _play_sound(self, name, volume=None) -> None:
        self.events.play_sound(name, volume)

    async def _play_sound_and_wait(self, name, volume=None) -> None:
        await self.events.play_sound_and_wait(name, volume)

    def _broadcast_snapshot(self) -> None:
        """每次状态变更后广播全量快照（客户端以快照为唯一真源）。"""
        self.events.snapshot()

    async def _sleep(self, ms: int) -> None:
        if ms and ms > 0:
            await asyncio.sleep(ms / 1000)

    # ── 发牌与牌墙 ──

    def _reset_players(self) -> None:
        previous = [p.score for p in self.players]
        self.players = []
        for index, seed in enumerate(self.seeds):
            score = previous[index] if index < len(previous) else seed['score']
            self.players.append(GamePlayer(
                name=seed['name'], avatar=seed['avatar'], score=score, seat=index,
                hand=[], discards=[], melds=[], redCount=0, drawnTileIndex=-1,
            ))
        self._table_context.players = self.players

    def _take_tile(self, from_tail: bool = False) -> Optional[TileType]:
        if not self.wall:
            return None
        return self.wall.pop() if from_tail else self.wall.pop(0)

    def _receive_dealt_tile(self, player: GamePlayer, tile: TileType) -> None:
        """发牌收牌：红中 → 花杠 + 牌墙尾补摸（递归）。"""
        if tile == 'red':
            player.redCount += 1
            player.melds.append(Meld(type='flower', tile='red', tiles=['red']))
            replacement = self._take_tile(True)
            if replacement:
                self._receive_dealt_tile(player, replacement)
        else:
            player.hand.append(tile)

    def _deal(self, player_index: int, count: int) -> None:
        for _ in range(count):
            tile = self._take_tile(False)
            if tile:
                self._receive_dealt_tile(self.players[player_index], tile)

    # ── 开局 ──

    async def start_game(self, mode: Optional[str] = None) -> None:
        """洗牌 → 发牌（红中花牌补摸）→ 四红中判定 → begin_turn。"""
        if mode and mode in MATCH_HANDS:
            self.match_type = mode
            self.round = 1
            self.dealer = 0
            self.honba = 0
            self.match_finished = False
            self.players = []
        self._reset_players()
        self.wall = shuffle(create_wall(), self._random)
        self.result = None
        self.win_presentation = None
        self.action_prompt = None
        self.pending_kong = None
        self.user_drew_this_turn = False
        self.selected_index = -1
        self.last_discard = None
        self.phase = 'dealing'

        seat_order = [(self.dealer + offset) % len(self.players) for offset in range(len(self.players))]
        for _ in range(3):
            for player_index in seat_order:
                self._deal(player_index, 4)
        for player_index in seat_order:
            self._deal(player_index, 1)

        self.phase = 'opening'
        for player in self.players:
            player.hand = sort_tiles(player.hand)
        self._broadcast_snapshot()

        four_red_winner = next((i for i, p in enumerate(self.players) if p.redCount >= 4), -1)
        if four_red_winner >= 0:
            self.end_game(four_red_winner, {'fourRed': True})
            return

        self._announce(f'{self.round_label()} · 开牌')
        await self.begin_turn(self.dealer)

    def round_label(self) -> str:
        wind = '南' if self.round > 4 else '东'
        hand_number = ((self.round - 1) % 4) + 1
        return f'{wind}{hand_number}局'

    # ── 摸牌 ──

    async def draw_for(self, player_index: int, from_tail: bool = False) -> bool:
        """摸牌：红中 → 亮花杠 → 牌墙尾补摸（递归 draw_for）。"""
        player = self.players[player_index]
        self.kong_draw_player_index = player_index if from_tail else -1
        tile = self._take_tile(from_tail)
        if not tile:
            self.end_draw()
            return False
        if tile == 'red':
            player.redCount += 1
            player.melds.append(Meld(type='flower', tile='red', tiles=['red']))
            self._show_table_action('flower-gang', player_index, None, tile, len(player.melds) - 1)
            if player.redCount >= 4:
                self.end_game(player_index, {'fourRed': True})
                return False
            await self._play_sound_and_wait('gang.mp3')
            if self.phase == 'settled':
                return False
            return await self.draw_for(player_index, True)
        # 保留刚摸到的牌在最右端，出牌前不要混入已整理的手牌
        player.hand = [*player.hand, tile]
        player.drawnTileIndex = len(player.hand) - 1
        self._play_sound('give.mp3', 0.7)
        return True

    # ── 回合流转 ──

    async def begin_turn(self, player_index: int, skip_draw: bool = False, from_tail: bool = False) -> None:
        """摸牌（draw_for）→ request_turn → 执行动作（胡/补杠/暗杠/弃牌）。"""
        if self.phase == 'settled':
            return
        if not self.wall:
            self.end_draw()
            return
        self.current_player = player_index
        self.user_drew_this_turn = False
        self.phase = 'drawing'
        self.selected_index = -1
        self.action_prompt = None
        if skip_draw:
            self.kong_draw_player_index = -1
        drawn = True if skip_draw else await self.draw_for(player_index, from_tail)
        if not drawn or self.phase == 'settled':
            return
        # 摸牌后立即广播快照：客户端看到刚摸到的牌，再等待该玩家的回合请求
        self._broadcast_snapshot()

        self.phase = 'thinking'
        player = self.players[player_index]
        ctx = TurnContext(
            hand=player.hand,
            melds=player.melds,
            exposedMelds=structural_meld_count(player),
            kongBloom=self.kong_draw_player_index == player_index,
            skipDraw=skip_draw,
            afterKong=from_tail,
        )
        action = await self.controllers[player_index].request_turn(ctx)
        # 守卫：游戏可能已在 await 期间结束或轮次已转移
        if self.phase == 'settled' or self.current_player != player_index:
            return

        kind = action['kind']
        if kind == 'win':
            self.end_game(player_index, {'kongBloom': self.kong_draw_player_index == player_index})
            return
        if kind == 'added-kong':
            meld_index = action['meldIndex']
            return await self.request_added_kong(player_index, meld_index, player.melds[meld_index].tile)
        if kind == 'concealed-kong':
            await self.perform_concealed_kong(player_index, action['tile'], no_continue=True)
            if self.phase == 'settled':
                return
            return await self.begin_turn(player_index, from_tail=True)
        if kind == 'discard':
            return await self.discard_tile(player_index, action['handIndex'])

    async def discard_tile(self, player_index: int, hand_index: int) -> None:
        """弃牌 → 找碰/杠候选 → offer_next_claim / 下一家回合。"""
        player = self.players[player_index]
        if not player.hand:
            return
        hand_index = min(hand_index, len(player.hand) - 1)
        tile = player.hand.pop(hand_index)
        player.hand = sort_tiles(player.hand)
        player.drawnTileIndex = -1
        self.kong_draw_player_index = -1
        player.discards.append(tile)
        controller = self.controllers[player_index]
        if hasattr(controller, 'on_discarded'):
            controller.on_discarded()
        self._id_counter += 1
        self.last_discard = {'tile': tile, 'from': player_index, 'id': self._id_counter}
        self._play_sound('dapai.mp3', 0.8)
        self.phase = 'checking'
        self._broadcast_snapshot()

        claimants = self.find_claims(player_index, tile)
        if claimants:
            return await self.offer_next_claim(claimants, tile, player_index)
        await self._sleep(self.pace['afterDiscardToNextTurn'])
        return await self.begin_turn((player_index + 1) % len(self.players))

    # ── 碰/杠候选 ──

    def find_claims(self, from_: int, tile: TileType) -> list[dict]:
        """找可以碰/杠该弃牌的玩家（按距离排序；白板/红中不可碰杠）。"""
        if tile in ('white', 'red'):
            return []
        claimants = []
        for player_index, player in enumerate(self.players):
            if player_index == from_:
                continue
            count = matching_count(player.hand, tile)
            if count >= 2:
                claimants.append({
                    'playerIndex': player_index,
                    'count': count,
                    'distance': self.seat_distance(from_, player_index),
                })
        claimants.sort(key=lambda item: item['distance'])
        return [{'playerIndex': c['playerIndex'], 'canGang': c['count'] >= 3} for c in claimants]

    async def offer_next_claim(self, claimants: list[dict], tile: TileType, from_: int) -> None:
        """按座位顺序询问碰/杠。AI 单次碰+出牌闭环由 ClaimAction.discardIndex 完成。"""
        if not claimants:
            await self._sleep(self.pace['afterDiscardToNextTurn'])
            return await self.begin_turn((from_ + 1) % len(self.players))

        claimant = claimants[0]
        remaining = claimants[1:]
        player = self.players[claimant['playerIndex']]
        ctx = ClaimContext(
            hand=player.hand,
            canGang=claimant['canGang'],
            tile=tile,
            from_=from_,
        )
        action = await self.controllers[claimant['playerIndex']].request_claim(ctx)
        if self.phase == 'settled':
            return

        kind = action['kind']
        if kind == 'pass':
            return await self.offer_next_claim(remaining, tile, from_)
        if kind == 'gang':
            perform_discard_gang(self._table_context, claimant['playerIndex'], tile, from_)
            self._broadcast_snapshot()
            if await self.draw_for(claimant['playerIndex'], True):
                await self._sleep(self.pace['afterClaimGang'])
                return await self.begin_turn(claimant['playerIndex'], from_tail=True)
            return
        # peng
        perform_peng(self._table_context, claimant['playerIndex'], tile, from_)
        self._broadcast_snapshot()
        if action.get('discardIndex') is not None:
            await self._sleep(self.pace['afterClaimPeng'])
            return await self.discard_tile(claimant['playerIndex'], action['discardIndex'])
        # 人类：碰后需要互动选弃牌 → 跳过摸牌直接出牌
        await self._sleep(self.pace['skipDrawPengDelay'])
        return await self.begin_turn(claimant['playerIndex'], skip_draw=True)

    # ── 杠 ──

    async def perform_concealed_kong(self, player_index: int, tile: TileType, no_continue: bool = False) -> None:
        """暗杠：移除 4 张手牌 → 杠副露 → 结算（其余三家各付底分两倍）。"""
        player = self.players[player_index]
        player.hand = remove_matches(player.hand, tile, 4)
        player.drawnTileIndex = -1
        player.melds.append(Meld(type='angang', tile=tile, tiles=[tile, tile, tile, tile]))
        score_deltas = apply_kong_score(self.players, player_index, 'concealed')
        self._show_table_action('concealed-gang', player_index, None, tile, len(player.melds) - 1)
        self._show_score_flow(score_deltas)
        self._play_sound('gang.mp3')
        self._broadcast_snapshot()
        if no_continue:
            return
        await self._sleep(self.pace['afterKongSettle'])
        return await self.begin_turn(player_index, from_tail=True)

    def declare_added_kong(self, player_index: int, meld_index: int, tile: TileType) -> None:
        """声明补杠：碰副露 → 杠副露（pending），等待抢杠询问。"""
        player = self.players[player_index]
        player.hand = remove_matches(player.hand, tile, 1)
        player.drawnTileIndex = -1
        meld = player.melds[meld_index]
        meld.type = 'gang'
        meld.added = True
        meld.pending = True
        meld.tile = tile
        meld.tiles = [tile, tile, tile, tile]
        self.phase = 'kong'
        self._show_table_action('added-gang', player_index, None, tile, meld_index)
        self._play_sound('gang.mp3')
        self._broadcast_snapshot()

    async def settle_added_kong(self, player_index: int) -> None:
        """补杠结算（其余三家各付底分）→ 补摸回合。"""
        player = self.players[player_index]
        for meld in player.melds:
            if meld.type == 'gang' and meld.added and meld.pending:
                meld.pending = False
                break
        score_deltas = apply_kong_score(self.players, player_index, 'added')
        self._show_score_flow(score_deltas)
        self._broadcast_snapshot()
        await self._sleep(self.pace['afterKongSettle'])
        return await self.begin_turn(player_index, from_tail=True)

    def find_robbers(self, kong_player_index: int, tile: TileType) -> list[int]:
        """找可以抢杠的玩家（按距离排序）。"""
        robbers = []
        for player_index, player in enumerate(self.players):
            if player_index != kong_player_index and can_rob_kong(
                player.hand, tile, structural_meld_count(player)
            ):
                robbers.append((self.seat_distance(kong_player_index, player_index), player_index))
        robbers.sort(key=lambda item: item[0])
        return [item[1] for item in robbers]

    async def request_added_kong(self, player_index: int, meld_index: int, tile: TileType) -> None:
        """声明补杠 → 依次询问抢杠（找到则 end_game）→ 无抢则补杠结算。"""
        robbers = self.find_robbers(player_index, tile)
        self.declare_added_kong(player_index, meld_index, tile)
        if not robbers:
            await self._sleep(self.pace['beforeRobKong'])
            return await self.settle_added_kong(player_index)

        self.pending_kong = {
            'playerIndex': player_index,
            'meldIndex': meld_index,
            'tile': tile,
            'remainingRobbers': robbers[1:],
        }
        await self._sleep(self.pace['beforeRobKong'])
        return await self.offer_rob_kong(robbers[0])

    async def offer_rob_kong(self, robber_index: int) -> None:
        """询问单个玩家抢杠：抢 → end_game；过 → 下一抢杠候选或补杠结算。"""
        kong = self.pending_kong
        if not kong or self.phase == 'settled':
            return

        robber = self.players[robber_index]
        ctx = RobKongContext(
            tile=kong['tile'],
            from_=kong['playerIndex'],
            hand=robber.hand,
            exposedMelds=structural_meld_count(robber),
        )
        action = await self.controllers[robber_index].request_rob_kong(ctx)
        # 守卫：await 期间游戏可能已结束或 kong 已被处理
        if self.phase == 'settled' or self.pending_kong is not kong:
            return

        if action == 'pass':
            remaining = kong['remainingRobbers']
            if not remaining:
                return await self.settle_added_kong(kong['playerIndex'])
            kong['remainingRobbers'] = remaining[1:]
            await self._sleep(self.pace['betweenRobKongs'])
            return await self.offer_rob_kong(remaining[0])

        self._announce(f'{self.players[robber_index].name} 抢杠胡', 'red')
        self.pending_kong = None
        await self._sleep(self.pace['betweenRobKongs'])
        # end_game 是同步函数（TS 端用 later 调度），直接调用
        return self.end_game(robber_index, {
            'robbedKong': True,
            'robbedKongPlayerIndex': kong['playerIndex'],
            'winTile': kong['tile'],
        })

    # ── 和牌结算 ──

    def take_robbed_kong_tile(self, player_index: int, tile: TileType) -> int:
        """抢杠后把加杠副露还原为碰副露（取走 3 张），返回副露索引。"""
        player = self.players[player_index]
        meld_index = -1
        for i, meld in enumerate(player.melds):
            if meld.type == 'gang' and meld.added and meld.pending and meld.tile == tile:
                meld_index = i
                break
        if meld_index < 0:
            return -1
        meld = player.melds[meld_index]
        player.melds[meld_index] = Meld(
            type='peng', tile=meld.tile, from_=meld.from_,
            tiles=meld.tiles[:3], added=None, pending=None,
        )
        return meld_index

    def end_game(self, winner_index: int, options: Optional[dict] = None) -> None:
        """和牌结束：设置展示信息 → finalize_win（同步推进到 settled）。"""
        if self.phase in ('win-effect', 'revealing', 'settled', 'finished'):
            return
        options = options or {}
        self.phase = 'win-effect'
        self.current_player = -1
        self.user_drew_this_turn = False
        self.action_prompt = None
        self.pending_kong = None
        winner = self.players[winner_index]
        self.winning_player_index = winner_index

        win_tile = resolve_win_tile(winner, options)
        robbed_kong_meld_index = (
            self.take_robbed_kong_tile(options['robbedKongPlayerIndex'], win_tile)
            if options.get('robbedKong') else -1
        )
        if options.get('robbedKong') or options.get('fourRed'):
            source_index = -1
        elif winner.drawnTileIndex >= 0:
            source_index = winner.drawnTileIndex
        else:
            source_index = len(winner.hand) - 1 - winner.hand[::-1].index(win_tile) if win_tile in winner.hand else -1

        self.win_presentation = {
            'winnerIndex': winner_index,
            'tile': win_tile,
            'sourceIndex': source_index,
            'robbedKong': bool(options.get('robbedKong')),
            'robbedKongPlayerIndex': options.get('robbedKongPlayerIndex') or -1,
            'robbedKongMeldIndex': robbed_kong_meld_index,
        }
        self._show_table_action(
            'robbed-kong-win' if options.get('robbedKong') else 'self-draw',
            winner_index,
            options.get('robbedKongPlayerIndex') if options.get('robbedKong') else None,
            win_tile,
            -1,
        )
        self._play_sound('hu.mp3' if options.get('robbedKong') else 'zimo.mp3')
        self.finalize_win(winner_index, options)
        self._broadcast_snapshot()

    def finalize_win(self, winner_index: int, options: dict) -> None:
        """胡牌结算：买马 + 算分 + 收付 → result → settled。"""
        winner = self.players[winner_index]
        scores_before = [p.score for p in self.players]
        horses_draw = draw_horses(self.wall, 8)
        horses = horses_draw['horses']
        hits = horses_draw['hits']
        score = score_hand(
            dealer=winner_index == self.dealer,
            no_joker='white' not in winner.hand,
            four_red=bool(options.get('fourRed')),
            kong_bloom=bool(options.get('kongBloom')),
            horse_hits=hits,
            robbed_kong=bool(options.get('robbedKong')),
        )
        total_won = apply_win_score(
            self.players,
            winner_index,
            score['points'],
            options.get('robbedKongPlayerIndex') if options.get('robbedKong') else None,
            self.dealer,
        )
        base = {
            'winnerIndex': winner_index,
            'winner': winner.name,
            'horses': horses,
            'hits': hits,
            **score,
            'totalWon': total_won,
            **options,
        }
        self.result = self.make_round_result(base, scores_before)
        self.phase = 'settled'

    def end_draw(self) -> None:
        """流局：荒庄 → 庄家连庄（本场累加由 next_round 处理）。"""
        self.phase = 'settled'
        self.current_player = -1
        self.winning_player_index = -1
        scores_before = [p.score for p in self.players]
        self.result = self.make_round_result(
            {'draw': True, 'winner': '荒庄', 'horses': [], 'hits': 0,
             'multiplier': 0, 'points': 0, 'details': []},
            scores_before,
        )
        self._broadcast_snapshot()

    def make_round_result(self, base: dict, scores_before: list[int]) -> dict:
        """组装局结果：排名 + 各家分数变化。"""
        ranking = sorted(
            enumerate(self.players),
            key=lambda item: (-item[1].score, item[0]),
        )
        ranks = {player_index: rank for rank, (player_index, _) in enumerate(ranking, start=1)}
        return {
            **base,
            'roundLabel': self.round_label(),
            'honba': self.honba,
            'scoreChanges': [
                {
                    'playerIndex': index,
                    'name': player.name,
                    'avatar': player.avatar,
                    'score': player.score,
                    'delta': player.score - scores_before[index],
                    'rank': ranks.get(index),
                }
                for index, player in enumerate(self.players)
            ],
        }

    # ── 场次推进 ──

    async def next_round(self) -> None:
        """推进场次：庄连庄 + 本场 / 轮庄 → 终局判断 → 下一局。"""
        if not self.result or self.match_finished:
            return
        nxt = advance_match_state(
            round_=self.round,
            dealer=self.dealer,
            honba=self.honba,
            match_type=self.match_type,
            result=self.result,
            player_count=len(self.players),
        )
        self.round = nxt['round']
        self.dealer = nxt['dealer']
        self.honba = nxt['honba']
        if nxt['finished']:
            self.match_finished = True
            self.phase = 'finished'
            self._broadcast_snapshot()
            return
        await self.start_game()

    def return_to_lobby(self) -> None:
        self.phase = 'lobby'
        self.result = None
        self.win_presentation = None
        self.winning_player_index = -1
        self.match_finished = False
        self.players = []
        self._broadcast_snapshot()
