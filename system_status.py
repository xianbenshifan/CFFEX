"""
运行时就绪状态检测。

给前端面板提供四类事实：
1. 题库在线状态（题库、向量、语义模型是否真的可用）；
2. 配套文件状态（缺失、Git LFS 指针文件、大小异常都能看出来）；
3. 抓包工具在线状态（最近一次抓到包的时间、Charles 进程、MCP 端点）；
4. 当前是否可以开始答题，并把阻塞项逐条列出来。

判据只认可验证的事实，不猜。
"""
# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import config

ROOT = Path(__file__).resolve().parent

# Git LFS 指针文件只有一百多字节，必须按真实大小判断，否则会把指针当成权重。
LFS_POINTER_LIMIT = 1024

# 超过这个秒数没有抓到任何包，就认为抓包工具没有在工作。
CAPTURE_STALE_SECONDS = 180


def _mtime_text(path: Path) -> Optional[str]:
    """返回文件最后修改时间。"""
    try:
        stamp = path.stat().st_mtime
    except OSError:
        return None
    return datetime.fromtimestamp(stamp, tz=timezone.utc).astimezone().isoformat(timespec="seconds")


def _age_seconds(path: Path) -> Optional[float]:
    """返回文件距今秒数。"""
    try:
        return max(0.0, time.time() - path.stat().st_mtime)
    except OSError:
        return None


def inspect_file(path: Path, min_bytes: int = 1) -> Dict[str, Any]:
    """检查单个文件的可用性，区分缺失、指针文件与正常文件。"""
    item: Dict[str, Any] = {
        "path": str(path),
        "name": path.name,
        "exists": False,
        "ok": False,
        "bytes": 0,
        "size_mb": 0.0,
        "updated_at": None,
        "issue": None,
    }
    if not path.exists():
        item["issue"] = "文件不存在"
        return item

    item["exists"] = True
    try:
        size = path.stat().st_size
    except OSError as exc:
        item["issue"] = f"无法读取: {exc}"
        return item

    item["bytes"] = size
    item["size_mb"] = round(size / 1024 / 1024, 2)
    item["updated_at"] = _mtime_text(path)

    if size < LFS_POINTER_LIMIT and path.suffix in (".safetensors", ".bin"):
        item["issue"] = "疑似 Git LFS 指针，需要 git lfs pull 取回真实文件"
        return item
    if size < min_bytes:
        item["issue"] = f"文件过小（{size} 字节，期望至少 {min_bytes}）"
        return item

    item["ok"] = True
    return item


def _model_dir() -> Path:
    """定位语义模型目录，与 semantic_search 的缓存约定一致。"""
    home = config.get("HF_HOME") or os.path.join(os.getcwd(), "hf_cache")
    return Path(home) / "models" / "paraphrase-multilingual-MiniLM-L12-v2"


def check_question_bank(server: Any) -> Dict[str, Any]:
    """题库在线状态：题库文本、向量缓存、语义模型三者是否都就绪。"""
    database_path = ROOT / "database.json"
    database_file = inspect_file(database_path, min_bytes=1024)

    question_count = 0
    if server._database:
        question_count = len(server._database)

    vector_count = len(server._meta) if server._meta else 0
    model_loaded = server._model is not None
    search_error = getattr(server, "_SEARCH_IMPORT_ERROR", None)

    # 三级能力：精确匹配只依赖题库文本，语义检索还要向量与模型。
    exact_ready = question_count > 0
    semantic_ready = model_loaded and vector_count > 0

    if semantic_ready and exact_ready:
        state, message = "online", f"题库 {question_count} 题，向量 {vector_count} 条，语义检索可用"
    elif exact_ready:
        state = "degraded"
        reason = search_error or ("向量尚未加载" if not vector_count else "语义模型未加载")
        message = f"题库 {question_count} 题可精确匹配，语义检索不可用：{reason}"
    else:
        state, message = "offline", "题库未加载，无法答题"

    return {
        "state": state,
        "message": message,
        "question_count": question_count,
        "vector_count": vector_count,
        "model_loaded": model_loaded,
        "exact_ready": exact_ready,
        "semantic_ready": semantic_ready,
        "search_error": search_error,
        "database_file": database_file,
    }


