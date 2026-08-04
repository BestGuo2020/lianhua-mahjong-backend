"""AI 决策层单元测试 —— 逐条对照 src/game/ai.test.ts 翻译"""

from app.core.ai import (
    choose_discard_index,
    decide_claim,
    decide_rob_kong,
    decide_turn,
    make_turn_view,
)
from app.models.game import GamePlayer, Meld


def view(hand, melds=None, exposed_melds=0, kong_bloom=False) -> dict:
    return {'hand': hand, 'melds': melds or [], 'exposedMelds': exposed_melds, 'kongBloom': kong_bloom}


class TestDecideTurnWin:
    """对应 ai.test.ts 'decideTurn 自摸胡'"""

    def test_winning_hand_returns_win(self):
        """牌型可胡时返回 win"""
        hand = ['m1', 'm1', 'm1', 'm2', 'm3', 'm4', 'p4', 'p5', 'p6', 's7', 's7', 's7', 'east', 'east']
        assert decide_turn(view(hand)) == {'kind': 'win'}

    def test_white_joker_as_any_tile_for_win(self):
        """白板（癞子）可当任意牌参与胡牌"""
        hand = ['m1', 'm1', 'm1', 'm2', 'm3', 'white', 'p4', 'p5', 'p6', 's7', 's7', 's7', 'east', 'east']
        assert decide_turn(view(hand)) == {'kind': 'win'}


class TestDecideTurnAddedKong:
    """对应 ai.test.ts 'decideTurn 补杠'"""

    def test_peng_with_fourth_in_hand_returns_added_kong(self):
        """已碰且有第四张在手时返回 added-kong"""
        melds = [Meld(type='peng', tile='east', from_=1, tiles=['east', 'east', 'east'])]
        hand = ['east', 'm1', 'm2']
        assert decide_turn(view(hand, melds, 1)) == {'kind': 'added-kong', 'meldIndex': 0}


class TestDecideTurnConcealedKong:
    """对应 ai.test.ts 'decideTurn 暗杠'"""

    def test_four_of_kind_returns_concealed_kong(self):
        """手牌有 4 张相同牌时返回 concealed-kong"""
        hand = ['s7', 's7', 's7', 's7', 'm1', 'm2', 'm3', 'p4', 'p5', 'east', 'east']
        assert decide_turn(view(hand)) == {'kind': 'concealed-kong', 'tile': 's7'}


class TestDecideTurnDiscard:
    """对应 ai.test.ts 'decideTurn 弃牌'"""

    def test_discard_when_no_win_or_kong(self):
        """无胡/无杠时返回 discard 且索引在合法范围"""
        hand = ['m1', 'p4', 'p5', 'p6', 'east', 's2', 's2', 's9', 's9', 'white', 'white']
        decision = decide_turn(view(hand))
        assert decision['kind'] == 'discard'
        assert 0 <= decision['handIndex'] < len(hand)


class TestChooseDiscardIndex:
    """对应 ai.test.ts 'chooseDiscardIndex 弃牌启发式'"""

    def test_discard_lone_tile_first(self):
        """优先打掉无对无靠的孤张"""
        hand = ['m1', 'm2', 'm3', 'p5', 'p5', 'east']
        index = choose_discard_index(hand, lambda: 0)
        assert hand[index] == 'east'

    def test_white_joker_kept(self):
        """癞子白板保手，优先打其它孤张"""
        hand = ['white', 's9', 's9', 'm7']
        index = choose_discard_index(hand, lambda: 0)
        assert hand[index] == 'm7'

    def test_pair_and_neighbors_discarded_last(self):
        """对子与靠张越多越靠后打"""
        hand = ['m1', 'm2', 'm3', 'p5', 'p5', 'north']
        index = choose_discard_index(hand, lambda: 0)
        assert hand[index] == 'north'


class TestDecideClaim:
    """对应 ai.test.ts 'decideClaim 吃碰杠响应'"""

    def test_gang_when_can_gang(self):
        assert decide_claim({'hand': ['east', 'east', 'east', 'm1'], 'canGang': True}) == 'gang'

    def test_peng_when_not_can_gang(self):
        assert decide_claim({'hand': ['east', 'east', 'm1'], 'canGang': False}) == 'peng'


class TestDecideRobKong:
    """对应 ai.test.ts 'decideRobKong 抢杠'"""

    def test_rob_kong_always_win(self):
        """当前 AI 能抢必抢"""
        assert decide_rob_kong({'hand': ['east', 'east', 'm1', 'm2'], 'exposedMelds': 1, 'tile': 'east', 'from': 2}) == 'win'


class TestMakeTurnView:
    """对应 ai.test.ts 'makeTurnView 快照构造'"""

    def test_only_exposes_hand_and_melds(self):
        """只暴露手牌与副露，不含分数等无关字段"""
        player = GamePlayer(
            name='AI', avatar='', score=1000, seat=1,
            hand=['m1', 'm2'], discards=[], melds=[], redCount=0, drawnTileIndex=-1,
        )
        assert make_turn_view(player, 0, True) == {
            'hand': ['m1', 'm2'],
            'melds': [],
            'exposedMelds': 0,
            'kongBloom': True,
        }
