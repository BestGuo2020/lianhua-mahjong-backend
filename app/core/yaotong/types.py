"""幺筒数据类型 —— 对应 src/game/variants/yaotong/types.ts。

引擎内部用 0..26 的整数牌；协议层再转成 m1/p1/s1。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Optional

Tile = int
Seat = int

Declaration = Literal['none', 'ready', 'original']
WinKind = Literal['self', 'discard', 'rob', 'hot', 'ready']
KongKind = Literal['open', 'turn', 'stored', 'added']
MeldKind = Literal['peng', 'open', 'turn', 'stored', 'added']
ResponseAction = Literal['hu', 'peng', 'kong', 'pass']
RiverStage = Literal['normal', 'pre', 'first', 'later', 'hot']
Phase = Literal['original', 'discard', 'response', 'transition', 'settled']
MatchLength = Literal[4, 8, 16]
SettlementKind = Literal['win', 'draw', 'penalty']
EventKind = Literal['draw', 'discard', 'peng', 'kong', 'kong-intent',
                    'hu', 'ready', 'penalty', 'start']
Account = Literal[
    '胡牌分', '鸡分', '杠分', '幺筒责任分', '见面礼',
    '杀报加分', '一般包牌赔付', '流局赔听款',
]


@dataclass
class Meld:
    tile: Tile
    kind: MeldKind
    from_seat: Optional[Seat] = None
    pre: bool = False
    chicken: int = 0
    responsibility: int = 0


@dataclass
class Discard:
    tile: Tile
    chicken: int
    stage: RiverStage
    id: int
    first_chicken: bool = False
    follow_chicken: bool = False
    pre: bool = False


@dataclass
class Player:
    name: str
    hand: list[Tile] = field(default_factory=list)
    melds: list[Meld] = field(default_factory=list)
    river: list[Discard] = field(default_factory=list)
    score: int = 0
    declaration: Declaration = 'none'
    draws: int = 0
    discards: int = 0
    drawn: Optional[Tile] = None
    after_kong: bool = False
    burned: bool = False
    passed_hu: Optional[int] = None
    passed_shape: Optional[int] = None
    chicken_peng: list[Tile] = field(default_factory=list)
    chicken_discard: list[Tile] = field(default_factory=list)


@dataclass
class Pattern:
    name: str
    points: int


@dataclass
class HandValue:
    h: int
    pattern_points: int
    patterns: list[Pattern]
    natural_yaos: int
    winning_yao_natural: bool
    wildcards: int
    big: bool
    structure: Literal['standard', 'pairs', 'earth']
    groups: list[list[Tile]]
    resolved_hand: Optional[list[Tile]] = None


@dataclass
class WinContext:
    kind: WinKind
    tile: Tile
    chicken: Optional[Tile] = None
    kills_report: bool = False
    # None = 调用方没提供（对齐 TS 的可选字段）：此时不追加天胡/地胡。
    is_dealer: Optional[bool] = None


@dataclass
class Wait:
    tile: Tile
    values: list[HandValue] = field(default_factory=list)


@dataclass
class ActionOptions:
    hu: bool = False
    peng: bool = False
    kong: bool = False
    restricted_hu: bool = False
    restricted_peng: bool = False
    mandatory: bool = False
    h: int = 0
    shape_points: Optional[int] = None


@dataclass
class Pending:
    kind: Literal['discard', 'rob', 'hot']
    tile: Tile
    from_seat: Seat
    discard: Optional[Discard] = None
    options: dict[Seat, ActionOptions] = field(default_factory=dict)
    choices: dict[Seat, ResponseAction] = field(default_factory=dict)
    hu_plans: dict[Seat, str] = field(default_factory=dict)
    deferred_hu: Optional[Seat] = None


@dataclass
class Transfer:
    from_seat: Seat
    to: Seat
    account: Account
    base: int
    multiplier: int
    amount: int
    reason: str


@dataclass
class Settlement:
    kind: SettlementKind
    winners: list[Seat]
    source: Optional[Seat]
    reason: str
    deltas: list[int]
    next_dealer: Seat
    finished: bool
    win_kind: Optional[WinKind] = None
    tile: Optional[Tile] = None
    offender: Optional[Seat] = None
    indicator: Optional[Tile] = None
    chicken: Optional[Tile] = None
    values: dict[Seat, HandValue] = field(default_factory=dict)
    ready: list[bool] = field(default_factory=list)
    transfers: list[Transfer] = field(default_factory=list)


@dataclass
class GameEvent:
    id: int
    seat: Seat
    text: str
    kind: EventKind
    after_kong: bool = False
    tile: Optional[Tile] = None
    chicken: Optional[int] = None
    rush: Optional[RiverStage] = None


@dataclass
class GameState:
    players: list[Player]
    wall: list[Tile]
    dice: tuple[int, int] = (1, 1)
    wall_side: Seat = 0
    reserved_stacks: int = 0
    dealer: Seat = 0
    current: Seat = 0
    phase: Phase = 'original'
    original_decisions: list[bool] = field(default_factory=lambda: [False] * 4)
    pending: Optional[Pending] = None
    result: Optional[Settlement] = None
    first_fixed_discard: list[Tile] = field(default_factory=list)
    first_fixed_peng: bool = False
    first_yao_discard: bool = False
    seen_chicken_discards: list[Tile] = field(default_factory=list)
    wait_remaining_ms: list[int] = field(default_factory=lambda: [15000] * 4)
    events: list[GameEvent] = field(default_factory=list)
    serial: int = 0
    match_length: MatchLength = 4
    dealer_round: int = 0
    hand_number: int = 0
    repeats: int = 0


CommandType = Literal['original', 'discard', 'respond', 'hu', 'declareHu',
                      'passHu', 'kong']


@dataclass
class Command:
    """客户端/本地动作。字段按 type 解释，与 TS 的 Command 判别联合同构。"""
    type: CommandType
    declare: Optional[bool] = None
    index: Optional[int] = None
    action: Optional[ResponseAction] = None
    plan: Optional[str] = None
    defer_hu: Optional[bool] = None
    tile: Optional[Tile] = None
