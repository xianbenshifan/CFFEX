"""
mitmproxy 收题插件。

手机流量进入 mitmproxy 后，本文件在响应到达时直接判断是不是题目或答案。
是的话，把请求和响应收成与现有管线相同的信封，推给答题服务。
Charles 那条轮询链路不经过这里。

启动见 start_mitm.bat。单独运行插件：

    mitmdump --listen-host 0.0.0.0 --listen-port 8080 ^
        --allow-hosts quiz-activity\\.eastmoney\\.com -s mitm_capture.py
"""
# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import logging
import os
import queue
import sys
import threading
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import config
from mcp_capture import is_quiz_answer, is_quiz_question

logger = logging.getLogger(__name__)

_QUEUE: queue.Queue = queue.Queue(maxsize=200)
_WORKER_STARTED = False
_WORKER_LOCK = threading.Lock()


def capture_url() -> str:
    """答题服务上的收题地址。"""
    return config.get("MITM_CAPTURE_URL") or "http://127.0.0.1:8889/mcp/charles/capture"


def _parse_body(value: Any) -> Any:
    """把响应或请求体解析成 JSON。解析不了就保留文本。"""
    if value is None:
        return None
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, (bytes, bytearray)):
        value = value.decode("utf-8", "replace")
    if not isinstance(value, str):
        return value
    text = value.strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


def _message_text(message: Any) -> str:
    """读取 mitmproxy 报文或测试替身的正文。"""
    if message is None:
        return ""
    getter = getattr(message, "get_text", None)
    if getter is None:
        raw = getattr(message, "content", b"") or b""
        if isinstance(raw, str):
            return raw
        return raw.decode("utf-8", "replace")
    try:
        return getter(strict=False) or ""
    except TypeError:
        return getter() or ""


def _header_dict(headers: Any) -> Dict[str, str]:
    """把头部转成普通字典。"""
    if not headers:
        return {}
    try:
        return {str(key): str(value) for key, value in headers.items()}
    except Exception:
        return {}


def envelope_from_parts(
    *,
    method: Any,
    url: Any,
    host: Any,
    path: Any,
    request_headers: Any = None,
    request_body: Any = None,
    status_code: Any = None,
    response_headers: Any = None,
    response_body: Any = None,
) -> Dict[str, Any]:
    """组成答题服务已经在收的那份信封。"""
    return {
        "request": {
            "method": method,
            "url": url,
            "host": host,
            "path": path,
            "headers": _header_dict(request_headers),
            "body": _parse_body(request_body),
        },
        "response": {
            "status_code": status_code,
            "headers": _header_dict(response_headers),
            "body": _parse_body(response_body),
        },
        "metadata": {
            "source": "mitmproxy",
            "ingest_source": "mitmproxy",
        },
    }


def envelope_from_flow(flow: Any) -> Optional[Dict[str, Any]]:
    """从一条 mitmproxy 流量取出信封。没有请求或响应时返回 None。"""
    request = getattr(flow, "request", None)
    response = getattr(flow, "response", None)
    if request is None or response is None:
        return None
    url = getattr(request, "pretty_url", None) or getattr(request, "url", "") or ""
    host = getattr(request, "pretty_host", None) or getattr(request, "host", "") or ""
    path = getattr(request, "path", "") or ""
    if not path and url:
        path = urlsplit(str(url)).path
    return envelope_from_parts(
        method=getattr(request, "method", None),
        url=url,
        host=host,
        path=path,
        request_headers=getattr(request, "headers", None),
        request_body=_message_text(request),
        status_code=getattr(response, "status_code", None),
        response_headers=getattr(response, "headers", None),
        response_body=_message_text(response),
    )


def should_capture(envelope: Dict[str, Any]) -> bool:
    """只留下带题目的响应，以及带判定或下一题的答案提交。"""
    return is_quiz_question(envelope) or is_quiz_answer(envelope)


def _log(message: str) -> None:
    """写到插件日志。mitmproxy 的界面日志失败时不影响收题。"""
    logger.info(message)
    try:
        from mitmproxy import ctx

        ctx.log.info(message)
    except Exception:
        pass


def _post(envelope: Dict[str, Any]) -> None:
    """把信封推给答题服务。失败只记日志，不打断代理。"""
    import requests

    target = capture_url()
    path = (envelope.get("request") or {}).get("path")
    last_error = ""
    for attempt in (1, 2):
        try:
            response = requests.post(target, json=envelope, timeout=120)
            response.raise_for_status()
            _log(f"[mitm] 已推送 {path}")
            return
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            if attempt == 1:
                continue
    _log(f"[mitm] 推送失败 {path}: {last_error}")


def _worker() -> None:
    """后台推送。大模型耗时留在答题服务里，不占手机那条连接。"""
    while True:
        envelope = _QUEUE.get()
        try:
            _post(envelope)
        except Exception:
            logger.exception("[mitm] 推送线程异常")


def _ensure_worker() -> None:
    """推送线程只起一次。"""
    global _WORKER_STARTED
    with _WORKER_LOCK:
        if _WORKER_STARTED:
            return
        threading.Thread(target=_worker, daemon=True, name="mitm-capture").start()
        _WORKER_STARTED = True


def submit_envelope(envelope: Dict[str, Any]) -> None:
    """送出一条信封。测试可设 MITM_CAPTURE_SYNC=1 改成同步推送。"""
    if os.environ.get("MITM_CAPTURE_SYNC") == "1":
        _post(envelope)
        return
    _ensure_worker()
    try:
        _QUEUE.put(envelope, timeout=5)
    except queue.Full:
        _log("[mitm] 推送队列已满，丢弃一条抓包")


class QuizCapture:
    """mitmproxy 加载的插件。"""

    def response(self, flow: Any) -> None:
        """响应已经解密时调用。不是答题流量就直接返回。"""
        envelope = envelope_from_flow(flow)
        if not envelope or not should_capture(envelope):
            return
        submit_envelope(envelope)


addons = [QuizCapture()]
