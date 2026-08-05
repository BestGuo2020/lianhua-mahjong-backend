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
        code_a = join_a['rejoinCode']

        # 房间信息反映座位占用
        seats = (await http.get(f'/api/rooms/{room_id}')).json()['seats']
        assert seats[0] == {'seat': 0, 'nickname': '甲', 'ready': False, 'connected': False}
        assert seats[1] is None

        # leave 释放座位
        resp = await http.post(f'/api/rooms/{room_id}/leave',
                               json={'seat': 0, 'rejoinCode': code_a})
        assert resp.status_code == 200
        seats = (await http.get(f'/api/rooms/{room_id}')).json()['seats']
        assert seats[0] is None


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
async def test_creator_seat_and_transfer(server, fresh_rooms, temp_storage):
    """创建者追踪：首个 join 者为创建者；创建者离房后房主转移给下一座位。"""
    async with httpx.AsyncClient(base_url=server['http']) as http:
        room_id = (await http.post('/api/rooms', json={'capacity': 2})).json()['roomId']
        # 创建者先 join（seat 0）→ creatorSeat = 0
        join_a = (await http.post(f'/api/rooms/{room_id}/join',
                                  json={'nickname': '甲'})).json()
        info = (await http.get(f'/api/rooms/{room_id}')).json()
        assert info['creatorSeat'] == 0

        # 乙加入（seat 1）→ creatorSeat 不变
        join_b = (await http.post(f'/api/rooms/{room_id}/join',
                                  json={'nickname': '乙'})).json()
        assert join_b['seat'] == 1
        info = (await http.get(f'/api/rooms/{room_id}')).json()
        assert info['creatorSeat'] == 0

        # 创建者离房 → 房主转移给 seat 1
        resp = await http.post(f'/api/rooms/{room_id}/leave',
                               json={'seat': 0, 'rejoinCode': join_a['rejoinCode']})
        assert resp.status_code == 200
        info = (await http.get(f'/api/rooms/{room_id}')).json()
        assert info['creatorSeat'] == 1

        # 最后一人离房 → creatorSeat 置空
        resp = await http.post(f'/api/rooms/{room_id}/leave',
                               json={'seat': 1, 'rejoinCode': join_b['rejoinCode']})
        assert resp.status_code == 200
        info = (await http.get(f'/api/rooms/{room_id}')).json()
        assert info['creatorSeat'] is None


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
        # 对局结束后全部离房 → room_seats 行被删
        for j in joins.values():
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
