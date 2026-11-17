"""运行就绪状态检测的回归测试。"""
# -*- coding: utf-8 -*-
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import system_status as ss


class FakeStore:
    """用固定时间替代真实抓包库。"""

    def __init__(self, latest):
        self._latest = latest

    def latest_capture_time(self):
        return self._latest


class FakeServer:
    """模拟服务进程内的加载状态。"""

    def __init__(self, latest=None, database=None, meta=None, model=None, error=None):
        self._capture_store = FakeStore(latest)
        self._database = database
        self._meta = meta
        self._model = model
        self._SEARCH_IMPORT_ERROR = error


def main():
    """覆盖文件判定、降级判定与就绪判定。"""
    print("=== 1. Git LFS 指针必须判为异常 ===")
    with tempfile.TemporaryDirectory() as folder:
        pointer = Path(folder) / "model.safetensors"
        pointer.write_text(
            "version https://git-lfs.github.com/spec/v1\noid sha256:abc\nsize 470641600\n",
            encoding="utf-8",
        )
        result = ss.inspect_file(pointer, min_bytes=1024 * 1024)
        assert result["ok"] is False, "134 字节的 LFS 指针不能当成真实权重"
        assert "LFS" in result["issue"]
        print("   ok:", result["issue"])

    print("=== 2. 缺失文件必须判为异常 ===")
    result = ss.inspect_file(Path("__不存在的文件__.json"))
    assert result["ok"] is False and "不存在" in result["issue"]
    print("   ok:", result["issue"])

    print("=== 3. 小文件必须给出真实字节数 ===")
    with tempfile.TemporaryDirectory() as folder:
        small = Path(folder) / "config.json"
        small.write_text("{}", encoding="utf-8")
        result = ss.inspect_file(small)
        assert result["ok"] is True and result["bytes"] == 2, result
        print("   bytes:", result["bytes"])

    print("=== 4. 题库降级：有题库无模型 ===")
    degraded = ss.check_question_bank(
        FakeServer(database=[1] * 1000, meta=[], model=None, error="模型缺失")
    )
    assert degraded["state"] == "degraded"
    assert degraded["exact_ready"] is True
    assert degraded["semantic_ready"] is False
    print("  ", degraded["message"])

    print("=== 5. 题库离线：题库为空 ===")
    offline = ss.check_question_bank(FakeServer(database=[], meta=[], model=None))
    assert offline["state"] == "offline" and offline["exact_ready"] is False
    print("  ", offline["message"])

    print("=== 6. 题库在线：题库、向量、模型齐备 ===")
    online = ss.check_question_bank(
        FakeServer(database=[1] * 1000, meta=[1] * 1000, model=object())
    )
    assert online["state"] == "online" and online["semantic_ready"] is True
    print("  ", online["message"])

    print("=== 7. 抓包新鲜度：刚抓到应判在线 ===")
    now = datetime.now(timezone.utc)
    fresh = ss.check_capture_tool(
        FakeServer(latest=(now - timedelta(seconds=20)).isoformat())
    )
    assert fresh["state"] == "online", fresh
    print("  ", fresh["message"])

    print("=== 8. 长时间无抓包：状态取决于通道是否可用 ===")
    stale = ss.check_capture_tool(
        FakeServer(latest=(now - timedelta(minutes=10)).isoformat())
    )
    assert stale["state"] in ("idle", "offline"), stale
    assert "10 分钟前" in stale["message"], stale["message"]
    print(f"   state={stale['state']} | {stale['message']}")

    print("=== 9. 从未抓过包：状态取决于通道是否可用 ===")
    never = ss.check_capture_tool(FakeServer(latest=None))
    assert never["state"] in ("unknown", "offline"), never
    print(f"   state={never['state']} | {never['message']}")

    print("=== 9b. 通道判定：必须是已知通道之一，并说明 stdio 限制 ===")
    channel = ss.check_capture_channel()
    known = ("none", "http_mcp", "http_mcp_down")
    assert channel["channel"] in known, channel
    assert "stdio" in channel["stdio_mcp_note"]
    # 配置了 URL 就必须给出真实探测结果，而不是仅凭配置断定可用
    assert "http_mcp" in channel and "reachable" in channel["http_mcp"]
    print(f"   channel={channel['channel']} | reachable={channel['http_mcp']['reachable']}")
    print(f"   message={channel['message']}")

    def capture_with(channel_kind, state="idle"):
        """构造指定通道的抓包状态，避免测试依赖真实目录。"""
        return {"state": state, "message": "测试", "channel": {"channel": channel_kind}}

    print("=== 10. 就绪判定：题库离线时必须阻塞 ===")
    readiness = ss.check_readiness(
        offline, {"state": "offline", "message": "缺文件"}, capture_with("mirror")
    )
    assert readiness["ready"] is False
    assert any("题库" in item for item in readiness["blockers"])
    print("   blockers:", readiness["blockers"])

    print("=== 11. 就绪判定：题库在线、通道正常时只保留降级提醒 ===")
    readiness = ss.check_readiness(
        online, {"state": "online", "message": "齐备"}, capture_with("http_mcp")
    )
    assert readiness["ready"] is True
    assert not any("抓包通道" in item for item in readiness["blockers"])
    print("   warnings:", readiness["warnings"])

    print("=== 12. 就绪判定：完全没有通道必须阻塞 ===")
    readiness = ss.check_readiness(
        online, {"state": "online", "message": "齐备"}, capture_with("none")
    )
    assert readiness["ready"] is False
    assert any("抓包通道" in item for item in readiness["blockers"])
    print("   blockers:", readiness["blockers"])

    print("=== 13. 就绪判定：HTTP MCP 配了但连不上必须阻塞 ===")
    readiness = ss.check_readiness(
        online, {"state": "online", "message": "齐备"}, capture_with("http_mcp_down")
    )
    assert readiness["ready"] is False
    assert any("charles_mcp_http" in item for item in readiness["blockers"])
    print("   blockers:", readiness["blockers"])

    print("=== 15. 就绪判定：HTTP MCP 连通时不阻塞 ===")
    readiness = ss.check_readiness(
        online, {"state": "online", "message": "齐备"}, capture_with("http_mcp")
    )
    assert readiness["ready"] is True
    print("   warnings:", readiness["warnings"])

    print("=== 16. MCP 客户端对不可用端点必须抛 CharlesMcpError ===")
    from charles_mcp_adapter import CharlesMcpClient, CharlesMcpError

    bad = CharlesMcpClient(endpoint="http://127.0.0.1:9/mcp", timeout=2)
    try:
        bad.list_tools()
        raise AssertionError("不可用端点不应成功")
    except CharlesMcpError as exc:
        assert "不可用" in str(exc) or "无法连接" in str(exc)
        print("   ok:", str(exc)[:80])

    print("status check test passed")


if __name__ == "__main__":
    main()
