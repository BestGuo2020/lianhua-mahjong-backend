"""幺筒 Agent 只在规则 AI 准备普通出牌时参与决策。"""

import json
from typing import NamedTuple

from loguru import logger

from app.core.yaotong.engine import discard_chicken_tier, preview_discard
from app.core.yaotong.hands import discard_waits
from app.core.yaotong.mode import action_protection
from app.core.yaotong.tiles import YAO, counts_of, tile_name
from app.core.yaotong.types import Command, GameState, Seat
from app.game.anime_characters import resolve_anime_character_id
from app.llm.client import request_llm_decision
from app.llm.config import LlmServerConfig
from app.llm.persona import avatar_url, default_nickname, display_name, provider_folder


_SYSTEM_PROMPT = (
    '你是幺筒麻将的牌手，本次只选一张合法弃牌。\n'
    '一筒（幺筒）作本牌时每张有10基础鸡分，也可当癞子替牌补对子、顺子或刻子。'
    '一万、八筒、一条（幺鸡）是固定鸡，只按本牌使用。\n'
    '候选的 chicken 给出鸡牌类型、弃牌档位和原始鸡分，普通牌为 null。'
    '幺筒分预冲、首冲、后冲、杠冲；固定鸡分首冲、跟冲、底鸡。'
    '鸡分按听牌和终局情况收赔，未听可能反赔，幺筒被碰、杠或胡走会涉及责任款。'
    'afterKong 表示杠后出牌，被胡会烧鸡。\n'
    'yaosKept 是弃牌后手里仍能作本牌或癞子的一筒张数。'
    'waits 已枚举本牌与癞子两种用法，普通点炮另看胡牌资格。remaining 是各听张的可见剩余张数上限，'
    'needsKeptYaoAsWildcard 表示这个听口必须用留在手里的一筒替牌。'
    '优先保住有活张的听口；没听也要考虑留一筒补牌形的价值，别只为了冲鸡分丢掉一筒。'
    '参考其他家的牌河和报听状态。engineSuggestion 是规则 AI 建议，可选其他候选。\n'
    'message 写一句最多16字、符合 style 的中文牌桌台词。可以闲聊，也可以说自己这次实际打出的'
    '牌及其档位。冲鸡时优先回应当前档位，例如「幺筒首冲！」或「八筒跟冲！」。'
    '只说公开事实，别报暗手、听口、选牌理由、候选编号或结算分，别把未发生的碰杠胡说成已发生。'
    '只输出 JSON {"choice":"候选编号","message":"一句台词"}。'
)


class YaotongDiscardDecision(NamedTuple):
    command: Command
    message: str


class YaotongDiscardAgent:
    def __init__(self, config: LlmServerConfig, *, nickname: str = '', avatar_folder: str = ''):
        self.config = config
        self.requests = 0
        folder = provider_folder(config.base_url, avatar_folder, config.provider_id,
                                 config.model, config.provider_type)
        nickname = nickname or default_nickname(config.base_url, provider_id=config.provider_id)
        self.nickname = display_name(nickname, config.style)
        self.avatar = avatar_url(config.base_url, config.style, folder, config.provider_id)
        self.character_id = resolve_anime_character_id(provider_id=config.provider_type,
                                                       avatar_folder=folder)

    async def choose_discard(self, state: GameState, seat: Seat,
                             rule_command: Command) -> YaotongDiscardDecision | None:
        if (rule_command.type != 'discard' or rule_command.declare
                or rule_command.index is None or state.phase != 'discard'
                or state.current != seat):
            return None
        player = state.players[seat]
        if player.declaration != 'none':
            return None
        budget = self.config.max_requests_per_room
        if budget > 0 and self.requests >= budget:
            return None

        blocked = set(action_protection(state, seat)['discards'])
        by_tile = {}
        for index, tile in enumerate(player.hand):
            if index not in blocked:
                by_tile.setdefault(tile, index)
        if len(by_tile) <= 1 or rule_command.index in blocked:
            return None
        suggested = player.hand[rule_command.index]
        by_tile[suggested] = rule_command.index

        visible_counts = counts_of([
            *player.hand,
            *(discard.tile for other in state.players for discard in other.river),
            *(meld.tile for other in state.players for meld in other.melds
              for _ in range(3 if meld.kind == 'peng' else 4)),
        ])
        waits = {entry['index']: entry['waits'] for entry in discard_waits(player)}
        yao_count = player.hand.count(YAO)
        indexes = {}
        candidates = []
        for number, (tile, index) in enumerate(by_tile.items(), start=1):
            candidate_id = f'A{number}'
            indexes[candidate_id] = index
            discard = preview_discard(state, seat, tile)
            tier = discard_chicken_tier(discard)
            candidates.append({
                'id': candidate_id,
                'tile': tile_name(tile),
                'chicken': {
                    'kind': '幺筒' if tile == YAO else '固定鸡',
                    'tier': tier,
                    'points': discard.chicken,
                } if tier else None,
                'yaosKept': yao_count - int(tile == YAO),
                'waits': [
                    {'tile': tile_name(wait.tile),
                     'remaining': max(0, 4 - visible_counts[wait.tile]),
                     'needsKeptYaoAsWildcard': all(
                         value.wildcards > int(wait.tile == YAO and not value.winning_yao_natural)
                         for value in wait.values
                     )}
                    for wait in waits.get(index, ())
                ],
            })
        payload = {
            'seat': seat,
            'style': self.config.style,
            'hand': [tile_name(tile) for tile in player.hand],
            'wallCount': len(state.wall),
            'afterKong': player.after_kong,
            'table': [
                {'seat': other_seat, 'score': other.score, 'declaration': other.declaration,
                 'river': [tile_name(discard.tile) for discard in other.river],
                 'melds': [{'tile': tile_name(meld.tile), 'kind': meld.kind}
                           for meld in other.melds]}
                for other_seat, other in enumerate(state.players)
            ],
            'candidates': candidates,
            'engineSuggestion': next(key for key, index in indexes.items()
                                     if index == rule_command.index),
        }
        self.requests += 1
        try:
            choice, message = await request_llm_decision(
                self.config, _SYSTEM_PROMPT, json.dumps(payload, ensure_ascii=False),
                list(indexes), retry_parse=False,
            )
        except Exception as exc:
            logger.bind(error=type(exc).__name__).info('幺筒 Agent 请求失败，使用规则 AI')
            return None
        if not isinstance(choice, str) or choice not in indexes:
            logger.info('幺筒 Agent 候选编号无效，使用规则 AI')
            return None
        return YaotongDiscardDecision(
            Command(type='discard', index=indexes[choice]),
            message if isinstance(message, str) else '',
        )
