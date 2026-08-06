"""SQLite 持久化层 —— 房间 / 玩家 / 对局 / 局结果 / 座位

开发计划 §6.1 DDL 落地。首版用内置 sqlite3，SQLite 是同步库：
- REST 路由定义为同步函数（FastAPI 自动放线程池），storage 方法直接调用
- 游戏主循环内的落库（_drive 钩子）用 asyncio.to_thread 包装

表结构（简化 JSON 字段，首版战绩查询返回 JSON 即可）：
- players       长期身份锚点（nickname 唯一，join 时幂等创建）
- rooms         房间元数据 + 生命周期状态
- matches       一场对局（东风/半庄 = 一个 match）
- round_results 每局结算（result_json 存完整 result）
- room_seats    座位/重进码（房间进行中时维护）
"""

import json
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import quote

import psycopg
from psycopg.rows import dict_row
from dotenv import load_dotenv

_BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DEFAULT_DB_PATH = os.path.join(_BASE_DIR, 'data', 'mahjong.db')

# 启动时加载 backend/.env（gitignored）：环境变量优先，.env 只作回退，不覆盖已设置的变量。
load_dotenv(os.path.join(_BASE_DIR, '.env'))

_SCHEMA_DIR = os.path.dirname(os.path.abspath(__file__))


def _load_schema(name: str) -> str:
    """从同目录的 .sql 文件读取 schema DDL。"""
    with open(os.path.join(_SCHEMA_DIR, name), 'r', encoding='utf-8') as fh:
        return fh.read()


_SCHEMA = _load_schema('schema_sqlite.sql')


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_id() -> str:
    """对局/局结果 ID：uuid4 hex 简化（将来可换 ULID，时间有序 + 不可猜）。"""
    return uuid.uuid4().hex


# ─── PostgreSQL（Supabase pooler / 任意 PG，读环境变量，仅启动时生效）────

_PG_DEFAULTS = {
    'PG_HOST': 'aws-1-ap-northeast-2.pooler.supabase.com',
    'PG_PORT': '6543',
    'PG_USER': 'postgres.szklriclurtinykjlano',
    'PG_DATABASE': 'postgres',
}


def postgres_dsn() -> Optional[str]:
    """构造 PG 连接串；未取得 PG_PASSWORD 时返回 None（走 SQLite 回退）。

    PG_PASSWORD 是唯一必需的机密，只在项目启动时读取：环境变量优先，
    backend/.env 由模块顶部的 load_dotenv 加载作回退。
    """
    password = os.environ.get('PG_PASSWORD')
    if not password:
        return None
    host = os.environ.get('PG_HOST', _PG_DEFAULTS['PG_HOST'])
    port = os.environ.get('PG_PORT', _PG_DEFAULTS['PG_PORT'])
    user = os.environ.get('PG_USER', _PG_DEFAULTS['PG_USER'])
    db = os.environ.get('PG_DATABASE', _PG_DEFAULTS['PG_DATABASE'])
    # 密码可能含 @ / : / = 等特殊字符：URI DSN 里必须百分号编码，否则 libpq 解析错乱
    user_enc = quote(user, safe='')
    password_enc = quote(password, safe='')
    return f'postgresql://{user_enc}:{password_enc}@{host}:{port}/{db}'


_PG_SCHEMA = _load_schema('schema_postgres.sql')


