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
    monkeypatch.setattr(rooms_api, 'storage', s)
    monkeypatch.setattr(matches_api, 'storage', s)
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
