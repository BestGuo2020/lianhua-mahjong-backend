"""幺筒对局引擎 —— 从 src/game/variants/yaotong/engine.ts 逐行翻译。

同步、确定性、无计时器/音频/Vue 副作用；房间层负责节拍与网络。
"""

from __future__ import annotations

import random as random_module
from dataclasses import dataclass, fields, replace
from typing import Callable, Literal, Optional

from app.core.yaotong.hands import (
    discard_waits,
    hu_opportunity,
    is_untouched_first_draw,
    waits,
)
from app.core.yaotong.hu_window import (
    hu_window,
    retain_hu_window,
    take_hu_window,
)
from app.core.yaotong.settlement import (
    Ending,
    hu_plan_key,
    legal_hu_plans,
    settle,
)
from app.core.yaotong.tiles import (
    YAO,
    break_wall,
    counts_of,
    is_chicken,
    is_fixed,
    shuffled_wall,
    sort_hand,
    tile_name,
)
from app.core.yaotong.types import (
    ActionOptions,
    Command,
    Discard,
    GameEvent,
    GameState,
    Meld,
    MatchLength,
    Pending,
    Player,
    Seat,
    Tile,
)

EventObserver = Callable[[GameState, GameEvent], None]
FlowStep = Literal[
    'first-draw', 'next-draw', 'after-peng', 'after-open-kong',
    'before-rob', 'between-robbers', 'after-added-kong', 'rob-win',
    'before-claim',
]
EngineFlow = Callable[[FlowStep, Callable[[], None]], None]

_EVENT_OBSERVER_ATTR = '_event_observer'
_FLOW_NAMES = {
    'first-draw', 'next-draw', 'after-peng', 'after-open-kong', 'before-rob',
    'between-robbers', 'after-added-kong', 'rob-win', 'before-claim',
}


def observe_game_events(state: GameState, observer: EventObserver):
    state.__dict__[_EVENT_OBSERVER_ATTR] = observer

    def unsubscribe() -> None:
        if state.__dict__.get(_EVENT_OBSERVER_ATTR) is observer:
            state.__dict__.pop(_EVENT_OBSERVER_ATTR, None)

    return unsubscribe


def notify_event(state: GameState, value: GameEvent) -> None:
    observer = state.__dict__.get(_EVENT_OBSERVER_ATTR)
    if observer is None:
        return
    try:
        observer(state, value)
    except Exception:
        # 录制/表现失败绝不能中断一局。
        pass


def advance(state: GameState, flow: Optional[EngineFlow],
            step: FlowStep, resume: Callable[[], None]) -> None:
    if flow is None:
        resume()
        return
    state.phase = 'transition'
    flow(step, resume)


def make_player(name: str = '玩家') -> Player:
    return Player(name=name)


@dataclass
class StartOptions:
    length: MatchLength = 4
    random: Optional[Callable[[], float]] = None
    wall: Optional[list[Tile]] = None
    dice: Optional[tuple[int, int]] = None
    dealer: Optional[int] = None


