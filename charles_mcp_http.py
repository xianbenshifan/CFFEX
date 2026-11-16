"""
把 charles-mcp 以 HTTP 方式暴露出来。

`charles-mcp` 的官方入口把传输方式写死成 stdio：

    server.run(transport="stdio")

stdio 的 MCP server 是 Agent 的子进程，独立工作流无法调用。底层
`FastMCP` 本身支持 `streamable-http`，因此这里复用它的 `create_server()`
组装逻辑，只替换传输方式，让服务变成一个可以长期驻留、任何人用 HTTP
就能访问的端点。

启动：

    python charles_mcp_http.py

默认监听 127.0.0.1:8931，MCP 端点为：

    http://127.0.0.1:8931/mcp
"""
# -*- coding: utf-8 -*-
from __future__ import annotations

import logging
import os
import sys

import config

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8931


def _setup_logging() -> None:
    """把日志写到文件并同时打到控制台。"""
    level = os.getenv("CHARLES_MCP_LOG_LEVEL", "INFO").upper()
    handlers = [logging.StreamHandler(sys.stdout)]
    log_dir = os.getenv("CHARLES_LOG_DIR") or os.path.join(os.getcwd(), "log")
    try:
        os.makedirs(log_dir, exist_ok=True)
        handlers.append(logging.FileHandler(os.path.join(log_dir, "charles_mcp_http.log"), encoding="utf-8"))
    except OSError:
        pass
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        handlers=handlers,
    )


def build_server(host: str, port: int):
    """组装 FastMCP 实例并设置为 HTTP 监听地址。"""
    try:
        from charles_mcp.server import create_server
        from mcp.server.fastmcp import FastMCP  # noqa: F401  仅用于确认依赖存在
    except ImportError as exc:
        raise SystemExit(
            "缺少 charles-mcp 依赖。请先安装：\n"
            "    pip install charles-mcp\n"
            f"原始错误：{exc}"
        ) from exc

    server = create_server()
    # FastMCP 的 settings 决定 HTTP 监听的地址与端口。
    settings = server.settings
    settings.host = host
    settings.port = port
    return server


def main() -> None:
    """以 streamable-http 传输启动 Charles MCP。"""
    _setup_logging()
    logger = logging.getLogger(__name__)

    host = config.get("CHARLES_MCP_HTTP_HOST", DEFAULT_HOST)
    port = config.get_int("CHARLES_MCP_HTTP_PORT", DEFAULT_PORT)

    server = build_server(host, port)
    endpoint = f"http://{host}:{port}/mcp"
    logger.info("=" * 60)
    logger.info("Charles MCP（HTTP 传输）启动")
    logger.info("监听地址: %s", endpoint)
    logger.info("把该地址写入 .env 的 CHARLES_MCP_URL 即可让服务直连")
    logger.info("=" * 60)

    server.run(transport="streamable-http")


if __name__ == "__main__":
    main()
