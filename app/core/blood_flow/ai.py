"""血流本地 AI 贪婪 EV 策略 —— 翻译自前端 patternPotentials.ts / evContext.ts / ai.ts 的 decideBloodFlowActionEv。

view 为前端 BloodFlowSeatView 同构 dict（room._seat_view 输出）。
弃牌排序 v1 沿用后端 lotus_ai.decide_turn（未注入番型潜力/放炮成本回调，见验收页口径）。
"""

from functools import cmp_to_key
from typing import Optional

from app.core.lotus_ai import decide_claim as lotus_decide_claim
from app.core.lotus_ai import decide_turn as lotus_decide_turn
from app.core.lotus_rules import waiting_tiles as lotus_waiting_tiles
from app.core.tiles import TILE_TYPES
from app.models.game import TileType

from .config import BLOOD_FLOW_AI, BLOOD_FLOW_CONFIG, BloodFlowAiConfig

HONORS: tuple[str, ...] = ('east', 'south', 'west', 'north', 'red', 'green', 'white')
DRAGONS: tuple[str, ...] = ('red', 'green', 'white')
WINDS: tuple[str, ...] = ('east', 'south', 'west', 'north')
GREEN_TILES = {'s2', 's3', 's4', 's6', 's8', 'green'}
TERMINAL_TILES = {'m1', 'm9', 'p1', 'p9', 's1', 's9'}
THIRTEEN_ORPHAN_TILES = ('m1', 'm9', 'p1', 'p9', 's1', 's9', *WINDS, *DRAGONS)


def wildcard_set(jokers: list[str]) -> set[str]:
    return {*jokers, 'white'}


# ── 特殊手潜力（与前端 patternPotentials.ts 同口径） ──

def _shi_san_lan_potential(hand: list[str], jokers: list[str]) -> int:
    joker_set = set(jokers)
    natural = [t for t in hand if t not in joker_set]
    defects = len(natural) - len(set(natural))
    for suit in ('m', 'p', 's'):
        ranks = sorted(int(t[1]) for t in natural if len(t) == 2 and t[0] == suit)
        for i in range(1, len(ranks)):
            if ranks[i] - ranks[i - 1] < 3:
                defects += 1
    honors_held = sum(1 for h in HONORS if h in natural)
    joker_count = len(hand) - len(natural)
    honor_shortfall = max(0, 7 - honors_held)
    jokers_after_honors = max(0, joker_count - honor_shortfall)
    defects_after_jokers = max(0, defects - jokers_after_honors)
    if defects_after_jokers > 3:
        return 0
    return (4 - defects_after_jokers) * 4 + honors_held + joker_count


def _thirteen_orphans_potential(hand: list[str], jokers: list[str]) -> int:
    joker_set = set(jokers)
    natural = [t for t in hand if t not in joker_set]
    held_kinds = sum(1 for t in THIRTEEN_ORPHAN_TILES if t in natural)
    joker_count = len(hand) - len(natural)
    kinds_after_jokers = held_kinds + joker_count
    if kinds_after_jokers < 10:
        return 0
    has_pair = any(natural.count(t) >= 2 for t in THIRTEEN_ORPHAN_TILES)
    pair_score = 8 if (has_pair or joker_count >= 2) else 0
    return (kinds_after_jokers - 10) * 3 + pair_score


def _seven_pairs_potential(hand: list[str], jokers: list[str]) -> int:
    joker_set = set(jokers)
    counts: dict[str, int] = {}
    joker_count = 0
    for tile in hand:
        if tile in joker_set:
            joker_count += 1
        else:
            counts[tile] = counts.get(tile, 0) + 1
    pairs = singles = 0
    for count in counts.values():
        pairs += count // 2
        singles += count % 2
    near_seven = pairs + min(singles, joker_count)
    if near_seven < 5:
        return 0
    return near_seven * 4


# ── 番型潜力（翻译 patternPotentials.ts） ──

def _triplet_melds(melds: list[dict]) -> list[dict]:
    return [m for m in melds if m.get('type') != 'chi' and not m.get('windKong')]