def create_game(options: Optional[StartOptions] = None) -> GameState:
    options = options or StartOptions()
    random_fn = options.random or random_module.random
    dice = options.dice or (
        1 + int(random_fn() * 6), 1 + int(random_fn() * 6),
    )
    dealer = options.dealer if options.dealer is not None else int(random_fn() * 4)
    wall = options.wall if options.wall is not None else shuffled_wall(random_fn)

    if (len(wall) != 108
            or any(count != 4 for count in counts_of(wall))
            or any(not isinstance(tile, int) or isinstance(tile, bool)
                   or tile < 0 or tile > 26 for tile in wall)):
        raise ValueError('牌墙必须是108张三色序数牌，各四张')
    if any(not isinstance(value, int) or isinstance(value, bool)
           or value < 1 or value > 6 for value in dice):
        raise ValueError('骰子须为1至6')
    if not isinstance(dealer, int) or isinstance(dealer, bool) or not 0 <= dealer < 4:
        raise ValueError('庄家须为0至3')

    broken = break_wall(list(wall), dice, dealer)
    players = [make_player(name) for name in ('你', '听风', '观山', '望月')]
    # 三轮每家四张，再各一张；庄家不跳牌。
    for batch in range(4):
        for offset in range(4):
            seat = (dealer + offset) % 4
            count = 1 if batch == 3 else 4
            players[seat].hand.extend(broken['wall'][:count])
            del broken['wall'][:count]
    for player in players:
        sort_hand(player.hand)

    state = GameState(
        players=players,
        wall=broken['wall'],
        dice=(dice[0], dice[1]),
        wall_side=broken['side'],
        reserved_stacks=broken['reserved'],
        dealer=dealer,
        current=dealer,
        phase='original',
        original_decisions=[False, False, False, False],
        pending=None,
        result=None,
        first_fixed_discard=[],
        first_fixed_peng=False,
        first_yao_discard=False,
        seen_chicken_discards=[],
        wait_remaining_ms=[15000, 15000, 15000, 15000],
        events=[],
        serial=0,
        match_length=options.length,
        dealer_round=1,
        hand_number=1,
        repeats=0,
    )
    _event(
        state, dealer, 'start',
        '同点双倍 · 本局全部分数 ×2' if dice[0] == dice[1] else '新局开桌 · 原报确认',
    )
    return state


def _event(
    state: GameState,
    seat: Seat,
    kind: str,
    text: str,
    tile: Optional[Tile] = None,
    *,
    chicken: Optional[int] = None,
    rush: Optional[str] = None,
    after_kong: bool = False,
    notify: bool = True,
) -> GameEvent:
    state.serial += 1
    value = GameEvent(
        id=state.serial, seat=seat, kind=kind, text=text, tile=tile,  # type: ignore[arg-type]
        chicken=chicken, rush=rush, after_kong=after_kong,  # type: ignore[arg-type]
    )
    state.events.append(value)
    if len(state.events) > 80:
        state.events.pop(0)
    if notify:
        notify_event(state, value)
    return value


def finish(state: GameState, ending: Ending) -> None:
    state.result = settle(state, ending)
    for index, player in enumerate(state.players):
        player.score += state.result.deltas[index]
    state.phase = 'settled'
    state.pending = None

    seat = (
        ending.offender
        if ending.offender is not None
        else (ending.winners[0] if ending.winners else state.dealer)
    )
    if ending.kind == 'penalty':
        event_kind = 'penalty'
        text = f'包牌 · {ending.reason}'
    elif ending.kind == 'win':
        event_kind = 'hu'
        if ending.winners and len(ending.winners) > 1:
            text = '一炮多响'
        elif ending.win_kind == 'self':
            text = '自摸'
        elif ending.win_kind == 'rob':
            text = '抢杠胡'
        elif ending.win_kind == 'hot':
            text = '接热炮'
        else:
            text = '胡牌'
    else:
        event_kind = 'start'
        text = '荒庄 · 赔听结算'
    _event(state, seat, event_kind, text, ending.tile)


def _penalty(state: GameState, offender: Seat, reason: str) -> None:
    finish(state, Ending(kind='penalty', offender=offender, reason=reason))


def _draw(state: GameState, seat: Seat, tail: bool = False) -> None:
    if not state.wall:
        finish(state, Ending(kind='draw'))
        return
    player = state.players[seat]
    sort_hand(player.hand)
    tile = state.wall.pop() if tail else state.wall.pop(0)
    player.hand.append(tile)
    player.drawn = tile
    player.draws += 1
    player.passed_hu = None
    player.passed_shape = None
    player.chicken_peng = []
    player.chicken_discard = []
    player.after_kong = tail
    state.current = seat
    state.phase = 'discard'
    _event(state, seat, 'draw', '杠后补摸' if tail else '摸牌')


