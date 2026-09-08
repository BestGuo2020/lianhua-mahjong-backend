"""血流 LLM 候选构建与校验 —— 对齐前端 bloodFlowDecisionInput.ts（M4）。

输入：血流引擎视图（与前端 BloodFlowSeatView 同构的 dict，含 ownActions/ownScore/
players/seat/jokers/public/window）。输出候选列表（id/label/action/features/summary/
legalityKey）与 engineSuggestion（默认推荐）。

EV 特征（features['ev']）由 app.core.blood_flow.ai 的 blood_flow_ev_context 注入；
本模块不依赖它也能产出基础特征（胡收益、锁手风险、听口、杠分）。
"""

from typing import Optional

from app.core.blood_flow.config import BLOOD_FLOW_CONFIG
from app.core.lotus_rules import chi_options as lotus_chi_options
from app.core.lotus_rules import waiting_tiles as lotus_waiting_tiles


def _label(action: dict, hand: list[str], melds: list[dict]) -> str:
    kind = action['kind']
    if kind == 'discard':
        return f"打出{_tile_name(hand[action['index']])}"
    if kind == 'chi':
        return f"吃{''.join(_tile_name(t) for t in action['tiles'])}"
    if kind == 'concealed-kong':
        return f"暗杠{_tile_name(action['tile'])}"
    if kind == 'added-kong':
        return f"补杠{_tile_name(melds[action['meldIndex']]['tile'])}"
    return {'win': '胡牌（首次胡后锁手）', 'pass': '过', 'peng': '碰', 'gang': '直杠',
            'wind-kong': '风杠'}[kind]


def _tile_name(tile: str) -> str:
    from app.llm.schema import tile_name
    return tile_name(tile)


def protected_discards(view: dict) -> set[str]:
    """血流 AI 候选保护：癞子与白板默认不打（与前端 lotusDiscardCandidates 同口径）。"""
    return {*view.get('jokers', []), 'white'}


def candidate_actions(view: dict) -> list[dict]:
    """候选动作：锁手不动；未锁手时过滤受保护弃牌（全保护时兜底保留）。"""
    actions = [dict(a) for a in (view.get('ownActions') or [])]
    if view['public']['seats'][view['seat']]['locked']:
        return actions
    protected = protected_discards(view)
    hand = view['players'][view['seat']]['hand']
    discards = [a for a in actions if a['kind'] == 'discard']
    ordinary = [a for a in discards if hand[a['index']] not in protected]
    allowed_discards = ordinary or discards
    allowed = {('discard', a['index']) for a in allowed_discards}
    return [a for a in actions if a['kind'] != 'discard' or ('discard', a['index']) in allowed]


def build_blood_flow_candidates(view: dict, request_id: str,
                                suggestion: Optional[dict] = None,
                                ev_by_key: Optional[dict] = None) -> dict:
    """构建候选。suggestion = decide_blood_flow_action_ev 的结果（None 则退化为首个候选）。

    ev_by_key: {'win': {...}, 'discard:3': {'reform': {...}}, 'pass': {...}, ...} 由
    blood_flow_ev_context 派生，注入 features['ev']。
    """
    actions = candidate_actions(view)
    seat = view['seat']
    hand = view['players'][seat]['hand']
    melds = view['players'][seat]['melds']
    jokers = view.get('jokers', [])
    window = view.get('window')
    own_score = view.get('ownScore')
    chi_actions = [a for a in actions if a['kind'] == 'chi']
    candidates: list[dict] = []
    for index, action in enumerate(actions):
        features: dict = _base_features(view, action)
        if action['kind'] == 'win' and own_score:
            payers = 3 if own_score.get('source') in ('self-draw', 'kong-bloom') else 1
            score_delta = own_score['paymentPerPayer'] * payers
            features['scoreDelta'] = score_delta
            features['scoreDeltaBand'] = '高' if score_delta >= 400 else ('中' if score_delta > 0 else 'n/a')
            features['specialPattern'] = '、'.join(p['label'] for p in own_score.get('items', [])) or 'n/a'
            if not view['public']['seats'][seat]['locked']:
                features.setdefault('risks', []).append(
                    '首次胡后锁手，不能再改手或吃碰杠；比较当前收益和后续听口')
        if action['kind'] == 'discard':
            after = [t for i, t in enumerate(hand) if i != action['index']]
            waits = lotus_waiting_tiles(after, len(melds), jokers)
            if waits:
                features['ready'] = True
                visible = _visible_tiles(view)
                features['waits'] = [{'tile': _tile_name(t),
                                      'remaining': max(0, 4 - visible.count(t))} for t in waits]
                features['effectiveRemaining'] = sum(w['remaining'] for w in features['waits'])
                features['shanten'] = 0
        key = _legality_key(action, chi_actions)
        if ev_by_key and key in ev_by_key:
            features['ev'] = ev_by_key[key]
        candidates.append({
            'id': f'A{index + 1}', 'label': _label(action, hand, melds), 'action': action,
            'features': features, 'legalityKey': key,
        })
    suggestion_id: Optional[str] = None
    if suggestion is not None:
        suggestion_key = _legality_key(suggestion, chi_actions)
        match = next((c for c in candidates if c['legalityKey'] == suggestion_key), None)
        if match is not None:
            suggestion_id = match['id']
    return {
        'requestId': request_id, 'window': window,
        'candidates': candidates,
        'engineSuggestion': suggestion_id or (candidates[0]['id'] if candidates else None),
        'ruleVersion': BLOOD_FLOW_CONFIG.version,
    }