class Storage:
    """SQLite 存储门面。每次操作新建连接（SQLite 多连接安全，写锁由 DB 管理）。"""

    def __init__(self, db_path: Optional[str] = None):
        self.path = db_path or DEFAULT_DB_PATH
        directory = os.path.dirname(self.path)
        if directory:
            os.makedirs(directory, exist_ok=True)

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    # ── 初始化 ───────────────────────────────────────────

    def init(self) -> None:
        with self._conn() as conn:
            conn.executescript(_SCHEMA)
            self._migrate(conn)

    @staticmethod
    def _migrate(conn: sqlite3.Connection) -> None:
        """旧库补列：room_seats.player_id（CREATE IF NOT EXISTS 不会改已存在表）。"""
        cols = {r['name'] for r in conn.execute('PRAGMA table_info(room_seats)').fetchall()}
        if 'player_id' not in cols:
            conn.execute('ALTER TABLE room_seats ADD COLUMN player_id TEXT')

    # ── 玩家 ─────────────────────────────────────────────

    def create_player(self, nickname: str, avatar: str = '') -> None:
        """按昵称幂等创建玩家行（存在则复用）。"""
        with self._conn() as conn:
            conn.execute(
                'INSERT OR IGNORE INTO players (id, nickname, avatar) VALUES (?, ?, ?)',
                (_new_id(), nickname, avatar),
            )

    # ── 房间 ─────────────────────────────────────────────

    def create_room(self, room_id: str, mode: str, capacity: int) -> None:
        with self._conn() as conn:
            conn.execute(
                'INSERT INTO rooms (id, mode, capacity) VALUES (?, ?, ?)',
                (room_id, mode, capacity),
            )

    def update_room_status(self, room_id: str, status: str) -> None:
        finished = _now() if status in ('finished', 'closed') else None
        with self._conn() as conn:
            conn.execute(
                'UPDATE rooms SET status = ?, finished_at = COALESCE(?, finished_at) '
                'WHERE id = ?',
                (status, finished, room_id),
            )

    # ── 对局 ─────────────────────────────────────────────

    def create_match(self, room_id: str, mode: str) -> str:
        """开局：insert 一行 match，返回 match_id。"""
        match_id = _new_id()
        with self._conn() as conn:
            conn.execute(
                'INSERT INTO matches (id, room_id, mode) VALUES (?, ?, ?)',
                (match_id, room_id, mode),
            )
        return match_id

    def upsert_match_players(self, match_id: str, players: list) -> None:
        """开局时记录参赛者身份（战绩真源：room_seats 离房即删，不能作为战绩依据）。"""
        with self._conn() as conn:
            conn.executemany(
                'INSERT OR REPLACE INTO match_players (match_id, seat, player_id, nickname) '
                'VALUES (?, ?, ?, ?)',
                [(match_id, p['seat'], p.get('player_id'), p['nickname']) for p in players])

    def finish_match(self, match_id: str, final_scores: list) -> None:
        with self._conn() as conn:
            conn.execute(
                'UPDATE matches SET end_at = ?, final_scores = ? WHERE id = ?',
                (_now(), json.dumps(final_scores, ensure_ascii=False), match_id),
            )

    def insert_round_result(self, match_id: str, round_data: dict) -> None:
        """每局结算：round_data 为 RoomSession._map_round_result 的映射结果。"""
        with self._conn() as conn:
            conn.execute(
                'INSERT INTO round_results (id, match_id, round, result_json) '
                'VALUES (?, ?, ?, ?)',
                (_new_id(), match_id, round_data.get('round', 0),
                 json.dumps(round_data, ensure_ascii=False)),
            )

    # ── 座位 ─────────────────────────────────────────────

    def upsert_room_seat(self, room_id: str, seat: int, nickname: str, rejoin_code: str,
                         player_id: Optional[str] = None) -> None:
        with self._conn() as conn:
            conn.execute(
                'INSERT OR REPLACE INTO room_seats '
                '(room_id, seat, nickname, rejoin_code, player_id) VALUES (?, ?, ?, ?, ?)',
                (room_id, seat, nickname, rejoin_code, player_id),
            )

    def remove_room_seat(self, room_id: str, seat: int) -> None:
        with self._conn() as conn:
            conn.execute(
                'DELETE FROM room_seats WHERE room_id = ? AND seat = ?',
                (room_id, seat),
            )

    # ── 封禁 / 举报 ──────────────────────────────────────

    def is_banned(self, scope: str, target: str) -> bool:
        with self._conn() as conn:
            row = conn.execute(
                'SELECT 1 FROM bans WHERE scope = ? AND target = ?',
                (scope, target)).fetchone()
            return row is not None

    def ban_target(self, scope: str, target: str, reason: str = '',
                   banned_by: str = '') -> None:
        """封禁（player/room/device）。同 scope+target 重复封禁 = 更新原因。"""
        with self._conn() as conn:
            conn.execute(
                'INSERT OR REPLACE INTO bans (scope, target, reason, banned_by) '
                'VALUES (?, ?, ?, ?)',
                (scope, target, reason, banned_by),
            )

    def unban(self, scope: str, target: str) -> None:
        with self._conn() as conn:
            conn.execute(
                'DELETE FROM bans WHERE scope = ? AND target = ?',
                (scope, target),
            )

    def add_report(self, room_id: str, reporter: str, target: str,
                   target_name: str, reason: str) -> None:
        with self._conn() as conn:
            conn.execute(
                'INSERT INTO reports (id, room_id, reporter, target, target_name, reason) '
                'VALUES (?, ?, ?, ?, ?, ?)',
                (_new_id(), room_id, reporter, target, target_name, reason),
            )

    # ── 查询 ─────────────────────────────────────────────

    def get_match(self, match_id: str) -> Optional[dict]:
        """单场详情：match + final_scores + 各局 round_results。"""
        with self._conn() as conn:
            row = conn.execute(
                'SELECT * FROM matches WHERE id = ?', (match_id,)).fetchone()
            if row is None:
                return None
            rounds = conn.execute(
                'SELECT round, result_json FROM round_results '
                'WHERE match_id = ? ORDER BY round', (match_id,)).fetchall()
        return {
            'id': row['id'],
            'roomId': row['room_id'],
            'mode': row['mode'],
            'startAt': row['start_at'],
            'endAt': row['end_at'],
            'finalScores': json.loads(row['final_scores']) if row['final_scores'] else None,
            'rounds': [
                {'round': r['round'], 'result': json.loads(r['result_json'])}
                for r in rounds
            ],
        }

    def list_room_matches(self, room_id: str) -> list[dict]:
        """房间历史对局列表（不含局详情，仅概览）。"""
        with self._conn() as conn:
            rows = conn.execute(
                'SELECT id, mode, start_at, end_at, final_scores FROM matches '
                'WHERE room_id = ? ORDER BY start_at', (room_id,)).fetchall()
        return [
            {
                'id': r['id'],
                'mode': r['mode'],
                'startAt': r['start_at'],
                'endAt': r['end_at'],
                'finalScores': json.loads(r['final_scores']) if r['final_scores'] else None,
            }
            for r in rows
        ]

    def get_player_stats(self, nickname: str) -> dict:
        """个人统计（按昵称，旧版兼容）：场次 / 参与局数 / 胡牌局数 / 总净胜分。

        从 match_players（开局记录参赛身份，离房不删）聚合，round_results 按座位号取
        deltas / winner_index。P1 之前的旧局无 match_players 行 → 返回 0（仅能靠新局）。
        """
        with self._conn() as conn:
            rows = conn.execute(
                'SELECT match_id, seat FROM match_players WHERE nickname = ?',
                (nickname,)).fetchall()
            if not rows:
                return {'nickname': nickname, 'matches': 0, 'hands': 0,
                        'wins': 0, 'totalDelta': 0}
            return {'nickname': nickname,
                    **self._aggregate_stats(conn, {r['match_id']: r['seat'] for r in rows})}

    def get_player_stats_by_id(self, player_id: str) -> dict:
        """按匿名身份（player_id / guestId）聚合战绩：场次 / 参与局数 / 胡牌局数 / 总净胜分。

        身份锚点是 player_id（match_players 开局记录，离房不删）：改名不丢历史、重名不混。
        """
        with self._conn() as conn:
            rows = conn.execute(
                'SELECT match_id, seat FROM match_players WHERE player_id = ?',
                (player_id,)).fetchall()
            if not rows:
                return {'playerId': player_id, 'matches': 0, 'hands': 0,
                        'wins': 0, 'totalDelta': 0}
            return {'playerId': player_id,
                    **self._aggregate_stats(conn, {r['match_id']: r['seat'] for r in rows})}

    @staticmethod
    def _aggregate_stats(conn, match_seats: dict) -> dict:
        """按 {match_id: seat} 聚合 round_results（存储格式 deltas[]/winner_index）。"""
        match_ids = list(match_seats)
        matches: set[str] = set()
        hands = wins = total_delta = 0
        if match_ids:
            mh = ','.join('?' * len(match_ids))
            for row in conn.execute(
                    f'SELECT match_id, result_json FROM round_results '
                    f'WHERE match_id IN ({mh})', match_ids).fetchall():
                seat = match_seats[row['match_id']]
                result = json.loads(row['result_json'])
                deltas = result.get('deltas', []) or []
                entry = next((d for d in deltas if d.get('playerIndex') == seat), None)
                if entry is None:
                    continue   # 该局无此座位记录
                matches.add(row['match_id'])
                hands += 1
                if result.get('winner_index') == seat:
                    wins += 1
                total_delta += entry.get('amount', 0) or 0
        return {'matches': len(matches), 'hands': hands, 'wins': wins,
                'totalDelta': total_delta}


