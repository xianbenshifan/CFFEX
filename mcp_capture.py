"""
从 Charles MCP 持续收题。

`charles-mcp` 暴露的是「查询抓包条目」的接口，不是推送。因此这里用一个
轮询循环：拉增量条目 → 挑出题目/答案相关请求 → 转成项目内部信封 →
交给 CapturePipeline 或 QuizAutomation 处理。

这条链路不需要任何 Agent：MCP 通过 streamable-http 暴露在本地端口，
本模块用标准 MCP 客户端连过去。
"""
# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import logging
import re
import threading
import time
import queue
from collections import OrderedDict
from typing import Any, Dict, List, Optional

import config

logger = logging.getLogger(__name__)

# 目标站点的抓包特征
QUIZ_HOST_KEYWORD = "quiz-activity.eastmoney.com"
QUIZ_QUESTION_PATH = "/qa/api/quiz/"
QUIZ_ANSWER_PATH = "/qa/api/quiz/answer"


def _as_dict(value: Any) -> Dict[str, Any]:
    """把 JSON 文本或对象转成字典。

    请求体若是 {"text": "<json>"}，展开内层，否则 questionId 进不了归档。
    """
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return {}
    if not isinstance(value, dict):
        return {}
    text = value.get("text")
    if isinstance(text, str) and "questionId" not in value and "answerIndex" not in value:
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            return value
        if isinstance(parsed, dict):
            return parsed
    return value


def _pick(entry: Dict[str, Any], *names: str) -> Any:
    """按候选字段名取值，兼容不同版本的字段命名。"""
    for name in names:
        if name in entry and entry[name] is not None:
            return entry[name]
    return None