def _legality_key(action: dict, chi_actions: list[dict]) -> str:
    kind = action['kind']
    if kind == 'discard':
        return f"discard:{action['index']}"
    if kind == 'chi':
        return f"chi:{chi_actions.index(action)}"
    if kind == 'added-kong':
        return f"added-kong:{action['meldIndex']}"
    if kind == 'concealed-kong':
        return f"concealed-kong:{action['tile']}"
    return kind


def _visible_tiles(view: dict) -> list[str]:
    tiles = [view.get('flipTile')]
    for p in view['players']:
        tiles.extend(p.get('discards') or [])
        tiles.extend(t for m in (p.get('melds') or []) for t in m['tiles'])
    tiles.extend(b['source']['tile'] for b in view['public'].get('batches', []))
    return [t for t in tiles if t]


def _base_features(view: dict, action: dict) -> dict:
    seat = view['seat']
    hand = view['players'][seat]['hand']
    features = {
        'shanten': 'n/a', 'ukeire': 'n/a', 'effectiveTiles': 'n/a', 'ready': 'unknown',
        'waits': 'n/a', 'effectiveRemaining': 'n/a', 'specialPattern': 'n/a',
        'safety': 'unknown', 'efficiency': 'unknown', 'risks': [],
    }
    if action['kind'] in ('gang', 'concealed-kong', 'added-kong', 'wind-kong'):
        kind = 'wind' if action['kind'] == 'wind-kong' else \
            'concealed' if action['kind'] == 'concealed-kong' else \
            'added' if action['kind'] == 'added-kong' else 'discard'
        features['scoreDelta'] = (BLOOD_FLOW_CONFIG.base_points
                                  * BLOOD_FLOW_CONFIG.kong_payments[kind]
                                  * (1 if kind == 'discard' else 3))
        features['scoreDeltaBand'] = '高' if features['scoreDelta'] >= 400 else '中'
    return features


def validate_blood_flow_action(view: dict, action: dict) -> bool:
    """模型输出动作必须落在当前窗口的合法候选中（执行前复核；与引擎同口径的整值相等）。"""
    return any(a == action for a in candidate_actions(view))


def blood_flow_prompt_rules() -> str:
    return ('莲花麻将血流：沿用翻精、白板受限替代、数牌吃和字牌顺；支持平胡、七对、十三幺、十三烂、'
            '七星十三烂及清一色、混一色、碰碰胡、大小三元、大小四喜、九莲宝灯、绿一色、清幺九、混幺九、'
            '三暗刻、四暗刻、字一色、三杠、四杠。自然成立硬胡×2；真实倍率、封顶和收益以 currentWin 为准。'
            '可点炮、多响和抢补杠，胡后继续；首次胡锁手，之后只能处理新摸牌，已胡仍付款；牌墙耗尽才结算。'
            '候选 features.ev 为本地期望收益估算（自摸按 2 倍×3 家、锁手连锁、首胡门槛、改张/单吊任意听、'
            '抢杠两值），仅作依据；早局低番胡会锁手，可结合潜力考虑改张或过。')
