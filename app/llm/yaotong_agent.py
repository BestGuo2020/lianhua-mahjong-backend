"""幺筒 Agent 只在规则 AI 准备普通出牌时参与决策。"""

import json
import re
from typing import NamedTuple

from loguru import logger

from app.core.yaotong.hands import discard_waits
from app.core.yaotong.mode import action_protection
from app.core.yaotong.tiles import counts_of, is_chicken, tile_name
from app.core.yaotong.types import Command, GameState, Seat
from app.game.anime_characters import resolve_anime_character_id
from app.llm.client import request_llm_decision
from app.llm.config import LlmServerConfig
from app.llm.decision_speech import resolve_decision_speech
from app.llm.persona import avatar_url, default_nickname, display_name, provider_folder


_PRIVATE_COMMENTARY = re.compile(
    r'暗手|手牌|手[里中上]|听口|听牌|向听|差.{0,3}张|'
    r'[一二三四五六七八九两1-9][万筒条索]|[幺么][筒鸡]|[mps][1-9]', re.I,
)
_SYSTEM_PROMPT = (
    '你是幺筒麻将的牌手，本次只选一张合法弃牌。'
    '一筒可以作本牌或癞子。固定鸡、幺筒及杠后出牌会影响分数和责任款。'
    '参考听口、可见剩余张数、其他家的牌河和报听状态。'
    '可参考 style 中的打法风格。'
    'engineSuggestion 是规则机器人的建议，模型可以选择其他候选。'
    'message 写一句最多16字、符合性格的中文牌桌闲聊，只谈公开局势。'
    '台词不要报具体牌名、手牌、听口、出牌理由或候选编号。'
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
        indexes = {}
        candidates = []
        for number, (tile, index) in enumerate(by_tile.items(), start=1):
            candidate_id = f'A{number}'
            indexes[candidate_id] = index
            candidates.append({
                'id': candidate_id,
                'tile': tile_name(tile),
                'chicken': is_chicken(tile),
                'waits': [
                    {'tile': tile_name(wait.tile),
                     'remaining': max(0, 4 - visible_counts[wait.tile])}
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
        message = message if isinstance(message, str) and not _PRIVATE_COMMENTARY.search(message) else ''
        speech = resolve_decision_speech(
            message, {'kind': 'discard'}, self.config.style, self.requests - 1,
            {'isDealer': seat == state.dealer},
        )
        return YaotongDiscardDecision(Command(type='discard', index=indexes[choice]), speech)