def self_options(state: GameState, seat: Seat) -> ActionOptions:
    empty = ActionOptions()
    if state.phase != 'discard' or state.current != seat:
        return empty
    player = state.players[seat]
    # 碰出来的回合不是自摸胡机会。
    if player.drawn is None:
        return empty
    kills_report = any(
        other.declaration != 'none' for index, other in enumerate(state.players)
        if index != seat
    )
    opportunity = hu_opportunity(
        player,
        __win_context(kind='self', tile=player.drawn, kills_report=kills_report,
                      is_dealer=seat == state.dealer),
    )
    return ActionOptions(
        hu=opportunity['hu'],
        h=opportunity['h'],
        shape_points=opportunity['shape_points'],
        restricted_hu=opportunity['restricted'],
        mandatory=(
            opportunity['hu'] and not opportunity['restricted'] and kills_report
        ),
    )


def kong_options(state: GameState, seat: Seat) -> list[Tile]:
    if state.phase != 'discard' or state.current != seat or not state.wall:
        return []
    player = state.players[seat]
    if player.declaration != 'none':
        return []
    counts = counts_of(player.hand)
    return [
        tile for tile, count in enumerate(counts)
        if count == 4 or (count > 0 and any(
            meld.kind == 'peng' and meld.tile == tile for meld in player.melds
        ))
    ]


def _pass_self(state: GameState, seat: Seat) -> bool:
    options = self_options(state, seat)
    if options.mandatory:
        _penalty(state, seat, '可杀报却放弃胡牌')
        return False
    if options.hu and not options.restricted_hu:
        player = state.players[seat]
        player.passed_hu = max(player.passed_hu or 0, options.h)
        player.passed_shape = max(player.passed_shape or 0, options.shape_points or 0)
    return True


def _close_hu_window(state: GameState) -> bool:
    entry = take_hu_window(state)
    if entry is None:
        return True
    seat, snapshot = entry
    pending = snapshot.pending
    if pending is None:
        return True
    if state.pending is not None and state.pending.deferred_hu == seat:
        state.pending.deferred_hu = None
    options = pending.options.get(seat)
    if options is None:
        return True
    if options.mandatory:
        _penalty(
            state, seat,
            '可杀报却放弃胡牌'
            if state.players[pending.from_seat].declaration != 'none'
            else '手中有幺筒却未抢杠／接热炮',
        )
        return False
    player = state.players[seat]
    # 真实摸牌已经清掉上一轮的限制。
    if (not options.restricted_hu
            and player.draws == snapshot.players[seat].draws):
        player.passed_hu = max(player.passed_hu or 0, options.h)
        player.passed_shape = max(player.passed_shape or 0, options.shape_points or 0)
    return True


def _offer_responses(state: GameState, flow: Optional[EngineFlow] = None) -> None:
    pending = state.pending
    assert pending is not None
    for offset in range(1, 4):
        seat = (pending.from_seat + offset) % 4
        player = state.players[seat]
        count = sum(1 for tile in player.hand if tile == pending.tile)
        kills_report = state.players[pending.from_seat].declaration != 'none'
        opportunity = hu_opportunity(
            replace(player, hand=[*player.hand, pending.tile]),
            __win_context(kind=pending.kind, tile=pending.tile,
                          kills_report=kills_report),
        )
        h = opportunity['h']
        peng = pending.kind != 'rob' and player.declaration == 'none' and count >= 2
        kong = (pending.kind != 'rob' and player.declaration == 'none'
                and count >= 3 and bool(state.wall))
        if h or peng or kong:
            pending.options[seat] = ActionOptions(
                hu=h > 0,
                peng=peng,
                kong=kong,
                h=h,
                shape_points=opportunity['shape_points'],
                restricted_hu=opportunity['restricted'],
                restricted_peng=peng and pending.tile in player.chicken_discard,
                mandatory=(
                    h > 0 and not opportunity['restricted']
                    and (kills_report or (
                        pending.kind in ('rob', 'hot') and YAO in player.hand
                    ))
                ),
            )
    state.phase = 'response'
    if not pending.options:
        _resolve_responses(state, flow)