def check_supporting_files(server: Any) -> Dict[str, Any]:
    """配套文件状态：题库、向量、元数据、模型权重、配置文件。"""
    model_dir = _model_dir()
    weights = model_dir / "model.safetensors"
    if not weights.exists():
        weights = model_dir / "pytorch_model.bin"

    files: List[Dict[str, Any]] = [
        inspect_file(ROOT / "database.json", min_bytes=1024),
        inspect_file(ROOT / "question_embeddings.npy", min_bytes=1024),
        inspect_file(ROOT / "question_meta.json", min_bytes=2),
        inspect_file(weights, min_bytes=1024 * 1024),
        dict(inspect_file(model_dir / "config.json", min_bytes=2), required=False),
        dict(inspect_file(ROOT / ".env"), required=False),
    ]
    for item in files:
        item.setdefault("required", True)

    missing = [f["name"] for f in files if not f["ok"] and f["required"]]
    optional_missing = [f["name"] for f in files if not f["ok"] and not f["required"]]

    if missing:
        state = "offline"
        message = "缺少必需文件：" + "、".join(missing)
    elif optional_missing:
        state = "degraded"
        message = "核心文件齐备，可选文件缺失：" + "、".join(optional_missing)
    else:
        state, message = "online", "配套文件齐备"

    return {"state": state, "message": message, "files": files}


def _check_mirror_channel() -> Dict[str, Any]:
    """检测 Mirror 文件通道：目录是否存在、最近写入时间、监听进程是否在跑。"""
    mirror_dir = Path(os.getcwd()) / "123321"
    if not mirror_dir.exists():
        # 换过工作目录时回落到脚本所在目录
        mirror_dir = ROOT / "123321"

    result: Dict[str, Any] = {
        "available": False,
        "directory": str(mirror_dir),
        "exists": mirror_dir.exists(),
        "file_count": 0,
        "latest_write_at": None,
        "idle_seconds": None,
        "watcher_running": False,
        "message": "",
    }
    if not mirror_dir.exists():
        result["message"] = f"Mirror 目录不存在：{mirror_dir}"
        return result

    newest_mtime: Optional[float] = None
    count = 0
    try:
        for path in mirror_dir.rglob("*"):
            if not path.is_file():
                continue
            count += 1
            try:
                mtime = path.stat().st_mtime
            except OSError:
                continue
            if newest_mtime is None or mtime > newest_mtime:
                newest_mtime = mtime
    except OSError as exc:
        result["message"] = f"无法读取 Mirror 目录: {exc}"
        return result

    result["file_count"] = count
    if newest_mtime is not None:
        result["latest_write_at"] = (
            datetime.fromtimestamp(newest_mtime, tz=timezone.utc)
            .astimezone()
            .isoformat(timespec="seconds")
        )
        result["idle_seconds"] = int(max(0.0, time.time() - newest_mtime))

    # 监听进程：watch_charles_mirror.py 常驻，按命令行特征识别
    result["watcher_running"] = _process_running("watch_charles_mirror")
    result["available"] = count > 0
    if not result["watcher_running"]:
        result["message"] = "Mirror 目录有文件，但文件监听未运行"
    elif count == 0:
        result["message"] = "文件监听在运行，Mirror 目录暂无文件"
    else:
        result["message"] = f"文件监听运行中，目录内 {count} 个文件"
    return result


def _process_running(keyword: str) -> bool:
    """按进程命令行关键字判断某脚本是否在运行。"""
    try:
        import psutil

        for proc in psutil.process_iter(["name", "cmdline"]):
            cmdline = proc.info.get("cmdline") or []
            joined = " ".join(cmdline).lower()
            if keyword.lower() in joined:
                return True
    except ImportError:
        return False
    except Exception:
        return False
    return False