class PostgresStorage:
    """PostgreSQL 存储（Supabase pooler / 任意 PG）。

    方法签名与 SQLite Storage 完全一致；SQL 换成 PG 方言（%s 占位、
    ON CONFLICT 代替 INSERT OR IGNORE/REPLACE、now() 代替 datetime('now')）。
    """

    def __init__(self, dsn: str):
        self.dsn = dsn

    def _conn(self) -> psycopg.Connection:
        return psycopg.connect(self.dsn, row_factory=dict_row)

    # ── 初始化 ───────────────────────────────────────────

    def init(self) -> None:
        with self._conn() as conn:
            for statement in _PG_SCHEMA.split(';'):
                statement = statement.strip()
                if statement:
                    conn.execute(statement)
            self._migrate(conn)

    def _migrate(self, conn: psycopg.Connection) -> None:
        row = conn.execute(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_name = 'room_seats' AND column_name = 'player_id'").fetchone()
        if row is None:
            conn.execute('ALTER TABLE room_seats ADD COLUMN player_id TEXT')

    # ── 玩家 ─────────────────────────────────────────────

    def create_player(self, nickname: str, avatar: str = '') -> None:
        with self._conn() as conn:
            conn.execute(
                'INSERT INTO players (id, nickname, avatar) VALUES (%s, %s, %s) '
                'ON CONFLICT (nickname) DO NOTHING',
                (_new_id(), nickname, avatar),
            )

    # ── 房间 ─────────────────────────────────────────────

    def create_room(self, room_id: str, mode: str, capacity: int) -> None:
        with self._conn() as conn:
            conn.execute(
                'INSERT INTO rooms (id, mode, capacity) VALUES (%s, %s, %s)',
                (room_id, mode, capacity),
            )

    def update_room_status(self, room_id: str, status: str) -> None:
        finished = _now() if status in ('finished', 'closed') else None
        with self._conn() as conn:
            conn.execute(
                'UPDATE rooms SET status = %s, finished_at = COALESCE(%s, finished_at) '
                'WHERE id = %s',
                (status, finished, room_id),
            )

    # ── 对局 ─────────────────────────────────────────────

    def create_match(self, room_id: str, mode: str) -> str:
        match_id = _new_id()
        with self._conn() as conn:
            conn.execute(
                'INSERT INTO matches (id, room_id, mode) VALUES (%s, %s, %s)',
                (match_id, room_id, mode),
            )
        return match_id

    def upsert_match_players(self, match_id: str, players: list) -> None:
        with self._conn() as conn:
            for p in players:
                conn.execute(
                    'INSERT INTO match_players (match_id, seat, player_id, nickname) '
                    'VALUES (%s, %s, %s, %s) '
                    'ON CONFLICT (match_id, seat) DO UPDATE SET '
                    'player_id = EXCLUDED.player_id, nickname = EXCLUDED.nickname',
                    (match_id, p['seat'], p.get('player_id'), p['nickname']),
                )

    def finish_match(self, match_id: str, final_scores: list) -> None:
        with self._conn() as conn:
            conn.execute(
                'UPDATE matches SET end_at = %s, final_scores = %s WHERE id = %s',
                (_now(), json.dumps(final_scores, ensure_ascii=False), match_id),
            )

    def insert_round_result(self, match_id: str, round_data: dict) -> None:
        with self._conn() as conn:
            conn.execute(
                'INSERT INTO round_results (id, match_id, round, result_json) '
                'VALUES (%s, %s, %s, %s)',
                (_new_id(), match_id, round_data.get('round', 0),
                 json.dumps(round_data, ensure_ascii=False)),
            )

    # ── 座位 ─────────────────────────────────────────────

    def upsert_room_seat(self, room_id: str, seat: int, nickname: str, rejoin_code: str,
                         player_id: Optional[str] = None) -> None:
        with self._conn() as conn:
            conn.execute(
                'INSERT INTO room_seats (room_id, seat, nickname, rejoin_code, player_id) '
                'VALUES (%s, %s, %s, %s, %s) '
                'ON CONFLICT (room_id, seat) DO UPDATE SET '
                'nickname = EXCLUDED.nickname, rejoin_code = EXCLUDED.rejoin_code, '
                'player_id = EXCLUDED.player_id',
                (room_id, seat, nickname, rejoin_code, player_id),
            )

    def remove_room_seat(self, room_id: str, seat: int) -> None:
        with self._conn() as conn:
            conn.execute(
                'DELETE FROM room_seats WHERE room_id = %s AND seat = %s',
                (room_id, seat),
            )

    # ── 封禁 / 举报 ──────────────────────────────────────

    def is_banned(self, scope: str, target: str) -> bool:
        with self._conn() as conn:
            row = conn.execute(
                'SELECT 1 FROM bans WHERE scope = %s AND target = %s',
                (scope, target)).fetchone()
            return row is not None

    def ban_target(self, scope: str, target: str, reason: str = '',
                   banned_by: str = '') -> None:
        with self._conn() as conn:
            conn.execute(
                'INSERT INTO bans (scope, target, reason, banned_by) VALUES (%s, %s, %s, %s) '
                'ON CONFLICT (scope, target) DO UPDATE SET '
                'reason = EXCLUDED.reason, banned_by = EXCLUDED.banned_by',
                (scope, target, reason, banned_by),
            )

    def unban(self, scope: str, target: str) -> None:
        with self._conn() as conn:
            conn.execute(
                'DELETE FROM bans WHERE scope = %s AND target = %s',
                (scope, target),
            )

    def add_report(self, room_id: str, reporter: str, target: str,
                   target_name: str, reason: str) -> None:
        with self._conn() as conn:
            conn.execute(
                'INSERT INTO reports (id, room_id, reporter, target, target_name, reason) '
                'VALUES (%s, %s, %s, %s, %s, %s)',
                (_new_id(), room_id, reporter, target, target_name, reason),
            )

    # ── 查询 ─────────────────────────────────────────────

    def get_match(self, match_id: str) -> Optional[dict]:
        with self._conn() as conn:
            row = conn.execute(
                'SELECT * FROM matches WHERE id = %s', (match_id,)).fetchone()
            if row is None:
                return None
            rounds = conn.execute(
                'SELECT round, result_json FROM round_results '
                'WHERE match_id = %s ORDER BY round', (match_id,)).fetchall()
        return {
            'id': row['id'],
            'roomId': row['room_id'],
            'mode': row['mode'],
            'startAt': row['start_at'],
            'endAt': row['end_at'],
            'finalScores': json.loads(row['final_scores']) if row['final_scores'] else None,
            'rounds': [
                {'round': r['round'], 'result': json.loads(r['result_json'])}
                for r in rounds
            ],
        }

    def list_room_matches(self, room_id: str) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                'SELECT id, mode, start_at, end_at, final_scores FROM matches '
                'WHERE room_id = %s ORDER BY start_at', (room_id,)).fetchall()
        return [
            {
                'id': r['id'],
                'mode': r['mode'],
                'startAt': r['start_at'],
                'endAt': r['end_at'],
                'finalScores': json.loads(r['final_scores']) if r['final_scores'] else None,
            }
            for r in rows
        ]

    def get_player_stats(self, nickname: str) -> dict:
        with self._conn() as conn:
            rows = conn.execute(
                'SELECT match_id, seat FROM match_players WHERE nickname = %s',
                (nickname,)).fetchall()
            if not rows:
                return {'nickname': nickname, 'matches': 0, 'hands': 0,
                        'wins': 0, 'totalDelta': 0}
            return {'nickname': nickname,
                    **self._aggregate_stats(conn, {r['match_id']: r['seat'] for r in rows})}

    def get_player_stats_by_id(self, player_id: str) -> dict:
        with self._conn() as conn:
            rows = conn.execute(
                'SELECT match_id, seat FROM match_players WHERE player_id = %s',
                (player_id,)).fetchall()
            if not rows:
                return {'playerId': player_id, 'matches': 0, 'hands': 0,
                        'wins': 0, 'totalDelta': 0}
            return {'playerId': player_id,
                    **self._aggregate_stats(conn, {r['match_id']: r['seat'] for r in rows})}

    @staticmethod
    def _aggregate_stats(conn: psycopg.Connection, match_seats: dict) -> dict:
        match_ids = list(match_seats)
        matches: set[str] = set()
        hands = wins = total_delta = 0
        if match_ids:
            mh = ','.join(['%s'] * len(match_ids))
            for row in conn.execute(
                    f'SELECT match_id, result_json FROM round_results '
                    f'WHERE match_id IN ({mh})', match_ids).fetchall():
                seat = match_seats[row['match_id']]
                result = json.loads(row['result_json'])
                deltas = result.get('deltas', []) or []
                entry = next((d for d in deltas if d.get('playerIndex') == seat), None)
                if entry is None:
                    continue
                matches.add(row['match_id'])
                hands += 1
                if result.get('winner_index') == seat:
                    wins += 1
                total_delta += entry.get('amount', 0) or 0
        return {'matches': len(matches), 'hands': hands, 'wins': wins,
                'totalDelta': total_delta}


# 模块级共享存储单例：设置了 PG_PASSWORD 走 PostgreSQL，否则回退 SQLite。
# PG_PASSWORD 仅在项目启动时从环境读取；测试自行构造临时 SQLite 实例，不受影响。
def _resolve_storage():
    dsn = postgres_dsn()
    return PostgresStorage(dsn) if dsn else Storage()


storage = _resolve_storage()
