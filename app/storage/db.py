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

_BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DEFAULT_DB_PATH = os.path.join(_BASE_DIR, 'data', 'mahjong.db')

_SCHEMA = """
CREATE TABLE IF NOT EXISTS players (
  id          TEXT PRIMARY KEY,
  nickname    TEXT NOT NULL UNIQUE,
  avatar      TEXT NOT NULL DEFAULT '',
  created_at  DATETIME NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS rooms (
  id          TEXT PRIMARY KEY,
  mode        TEXT NOT NULL CHECK (mode IN ('east','hanchan')),
  capacity    INTEGER NOT NULL DEFAULT 4,
  status      TEXT NOT NULL DEFAULT 'lobby',
  created_at  DATETIME NOT NULL DEFAULT (datetime('now')),
  finished_at DATETIME
);

CREATE TABLE IF NOT EXISTS matches (
  id           TEXT PRIMARY KEY,
  room_id      TEXT NOT NULL REFERENCES rooms(id),
  mode         TEXT NOT NULL,
  start_at     DATETIME NOT NULL DEFAULT (datetime('now')),
  end_at       DATETIME,
  final_scores TEXT
);

CREATE TABLE IF NOT EXISTS round_results (
  id          TEXT PRIMARY KEY,
  match_id    TEXT NOT NULL REFERENCES matches(id),
  round       INTEGER NOT NULL,
  result_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS room_seats (
  room_id         TEXT NOT NULL REFERENCES rooms(id),
  seat            INTEGER NOT NULL,
  nickname        TEXT NOT NULL,
  rejoin_code     TEXT NOT NULL,
  disconnected_at DATETIME,
  PRIMARY KEY (room_id, seat)
);

CREATE INDEX IF NOT EXISTS idx_matches_room ON matches(room_id);
CREATE INDEX IF NOT EXISTS idx_round_results_match ON round_results(match_id);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_id() -> str:
    """对局/局结果 ID：uuid4 hex 简化（将来可换 ULID，时间有序 + 不可猜）。"""
    return uuid.uuid4().hex


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

    def upsert_room_seat(self, room_id: str, seat: int, nickname: str, rejoin_code: str) -> None:
        with self._conn() as conn:
            conn.execute(
                'INSERT OR REPLACE INTO room_seats '
                '(room_id, seat, nickname, rejoin_code) VALUES (?, ?, ?, ?)',
                (room_id, seat, nickname, rejoin_code),
            )

    def remove_room_seat(self, room_id: str, seat: int) -> None:
        with self._conn() as conn:
            conn.execute(
                'DELETE FROM room_seats WHERE room_id = ? AND seat = ?',
                (room_id, seat),
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
        """个人统计（首版简化）：场次 / 参与局数 / 胡牌局数 / 总净胜分。

        通过 room_seats.nickname → matches.room_id 关联出该玩家参与过的对局，
        再聚合 round_results 里的 winner（胡牌）与 scoreChanges（净胜分）。
        """
        with self._conn() as conn:
            seats = conn.execute(
                'SELECT room_id FROM room_seats WHERE nickname = ?', (nickname,)).fetchall()
            room_ids = [r['room_id'] for r in seats]
            if not room_ids:
                return {'nickname': nickname, 'matches': 0, 'hands': 0,
                        'wins': 0, 'totalDelta': 0}
            placeholders = ','.join('?' * len(room_ids))
            matches = conn.execute(
                f'SELECT id FROM matches WHERE room_id IN ({placeholders})',
                room_ids).fetchall()
            match_ids = [m['id'] for m in matches]

            hands = wins = total_delta = 0
            if match_ids:
                mh = ','.join('?' * len(match_ids))
                for row in conn.execute(
                        f'SELECT result_json FROM round_results WHERE match_id IN ({mh})',
                        match_ids).fetchall():
                    result = json.loads(row['result_json'])
                    hands += 1
                    if result.get('winner') == nickname:
                        wins += 1
                    for change in result.get('scoreChanges', []):
                        if change.get('name') == nickname:
                            total_delta += change.get('delta', 0)
        return {
            'nickname': nickname,
            'matches': len(matches),
            'hands': hands,
            'wins': wins,
            'totalDelta': total_delta,
        }


# 模块级共享存储单例（REST 层与 RoomSession 注入用；测试可自行构造临时库实例）
storage = Storage()