def check_capture_channel() -> Dict[str, Any]:
    """判定当前实际生效的抓包通道。

    stdio MCP 必须由 Agent 拉起子进程，独立工作流无法使用，
    因此只把 HTTP MCP 与 Mirror 文件监听视为可用通道。

    配置了 CHARLES_MCP_URL 不代表能用，必须真实握手成功才算在线。
    """
    mcp_url = config.get("CHARLES_MCP_URL")
    http_mcp_configured = bool(mcp_url)
    mcp_probe: Dict[str, Any] = {
        "configured": http_mcp_configured,
        "url": mcp_url,
        "reachable": False,
        "tool_count": 0,
        "error": None,
    }

    if http_mcp_configured:
        try:
            from charles_mcp_adapter import CharlesMcpClient

            client = CharlesMcpClient(endpoint=mcp_url, timeout=8.0)
            tools = client.list_tools()
            mcp_probe["reachable"] = True
            mcp_probe["tool_count"] = len(tools)
        except Exception as exc:
            mcp_probe["error"] = str(exc)[:200]

    mirror = _check_mirror_channel()

    if mcp_probe["reachable"]:
        channel = "http_mcp"
        message = f"HTTP MCP 已连通（{mcp_probe['tool_count']} 个工具）：{mcp_url}"
    elif http_mcp_configured:
        channel = "http_mcp_down"
        message = (
            f"已配置 HTTP MCP 端点但连不上：{mcp_probe['error']}；"
            "请先运行 charles_mcp_http.py"
        )
    elif mirror["watcher_running"]:
        channel, message = "mirror", mirror["message"]
    elif mirror["exists"] and mirror["file_count"] > 0:
        channel = "mirror_idle"
        message = "Mirror 目录有历史抓包，但文件监听未运行；启动 watch_charles_mirror.py 才会自动收题"
    else:
        channel = "none"
        message = "没有任何抓包通道在生效：未配置 HTTP MCP，Mirror 监听也未运行"

    return {
        "channel": channel,
        "message": message,
        "http_mcp_configured": http_mcp_configured,
        "http_mcp_url": mcp_url,
        "http_mcp": mcp_probe,
        "mirror": mirror,
        "stdio_mcp_note": "Cursor 里的 charles MCP 是 stdio 形态，必须由 Agent 拉起，独立服务无法调用",
    }


def _human_seconds(seconds: float) -> str:
    """把秒数写成便于阅读的时长。"""
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds} 秒前"
    if seconds < 3600:
        return f"{seconds // 60} 分钟前"
    if seconds < 86400:
        return f"{seconds // 3600} 小时前"
    return f"{seconds // 86400} 天前"


