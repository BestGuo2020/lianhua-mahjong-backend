"""REST API 集成测试 —— 房间生命周期 + 战绩落库

对应开发计划 Phase 6 验收：
- 房间生命周期完整：创建 → 加入 → 准备 → 开局 → 结算 → 战绩落库
- 对局结束后可查询历史战绩与牌谱
- 并发创建/加入房间无竞态（房间 ID 唯一，座位互斥）

storage 用临时 SQLite 库（monkeypatch），避免污染默认库文件。
start 走 REST（uvicorn 事件循环），对局 AI 补位自动打完。
"""

import asyncio
import time

import httpx
import pytest

from app.game.room import room_registry as rooms


@pytest.fixture()
def temp_storage(tmp_path, monkeypatch):
    """临时 SQLite 库，替换 api 层全局 storage（写盘隔离）。"""
    from app.storage.db import Storage
    s = Storage(str(tmp_path / 'test.db'))
    s.init()
    import app.api.rooms as rooms_api
    import app.api.matches as matches_api
    import app.api.moderation as moderation_api
    monkeypatch.setattr(rooms_api, 'storage', s)
    monkeypatch.setattr(matches_api, 'storage', s)
    monkeypatch.setattr(moderation_api, 'storage', s)
    return s


async def wait_until(cond, timeout=30.0, interval=0.05) -> None:
    """轮询等待条件成立（跨线程读 room 状态时用）。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cond():
            return
        await asyncio.sleep(interval)
    raise AssertionError(f'等待超时（{timeout}s）: 条件未成立')


# ─── 创建 / 查询 ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_create_and_get_room(server, fresh_rooms, temp_storage):
    async with httpx.AsyncClient(base_url=server['http']) as http:
        resp = await http.post('/api/rooms', json={'mode': 'east', 'capacity': 4})
        assert resp.status_code == 200
        data = resp.json()
        room_id = data['roomId']
        assert len(room_id) == 6
        assert data['mode'] == 'east'
        assert data['capacity'] == 4
        assert data['status'] == 'lobby'
        assert data['seats'] == [None, None, None, None]  # 4 个空座

        # GET 房间信息
        resp = await http.get(f'/api/rooms/{room_id}')
        assert resp.status_code == 200
        assert resp.json()['roomId'] == room_id

        # 未知房间 → 404
        resp = await http.get('/api/rooms/ZZZZZZ')
        assert resp.status_code == 404
        assert resp.json()['detail']['code'] == 'ROOM_NOT_FOUND'


@pytest.mark.asyncio
async def test_room_count_limit(server, fresh_rooms, temp_storage):
    """本服务器最多 4 个房间：第 5 个创建 → 409 ROOM_LIMIT_REACHED。"""
    async with httpx.AsyncClient(base_url=server['http']) as http:
        created = []
        for _ in range(4):
            resp = await http.post('/api/rooms', json={'mode': 'east', 'capacity': 2})
            assert resp.status_code == 200, resp.text
            created.append(resp.json()['roomId'])
        assert len(set(created)) == 4

        # 第 5 个 → 房间已满
        resp = await http.post('/api/rooms', json={'mode': 'east', 'capacity': 2})
        assert resp.status_code == 409
        assert resp.json()['detail']['code'] == 'ROOM_LIMIT_REACHED'


@pytest.mark.asyncio
async def test_room_meta_count(server, fresh_rooms, temp_storage):
    """GET /api/rooms/meta 返回在册房间数与上限，随建房递增（大厅「剩余房间」数据源）。"""
    async with httpx.AsyncClient(base_url=server['http']) as http:
        resp = await http.get('/api/rooms/meta')
        assert resp.status_code == 200
        assert resp.json() == {'active': 0, 'max': 4}

        for _ in range(2):
            resp = await http.post('/api/rooms', json={'mode': 'east', 'capacity': 2})
            assert resp.status_code == 200, resp.text
        resp = await http.get('/api/rooms/meta')
        assert resp.json() == {'active': 2, 'max': 4}


@pytest.mark.asyncio
async def test_create_rejected_when_player_in_room(server, fresh_rooms, temp_storage):
    """已在房间占座的玩家（guestId）再创建房间 → 409 ALREADY_IN_ROOM；离房后可再建。"""
    async with httpx.AsyncClient(base_url=server['http']) as http:
        room1 = (await http.post('/api/rooms', json={'mode': 'east', 'capacity': 4})).json()['roomId']
        join = (await http.post(f'/api/rooms/{room1}/join',
                                json={'nickname': '甲', 'playerId': 'guest-A'})).json()

        # 同一 guestId 已在 room1 占座 → 再建新房被拒
        resp = await http.post('/api/rooms', json={'mode': 'east', 'capacity': 4, 'playerId': 'guest-A'})
        assert resp.status_code == 409
        assert resp.json()['detail']['code'] == 'ALREADY_IN_ROOM'

        # 未占座的玩家可正常创建
        resp = await http.post('/api/rooms', json={'mode': 'east', 'capacity': 4, 'playerId': 'guest-B'})
        assert resp.status_code == 200

        # 离房后可再创建
        await http.post(f'/api/rooms/{room1}/leave',
                        json={'seat': join['seat'], 'rejoinCode': join['rejoinCode']})
        resp = await http.post('/api/rooms', json={'mode': 'east', 'capacity': 4, 'playerId': 'guest-A'})
        assert resp.status_code == 200, resp.text


@pytest.mark.asyncio
async def test_join_rejected_when_player_in_another_room(server, fresh_rooms, temp_storage):
    """已在房间占座的玩家加入另一个房间 → 409 ALREADY_IN_ROOM。"""
    async with httpx.AsyncClient(base_url=server['http']) as http:
        room1 = (await http.post('/api/rooms', json={'mode': 'east', 'capacity': 4})).json()['roomId']
        room2 = (await http.post('/api/rooms', json={'mode': 'east', 'capacity': 4})).json()['roomId']
        await http.post(f'/api/rooms/{room1}/join', json={'nickname': '甲', 'playerId': 'guest-A'})

        resp = await http.post(f'/api/rooms/{room2}/join',
                               json={'nickname': '甲', 'playerId': 'guest-A'})
        assert resp.status_code == 409
        assert resp.json()['detail']['code'] == 'ALREADY_IN_ROOM'


@pytest.mark.asyncio
async def test_human_avatar_persists_ai_unchanged(server, fresh_rooms, temp_storage,
                                                  stub_avatar_fetch):
    """真人头像：首次进房从外部 API 取并落库（跨房间稳定）；AI 空座头像保持 PLAYER_SEED 不变。"""
    async with httpx.AsyncClient(base_url=server['http']) as http:
        room_id = (await http.post('/api/rooms', json={'mode': 'east', 'capacity': 2})).json()['roomId']
        joins = {}
        for nickname, pid in (('甲', 'guest-1'), ('乙', 'guest-2')):
            resp = await http.post(f'/api/rooms/{room_id}/join',
                                   json={'nickname': nickname, 'playerId': pid})
            assert resp.status_code == 200, resp.text
            joins[nickname] = resp.json()

        seeds = rooms.get(room_id)._seeds()
        # 真人（0/1 座）：头像来自 stub 且各自不同；AI 空座（2/3）保持 PLAYER_SEED 固定
        assert seeds[0]['avatar'] == 'https://example.com/avatar/fake-1.jpg'
        assert seeds[1]['avatar'] == 'https://example.com/avatar/fake-2.jpg'
        assert seeds[2]['avatar'] == 'avatars/shisan.svg'
        assert seeds[3]['avatar'] == 'avatars/young-master.svg'
        assert stub_avatar_fetch['n'] == 2   # 只在首次进房取图

        # 持久化落库 + 跨房间复用（同一 player_id 不再重新取图）。
        # 一人只能在一间房（ALREADY_IN_ROOM）：甲先离房（房主离开，房间解散）再进新房
        assert temp_storage.get_player_avatar('guest-1') == 'https://example.com/avatar/fake-1.jpg'
        await http.post(f'/api/rooms/{room_id}/leave',
                        json={'seat': joins['甲']['seat'], 'rejoinCode': joins['甲']['rejoinCode']})
        room2 = (await http.post('/api/rooms', json={'mode': 'east', 'capacity': 2})).json()['roomId']
        await http.post(f'/api/rooms/{room2}/join', json={'nickname': '甲', 'playerId': 'guest-1'})
        seeds2 = rooms.get(room2)._seeds()
        assert seeds2[0]['avatar'] == 'https://example.com/avatar/fake-1.jpg'
        assert stub_avatar_fetch['n'] == 2   # 复用已存头像，未再取图


@pytest.mark.asyncio
async def test_join_leave_room(server, fresh_rooms, temp_storage):
    async with httpx.AsyncClient(base_url=server['http']) as http:
        room_id = (await http.post('/api/rooms', json={'capacity': 2})).json()['roomId']

        # join 甲 → seat 0 + rejoinCode
        resp = await http.post(f'/api/rooms/{room_id}/join', json={'nickname': '甲'})
        assert resp.status_code == 200
        join_a = resp.json()
        assert join_a['seat'] == 0
        assert join_a['rejoin'] is False
        assert len(join_a['rejoinCode']) == 9  # XXXX-XXXX

        # 乙加入 seat 1
        join_b = (await http.post(f'/api/rooms/{room_id}/join',
                                  json={'nickname': '乙'})).json()

        # 房间信息反映座位占用
        seats = (await http.get(f'/api/rooms/{room_id}')).json()['seats']
        assert seats[0] == {'seat': 0, 'nickname': '甲', 'ready': False, 'connected': False}
        assert seats[1] == {'seat': 1, 'nickname': '乙', 'ready': False, 'connected': False}

        # 非房主（乙）leave 释放座位，房间保留
        resp = await http.post(f'/api/rooms/{room_id}/leave',
                               json={'seat': 1, 'rejoinCode': join_b['rejoinCode']})
        assert resp.status_code == 200
        seats = (await http.get(f'/api/rooms/{room_id}')).json()['seats']
        assert seats[0] is not None
        assert seats[1] is None


@pytest.mark.asyncio
async def test_full_room_rejects_join(server, fresh_rooms, temp_storage):
    async with httpx.AsyncClient(base_url=server['http']) as http:
        room_id = (await http.post('/api/rooms', json={'capacity': 2})).json()['roomId']
        for nickname in ('甲', '乙'):
            resp = await http.post(f'/api/rooms/{room_id}/join', json={'nickname': nickname})
            assert resp.status_code == 200, resp.text
        # 第三人 → ROOM_FULL
        resp = await http.post(f'/api/rooms/{room_id}/join', json={'nickname': '丙'})
        assert resp.status_code == 409
        assert resp.json()['detail']['code'] == 'ROOM_FULL'


@pytest.mark.asyncio
async def test_join_rejects_duplicate_nickname(server, fresh_rooms, temp_storage):
    """昵称查重：房间内已有同名玩家占座 → NICKNAME_TAKEN。"""
    async with httpx.AsyncClient(base_url=server['http']) as http:
        room_id = (await http.post('/api/rooms', json={'capacity': 2})).json()['roomId']
        resp = await http.post(f'/api/rooms/{room_id}/join', json={'nickname': '甲'})
        assert resp.status_code == 200
        # 同名再占 → 拒绝
        resp = await http.post(f'/api/rooms/{room_id}/join', json={'nickname': '甲'})
        assert resp.status_code == 409
        assert resp.json()['detail']['code'] == 'NICKNAME_TAKEN'
        # 不同名可正常加入
        resp = await http.post(f'/api/rooms/{room_id}/join', json={'nickname': '乙'})
        assert resp.status_code == 200


@pytest.mark.asyncio
async def test_creator_leave_in_lobby_dissolves_room(server, fresh_rooms, temp_storage):
    """房主在非对局中离开 → 房间自动解散（GET 404）。"""
    async with httpx.AsyncClient(base_url=server['http']) as http:
        room_id = (await http.post('/api/rooms', json={'capacity': 2})).json()['roomId']
        join_a = (await http.post(f'/api/rooms/{room_id}/join',
                                  json={'nickname': '甲'})).json()
        await http.post(f'/api/rooms/{room_id}/join', json={'nickname': '乙'})
        info = (await http.get(f'/api/rooms/{room_id}')).json()
        assert info['creatorSeat'] == 0

        # 房主（甲）在 lobby 离开 → 房间解散
        resp = await http.post(f'/api/rooms/{room_id}/leave',
                               json={'seat': 0, 'rejoinCode': join_a['rejoinCode']})
        assert resp.status_code == 200
        resp = await http.get(f'/api/rooms/{room_id}')
        assert resp.status_code == 404
        assert resp.json()['detail']['code'] == 'ROOM_NOT_FOUND'


@pytest.mark.asyncio
async def test_non_creator_leave_keeps_room(server, fresh_rooms, temp_storage):
    """非房主离开 → 房间保留、房主不变。"""
    async with httpx.AsyncClient(base_url=server['http']) as http:
        room_id = (await http.post('/api/rooms', json={'capacity': 2})).json()['roomId']
        await http.post(f'/api/rooms/{room_id}/join', json={'nickname': '甲'})
        join_b = (await http.post(f'/api/rooms/{room_id}/join',
                                  json={'nickname': '乙'})).json()

        resp = await http.post(f'/api/rooms/{room_id}/leave',
                               json={'seat': join_b['seat'], 'rejoinCode': join_b['rejoinCode']})
        assert resp.status_code == 200
        info = (await http.get(f'/api/rooms/{room_id}')).json()
        assert info['creatorSeat'] == 0
        assert info['seats'][0] is not None
        assert info['seats'][1] is None


@pytest.mark.asyncio
async def test_creator_leave_during_match_keeps_room(server, fresh_rooms, temp_storage):
    """对局中房主离开 → 房间保留（AI 代打），房主转移给下一座位。"""
    async with httpx.AsyncClient(base_url=server['http']) as http:
        room_id = (await http.post('/api/rooms', json={'capacity': 4})).json()['roomId']
        joins = {}
        for nickname in ('甲', '乙', '丙', '丁'):
            joins[nickname] = (await http.post(
                f'/api/rooms/{room_id}/join', json={'nickname': nickname})).json()
        room = rooms.get(room_id)
        assert room is not None
        # 模拟对局进行中（不真实开局，避免 pace={} 下对局秒结束的时序竞争）
        room.status = 'playing'

        # 房主（甲，seat 0）对局中离开 → 不散房（仍有其他玩家）
        resp = await http.post(f'/api/rooms/{room_id}/leave',
                               json={'seat': joins['甲']['seat'],
                                     'rejoinCode': joins['甲']['rejoinCode']})
        assert resp.status_code == 200
        assert rooms.get(room_id) is not None
        info = (await http.get(f'/api/rooms/{room_id}')).json()
        assert info['status'] == 'playing'
        assert info['creatorSeat'] != joins['甲']['seat']   # 房主已转移


@pytest.mark.asyncio
async def test_close_room_creator_only(server, fresh_rooms, temp_storage):
    """DELETE 房间：仅创建者可关；对局中拒绝；关闭后房间移除。"""
    async with httpx.AsyncClient(base_url=server['http']) as http:
        room_id = (await http.post('/api/rooms', json={'capacity': 2})).json()['roomId']
        join_a = (await http.post(f'/api/rooms/{room_id}/join',
                                  json={'nickname': '甲'})).json()
        join_b = (await http.post(f'/api/rooms/{room_id}/join',
                                  json={'nickname': '乙'})).json()

        # 非创建者（乙）关闭 → 403
        resp = await http.request('DELETE', f'/api/rooms/{room_id}',
                                  json={'seat': 1, 'rejoinCode': join_b['rejoinCode']})
        assert resp.status_code == 403
        assert resp.json()['detail']['code'] == 'NOT_CREATOR'

        # 创建者关闭 → 房间移除
        resp = await http.request('DELETE', f'/api/rooms/{room_id}',
                                  json={'seat': 0, 'rejoinCode': join_a['rejoinCode']})
        assert resp.status_code == 200
        assert resp.json()['closed'] is True
        assert rooms.get(room_id) is None
        resp = await http.get(f'/api/rooms/{room_id}')
        assert resp.status_code == 404


@pytest.mark.asyncio
async def test_close_room_rejected_while_playing(server, fresh_rooms, temp_storage):
    """对局中（playing）关闭房间 → ROOM_PLAYING。"""
    async with httpx.AsyncClient(base_url=server['http']) as http:
        room_id = (await http.post('/api/rooms', json={'capacity': 2})).json()['roomId']
        joins = {}
        for nickname in ('甲', '乙'):
            joins[nickname] = (await http.post(
                f'/api/rooms/{room_id}/join', json={'nickname': nickname})).json()
        for join in joins.values():
            await http.post(f'/api/rooms/{room_id}/ready',
                            json={'seat': join['seat'], 'rejoinCode': join['rejoinCode']})
        room = rooms.get(room_id)
        room.pace = {}
        resp = await http.post(f'/api/rooms/{room_id}/start')
        assert resp.status_code == 200
        assert room.status == 'playing'

        resp = await http.request('DELETE', f'/api/rooms/{room_id}',
                                  json={'seat': joins['甲']['seat'],
                                        'rejoinCode': joins['甲']['rejoinCode']})
        assert resp.status_code == 409
        assert resp.json()['detail']['code'] == 'ROOM_PLAYING'


# ─── 准备 / 开局 ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_start_without_ready_rejected(server, fresh_rooms, temp_storage):
    async with httpx.AsyncClient(base_url=server['http']) as http:
        room_id = (await http.post('/api/rooms', json={'capacity': 2})).json()['roomId']
        join_a = (await http.post(f'/api/rooms/{room_id}/join',
                                  json={'nickname': '甲'})).json()
        # 未 ready → start 拒绝
        resp = await http.post(f'/api/rooms/{room_id}/start')
        assert resp.status_code == 409
        assert resp.json()['detail']['code'] == 'NOT_ALL_READY'

        # ready 后 start 成功
        resp = await http.post(f'/api/rooms/{room_id}/ready',
                               json={'seat': 0, 'rejoinCode': join_a['rejoinCode']})
        assert resp.status_code == 200
        assert resp.json()['ready'] is True
        resp = await http.post(f'/api/rooms/{room_id}/start')
        assert resp.status_code == 200, resp.text

        # 重复 start → ALREADY_STARTED
        resp = await http.post(f'/api/rooms/{room_id}/start')
        assert resp.status_code == 409
        assert resp.json()['detail']['code'] == 'ALREADY_STARTED'


@pytest.mark.asyncio
async def test_room_lifecycle_persists_match(server, fresh_rooms, temp_storage):
    """创建 → 加入 ×2 → 准备 ×2 → 开局 → 对局打完 → 战绩落库可查询。"""
    async with httpx.AsyncClient(base_url=server['http']) as http:
        room_id = (await http.post('/api/rooms', json={'capacity': 2})).json()['roomId']
        joins = {}
        for nickname in ('甲', '乙'):
            joins[nickname] = (await http.post(
                f'/api/rooms/{room_id}/join', json={'nickname': nickname})).json()

        # 准备
        for nickname, join in joins.items():
            resp = await http.post(f'/api/rooms/{room_id}/ready',
                                   json={'seat': join['seat'],
                                         'rejoinCode': join['rejoinCode']})
            assert resp.status_code == 200

        # 开局（客户端未连接，座位由 AI 托管，对局自动打完）
        room = rooms.get(room_id)
        assert room is not None and room.status == 'lobby'
        # REST 创建默认注入 PLAY_PACE（真人节奏）；本测试只验证落库链路，跳过节奏加速
        room.pace = {}
        resp = await http.post(f'/api/rooms/{room_id}/start')
        assert resp.status_code == 200, resp.text

        assert room.status == 'playing'
        await wait_until(lambda: room.status == 'finished', timeout=30)
        assert room.manager.match_finished
        assert room.match_id is not None

        # 战绩落库：房间对局列表
        resp = await http.get(f'/api/rooms/{room_id}/matches')
        assert resp.status_code == 200
        matches = resp.json()['matches']
        assert len(matches) == 1
        match_id = matches[0]['id']
        assert matches[0]['finalScores'] is not None

        # 单场详情：至少 1 局结算明细
        resp = await http.get(f'/api/matches/{match_id}')
        assert resp.status_code == 200
        detail = resp.json()
        assert detail['roomId'] == room_id
        assert len(detail['rounds']) >= 1
        first_round = detail['rounds'][0]['result']
        assert 'winner' in first_round
        # 存储格式（RoomSession._map_round_result）：分数流水 + 结算后分数
        assert 'deltas' in first_round
        assert 'scores_after' in first_round

        # 个人统计：参与玩家有场次记录
        for nickname in ('甲', '乙'):
            resp = await http.get(f'/api/players/{nickname}/stats')
            assert resp.status_code == 200
            stats = resp.json()
            assert stats['matches'] == 1
            assert stats['hands'] == len(detail['rounds'])

        # 未知对局 → 404
        resp = await http.get('/api/matches/does-not-exist')
        assert resp.status_code == 404
        assert resp.json()['detail']['code'] == 'MATCH_NOT_FOUND'


@pytest.mark.asyncio
async def test_room_can_restart_after_match(server, fresh_rooms, temp_storage):
    """对局结束后房间保留（finished）且座位解除准备态，可再准备再开一局。"""
    async with httpx.AsyncClient(base_url=server['http']) as http:
        room_id = (await http.post('/api/rooms', json={'capacity': 2})).json()['roomId']
        joins = {}
        for nickname in ('甲', '乙'):
            joins[nickname] = (await http.post(
                f'/api/rooms/{room_id}/join', json={'nickname': nickname})).json()
        room = rooms.get(room_id)
        assert room is not None
        room.pace = {}

        async def ready_and_start():
            for j in joins.values():
                resp = await http.post(f'/api/rooms/{room_id}/ready',
                                       json={'seat': j['seat'], 'rejoinCode': j['rejoinCode']})
                assert resp.status_code == 200
            resp = await http.post(f'/api/rooms/{room_id}/start')
            assert resp.status_code == 200, resp.text
            assert room.status == 'playing'
            await wait_until(lambda: room.game_task is not None and room.game_task.done(), timeout=30)
            assert room.status == 'finished'

        # 第一场
        await ready_and_start()
        assert rooms.get(room_id) is not None   # 房间保留，未被释放
        # 对局结束：座位解除准备态（再开一局需重新准备）
        assert all(s.ready is False for s in room.seats if s is not None)

        # 再开一局
        await ready_and_start()
        assert rooms.get(room_id) is not None

        # 两场均已落库
        resp = await http.get(f'/api/rooms/{room_id}/matches')
        assert resp.status_code == 200
        assert len(resp.json()['matches']) == 2


@pytest.mark.asyncio
async def test_non_creator_can_rejoin_finished_room(server, fresh_rooms, temp_storage):
    """对局结束后（finished）非房主离开后可重新加入房间（打下一场）。"""
    async with httpx.AsyncClient(base_url=server['http']) as http:
        room_id = (await http.post('/api/rooms', json={'capacity': 2})).json()['roomId']
        joins = {}
        for nickname in ('甲', '乙'):
            joins[nickname] = (await http.post(
                f'/api/rooms/{room_id}/join', json={'nickname': nickname})).json()
        for j in joins.values():
            await http.post(f'/api/rooms/{room_id}/ready',
                            json={'seat': j['seat'], 'rejoinCode': j['rejoinCode']})
        room = rooms.get(room_id)
        assert room is not None
        room.pace = {}
        await http.post(f'/api/rooms/{room_id}/start')
        await wait_until(lambda: room.status == 'finished', timeout=30)

        # 非房主（乙）离房
        resp = await http.post(f'/api/rooms/{room_id}/leave',
                               json={'seat': joins['乙']['seat'],
                                     'rejoinCode': joins['乙']['rejoinCode']})
        assert resp.status_code == 200

        # 重新加入 finished 房间 → 放行，占回原空座
        resp = await http.post(f'/api/rooms/{room_id}/join', json={'nickname': '乙'})
        assert resp.status_code == 200, resp.text
        rejoin = resp.json()
        assert rejoin['rejoin'] is False
        assert rejoin['seat'] == joins['乙']['seat']
        assert rooms.get(room_id) is not None

        # 房主身份未被转移
        info = (await http.get(f'/api/rooms/{room_id}')).json()
        assert info['creatorSeat'] == joins['甲']['seat']


@pytest.mark.asyncio
async def test_all_leave_releases_room(server, fresh_rooms, temp_storage):
    """全员离开（非对局中）→ 房间立即释放，不占槽位。"""
    async with httpx.AsyncClient(base_url=server['http']) as http:
        room_id = (await http.post('/api/rooms', json={'capacity': 2})).json()['roomId']
        joins = {}
        for nickname in ('甲', '乙'):
            joins[nickname] = (await http.post(
                f'/api/rooms/{room_id}/join', json={'nickname': nickname})).json()
        # 非房主先离 → 房间保留
        await http.post(f'/api/rooms/{room_id}/leave',
                        json={'seat': joins['乙']['seat'], 'rejoinCode': joins['乙']['rejoinCode']})
        assert rooms.get(room_id) is not None
        # 房主再离 → 房间解散
        await http.post(f'/api/rooms/{room_id}/leave',
                        json={'seat': joins['甲']['seat'], 'rejoinCode': joins['甲']['rejoinCode']})
        assert rooms.get(room_id) is None


@pytest.mark.asyncio
async def test_all_exit_mid_match_releases_room(server, fresh_rooms, temp_storage):
    """对局进行中全员退出 → 房间立即释放（不等到对局结束、不占槽位）。"""
    async with httpx.AsyncClient(base_url=server['http']) as http:
        room_id = (await http.post('/api/rooms', json={'capacity': 2})).json()['roomId']
        joins = {}
        for nickname in ('甲', '乙'):
            joins[nickname] = (await http.post(
                f'/api/rooms/{room_id}/join', json={'nickname': nickname})).json()
        for j in joins.values():
            await http.post(f'/api/rooms/{room_id}/ready',
                            json={'seat': j['seat'], 'rejoinCode': j['rejoinCode']})
        room = rooms.get(room_id)
        assert room is not None
        room.pace = {}
        await http.post(f'/api/rooms/{room_id}/start')
        assert room.status == 'playing'

        # 全员退出（房主先退 → AI 代打房间保留；最后一人退 → 立即释放）
        for nickname in ('甲', '乙'):
            await http.post(f'/api/rooms/{room_id}/leave',
                            json={'seat': joins[nickname]['seat'],
                                  'rejoinCode': joins[nickname]['rejoinCode']})
        # 房间应立刻被释放（无论对局是否已打完），不等到 60 分钟 deadline
        assert rooms.get(room_id) is None


@pytest.mark.asyncio
async def test_expired_room_released_after_match(server, fresh_rooms, temp_storage):
    """对局中超过 60 分钟限时 → 等对局结束自动释放房间。"""
    async with httpx.AsyncClient(base_url=server['http']) as http:
        room_id = (await http.post('/api/rooms', json={'capacity': 2})).json()['roomId']
        joins = {}
        for nickname in ('甲', '乙'):
            joins[nickname] = (await http.post(
                f'/api/rooms/{room_id}/join', json={'nickname': nickname})).json()
        for join in joins.values():
            await http.post(f'/api/rooms/{room_id}/ready',
                            json={'seat': join['seat'], 'rejoinCode': join['rejoinCode']})

        room = rooms.get(room_id)
        assert room is not None
        room.pace = {}
        # 模拟：对局已持续超过限时（deadline 拨到过去）
        room.deadline = time.monotonic() - 1
        resp = await http.post(f'/api/rooms/{room_id}/start')
        assert resp.status_code == 200, resp.text
        assert room.status == 'playing'

        # 对局结束后房间自动释放（不等待房主手动解散）
        await wait_until(lambda: rooms.get(room_id) is None, timeout=30)
        assert room.status == 'closed'


@pytest.mark.asyncio
async def test_stats_by_player_id(server, fresh_rooms, temp_storage):
    """按匿名身份（playerId / guestId）查战绩：身份锚点而非昵称。"""
    async with httpx.AsyncClient(base_url=server['http']) as http:
        room_id = (await http.post('/api/rooms', json={'capacity': 2})).json()['roomId']
        for nickname, pid in (('甲', 'guest-A'), ('乙', 'guest-B')):
            j = (await http.post(f'/api/rooms/{room_id}/join',
                                 json={'nickname': nickname, 'playerId': pid})).json()
            await http.post(f'/api/rooms/{room_id}/ready',
                            json={'seat': j['seat'], 'rejoinCode': j['rejoinCode']})
        room = rooms.get(room_id)
        room.pace = {}
        await http.post(f'/api/rooms/{room_id}/start')
        await wait_until(lambda: room.status == 'finished', timeout=30)

        resp = await http.get('/api/players/by-id/guest-A/stats')
        assert resp.status_code == 200
        stats = resp.json()
        assert stats['playerId'] == 'guest-A'
        assert stats['matches'] == 1
        assert stats['hands'] >= 1

        # guest-B 各算各的，不与 guest-A 混淆
        resp_b = await http.get('/api/players/by-id/guest-B/stats')
        assert resp_b.json()['matches'] == 1

        # 未参与过对局的 playerId → 全 0
        resp_c = await http.get('/api/players/by-id/guest-nobody/stats')
        assert resp_c.json()['matches'] == 0 and resp_c.json()['hands'] == 0


@pytest.mark.asyncio
async def test_stats_survive_leaving_room(server, fresh_rooms, temp_storage):
    """离房后战绩仍在：match_players 开局记录参赛身份（room_seats 离房即删，不能作战绩真源）。"""
    async with httpx.AsyncClient(base_url=server['http']) as http:
        room_id = (await http.post('/api/rooms', json={'capacity': 2})).json()['roomId']
        joins = {}
        for nickname, pid in (('甲', 'guest-A'), ('乙', 'guest-B')):
            joins[nickname] = (await http.post(f'/api/rooms/{room_id}/join',
                                               json={'nickname': nickname, 'playerId': pid})).json()
        for j in joins.values():
            await http.post(f'/api/rooms/{room_id}/ready',
                            json={'seat': j['seat'], 'rejoinCode': j['rejoinCode']})
        room = rooms.get(room_id)
        room.pace = {}
        await http.post(f'/api/rooms/{room_id}/start')
        await wait_until(lambda: room.status == 'finished', timeout=30)
        # 对局结束后全部离房 → room_seats 行被删。
        # 先离非房主（乙），再房主（甲）离房触发房间解散 —— 房主一离，房间即从注册表移除，
        # 其余玩家将无法再对该房间发请求。
        for j in reversed(list(joins.values())):
            await http.post(f'/api/rooms/{room_id}/leave',
                            json={'seat': j['seat'], 'rejoinCode': j['rejoinCode']})
        assert temp_storage._conn().execute(
            'SELECT COUNT(*) AS c FROM room_seats').fetchone()['c'] == 0
        # 战绩仍可查（match_players 持久）
        resp = await http.get('/api/players/by-id/guest-A/stats')
        stats = resp.json()
        assert stats['matches'] == 1 and stats['hands'] >= 1
        resp_b = await http.get('/api/players/by-id/guest-B/stats')
        assert resp_b.json()['matches'] == 1