def entry_to_envelope(entry: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """把一条 MCP 抓包条目转成项目内部信封。

    只保留管线需要的字段，避免把整个 MCP 结构塞进归档。
    """
    request = _as_dict(
        _pick(entry, "request", "requestDetail", "request_detail") or {}
    )
    response = _as_dict(
        _pick(entry, "response", "responseDetail", "response_detail") or {}
    )

    # 有些版本把请求/响应拆成平铺字段
    method = _pick(entry, "method") or _pick(request, "method")
    host = _pick(entry, "host") or _pick(request, "host")
    path = _pick(entry, "path") or _pick(request, "path")
    status = _pick(entry, "status") or _pick(response, "status")

    # 请求体
    req_body = (
        _pick(request, "body", "bodyText", "body_text")
        or _pick(entry, "request_body", "requestBody", "request_body_preview")
    )
    req_body = _as_dict(req_body) if not isinstance(req_body, dict) else req_body
    if isinstance(req_body, str):
        req_body = _as_dict(req_body)

    # 响应体
    resp_body = (
        _pick(response, "body", "bodyText", "body_text")
        or _pick(entry, "response_body", "responseBody", "response_body_preview")
    )
    if isinstance(resp_body, str):
        resp_body = _as_dict(resp_body)

    if not (host or path or resp_body):
        return None

    return {
        "request": {
            "method": method,
            "url": _pick(entry, "url") or (f"https://{host}{path}" if host and path else None),
            "host": host,
            "path": path,
            "headers": _pick(request, "headers") or {},
            "body": req_body,
        },
        "response": {
            "status_code": int(status) if str(status).isdigit() else status,
            "headers": _pick(response, "headers") or {},
            "body": resp_body,
        },
        "metadata": {
            "entry_id": _pick(entry, "entry_id", "entryId", "id"),
            "source": "charles_mcp_http",
        },
    }


def _has_next_question(body: Any) -> bool:
    """响应体里是否真的带了一道题。

    东方财富的题目总是藏在 nextQuestion 里（开局响应或提交答案响应），
    /status 之类的接口 data 为 null，必须排除，否则会触发无用的大模型调用。
    """
    if not body:
        return False
    if not isinstance(body, dict):
        return False
    data = body.get("data")
    if not isinstance(data, dict):
        return False
    nq = data.get("nextQuestion")
    return isinstance(nq, dict) and bool(nq.get("questionId"))


def is_quiz_question(entry_or_envelope: Dict[str, Any]) -> bool:
    """判断是否为带题目数据的请求。

    必须真的有 nextQuestion，否则 /status 这类空接口也会被误当题目，
    白白触发一次大模型调用（实测每次 8 秒）。
    """
    req = entry_or_envelope.get("request") or {}
    res = entry_or_envelope.get("response") or {}
    path = str(req.get("path") or "")
    host = str(req.get("host") or req.get("url") or "")
    if QUIZ_HOST_KEYWORD not in host or QUIZ_QUESTION_PATH not in path:
        return False
    if "answer" in path or "/status" in path or "/config/" in path:
        return False
    return _has_next_question(res.get("body"))


def is_quiz_answer(entry_or_envelope: Dict[str, Any]) -> bool:
    """判断是否为带判定结果或下一题的答案提交。"""
    req = entry_or_envelope.get("request") or {}
    res = entry_or_envelope.get("response") or {}
    path = str(req.get("path") or "")
    host = str(req.get("host") or req.get("url") or "")
    if QUIZ_HOST_KEYWORD not in host or QUIZ_ANSWER_PATH not in path:
        return False
    body = res.get("body")
    if not isinstance(body, dict):
        return False
    data = body.get("data")
    if not isinstance(data, dict):
        return False
    # 有判定结果，或带出了下一题，才算有价值的答案响应
    return data.get("correct") is not None or _has_next_question(body)


class McpCapturePoller:
    """持续拉取 Charles MCP 的增量抓包并交给处理函数。

    实测单次 query_entries 约 0.21 秒（本地 HTTP + 已有抓包会话），
    因此轮询间隔可以设得很短；间隔在运行时可调，无需重启。
    """

    MIN_INTERVAL = 0.2
    MAX_INTERVAL = 60.0
    # 连续多少次轮询超时就自动停掉收题，避免把 Charles 拖死
    SLOW_POLL_LIMIT = 3
    # 单次轮询超过这个秒数算"慢轮询"
    SLOW_POLL_SECONDS = 3.0

    def __init__(self, handler=None, endpoint: Optional[str] = None) -> None:
        self.handler = handler
        self.endpoint = endpoint or config.get("CHARLES_MCP_URL")
        self._interval = self._clamp(config.get_float("MCP_POLL_INTERVAL", 1.0))
        self.running = False
        self.capture_id: Optional[str] = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        # 间隔变化时唤醒等待，让新值立即生效
        self._wake = threading.Event()
        self._slow_streak = 0
        # 抓包游标：只拉新增，避免同一批条目被反复处理
        self._cursor = 0
        # 已处理过的 entry_id，游标失效时也能兜住重复
        # 插入顺序即处理顺序，超限时丢掉最早的，避免 set 无序截断后把刚处理的又放出来
        self._seen_ids: OrderedDict[str, None] = OrderedDict()
        # 处理队列：轮询只入队，后台线程慢慢处理（大模型调用可能 3~6 秒）
        self._queue: queue.Queue = queue.Queue(maxsize=200)
        self._worker_stop = threading.Event()
        self._worker_thread: Optional[threading.Thread] = None
        self.stats: Dict[str, Any] = {
            "started_at": None,
            "polls": 0,
            "entries_seen": 0,
            "questions": 0,
            "answers": 0,
            "handled": 0,
            "queued": 0,
            "last_error": None,
            "last_poll_at": None,
            "last_poll_duration": None,
            "last_handle_duration": None,
            "slow_polls": 0,
            "tripped": False,
        }

    # ---- 间隔 ----

    @classmethod
    def _clamp(cls, value: float) -> float:
        """把间隔限制在合理范围，避免 0 造成空转。"""
        try:
            value = float(value)
        except (TypeError, ValueError):
            return 1.0
        return max(cls.MIN_INTERVAL, min(cls.MAX_INTERVAL, value))

    @property
    def interval(self) -> float:
        """当前轮询间隔（秒）。"""
        return self._interval

    def set_interval(self, value: float) -> Dict[str, Any]:
        """运行时调整轮询间隔，立即生效。非法输入回落到默认值。"""
        try:
            requested = float(value)
        except (TypeError, ValueError):
            requested = None

        clamped = self._clamp(requested if requested is not None else 1.0)
        old = self._interval
        self._interval = clamped
        self._wake.set()
        logger.info("[收题] 轮询间隔 %s -> %s 秒", old, clamped)
        return {
            "interval": clamped,
            "previous": old,
            "clamped": requested is None or clamped != requested,
            "invalid": requested is None,
        }

    # ---- 生命周期 ----

    def start(self) -> Dict[str, Any]:
        """启动抓包会话并开始轮询。"""
        if self.running:
            return {"status": "already_running", "capture_id": self.capture_id}
        if not self.endpoint:
            return {"status": "disabled", "error": "未配置 CHARLES_MCP_URL"}

        from charles_mcp_adapter import CharlesMcpClient, CharlesMcpError

        client = CharlesMcpClient(endpoint=self.endpoint, timeout=30)

        # 上次异常退出时可能残留活动会话，MCP 会拒绝再开新的，先清掉。
        self._close_leftover_capture(client)

        try:
            result = client.start_capture(reset_session=True, adopt_existing=True)
        except CharlesMcpError as exc:
            return {"status": "failed", "error": str(exc)}

        capture_id = (result or {}).get("capture_id") if isinstance(result, dict) else None
        if not capture_id:
            return {"status": "failed", "error": f"未拿到 capture_id: {result}"}

        self.capture_id = capture_id
        self.running = True
        self._stop.clear()
        self._wake.clear()
        self._slow_streak = 0
        # 新会话从零开始：游标归零、去重集合清空
        self._cursor = 0
        self._seen_ids = OrderedDict()
        self.stats["tripped"] = False
        self.stats["started_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        self._thread = threading.Thread(target=self._loop, daemon=True, name="mcp-capture-poller")
        self._thread.start()
        # 起一个后台消费者，让慢处理不占用轮询节奏
        self._worker_stop.clear()
        self._worker_thread = threading.Thread(
            target=self._worker, daemon=True, name="mcp-capture-worker"
        )
        self._worker_thread.start()
        logger.info("[收题] 已启动，capture_id=%s", capture_id)
        return {"status": "running", "capture_id": capture_id}

    def _close_leftover_capture(self, client) -> None:
        """关闭 MCP 里遗留的活动抓包会话。

        MCP 同一时刻只允许一个活动会话；若上次进程被强杀，
        会话会残留并导致新的 start_live_capture 直接报错。
        """
        try:
            status = client.status()
        except Exception as exc:
            logger.warning("[收题] 无法读取 MCP 状态: %s", exc)
            return

        active = ((status or {}).get("live_capture") or {}).get("active_capture") or {}
        leftover = active.get("capture_id")
        if not leftover:
            return
        try:
            client.stop_capture(str(leftover), persist=False)
            logger.info("[收题] 已清理遗留会话 %s", leftover)
        except Exception as exc:
            logger.warning("[收题] 清理遗留会话失败: %s", exc)

    def stop(self) -> Dict[str, Any]:
        """停止轮询并关闭抓包会话。"""
        if not self.running:
            return {"status": "not_running"}
        self._stop.set()
        self._wake.set()  # 立刻打断等待，避免最长 60 秒的延迟
        self._worker_stop.set()
        self.running = False
        if self.capture_id:
            try:
                from charles_mcp_adapter import CharlesMcpClient

                CharlesMcpClient(endpoint=self.endpoint, timeout=15).stop_capture(
                    self.capture_id, persist=False
                )
            except Exception as exc:
                logger.warning("[收题] 关闭抓包会话失败: %s", exc)
        logger.info("[收题] 已停止")
        return {"status": "stopped", "capture_id": self.capture_id, "stats": dict(self.stats)}

    # ---- 轮询 ----

    def _loop(self) -> None:
        """按间隔拉取增量条目。

        熔断只衡量 Charles 查询本身的耗时，不含后处理（大模型调用可能 3~6 秒，
        若计入会把正常的推理延迟误判成 Charles 故障）。
        """
        from charles_mcp_adapter import CharlesMcpClient

        client = CharlesMcpClient(endpoint=self.endpoint, timeout=30)
        while not self._stop.is_set():
            fetch_seconds = 0.0
            try:
                fetch_seconds = self._poll_once(client)
                self.stats["last_poll_duration"] = round(fetch_seconds, 3)
                # 熔断：只看拉取耗时，连续多次慢说明 Charles 快撑不住了
                if fetch_seconds > self.SLOW_POLL_SECONDS:
                    self._slow_streak += 1
                    self.stats["slow_polls"] += 1
                    logger.warning(
                        "[收题] 拉取耗时 %.1fs 超过 %.0fs（连续 %d 次）",
                        fetch_seconds, self.SLOW_POLL_SECONDS, self._slow_streak,
                    )
                    if self._slow_streak >= self.SLOW_POLL_LIMIT:
                        self.stats["tripped"] = True
                        self.stats["last_error"] = (
                            f"连续 {self._slow_streak} 次拉取超过 "
                            f"{self.SLOW_POLL_SECONDS:.0f}s，已自动停止以保护 Charles"
                        )
                        logger.error("[收题] %s", self.stats["last_error"])
                        self._stop.set()
                        break
                else:
                    self._slow_streak = 0
            except Exception as exc:
                self.stats["last_error"] = f"{type(exc).__name__}: {exc}"
                logger.warning("[收题] 轮询失败: %s", exc)
                # 会话被 Charles 清掉后，继续用旧 capture_id 只会一直报 not found。
                if "was not found" in str(exc):
                    self._reopen_capture(client)
            # 用可唤醒的等待代替 sleep：改间隔后立即按新值继续
            self._wake.wait(self._interval)
            self._wake.clear()
        self.running = False
        # 熔断或被要求停止时，顺手释放 MCP 侧的会话，避免残留导致无法重启
        self._release_capture()

    def _reopen_capture(self, client) -> None:
        """抓包会话失效时重新打开，并从头拉增量。"""
        try:
            self._close_leftover_capture(client)
            result = client.start_capture(reset_session=True, adopt_existing=True)
        except Exception as exc:
            logger.warning("[收题] 重新打开会话失败: %s", exc)
            return
        capture_id = (result or {}).get("capture_id") if isinstance(result, dict) else None
        if not capture_id:
            logger.warning("[收题] 重新打开会话没有拿到 capture_id: %s", result)
            return
        self.capture_id = capture_id
        self._cursor = 0
        self._seen_ids = OrderedDict()
        self.stats["last_error"] = None
        logger.info("[收题] 会话已失效，已重新打开 %s", capture_id)

    def _poll_once(self, client) -> float:
        """拉一次增量并把待处理的条目投进后台队列，返回拉取耗时。

        查询与处理解耦：大模型调用可能 3~6 秒，若在轮询线程里同步执行，
        会把轮询间隔拉长并被熔断误判成 Charles 故障。
        """
        if not self.capture_id:
            return 0.0
        self.stats["polls"] += 1
        self.stats["last_poll_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")

        started = time.time()
        # 必须带 cursor：否则每次都会从头重扫全部条目，同一批数据被反复处理。
        result = client.query_entries(
            self.capture_id,
            host_contains=QUIZ_HOST_KEYWORD,
            cursor=self._cursor,
            max_items=20,
            include_body_preview=True,
            max_preview_chars=4000,
        )
        fetch_seconds = time.time() - started

        items: List[Dict[str, Any]] = []
        next_cursor = self._cursor
        if isinstance(result, dict):
            items = result.get("items") or []
            nc = result.get("next_cursor")
            if isinstance(nc, int) and nc >= 0:
                next_cursor = nc
        elif isinstance(result, list):
            items = result
        # 推进游标，下一轮只看新增
        self._cursor = next_cursor

        for entry in items:
            if not isinstance(entry, dict):
                continue
            self.stats["entries_seen"] += 1
            envelope = entry_to_envelope(entry)
            if not envelope:
                continue

            # 去重：同一 entry 只处理一次，即使游标失效也不会重复
            # entry_id 在条目顶层，不在 metadata 里
            entry_id = entry.get("entry_id") or (envelope.get("metadata") or {}).get("entry_id")
            if entry_id is not None:
                key = str(entry_id)
                if key in self._seen_ids:
                    continue
                self._seen_ids[key] = None
                # 只保留最近处理过的条目，丢掉最早的
                if len(self._seen_ids) > 5000:
                    for _ in range(2500):
                        self._seen_ids.popitem(last=False)

            if is_quiz_answer(envelope):
                self.stats["answers"] += 1
            elif is_quiz_question(envelope):
                self.stats["questions"] += 1
            else:
                continue
            # 只入队，不在此处调用大模型
            self._queue.put(envelope)
            self.stats["queued"] = self.stats.get("queued", 0) + 1
        return fetch_seconds

    def _worker(self) -> None:
        """后台处理线程：从队列取信封交给 handler，慢调用不影响轮询节奏。"""
        while not self._worker_stop.is_set():
            try:
                envelope = self._queue.get(timeout=1.0)
            except queue.Empty:
                continue
            if not self.handler:
                continue
            try:
                t0 = time.time()
                self.handler(envelope)
                self.stats["handled"] += 1
                self.stats["last_handle_duration"] = round(time.time() - t0, 3)
            except Exception as exc:
                self.stats["last_error"] = f"handler: {type(exc).__name__}: {exc}"
                logger.exception("[收题] 处理失败")

    def _release_capture(self) -> None:
        """释放 MCP 侧的活动会话，避免熔断后残留导致无法重新启动。"""
        capture_id = self.capture_id
        if not capture_id:
            return
        try:
            from charles_mcp_adapter import CharlesMcpClient

            CharlesMcpClient(endpoint=self.endpoint, timeout=10).stop_capture(
                str(capture_id), persist=False
            )
            logger.info("[收题] 已释放会话 %s", capture_id)
        except Exception as exc:
            logger.warning("[收题] 释放会话失败（可忽略）: %s", exc)


_GLOBAL_POLLER: Optional[McpCapturePoller] = None


def get_poller(handler=None) -> McpCapturePoller:
    """获取进程级单例收题器。"""
    global _GLOBAL_POLLER
    if _GLOBAL_POLLER is None:
        _GLOBAL_POLLER = McpCapturePoller(handler=handler)
    elif handler is not None and _GLOBAL_POLLER.handler is None:
        _GLOBAL_POLLER.handler = handler
    return _GLOBAL_POLLER