def check_capture_tool(server: Any) -> Dict[str, Any]:
    """抓包工具在线状态：最近抓包时间、Charles 进程、MCP 端点配置。"""
    latest = server._capture_store.latest_capture_time()
    idle_seconds: Optional[float] = None
    if latest:
        try:
            stamp = datetime.fromisoformat(latest)
            idle_seconds = max(0.0, (datetime.now(timezone.utc) - stamp).total_seconds())
        except (TypeError, ValueError):
            idle_seconds = None

    charles_running = False
    charles_detail = "未检测到 Charles 进程"
    try:
        import psutil

        for proc in psutil.process_iter(["name"]):
            name = (proc.info.get("name") or "").lower()
            if "charles" in name:
                charles_running = True
                charles_detail = proc.info.get("name") or "Charles"
                break
    except ImportError:
        charles_detail = "psutil 未安装，无法检测进程"
    except Exception as exc:  # 进程枚举失败不应影响面板
        charles_detail = f"进程检测失败: {exc}"

    mcp_url = config.get("CHARLES_MCP_URL")
    mcp_configured = bool(mcp_url)
    channel = check_capture_channel()

    if idle_seconds is None:
        state = "unknown"
        message = "还没有收到任何抓包，无法判断抓包工具是否在工作"
    elif idle_seconds <= CAPTURE_STALE_SECONDS:
        state = "online"
        message = f"{int(idle_seconds)} 秒前刚收到抓包"
    else:
        state = "idle"
        message = f"最近一次抓包在 {_human_seconds(idle_seconds)}"

    # 长时间没有抓包，同时既无 Charles 进程也无可用通道，才判定为离线。
    if state != "online" and not charles_running and channel["channel"] == "none":
        state = "offline"
        if idle_seconds is not None:
            message = f"未检测到 Charles 进程，也没有可用抓包通道；最近一次抓包在 {_human_seconds(idle_seconds)}"
        else:
            message = "未检测到 Charles 进程，也没有可用抓包通道，且从未收到过抓包"

    return {
        "state": state,
        "message": message,
        "latest_capture_at": latest,
        "idle_seconds": int(idle_seconds) if idle_seconds is not None else None,
        "stale_after_seconds": CAPTURE_STALE_SECONDS,
        "charles_running": charles_running,
        "charles_detail": charles_detail,
        "mcp_configured": mcp_configured,
        "mcp_url": mcp_url,
        "channel": channel,
    }


def check_readiness(bank: Dict[str, Any], files: Dict[str, Any], capture: Dict[str, Any]) -> Dict[str, Any]:
    """综合判断当前是否可以开始答题，并给出阻塞项与提醒项。"""
    blockers: List[str] = []
    warnings: List[str] = []

    if not bank["exact_ready"]:
        blockers.append("题库未加载，连精确匹配都无法进行")
    if not bank["semantic_ready"]:
        warnings.append("语义检索不可用，只能依赖题库精确匹配")
    if files["state"] == "offline":
        blockers.append(files["message"])

    if not config.api_key("DEEPSEEK_API_KEY"):
        warnings.append("未配置 DEEPSEEK_API_KEY，大模型兜底被跳过")

    # 没有任何可用通道时，题目根本进不来，属于硬阻塞。
    channel = capture.get("channel") or {}
    channel_kind = channel.get("channel")
    if channel_kind == "none":
        blockers.append("没有可用的抓包通道：题目无法进入，自动答题不会启动")
    elif channel_kind == "http_mcp_down":
        blockers.append("HTTP MCP 端点连不上：先运行 charles_mcp_http.py，否则收不到题目")
    elif channel_kind == "mirror_idle":
        blockers.append("Mirror 目录有历史抓包但监听未运行：启动 watch_charles_mirror.py 才能自动收题")
    elif capture["state"] in ("offline", "unknown"):
        warnings.append("抓包链路尚未确认在线，收不到新题就无法自动答题")

    if int(config.get_int("SUBMIT_INDEX_BASE", 1)) not in (0, 1):
        warnings.append("SUBMIT_INDEX_BASE 配置非法，提交序号可能错位")

    submit_base = config.get_int("SUBMIT_INDEX_BASE", 1)
    return {
        "ready": not blockers,
        "blockers": blockers,
        "warnings": warnings,
        "submit_index_base": submit_base,
        "submit_index_hint": "1 基（服务器内部序号从 1 开始）" if submit_base == 1 else "0 基",
        # 答案只呈现不提交，此项固定为 False，保留字段是为了兼容旧前端
        "auto_submit": False,
    }


def collect(server: Any) -> Dict[str, Any]:
    """汇总一份完整状态快照。"""
    bank = check_question_bank(server)
    files = check_supporting_files(server)
    capture = check_capture_tool(server)
    readiness = check_readiness(bank, files, capture)
    return {
        "checked_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "question_bank": bank,
        "files": files,
        "capture_tool": capture,
        "readiness": readiness,
    }