def _declared_gang_count(melds: list[dict]) -> int:
    return sum(1 for m in melds if m.get('type') in ('gang', 'angang') and not m.get('windKong'))


def _shape(hand: list[str], melds: list[dict], jokers: list[str]) -> dict:
    wild = wildcard_set(jokers)
    natural = [t for t in hand if t not in wild]
    counts: dict[str, int] = {}
    for t in natural:
        counts[t] = counts.get(t, 0) + 1
    meld_tiles = [t for m in melds for t in m.get('tiles', [])]
    return {'natural': natural, 'counts': counts, 'joker_count': len(hand) - len(natural),
            'suited': [t for t in natural if len(t) == 2],
            'honors': [t for t in natural if len(t) != 2],
            'meld_tiles': meld_tiles}


def pattern_potentials(hand: list[str], melds: list[dict], jokers: list[str]) -> list[dict]:
    s = _shape(hand, melds, jokers)
    effective = len(hand) + 3 * len(melds)
    directions: list[dict] = []

    def add(pattern_id: str, progress: float) -> None:
        if progress <= 0:
            return
        progress = min(1.0, progress)
        weight = BLOOD_FLOW_CONFIG.patterns[pattern_id].weight
        directions.append({'id': pattern_id, 'weight': weight, 'progress': progress,
                           'score': weight * progress ** 2})

    meld_tiles = s['meld_tiles']
    suit_counts: dict[str, int] = {}
    for tile in s['suited']:
        suit_counts[tile[0]] = suit_counts.get(tile[0], 0) + 1
    meld_honors = sum(1 for t in meld_tiles if len(t) != 2)
    meld_suits = {t[0] for t in meld_tiles if len(t) == 2}
    main_suit = max(suit_counts, key=suit_counts.get) if suit_counts else None
    main_count = suit_counts.get(main_suit, 0) if main_suit else 0
    single_suit_melds = len(meld_suits) <= 1 and (main_suit is None or not meld_suits or main_suit in meld_suits)
    if not s['honors'] and not meld_honors and single_suit_melds and (main_count + s['joker_count']) > 0:
        add('pure-suit', (main_count + s['joker_count']) / effective)
    honors_or_joker = len(s['honors']) + meld_honors + s['joker_count']
    if single_suit_melds and (main_count + s['joker_count']) > 0 and honors_or_joker > 0 \
            and (len(s['honors']) + meld_honors > 0 or s['joker_count'] > 0):
        add('mixed-suit', (main_count + s['joker_count'] + len(s['honors']) + meld_honors) / effective)

    has_chi_meld = any(m.get('type') == 'chi' for m in melds)
    if not has_chi_meld:
        triplet_units = len(_triplet_melds(melds))
        for count in s['counts'].values():
            triplet_units += count // 3 + (1 if count % 3 == 2 else 0)
        add('all-triplets', (triplet_units + s['joker_count']) / 5)

    for pattern_id, tile_set, need, pair_need in (
        ('little-three-dragons', DRAGONS, 2, 1), ('big-three-dragons', DRAGONS, 3, 0),
        ('little-four-winds', WINDS, 3, 1), ('big-four-winds', WINDS, 4, 0),
    ):
        units = pairs = 0
        for tile in tile_set:
            count = s['counts'].get(tile, 0)
            units += 1 if count >= 3 or any(m.get('tile') == tile for m in _triplet_melds(melds)) else 0
            pairs += 1 if count == 2 else 0
        jk = s['joker_count']
        with_jokers = min(need, units + max(0, jk - pair_need))
        add(pattern_id, (with_jokers + min(pair_need, pairs + min(jk, pair_need))) / (need + pair_need))

    if not melds and not s['honors'] and len(suit_counts) <= 1:
        need = (3, 1, 1, 1, 1, 1, 1, 1, 3)
        satisfied = 0
        for rank in range(1, 10):
            satisfied += min(s['counts'].get(f'{main_suit or "m"}{rank}', 0), need[rank - 1])
        add('nine-gates', min(1.0, (satisfied + s['joker_count']) / 14) * (1 if len(suit_counts) <= 1 else 0))

    if all(t in GREEN_TILES or t in wildcard_set(jokers) for t in [*hand, *meld_tiles]):
        in_set = sum(1 for t in s['natural'] if t in GREEN_TILES)
        add('all-green', (in_set + s['joker_count']) / effective)

    if not has_chi_meld:
        all_natural = [*s['natural'], *meld_tiles]
        restricted = all(t in TERMINAL_TILES or len(t) != 2 for t in all_natural)
        if restricted and len(s['honors']) + meld_honors == 0:
            units = len(_triplet_melds(melds))
            units += sum(c // 3 for c in s['counts'].values())
            add('pure-terminals', (units + s['joker_count']) / 5)
        if restricted and (len(s['suited']) > 0 or s['joker_count'] > 0) \
                and (len(s['honors']) + meld_honors > 0 or s['joker_count'] > 0):
            units = len(_triplet_melds(melds))
            units += sum(c // 3 for c in s['counts'].values())
            add('mixed-terminals', (units + s['joker_count']) / 5)

    concealed_units = sum(1 for m in melds if m.get('type') == 'angang')
    concealed_units += sum(1 for c in s['counts'].values() if c >= 3)
    add('three-concealed-triplets', (concealed_units + s['joker_count']) / 3)
    add('four-concealed-triplets', (concealed_units + s['joker_count']) / 4)

    if not s['suited'] and all(len(t) != 2 for t in meld_tiles):
        add('all-honors', (len(s['honors']) + s['joker_count']) / effective)

    gangs = _declared_gang_count(melds)
    if gangs > 0:
        add('three-kongs', gangs / 3)
        add('four-kongs', gangs / 4)

    wild = list(wildcard_set(jokers))
    shi_san = _shi_san_lan_potential(hand, wild)
    if shi_san > 0:
        honors_held = sum(1 for h in HONORS if h in s['natural'])
        add('shiSanLan', min(1.0, shi_san / 24))
        if honors_held == 7 and s['joker_count'] == 0:
            add('qiXing', min(1.0, shi_san / 24))
    orphans = _thirteen_orphans_potential(hand, wild)
    if orphans > 0:
        add('thirteenOrphans', min(1.0, orphans / 17))
    seven = _seven_pairs_potential(hand, wild)
    if seven > 0:
        add('sevenPairs', min(1.0, seven / 28))
    return directions


def pattern_potential_total(hand: list[str], melds: list[dict], jokers: list[str]) -> float:
    return sum(d['score'] for d in pattern_potentials(hand, melds, jokers))


def pattern_potential_ev(hand: list[str], melds: list[dict], jokers: list[str], wall_count: int) -> float:
    late = 0.4 if wall_count <= BLOOD_FLOW_AI.late_game_wall_count else 1.0
    return pattern_potential_total(hand, melds, jokers) * BLOOD_FLOW_CONFIG.base_points * late


# ── 收益估算（翻译 estimateWinIncome / certainPatterns） ──

def _certain_patterns(hand: list[str], melds: list[dict], jokers: list[str]) -> set[str]:
    s = _shape(hand, melds, jokers)
    certain: set[str] = set()
    meld_tiles = s['meld_tiles']
    suits = {t[0] for t in s['suited']}
    honors = len(s['honors']) + sum(1 for t in meld_tiles if len(t) != 2)
    joker_count = s['joker_count']
    has_chi_meld = any(m.get('type') == 'chi' for m in melds)

    if honors == 0 and len(suits) <= 1 and len(s['suited']) + joker_count > 0:
        certain.add('pure-suit')
    if len(suits) <= 1 and honors > 0 and len(s['suited']) + joker_count > 0:
        certain.add('mixed-suit')

    if not has_chi_meld:
        joker_demand = 0
        pair_found = False
        for count in s['counts'].values():
            rest = count % 3
            if rest == 1:
                joker_demand += 2
            if rest == 2:
                pair_found = True
        if not pair_found:
            joker_demand += max(0, 2 - joker_count)
        if joker_demand <= joker_count:
            certain.add('all-triplets')

    concealed_units = sum(1 for m in melds if m.get('type') == 'angang')
    concealed_units += sum(1 for c in s['counts'].values() if c >= 3)
    if concealed_units >= 3:
        certain.add('three-concealed-triplets')
    if concealed_units >= 4:
        certain.add('four-concealed-triplets')

    triplets = _triplet_melds(melds)
    dragon_units = [t for t in DRAGONS if s['counts'].get(t, 0) >= 3 or any(m.get('tile') == t for m in triplets)]
    dragon_pairs = [t for t in DRAGONS if s['counts'].get(t, 0) == 2]
    if len(dragon_units) == 3:
        certain.add('big-three-dragons')
    elif len(dragon_units) == 2 and (dragon_pairs or joker_count > 0):
        certain.add('little-three-dragons')
    wind_units = [t for t in WINDS if s['counts'].get(t, 0) >= 3 or any(m.get('tile') == t for m in triplets)]
    wind_pairs = [t for t in WINDS if s['counts'].get(t, 0) == 2]
    if len(wind_units) == 4:
        certain.add('big-four-winds')
    elif len(wind_units) == 3 and (wind_pairs or joker_count > 0):
        certain.add('little-four-winds')

    if not melds and honors == 0 and len(suits) <= 1:
        need = (3, 1, 1, 1, 1, 1, 1, 1, 3)
        suit = next(iter(suits)) if suits else 'm'
        deficit = 0
        for rank in range(1, 10):
            deficit += max(0, need[rank - 1] - s['counts'].get(f'{suit}{rank}', 0))
        if deficit <= joker_count:
            certain.add('nine-gates')

    wild = wildcard_set(jokers)
    if all(t in GREEN_TILES or t in wild for t in [*hand, *meld_tiles]):
        certain.add('all-green')

    restricted = all(t in TERMINAL_TILES or len(t) != 2 or t in wild for t in [*hand, *meld_tiles])
    if restricted and honors == 0 and not has_chi_meld:
        certain.add('pure-terminals')
    if restricted and honors > 0 and len(s['suited']) + joker_count > 0 and not has_chi_meld:
        certain.add('mixed-terminals')

    if not s['suited'] and all(len(t) != 2 for t in meld_tiles):
        certain.add('all-honors')

    gangs = _declared_gang_count(melds)
    if gangs >= 3:
        certain.add('three-kongs')
    if gangs >= 4:
        certain.add('four-kongs')

    if _seven_pairs_potential(hand, list(wild)) >= 28:
        certain.add('sevenPairs')
    shi_san = _shi_san_lan_potential(hand, list(wild))
    honors_held = sum(1 for h in HONORS if h in s['natural'])
    if shi_san > 0 and _shi_san_lan_defect_free(hand, list(wild)):
        certain.add('qiXing' if (honors_held == 7 and joker_count == 0) else 'shiSanLan')
    orphans = _thirteen_orphans_potential(hand, list(wild))
    if orphans >= 17:
        certain.add('thirteenOrphans')

    for pattern_id in list(certain):
        for excluded in BLOOD_FLOW_CONFIG.patterns[pattern_id].excludes:
            certain.discard(excluded)
    return certain


def _shi_san_lan_defect_free(hand: list[str], jokers: list[str]) -> bool:
    joker_set = set(jokers)
    natural = [t for t in hand if t not in joker_set]
    defects = len(natural) - len(set(natural))
    for suit in ('m', 'p', 's'):
        ranks = sorted(int(t[1]) for t in natural if len(t) == 2 and t[0] == suit)
        for i in range(1, len(ranks)):
            if ranks[i] - ranks[i - 1] < 3:
                defects += 1
    honors_held = sum(1 for h in HONORS if h in natural)
    joker_count = len(hand) - len(natural)
    honor_shortfall = max(0, 7 - honors_held)
    jokers_after_honors = max(0, joker_count - honor_shortfall)
    return max(0, defects - jokers_after_honors) == 0


def estimate_win_income(hand: list[str], melds: list[dict], jokers: list[str], source: str) -> dict:
    patterns = _certain_patterns(hand, melds, jokers)
    multiplier = 1
    for pattern_id in patterns:
        multiplier += BLOOD_FLOW_CONFIG.patterns[pattern_id].weight - 1
    hard_likely = not any(t in wildcard_set(jokers) for t in hand)
    event_multiplier = BLOOD_FLOW_CONFIG.event_multipliers[source]
    final_multiplier = min(multiplier * event_multiplier * (2 if hard_likely else 1),
                           BLOOD_FLOW_CONFIG.max_multiplier_per_payer)
    payment_per_payer = BLOOD_FLOW_CONFIG.base_points * final_multiplier
    payers = 3 if source in ('self-draw', 'kong-bloom') else 1
    return {'paymentPerPayer': payment_per_payer, 'total': payment_per_payer * payers,
            'multiplier': final_multiplier, 'hardLikely': hard_likely}


# ── 连锁期望与听口缓存 ──

_waiting_cache: dict[str, list[str]] = {}


def waiting_tiles_cached(hand: list[str], exposed_melds: int, jokers: list[str]) -> list[str]:
    key = f"{exposed_melds}|{','.join(sorted(jokers))}|{','.join(sorted(hand))}"
    if key in _waiting_cache:
        return _waiting_cache[key]
    waits = list(lotus_waiting_tiles(list(hand), exposed_melds, list(jokers)))
    if len(_waiting_cache) >= 20_000:
        _waiting_cache.pop(next(iter(_waiting_cache)))
    _waiting_cache[key] = waits
    return waits


def _remaining_count(tile: str, visible: list[str]) -> int:
    return max(0, 4 - visible.count(tile))


def chain_ev_est(hand: list[str], melds: list[dict], jokers: list[str],
                 visible: list[str], wall_count: int) -> float:
    if not hand:
        return 0.0
    waits = waiting_tiles_cached(hand, len(melds), jokers)
    if not waits:
        return 0.0
    chain_factor = min(1.0, BLOOD_FLOW_AI.chain_horizon / max(1, wall_count / 4))
    total = 0.0
    for tile in waits:
        remaining = _remaining_count(tile, visible)
        if not remaining:
            continue
        self_income = estimate_win_income([*hand, tile], melds, jokers, 'self-draw')
        discard_income = estimate_win_income([*hand, tile], melds, jokers, 'discard')
        average = (BLOOD_FLOW_AI.self_draw_weight * self_income['total'] + discard_income['total']) \
            / (BLOOD_FLOW_AI.self_draw_weight + 1)
        total += remaining * average * chain_factor
    return total


def is_any_tile_wait(waits: list[str]) -> bool:
    return len(waits) >= len(TILE_TYPES)


# ── EV 上下文与决策 ──

def first_win_floor(wall_count: int, config: BloodFlowAiConfig = BLOOD_FLOW_AI) -> int:
    if wall_count <= config.late_game_wall_count:
        return config.first_win_floor_late
    if wall_count > config.early_game_wall_count:
        return config.first_win_floor_early
    return config.first_win_floor_mid


def _visible_tiles(view: dict) -> list[str]:
    tiles = [view.get('flipTile')]
    for p in view.get('players', []):
        tiles.extend(p.get('discards') or [])
        tiles.extend(t for m in (p.get('melds') or []) for t in m.get('tiles', []))
    tiles.extend(b['source']['tile'] for b in view.get('public', {}).get('batches', []))
    return [t for t in tiles if t]


def blood_flow_ev_context(view: dict, config: BloodFlowAiConfig = BLOOD_FLOW_AI) -> dict:
    seat = view['seat']
    player = view['players'][seat]
    locked = view['public']['seats'][seat]['locked']
    hand = player.get('hand') or []
    melds = player.get('melds') or []
    jokers = list(view.get('jokers') or [])
    visible = _visible_tiles(view)
    wall_count = view.get('wallCount', 0)
    drawn_index = player.get('drawnTileIndex', -1)
    window = view.get('window')
    own_actions = view.get('ownActions') or []
    win_offered = any(a['kind'] == 'win' for a in own_actions)
    score = view.get('ownScore')
    payers = 3 if score and score.get('source') in ('self-draw', 'kong-bloom') else 1
    immediate_total = (score or {}).get('paymentPerPayer', 0) * payers
    locked_hand = [t for i, t in enumerate(hand) if i != drawn_index] \
        if (window and window.get('kind') == 'turn' and drawn_index >= 0) else list(hand)
    chain_after_win = chain_ev_est(locked_hand, melds, jokers, visible, wall_count) if win_offered else 0.0
    win_ev = immediate_total + chain_after_win
    floor = first_win_floor(wall_count, config)
    floor_stage = 'late' if wall_count <= config.late_game_wall_count else \
        'early' if wall_count > config.early_game_wall_count else 'mid'
    potential_total = pattern_potential_total(locked_hand, melds, jokers)
    top_directions = sorted(pattern_potentials(locked_hand, melds, jokers),
                            key=lambda d: -d['score'])[:3]
    develop_ev = pattern_potential_ev(locked_hand, melds, jokers, wall_count)

    reform_candidates: list[dict] = []
    if not locked and window and window.get('kind') == 'turn' \
            and (window.get('source') or {}).get('kind') == 'draw' and drawn_index >= 0 and win_offered:
        for action in own_actions:
            if action['kind'] != 'discard' or action['index'] == drawn_index:
                continue
            after = [t for i, t in enumerate(hand) if i != action['index']]
            waits = waiting_tiles_cached(after, len(melds), jokers)
            if not waits:
                continue
            reform_candidates.append({
                'index': action['index'], 'tile': hand[action['index']],
                'ev': chain_ev_est(after, melds, jokers, visible, wall_count),
                'anyWait': len(waits) >= len(TILE_TYPES), 'waitCount': len(waits),
                'patterns': [BLOOD_FLOW_CONFIG.patterns[d['id']].label
                             for d in sorted(pattern_potentials(after, melds, jokers),
                                             key=lambda x: -x['score'])[:3]],
            })
        reform_candidates.sort(key=lambda c: -c['ev'])

    rob_ev = None
    if not locked and window and window.get('kind') != 'turn' \
            and (window.get('source') or {}).get('kind') == 'added-kong' and win_offered:
        kong_fee = BLOOD_FLOW_CONFIG.base_points * BLOOD_FLOW_CONFIG.kong_payments['added']
        rob_ev = {
            'winEv': win_ev,
            'passEv': -kong_fee + chain_ev_est(hand, melds, jokers, visible, wall_count)
            + pattern_potential_ev(hand, melds, jokers, wall_count),
        }

    return {
        'locked': locked, 'wallCount': wall_count, 'drawnIndex': drawn_index,
        'hand': hand, 'melds': melds, 'jokers': jokers, 'winOffered': win_offered,
        'immediateTotal': immediate_total, 'chainAfterWin': chain_after_win, 'winEv': win_ev,
        'floor': floor, 'floorStage': floor_stage, 'potentialTotal': potential_total,
        'topDirections': top_directions, 'developEv': develop_ev,
        'reformCandidates': reform_candidates, 'robEv': rob_ev,
    }


def _legal_actions(view: dict) -> list[dict]:
    actions = [dict(a) for a in (view.get('ownActions') or [])]
    if view['public']['seats'][view['seat']]['locked']:
        return actions
    protected = {*view.get('jokers', []), 'white'}
    hand = view['players'][view['seat']].get('hand') or []
    discards = [a for a in actions if a['kind'] == 'discard']
    ordinary = [a for a in discards if hand[a['index']] not in protected]
    allowed = ordinary or discards
    keys = {('discard', a['index']) for a in allowed}
    return [a for a in actions if a['kind'] != 'discard' or ('discard', a['index']) in keys]


def _fallback_discard(hand: list[str], jokers: list[str], discards: list[dict]) -> dict:
    protected = {*jokers, 'white'}
    ordinary = [a for a in discards if hand[a['index']] not in protected]
    return (ordinary or discards)[0] if (ordinary or discards) else {'kind': 'pass'}


def decide_blood_flow_action_ev(view: dict, config: BloodFlowAiConfig = BLOOD_FLOW_AI) -> Optional[dict]:
    moves = _legal_actions(view)
    if not moves:
        return None
    if len(moves) == 1:
        return moves[0]
    seat = view['seat']
    locked = view['public']['seats'][seat]['locked']
    win = next((a for a in moves if a['kind'] == 'win'), None)
    if locked:
        return win or next((a for a in moves if a['kind'] == 'pass'), None) or moves[0]

    hand = view['players'][seat].get('hand') or []
    melds = view['players'][seat].get('melds') or []
    jokers = list(view.get('jokers') or [])
    visible = _visible_tiles(view)
    discards = [a for a in moves if a['kind'] == 'discard']

    def offered(action: dict) -> Optional[dict]:
        return next((a for a in moves if a == action), None)

    def decide_discard() -> Optional[dict]:
        try:
            turn_view = {
                'hand': hand, 'melds': melds, 'exposedMelds': len(melds), 'jokers': jokers,
                'visibleTiles': visible, 'publicTiles': visible,
                'upperLastDiscard': None, 'earlyRound': False,
                'wallCount': view.get('wallCount', 0), 'kongBloom': False,
            }
            decision = lotus_decide_turn(turn_view, jokers)
            if decision['kind'] == 'discard':
                action = {'kind': 'discard', 'index': decision['handIndex']}
            elif decision['kind'] == 'added-kong':
                action = {'kind': 'added-kong', 'meldIndex': decision['meldIndex']}
            else:
                action = dict(decision)
            return offered(action) or _fallback_discard(hand, jokers, discards)
        except Exception:
            return _fallback_discard(hand, jokers, discards)

    def decide_claim_turn() -> Optional[dict]:
        pass_action = next((a for a in moves if a['kind'] == 'pass'), None)
        window = view.get('window') or {}
        source = window.get('source') or {}
        if source.get('kind') != 'discard':
            return pass_action
        try:
            claim_view = {
                'hand': hand, 'exposedMelds': len(melds), 'jokers': jokers,
                'tile': source['tile'], 'from': source.get('seat', 0),
                'canGang': any(a['kind'] == 'gang' for a in moves),
                'canPeng': any(a['kind'] == 'peng' for a in moves),
                'chiOptions': [a for a in moves if a['kind'] == 'chi'],
                'visibleTiles': visible, 'publicTiles': visible,
                'upperLastDiscard': None, 'earlyRound': False, 'wallCount': view.get('wallCount', 0),
            }
            decision = lotus_decide_claim(claim_view)
            if decision['kind'] == 'chi':
                action = {'kind': 'chi', 'tiles': decision['meld']['tiles']}
            else:
                action = dict(decision)
            return offered(action) or pass_action
        except Exception:
            return pass_action

    if win:
        ctx = blood_flow_ev_context(view, config)
        window = view.get('window') or {}
        source = window.get('source') or {}
        drawn_index = view['players'][seat].get('drawnTileIndex', -1)
        # 自摸窗口：改张优先于门槛，再决定胡或继续发育。
        if window.get('kind') == 'turn' and source.get('kind') == 'draw' and drawn_index >= 0:
            best = next((c for c in ctx['reformCandidates']
                         if any(d['index'] == c['index'] for d in discards)), None)
            if best and best['ev'] >= ctx['winEv'] * config.reform_gain_ratio:
                return next(d for d in discards if d['index'] == best['index']) or win
            below_floor = bool(view.get('ownScore')
                               and view['ownScore']['paymentPerPayer'] < ctx['floor'])
            if below_floor and ctx['potentialTotal'] >= config.potential_floor:
                return decide_discard() or next((a for a in moves if a['kind'] == 'pass'), None) or win
            return win
        # 抢杠窗口：胡 / 过的贪婪比较。
        if window.get('kind') != 'turn' and source.get('kind') == 'added-kong':
            rob = ctx['robEv']
            if rob and rob['winEv'] >= rob['passEv']:
                return win
            return next((a for a in moves if a['kind'] == 'pass'), None) or win
        # 点炮窗口：低于首胡门槛且手牌有潜力 → 不胡。
        if view.get('ownScore') and view['ownScore']['paymentPerPayer'] < ctx['floor'] \
                and ctx['potentialTotal'] >= config.potential_floor:
            return decide_claim_turn() or next((a for a in moves if a['kind'] == 'pass'), None) or win
        return win

    if discards:
        return decide_discard()
    return decide_claim_turn()
