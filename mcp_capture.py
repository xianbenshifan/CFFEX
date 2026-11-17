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
from typing import Any, Dict, List, Optional

import config

logger = logging.getLogger(__name__)

# 目标站点的抓包特征
QUIZ_HOST_KEYWORD = "quiz-activity.eastmoney.com"
QUIZ_QUESTION_PATH = "/qa/api/quiz/"
QUIZ_ANSWER_PATH = "/qa/api/quiz/answer"


def _as_dict(value: Any) -> Dict[str, Any]:
    """把 JSON 文本或对象转成字典。"""
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


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


def is_quiz_question(entry_or_envelope: Dict[str, Any]) -> bool:
    """判断是否为题目请求。"""
    req = entry_or_envelope.get("request") or {}
    path = str(req.get("path") or "")
    host = str(req.get("host") or req.get("url") or "")
    return QUIZ_HOST_KEYWORD in host and QUIZ_QUESTION_PATH in path and "answer" not in path


def is_quiz_answer(entry_or_envelope: Dict[str, Any]) -> bool:
    """判断是否为答案提交请求。"""
    req = entry_or_envelope.get("request") or {}
    path = str(req.get("path") or "")
    host = str(req.get("host") or req.get("url") or "")
    return QUIZ_HOST_KEYWORD in host and QUIZ_ANSWER_PATH in path


class McpCapturePoller:
    """持续拉取 Charles MCP 的增量抓包并交给处理函数。

    实测单次 query_entries 约 0.21 秒（本地 HTTP + 已有抓包会话），
    因此轮询间隔可以设得很短；间隔在运行时可调，无需重启。
    """

    MIN_INTERVAL = 0.2
    MAX_INTERVAL = 60.0

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
        self.stats: Dict[str, Any] = {
            "started_at": None,
            "polls": 0,
            "entries_seen": 0,
            "questions": 0,
            "answers": 0,
            "last_error": None,
            "last_poll_at": None,
            "last_poll_duration": None,
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
        self.stats["started_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        self._thread = threading.Thread(target=self._loop, daemon=True, name="mcp-capture-poller")
        self._thread.start()
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
        """按间隔拉取增量条目。"""
        from charles_mcp_adapter import CharlesMcpClient

        client = CharlesMcpClient(endpoint=self.endpoint, timeout=30)
        while not self._stop.is_set():
            started = time.time()
            try:
                self._poll_once(client)
                self.stats["last_poll_duration"] = round(time.time() - started, 3)
            except Exception as exc:
                self.stats["last_error"] = f"{type(exc).__name__}: {exc}"
                logger.warning("[收题] 轮询失败: %s", exc)
            # 用可唤醒的等待代替 sleep：改间隔后立即按新值继续
            self._wake.wait(self._interval)
            self._wake.clear()

    def _poll_once(self, client) -> None:
        """拉一次增量并分发。"""
        if not self.capture_id:
            return
        self.stats["polls"] += 1
        self.stats["last_poll_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")

        result = client.query_entries(
            self.capture_id,
            host_contains=QUIZ_HOST_KEYWORD,
            max_items=20,
            include_body_preview=True,
            max_preview_chars=4000,
        )
        items: List[Dict[str, Any]] = []
        if isinstance(result, dict):
            items = result.get("items") or []
        elif isinstance(result, list):
            items = result

        for entry in items:
            if not isinstance(entry, dict):
                continue
            self.stats["entries_seen"] += 1
            envelope = entry_to_envelope(entry)
            if not envelope:
                continue
            if is_quiz_answer(envelope):
                self.stats["answers"] += 1
            elif is_quiz_question(envelope):
                self.stats["questions"] += 1
            else:
                continue
            if self.handler:
                try:
                    self.handler(envelope)
                except Exception as exc:
                    self.stats["last_error"] = f"handler: {type(exc).__name__}: {exc}"
                    logger.exception("[收题] 处理失败")


_GLOBAL_POLLER: Optional[McpCapturePoller] = None


def get_poller(handler=None) -> McpCapturePoller:
    """获取进程级单例收题器。"""
    global _GLOBAL_POLLER
    if _GLOBAL_POLLER is None:
        _GLOBAL_POLLER = McpCapturePoller(handler=handler)
    elif handler is not None and _GLOBAL_POLLER.handler is None:
        _GLOBAL_POLLER.handler = handler
    return _GLOBAL_POLLER