def _remove_tiles(player: Player, tile: Tile, count: int) -> None:
    for _ in range(count):
        try:
            index = player.hand.index(tile)
        except ValueError as exc:
            raise ValueError('内部牌张数量错误') from exc
        player.hand.pop(index)


def _resolve_responses(state: GameState,
                       flow: Optional[EngineFlow] = None) -> None:
    pending = state.pending
    assert pending is not None
    winners = [
        seat for seat, choice in pending.choices.items() if choice == 'hu'
    ]
    if winners:
        def end() -> None:
            if pending.kind == 'rob':
                _remove_tiles(state.players[pending.from_seat], pending.tile, 1)
            else:
                state.players[pending.from_seat].river.pop()
            for seat in winners:
                state.players[seat].hand.append(pending.tile)
            if pending.kind in ('hot', 'rob'):
                state.players[pending.from_seat].burned = True
            finish(state, Ending(
                kind='win',
                winners=winners,
                source=pending.from_seat,
                win_kind=pending.kind,  # type: ignore[arg-type]
                tile=pending.tile,
                responsibility=(
                    (pending.discard.chicken if pending.discard is not None else 0)
                    if pending.tile == YAO else 0
                ),
                hu_plans=pending.hu_plans,
            ))

        if pending.kind == 'rob':
            advance(state, flow, 'rob-win', end)
        else:
            end()
        return

    if pending.deferred_hu is not None:
        retain_hu_window(state, pending.deferred_hu)

    if pending.kind == 'rob':
        player = state.players[pending.from_seat]
        _remove_tiles(player, pending.tile, 1)
        meld = next(
            (item for item in player.melds
             if item.tile == pending.tile and item.kind == 'peng'),
            None,
        )
        if meld is None:
            raise ValueError('补杠找不到原碰')
        meld.kind = 'added'
        meld.chicken = (
            120 if meld.pre else 60
        ) if pending.tile == YAO else (10 if is_fixed(pending.tile) else 0)
        state.pending = None
        _event(state, pending.from_seat, 'kong', '补杠', pending.tile)
        advance(state, flow, 'after-added-kong',
                lambda: _draw(state, pending.from_seat, True))
        return

    claimant = next(
        (
            (pending.from_seat + offset) % 4
            for offset in range(1, 4)
            if pending.choices.get((pending.from_seat + offset) % 4)
            in ('peng', 'kong')
        ),
        None,
    )
    state.pending = None
    if claimant is not None:
        player = state.players[claimant]
        kong = pending.choices[claimant] == 'kong'
        _remove_tiles(player, pending.tile, 3 if kong else 2)
        state.players[pending.from_seat].river.pop()
        pre = bool(
            pending.discard is not None
            and (pending.discard.pre or pending.discard.stage == 'pre')
        )
        if pending.tile == YAO:
            chicken = (30 if pre else 15) * (4 if kong else 3)
        elif is_fixed(pending.tile):
            chicken = 10 if kong else (6 if state.first_fixed_peng else 8)
        else:
            chicken = 0
        player.melds.append(Meld(
            tile=pending.tile,
            kind='open' if kong else 'peng',
            from_seat=pending.from_seat,
            pre=pre,
            chicken=chicken,
            responsibility=(30 if pre else 20) if pending.tile == YAO else 0,
        ))
        if not kong and is_fixed(pending.tile):
            state.first_fixed_peng = True
        player.drawn = None
        player.after_kong = False
        if not kong and is_chicken(pending.tile):
            player.chicken_peng.append(pending.tile)
        state.current = claimant
        state.phase = 'discard'
        _event(state, claimant, 'kong' if kong else 'peng',
               '明杠' if kong else '碰', pending.tile)
        if kong:
            advance(state, flow, 'after-open-kong',
                    lambda: _draw(state, claimant, True))
        else:
            advance(state, flow, 'after-peng',
                    lambda: setattr(state, 'phase', 'discard'))
    else:
        advance(state, flow, 'next-draw',
                lambda: _draw(state, (pending.from_seat + 1) % 4))


