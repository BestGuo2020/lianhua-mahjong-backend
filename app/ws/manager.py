"""WebSocket 连接管理器 —— 单房间连接注册表与广播

每个在位座位一条「出站队列」+ 一个发送任务（发送任务在 WS 处理器内运行，
负责真正的 send_json）。广播 = 入队到所有在位座位的队列，把慢/断客户端
与游戏主循环解耦：游戏循环只做 O(1) 的 put_nowait，绝不阻塞在某个连接上。

注意：本类是「每房间一个」的实例。多房间的注册表见 app/game/room.py。
"""

import asyncio
from typing import Optional


class ConnectionManager:
    """seat → 出站队列 / 发送任务 的映射。"""

    def __init__(self) -> None:
        self._queues: dict[int, asyncio.Queue] = {}
        self._tasks: dict[int, asyncio.Task] = {}

    @property
    def connected_seats(self) -> list[int]:
        return list(self._queues.keys())

    def register(self, seat: int, queue: asyncio.Queue, sender_task: asyncio.Task) -> None:
        self._queues[seat] = queue
        self._tasks[seat] = sender_task

    def unregister(self, seat: int) -> None:
        """座位下线：移除队列，取消发送任务（连接已断开，无消息可发）。"""
        self._queues.pop(seat, None)
        task = self._tasks.pop(seat, None)
        if task is not None and not task.done():
            task.cancel()

    def broadcast(self, message: dict) -> None:
        """入队到所有在位座位（同步；unbounded 队列 put_nowait 不会失败）。"""
        for queue in list(self._queues.values()):
            queue.put_nowait(message)

    async def send_to_seat(self, seat: int, message: dict) -> None:
        """向指定座位发送（await 队列入队；座位不在位则丢弃）。"""
        queue = self._queues.get(seat)
        if queue is not None:
            await queue.put(message)

    def send_to_seat_nowait(self, seat: int, message: dict) -> bool:
        """向指定座位发送（同步版）。返回是否成功投递。"""
        queue = self._queues.get(seat)
        if queue is None:
            return False
        queue.put_nowait(message)
        return True

    def is_connected(self, seat: int) -> bool:
        return seat in self._queues
