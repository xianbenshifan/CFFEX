"""
自动答题进度中心。

提供：
1. 线程安全的当前状态；
2. 最近事件历史；
3. SSE 实时事件流；
4. 终端日志和 Web 仪表盘共用同一份状态。
"""
# -*- coding: utf-8 -*-
from __future__ import annotations

import threading
import time
from collections import deque
from datetime import datetime, timezone
from queue import Empty, Queue
from typing import Any, Dict, Generator, Optional


def now_text() -> str:
    """返回带时区的当前时间。"""
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


class ProgressTracker:
    """自动化任务的内存进度总线。"""

    def __init__(self, history_size: int = 500) -> None:
        self._lock = threading.RLock()
        self._sequence = 0
        self._history = deque(maxlen=history_size)
        self._subscribers: set[Queue] = set()
        self._state: Dict[str, Any] = {
            "run_id": None,
            "status": "idle",
            "stage": "idle",
            "message": "等待抓包",
            "started_at": None,
            "updated_at": now_text(),
            "finished_at": None,
            "current": 0,
            "total": None,
            "question_id": None,
            "answer_source": None,
            "similarity": None,
            "transport": None,
            "last_error": None,
        }

    def start_run(self, run_id: Optional[str] = None, total: Optional[int] = None) -> Dict[str, Any]:
        """开始一轮自动化任务。"""
        with self._lock:
            self._state.update(
                {
                    "run_id": run_id or str(int(time.time() * 1000)),
                    "status": "running",
                    "stage": "capture",
                    "message": "等待题目抓包",
                    "started_at": now_text(),
                    "updated_at": now_text(),
                    "finished_at": None,
                    "current": 0,
                    "total": total,
                    "last_error": None,
                }
            )
            return self._publish_locked("run_started", dict(self._state))

    def update(self, stage: str, message: str, **fields: Any) -> Dict[str, Any]:
        """更新当前阶段并广播事件。"""
        with self._lock:
            self._state.update(
                {
                    "status": fields.pop("status", self._state.get("status", "running")),
                    "stage": stage,
                    "message": message,
                    "updated_at": now_text(),
                    **fields,
                }
            )
            return self._publish_locked("progress", dict(self._state))

    def finish(self, message: str = "任务完成", **fields: Any) -> Dict[str, Any]:
        """标记任务成功完成。"""
        return self.update(
            "finished",
            message,
            status="completed",
            finished_at=now_text(),
            **fields,
        )

    def fail(self, message: str, error: Optional[str] = None, **fields: Any) -> Dict[str, Any]:
        """标记任务失败并保留错误信息。"""
        return self.update(
            "error",
            message,
            status="failed",
            last_error=error or message,
            finished_at=now_text(),
            **fields,
        )

    def snapshot(self) -> Dict[str, Any]:
        """返回当前状态和最近事件。"""
        with self._lock:
            return {
                "state": dict(self._state),
                "last_sequence": self._sequence,
                "recent_events": list(self._history)[-20:],
            }

    def events_since(self, sequence: int = 0) -> list[Dict[str, Any]]:
        """读取指定序号之后的事件。"""
        with self._lock:
            return [event for event in self._history if event["sequence"] > sequence]

    def subscribe(self) -> Queue:
        """创建一个 SSE 订阅队列。"""
        queue: Queue = Queue()
        with self._lock:
            self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: Queue) -> None:
        """释放 SSE 订阅队列。"""
        with self._lock:
            self._subscribers.discard(queue)

    def stream(self, queue: Queue) -> Generator[Dict[str, Any], None, None]:
        """持续读取订阅事件，供 Flask SSE 使用。"""
        try:
            while True:
                try:
                    yield queue.get(timeout=20)
                except Empty:
                    yield {
                        "event": "heartbeat",
                        "sequence": self._sequence,
                        "data": self.snapshot()["state"],
                    }
        finally:
            self.unsubscribe(queue)

    def _publish_locked(self, event_name: str, data: Dict[str, Any]) -> Dict[str, Any]:
        """在持锁状态下创建并广播事件。"""
        self._sequence += 1
        event = {
            "event": event_name,
            "sequence": self._sequence,
            "at": now_text(),
            "data": data,
        }
        self._history.append(event)
        for subscriber in list(self._subscribers):
            subscriber.put(event)
        return event


_GLOBAL_TRACKER = ProgressTracker()


def get_progress_tracker() -> ProgressTracker:
    """获取进程级单例进度中心。"""
    return _GLOBAL_TRACKER