def __win_context(*, kind: str, tile: Tile, kills_report: bool = False,
                  is_dealer: Optional[bool] = None):
    # 延迟导入避免循环依赖（types 层不依赖引擎）。
    from app.core.yaotong.types import WinContext
    return WinContext(
        kind=kind,  # type: ignore[arg-type]
        tile=tile,
        kills_report=kills_report,
        is_dealer=is_dealer,
    )


def _restore_from_snapshot(state: GameState, snapshot: GameState) -> None:
    events = state.events
    serial = state.serial
    wait_remaining = state.wait_remaining_ms
    for field in fields(GameState):
        if field.name in ('events', 'serial', 'wait_remaining_ms'):
            continue
        setattr(state, field.name, getattr(snapshot, field.name))
    state.events = events
    state.serial = serial
    state.wait_remaining_ms = wait_remaining


def act(state: GameState, seat: Seat, command: Command,
        flow: Optional[EngineFlow] = None) -> bool:
    if (not isinstance(seat, int) or isinstance(seat, bool)
            or seat < 0 or seat > 3 or state.phase == 'settled'):
        return False
    player = state.players[seat]

    if command.type in ('declareHu', 'passHu'):
        previous = hu_window(state, seat)
        if previous is not None:
            if command.type == 'passHu':
                _close_hu_window(state)
                return True
            old = previous.pending
            if old is None:
                return False
            if command.plan and not any(
                hu_plan_key(value) == command.plan
                for value in legal_hu_plans(
                    replace(previous.players[seat],
                            hand=[*previous.players[seat].hand, old.tile]),
                    __win_context(
                        kind=old.kind, tile=old.tile,
                        is_dealer=seat == previous.dealer,
                        kills_report=(
                            previous.players[old.from_seat].declaration != 'none'
                        ),
                    ),
                )
            ):
                return False
            take_hu_window(state)
            _restore_from_snapshot(state, previous)
            return act(state, seat, Command(
                type='respond', action='hu', plan=command.plan,
            ), flow)

        if state.phase == 'discard' and state.current == seat:
            if command.type == 'passHu':
                _pass_self(state, seat)
                return True
            if self_options(state, seat).hu:
                return act(state, seat, Command(type='hu', plan=command.plan), flow)

        pending = state.pending
        if (pending is not None and pending.from_seat != seat
                and state.phase in ('response', 'transition')):
            if pending.choices.get(seat) == 'hu':
                return False
            if state.phase == 'transition' and not pending.choices.get(seat):
                opportunity = hu_opportunity(
                    replace(player, hand=[*player.hand, pending.tile]),
                    __win_context(
                        kind=pending.kind, tile=pending.tile,
                        kills_report=(
                            state.players[pending.from_seat].declaration != 'none'
                        ),
                    ),
                )
                if opportunity['hu']:
                    _offer_responses(state, flow)
            if (state.phase == 'response' and pending.options.get(seat)
                    and not pending.choices.get(seat)):
                if command.type == 'passHu':
                    return act(state, seat, Command(
                        type='respond', action='pass',
                    ), flow)
                if pending.options[seat].hu:
                    return act(state, seat, Command(
                        type='respond', action='hu', plan=command.plan,
                    ), flow)

        if command.type == 'passHu':
            return False
        _penalty(state, seat, '炸胡：当前没有可声明的胡牌机会')
        return True

    if command.type == 'original':
        if state.phase != 'original' or state.original_decisions[seat]:
            return False
        if command.declare and not waits(player):
            return False
        state.original_decisions[seat] = True
        if command.declare:
            player.declaration = 'original'
            _event(state, seat, 'ready', '原报 · 锁定听口')
        if all(state.original_decisions):
            advance(state, flow, 'first-draw',
                    lambda: _draw(state, state.dealer))
        return True

    if command.type == 'respond':
        pending = state.pending
        if (state.phase != 'response' or pending is None
                or pending.choices.get(seat)):
            return False
        options = pending.options.get(seat)
        if options is None:
            return False
        if command.action != 'pass' and not (
            (command.action == 'hu' and options.hu)
            or (command.action == 'peng' and options.peng)
            or (command.action == 'kong' and options.kong)
        ):
            return False
        if command.action == 'hu' and command.plan and not any(
            hu_plan_key(value) == command.plan
            for value in legal_hu_plans(
                replace(player, hand=[*player.hand, pending.tile]),
                __win_context(
                    kind=pending.kind, tile=pending.tile,
                    is_dealer=seat == state.dealer,
                    kills_report=(
                        state.players[pending.from_seat].declaration != 'none'
                    ),
                ),
            )
        ):
            return False
        if command.action == 'hu' and options.restricted_hu:
            _penalty(state, seat, '违反过手胡限制')
            return True
        defer = (
            command.action == 'pass' and bool(command.defer_hu)
            and pending.kind != 'rob' and options.hu
        )
        if command.action != 'hu' and options.mandatory and not defer:
            _penalty(
                state, seat,
                '可杀报却放弃胡牌'
                if state.players[pending.from_seat].declaration != 'none'
                else '手中有幺筒却未抢杠／接热炮',
            )
            return True
        if command.action == 'peng' and options.restricted_peng:
            _penalty(state, seat, '同巡打鸡后碰鸡')
            return True
        if (command.action != 'hu' and options.hu
                and not options.restricted_hu and not defer):
            player.passed_hu = max(player.passed_hu or 0, options.h)
            player.passed_shape = max(
                player.passed_shape or 0, options.shape_points or 0,
            )
        pending.choices[seat] = command.action  # type: ignore[index]
        if defer:
            pending.deferred_hu = seat
            retain_hu_window(state, seat)
        if command.action == 'hu' and command.plan:
            pending.hu_plans[seat] = command.plan
        if all(index in pending.choices for index in pending.options):
            choices = list(pending.choices.values())
            if ('hu' not in choices
                    and (pending.kind == 'rob'
                         or any(choice in ('peng', 'kong') for choice in choices))):
                advance(state, flow, 'before-claim',
                        lambda: _resolve_responses(state, flow))
            else:
                _resolve_responses(state, flow)
        elif pending.kind == 'rob':
            advance(state, flow, 'between-robbers',
                    lambda: setattr(state, 'phase', 'response'))
        return True

    if state.phase != 'discard' or state.current != seat:
        return False

    if command.type == 'hu':
        options = self_options(state, seat)
        if not options.hu:
            return False
        if command.plan and not any(
            hu_plan_key(value) == command.plan
            for value in legal_hu_plans(
                player,
                __win_context(
                    kind='self', tile=player.drawn,
                    is_dealer=seat == state.dealer,
                    kills_report=any(
                        other.declaration != 'none'
                        for index, other in enumerate(state.players)
                        if index != seat
                    ),
                ),
            )
        ):
            return False
        if options.restricted_hu:
            _penalty(state, seat, '违反过手胡限制')
        else:
            finish(state, Ending(
                kind='win', winners=[seat], win_kind='self', tile=player.drawn,
                hu_plans={seat: command.plan} if command.plan else {},
            ))
        return True

    if command.type == 'kong':
        if command.tile not in kong_options(state, seat):
            return False
        if not _pass_self(state, seat):
            return True
        meld = next(
            (item for item in player.melds
             if item.kind == 'peng' and item.tile == command.tile),
            None,
        )
        if meld is not None:
            if not _close_hu_window(state):
                return True
            state.pending = Pending(
                kind='rob', tile=command.tile, from_seat=seat,
                discard=None, options={}, choices={},
            )
            if flow:
                _event(state, seat, 'kong-intent', '宣告补杠', command.tile)
            advance(state, flow, 'before-rob',
                    lambda: _offer_responses(state, flow))
        else:
            kind = 'turn' if player.drawn == command.tile else 'stored'
            _remove_tiles(player, command.tile, 4)
            player.melds.append(Meld(
                tile=command.tile, kind=kind, pre=False, responsibility=0,
                chicken=60 if command.tile == YAO else (
                    10 if is_fixed(command.tile) else 0
                ),
            ))
            _event(state, seat, 'kong',
                   '转弯杠' if kind == 'turn' else '存杠', command.tile)
            _draw(state, seat, True)
        return True

    if (command.type != 'discard'
            or not isinstance(command.index, int)
            or isinstance(command.index, bool)
            or command.index < 0
            or command.index >= len(player.hand)):
        return False
    if (player.declaration != 'none'
            and (player.drawn is None
                 or command.index != len(player.hand) - 1)):
        return False
    if command.declare and (
        player.declaration != 'none'
        or not is_untouched_first_draw(player)
        or not any(option['index'] == command.index
                   for option in discard_waits(player))
    ):
        return False
    if not _close_hu_window(state) or not _pass_self(state, seat):
        return True

    tile = player.hand[command.index]
    illegal_chicken_discard = tile in player.chicken_peng
    pre = any(other.discards == 0 for other in state.players)
    chicken = 0
    stage = 'normal'
    if tile == YAO:
        stage = (
            'hot' if player.after_kong
            else 'pre' if pre
            else 'later' if state.first_yao_discard
            else 'first'
        )
        chicken = {'hot': 100, 'pre': 30, 'first': 20, 'later': 15, 'normal': 0}[stage]
        if stage == 'first':
            state.first_yao_discard = True
    elif is_fixed(tile):
        chicken = 4 if (
            tile not in state.first_fixed_discard or player.discards == 0
        ) else 2
        if tile not in state.first_fixed_discard:
            state.first_fixed_discard.append(tile)

    hot = player.after_kong
    player.hand.pop(command.index)
    sort_hand(player.hand)
    player.discards += 1
    player.drawn = None
    player.after_kong = False
    if is_chicken(tile):
        player.chicken_discard.append(tile)

    stage_labels = {
        'pre': '预冲', 'first': '首冲', 'later': '后冲',
        'hot': '杠冲', 'normal': '',
    }
    discarded_event = _event(
        state, seat, 'discard',
        f'{stage_labels[stage]}幺筒 · {chicken}' if tile == YAO
        else f'打出{tile_name(tile)}',
        tile,
        chicken=chicken,
        rush=stage,
        after_kong=hot,
        notify=False,
    )
    first_chicken = is_chicken(tile) and tile not in state.seen_chicken_discards
    if first_chicken:
        state.seen_chicken_discards.append(tile)
    follow_chicken = (
        is_fixed(tile) and chicken == 4 and not first_chicken
    )
    discard = Discard(
        tile=tile, chicken=chicken, stage=stage,  # type: ignore[arg-type]
        id=state.serial, first_chicken=first_chicken,
        follow_chicken=follow_chicken, pre=pre,
    )
    player.river.append(discard)
    notify_event(state, discarded_event)

    if illegal_chicken_discard:
        _penalty(state, seat, '同巡碰鸡后打鸡')
        return True
    if command.declare:
        player.declaration = 'ready'
        _event(state, seat, 'ready', '报听 · 锁定听口')
    state.pending = Pending(
        kind='hot' if hot else 'discard',  # type: ignore[arg-type]
        tile=tile, from_seat=seat, discard=discard, options={}, choices={},
    )
    _offer_responses(state, flow)
    return True


def continue_game(
    state: GameState,
    options: Optional[StartOptions] = None,
) -> GameState:
    if state.result is None or state.result.finished:
        return state
    options = options or StartOptions()
    next_state = create_game(StartOptions(
        length=state.match_length,
        random=options.random,
        wall=options.wall,
        dice=options.dice,
        dealer=state.result.next_dealer,
    ))
    same = next_state.dealer == state.dealer
    next_state.dealer_round = state.dealer_round + (0 if same else 1)
    next_state.repeats = state.repeats + 1 if same else 0
    next_state.hand_number = state.hand_number + 1
    for index, player in enumerate(next_state.players):
        player.score = state.players[index].score
        player.name = state.players[index].name
    return next_state
