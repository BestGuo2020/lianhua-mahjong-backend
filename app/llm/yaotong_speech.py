"""从已经公开的幺筒动作校验或生成牌桌短句。"""

import re
from unicodedata import normalize

from app.core.yaotong.engine import discard_chicken_tier
from app.core.yaotong.tiles import YAO, is_chicken, tile_name
from app.core.yaotong.types import Discard, GameEvent, GameState
from app.llm.decision_speech import resolve_decision_speech
from app.llm.speech_policy import compact_speech_text


_TILE_WORDS = r'[一二三四五六七八九两1-9][万筒条索]|[幺么][筒鸡]'
_TILE_MENTION = re.compile(_TILE_WORDS)
_TILE_ALIASES = {'幺筒': '一筒', '么筒': '一筒', '幺鸡': '一条', '么鸡': '一条'}
_TILE_TRANSLATION = str.maketrans('123456789两索', '一二三四五六七八九二条')
_CHICKEN_WORDS = '预冲|首冲|后冲|杠冲|跟冲|底鸡|冲锋鸡|冲鸡|固定鸡|鸡牌'
_CHICKEN_CLAIM = re.compile(_CHICKEN_WORDS)
_GENERIC_RUSH = re.compile(r'(?:这张|这牌)(?:先|来)?冲|冲这张')
_UNSUPPORTED_CLAIM = re.compile(r'上家|对家|下家|别人|你|他|不是|并非|没|不算|不能|别')

# 报出实际牌名时，只允许把它用在本次出牌的短句中。
_NAMED_DISCARD = re.compile(
    rf'(?:我)?(?:这张|先打(?:张)?|打(?:出|了|张)?|丢(?:张)?|出(?:张)?|{_CHICKEN_WORDS}|冲)?'
    rf'(?:{_TILE_WORDS})(?:先走|走|先打|不要了|{_CHICKEN_WORDS}|(?:先|来)?冲)?'
    r'(?:了|吧|啦|呀|咯|喽)?',
)
_PRIVATE_COMMENTARY = re.compile(
    r'暗手|手牌|手[里中上头]|在手|听口|听牌|向听|单吊|对倒|两面|我听了|差.{0,3}张|'
    r'还[有剩留]|(?:有|拿|藏|留|剩)(?:了|着)?[一二三四五六七八九两几0-9][张对组]|'
    r'[癞赖]子|万能牌|[mps][1-9]', re.I,
)
_UNCONFIRMED_RESULT = re.compile(
    r'责任鸡|责任款|碰鸡|首碰|后碰|跟庄|烧鸡|翻[鸡中]|见面礼|热炮|[点放接]炮|包牌|'
    r'碰了|杠了|胡了|自摸|抢杠|杀报|原报|报听|到手|赔|[一二三四五六七八九十百两几0-9]分',
)


def _matches_discard(text: str, tile: str, tier: str | None) -> bool:
    checked = ''.join(normalize('NFKC', text).split())
    if _PRIVATE_COMMENTARY.search(checked) or _UNCONFIRMED_RESULT.search(checked):
        return False
    allowed = {tier, '鸡牌'} if tier else set()
    if tier and tile != tile_name(YAO):
        allowed.add('固定鸡')
    if tier and tier != '底鸡':
        allowed.update(('冲鸡', '冲锋鸡'))

    for phrase in re.split(r'[，,、：:；;。!?…]', checked):
        named = _TILE_MENTION.search(phrase)
        if named:
            spoken = named.group()
            if (not _NAMED_DISCARD.fullmatch(phrase)
                    or _TILE_ALIASES.get(spoken, spoken.translate(_TILE_TRANSLATION)) != tile):
                return False
        claims = set(_CHICKEN_CLAIM.findall(phrase))
        if not claims and ((named and '冲' in phrase) or _GENERIC_RUSH.search(phrase)):
            claims.add('冲鸡')
        if claims and (not claims <= allowed or _UNSUPPORTED_CLAIM.search(phrase)):
            return False
    return True


def discard_speech(message: str, discard: Discard, style: str, sequence: int,
                   *, is_dealer: bool) -> str:
    tile = tile_name(discard.tile)
    tier = discard_chicken_tier(discard)
    text = compact_speech_text(message)
    if not _matches_discard(text, tile, tier):
        text = ''
    if not text and tier and tier != '底鸡':
        text = {
            '激进': f'{tier}，走起！',
            '稳健': f'{tile}{tier}。',
            '话痨': f'{tier}，这张我先打啦！',
            '高冷': f'{tier}。',
        }.get(style, f'{tier}。')
    return resolve_decision_speech(
        text, {'kind': 'discard'}, style, sequence, {'isDealer': is_dealer, 'discardedTile': tile},
    )


def chicken_claim_speech(state: GameState, event: GameEvent, action_kind: str, style: str) -> str | None:
    if action_kind not in ('peng', 'gang') or event.tile is None or not is_chicken(event.tile):
        return None
    kind, action = ('peng', '碰') if action_kind == 'peng' else ('open', '杠')
    meld = next((
        meld for meld in state.players[event.seat].melds
        if meld.tile == event.tile and meld.kind == kind and meld.from_seat not in (None, event.seat)
    ), None)
    if meld is None:
        return None
    if meld.tile == YAO and meld.responsibility > 0:
        topic = '责任鸡'
    elif meld.tile != YAO and kind == 'peng' and meld.chicken in (8, 6):
        topic = '首碰' if meld.chicken == 8 else '碰鸡'
    else:
        return None
    return {
        '激进': f'{action}，{topic}拿下！',
        '稳健': f'{action}，这手是{topic}。',
        '话痨': f'这张我{action}啦，{topic}！',
        '高冷': f'{action}，{topic}。',
    }.get(style, f'{action}，{topic}。')
