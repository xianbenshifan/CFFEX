"""
Charles MCP 适配层。

`charles-mcp` 官方入口只跑 stdio，必须由 Agent 拉起子进程，独立工作流无法使用。
`charles_mcp_http.py` 把它以 `streamable-http` 方式暴露成本地 HTTP 端点，
本模块用标准 MCP 客户端连过去，因此不需要任何 Agent。

端点默认 `http://127.0.0.1:8931/mcp`，可用 `.env` 的 `CHARLES_MCP_URL` 覆盖。

MCP 是异步协议，而调用方（Flask 路由、同步脚本）是同步的，
因此每次调用都在独立事件循环里跑完整会话。
"""
# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
import json
from typing import Any, Dict, List, Optional

import config


def default_endpoint() -> str:
    """返回 MCP 端点地址。"""
    return config.get("CHARLES_MCP_URL") or "http://127.0.0.1:8931/mcp"


def _content_to_text(content: Any) -> List[str]:
    """把 MCP 返回的内容块拍平成文本。"""
    texts: List[str] = []
    for item in content or []:
        text = getattr(item, "text", None)
        if text:
            texts.append(text)
    return texts


def _maybe_json(text: str) -> Any:
    """尽量把 JSON 文本解析成对象，失败时保留原文。"""
    stripped = text.strip()
    if not stripped or stripped[0] not in "[{":
        return text
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        return text


class CharlesMcpError(RuntimeError):
    """MCP 调用失败。"""


def _root_cause(exc: BaseException) -> str:
    """剥掉 ExceptionGroup 外壳，取出最内层的真实原因。"""
    seen = 0
    current: BaseException = exc
    while getattr(current, "exceptions", None) and seen < 8:
        current = current.exceptions[0]  # type: ignore[attr-defined]
        seen += 1
    text = str(current) or type(current).__name__
    return f"{type(current).__name__}: {text}" if str(current) else type(current).__name__


class CharlesMcpClient:
    """通过 HTTP 连接 Charles MCP 的同步客户端。"""

    def __init__(self, endpoint: Optional[str] = None, timeout: float = 20.0) -> None:
        self.endpoint = (endpoint or default_endpoint()).rstrip("/")
        self.timeout = timeout

    # ---- 底层 ----

    async def _call_async(self, tool_name: str, arguments: Dict[str, Any]) -> Any:
        """在一个完整会话里调用指定工具。"""
        try:
            from mcp import ClientSession
            from mcp.client.streamable_http import streamablehttp_client
        except ImportError as exc:
            raise CharlesMcpError(
                "缺少 mcp 客户端依赖。请先安装：pip install mcp"
            ) from exc

        async with streamablehttp_client(self.endpoint) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool(tool_name, arguments or {})

        if getattr(result, "isError", False):
            detail = "；".join(_content_to_text(result.content)) or "未返回错误详情"
            raise CharlesMcpError(f"{tool_name} 调用失败: {detail}")

        texts = _content_to_text(result.content)
        if not texts:
            # 有些工具通过 structuredContent 返回结构化结果
            structured = getattr(result, "structuredContent", None)
            return structured if structured is not None else None
        combined = "\n".join(texts)
        return _maybe_json(combined)

    def call_tool(self, tool_name: str, arguments: Optional[Dict[str, Any]] = None) -> Any:
        """同步调用一个 MCP 工具。"""
        try:
            return asyncio.run(self._call_async(tool_name, arguments or {}))
        except CharlesMcpError:
            raise
        except BaseException as exc:  # anyio 会把底层错误包成 ExceptionGroup
            raise CharlesMcpError(
                f"无法调用 {tool_name}，端点 {self.endpoint} 不可用: {_root_cause(exc)}"
            ) from exc

    async def _list_tools_async(self) -> List[str]:
        from mcp import ClientSession
        from mcp.client.streamable_http import streamablehttp_client

        async with streamablehttp_client(self.endpoint) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = await session.list_tools()
        return [tool.name for tool in tools.tools]

    def list_tools(self) -> List[str]:
        """列出端点暴露的工具名。"""
        try:
            return asyncio.run(self._list_tools_async())
        except BaseException as exc:
            raise CharlesMcpError(
                f"无法连接 MCP 端点 {self.endpoint}: {_root_cause(exc)}"
            ) from exc

    def is_available(self) -> bool:
        """探测端点是否可握手。"""
        try:
            self.list_tools()
            return True
        except Exception:
            return False

    # ---- 业务封装 ----

    def status(self) -> Any:
        """读取 Charles 连接状态。"""
        return self.call_tool("charles_status", {})

    def start_capture(self, reset_session: bool = True, adopt_existing: bool = False) -> Any:
        """启动或接管实时抓包，返回结果里含后续调用需要的 capture_id。"""
        return self.call_tool(
            "start_live_capture",
            {"reset_session": reset_session, "adopt_existing": adopt_existing},
        )

    def stop_capture(self, capture_id: str, persist: bool = True) -> Any:
        """停止抓包会话。"""
        return self.call_tool("stop_live_capture", {"capture_id": capture_id, "persist": persist})

    def query_entries(
        self,
        capture_id: str,
        host_contains: Optional[str] = None,
        path_contains: Optional[str] = None,
        cursor: int = 0,
        max_items: int = 10,
        include_body_preview: bool = True,
    ) -> Any:
        """按条件查询实时抓包条目。"""
        args: Dict[str, Any] = {
            "capture_id": capture_id,
            "cursor": cursor,
            "max_items": max_items,
            "include_body_preview": include_body_preview,
        }
        if host_contains:
            args["host_contains"] = host_contains
        if path_contains:
            args["path_contains"] = path_contains
        return self.call_tool("query_live_capture_entries", args)

    def get_entry_detail(self, entry_id: str) -> Any:
        """读取单条请求/响应详情。"""
        return self.call_tool("get_traffic_entry_detail", {"entry_id": entry_id})


def make_capture_envelope(
    request_data: Dict[str, Any],
    response_data: Dict[str, Any],
    metadata: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """把抓包对象标准化为项目内部信封。"""
    return {
        "request": request_data or {},
        "response": response_data or {},
        "metadata": metadata or {},
    }
