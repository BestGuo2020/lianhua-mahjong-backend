"""AI 决策层 —— 从 src/game/ai.ts 翻译

纯决策：只负责「看状态 → 给出动作命令」，不修改任何游戏状态、
不触发表现副作用。动作的「执行」由 actions.py / GameManager 完成。

优先级链（与原 playAI 一致）：自摸胡 → 补杠 → 暗杠 → 弃牌。
"""

import random as _random
import re
from typing import Optional

from app.models.game import GamePlayer, Meld, TileType
from app.core.rules import concealed_kongs, is_winning_hand, matching_count

# ─── 决策类型（对应 TS 端联合类型，用 dict 表示）────────────────

TurnDecision = dict
ClaimDecision = str
RobKongDecision = str

_SUITED_RE = re.compile(r'^([mps])([1-9])$')


def decide_turn(view: dict) -> dict:
    """决策当前 AI 回合的动作。

    view: {hand, melds, exposedMelds, kongBloom}（见 make_turn_view）
    优先级：自摸胡 → 补杠 → 暗杠 → 弃牌。
    """
    if is_winning_hand(view['hand'], view['exposedMelds']):
        return {'kind': 'win'}

    meld_index = -1
    for i, meld in enumerate(view['melds']):
        if meld.type == 'peng' and meld.tile in view['hand']:
            meld_index = i
            break
    if meld_index >= 0:
        return {'kind': 'added-kong', 'meldIndex': meld_index}

    kongs = concealed_kongs(view['hand'])
    if kongs:
        return {'kind': 'concealed-kong', 'tile': kongs[0]}

    return {'kind': 'discard', 'handIndex': choose_discard_index(view['hand'])}


def decide_claim(view: dict) -> str:
    """面对弃牌：能杠必杠，否则能碰必碰（与原 aiClaim 行为一致）。"""
    if view['canGang']:
        return 'gang'
    return 'peng'


def decide_rob_kong(_view: dict) -> str:
    """面对加杠：当前 AI 能抢必抢；未来可按听牌风险权衡后返回 'pass'。"""
    return 'win'


def choose_discard_index(hand: list[TileType], random=None) -> int:
    """弃牌启发式：优先打掉「孤张」——同牌少、无相邻靠张的牌；
    白板（癞子）加罚分保手。random 注入以便测试确定化。

    分数 = 同牌数×4 + 相邻靠张数×2 + 白板罚分10 + 随机抖动，取最小者。
    """
    _rand = random if random is not None else _random.random
    scored = []
    for index, tile in enumerate(hand):
        same = matching_count(hand, tile) - 1
        neighbors = 0
        match = _SUITED_RE.match(tile)
        if match:
            suit, rank_s = match.groups()
            number = int(rank_s)
            if f'{suit}{number - 1}' in hand:
                neighbors += 1
            if f'{suit}{number + 1}' in hand:
                neighbors += 1
        penalty = 10 if tile == 'white' else 0
        scored.append((same * 4 + neighbors * 2 + penalty + _rand(), index))
    # 稳定排序：同分保持原手牌顺序（与 TS 端 Array.sort 一致）
    scored.sort(key=lambda pair: pair[0])
    return scored[0][1] if scored else 0


def make_turn_view(player: GamePlayer, exposed_melds: int, kong_bloom: bool) -> dict:
    """构造 AI 回合决策快照，只暴露决策需要的只读信息。"""
    return {
        'hand': player.hand,
        'melds': player.melds,
        'exposedMelds': exposed_melds,
        'kongBloom': kong_bloom,
    }
