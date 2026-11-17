"""用已经入库的真实抓包，验证 mitmproxy 收题和现有管线得到同一批题。"""
# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import os
import sqlite3
import tempfile
from pathlib import Path

from automation_runner import capture_ingest_source
from capture_pipeline import CapturePipeline
from capture_store import QuizCaptureStore
from mitm_capture import (
    QuizCapture,
    envelope_from_flow,
    envelope_from_parts,
    should_capture,
)

ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "capture_data" / "quiz_capture.db"


class _Message:
    """最小报文替身，字段与 mitmproxy 的 request/response 对齐。"""

    def __init__(self, text: str, headers=None, status_code=200):
        self._text = text
        self.headers = headers or {}
        self.status_code = status_code
        self.content = text.encode("utf-8")

    def get_text(self, strict: bool = False) -> str:
        return self._text


class _Flow:
    def __init__(self, request, response):
        self.request = request
        self.response = response


def _load_rows():
    """只读打开已经跑过的抓包库。"""
    if not DB_PATH.exists():
        raise SystemExit(f"找不到已跑过的抓包库: {DB_PATH}")
    uri = DB_PATH.resolve().as_posix()
    conn = sqlite3.connect(f"file:{uri}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    rows = list(conn.execute(
        """
        select id, method, url, host, path, status_code,
               request_headers_json, request_body_json,
               response_headers_json, response_body_json
        from captures
        order by id
        """
    ))
    questions = {
        str(row["question_id"]): row
        for row in conn.execute(
            "select question_id, question_title, options_json from questions"
        )
    }
    conn.close()
    return rows, questions


def _loads(text):
    if text is None or text == "":
        return None
    return json.loads(text)


def _envelope_from_row(row):
    return envelope_from_parts(
        method=row["method"],
        url=row["url"],
        host=row["host"],
        path=row["path"],
        request_headers=_loads(row["request_headers_json"]) or {},
        request_body=row["request_body_json"],
        status_code=row["status_code"],
        response_headers=_loads(row["response_headers_json"]) or {},
        response_body=row["response_body_json"],
    )


def _question_map(store: QuizCaptureStore):
    found = {}
    for row in store.list_questions(limit=500):
        found[str(row["question_id"])] = row
    return found


def main() -> None:
    rows, original = _load_rows()
    print(f"=== 1. 回放 {len(rows)} 条已入库抓包 ===")

    forwarded = []
    status_kept = 0
    for row in rows:
        envelope = _envelope_from_row(row)
        path = str(row["path"] or "")
        keep = should_capture(envelope)
        if "/status" in path:
            if keep:
                status_kept += 1
            assert keep is False, path
            continue
        if keep:
            forwarded.append(envelope)

    assert status_kept == 0
    print(f"   状态接口已丢弃，留下 {len(forwarded)} 条题目或答案")

    with tempfile.TemporaryDirectory() as folder:
        store = QuizCaptureStore(os.path.join(folder, "replay.db"))
        pipeline = CapturePipeline(
            store,
            answer_resolver=None,
            markdown_path=os.path.join(folder, "答题记录.md"),
        )
        for envelope in forwarded:
            pipeline.ingest(envelope, source=capture_ingest_source(envelope))
        replayed = _question_map(store)

    missing = sorted(set(original) - set(replayed))
    extra = sorted(set(replayed) - set(original))
    assert not missing, missing
    assert not extra, extra

    title_mismatch = []
    option_mismatch = []
    for question_id, row in original.items():
        got = replayed[question_id]
        if got["question_title"] != row["question_title"]:
            title_mismatch.append(question_id)
        if got["options"] != json.loads(row["options_json"]):
            option_mismatch.append(question_id)
    assert not title_mismatch, title_mismatch
    assert not option_mismatch, option_mismatch
    print(f"   {len(original)} 道题的题干和选项与已跑结果一致")

    print("=== 2. 来源标记 ===")
    assert capture_ingest_source({}) == "charles_mcp_automation"
    assert capture_ingest_source({"metadata": {"source": "charles_mcp_http"}}) == "charles_mcp_automation"
    assert capture_ingest_source(forwarded[0]) == "mitmproxy"
    print("   Charles 仍记为 charles_mcp_automation，mitmproxy 记为 mitmproxy")

    print("=== 3. 插件从流量对象抽出的信封与库里的一致 ===")
    sample = next(row for row in rows if should_capture(_envelope_from_row(row)))
    request = _Message(sample["request_body_json"] or "")
    request.method = sample["method"]
    request.pretty_url = sample["url"]
    request.pretty_host = sample["host"]
    request.host = sample["host"]
    request.path = sample["path"]
    response = _Message(sample["response_body_json"] or "")
    from_flow = envelope_from_flow(_Flow(request, response))
    direct = _envelope_from_row(sample)
    assert from_flow["request"]["path"] == direct["request"]["path"]
    assert from_flow["response"]["body"] == direct["response"]["body"]
    assert should_capture(from_flow) is should_capture(direct)

    other = _Flow(request, response)
    other.request = _Message("")
    other.request.method = "GET"
    other.request.pretty_url = "https://example.com/qa/api/quiz/zjsb12_cs"
    other.request.pretty_host = "example.com"
    other.request.host = "example.com"
    other.request.path = "/qa/api/quiz/zjsb12_cs"
    assert should_capture(envelope_from_flow(other)) is False
    print("   流量对象与别的域名都按预期过滤")

    print("=== 4. mitmproxy 真实流量对象 ===")
    _replay_with_mitmproxy(sample)
    print("mitm capture replay passed")


def _replay_with_mitmproxy(sample) -> None:
    """用已入库的一条响应，走真实的 mitmproxy 流量对象和本地代理。"""
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    import requests
    from mitmproxy import connection, http

    body = sample["response_body_json"].encode("utf-8")
    received = []

    class Upstream(BaseHTTPRequestHandler):
        def do_GET(self):
            self._send()

        def do_POST(self):
            self._send()

        def _send(self):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt, *args):
            return

    class Collector(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length)
            received.append(json.loads(raw.decode("utf-8")))
            data = b'{"success":true}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, fmt, *args):
            return

    upstream = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
    collector = ThreadingHTTPServer(("127.0.0.1", 0), Collector)
    threading.Thread(target=upstream.serve_forever, daemon=True).start()
    threading.Thread(target=collector.serve_forever, daemon=True).start()
    upstream_port = upstream.server_address[1]
    collector_port = collector.server_address[1]

    client = connection.Client(
        peername=("127.0.0.1", 40000),
        sockname=("127.0.0.1", 8080),
        timestamp_start=0,
    )
    server = connection.Server(address=("quiz-activity.eastmoney.com", 443))
    flow = http.HTTPFlow(client, server)
    flow.request = http.Request.make(
        sample["method"] or "GET",
        sample["url"],
        sample["request_body_json"] or "",
        {"Content-Type": "application/json"},
    )
    flow.response = http.Response.make(200, body, {"Content-Type": "application/json"})
    built = envelope_from_flow(flow)
    assert built is not None
    assert built["request"]["host"] == "quiz-activity.eastmoney.com"
    assert should_capture(built) is should_capture(_envelope_from_row(sample))
    print("   HTTPFlow 信封与库中记录一致")

    os.environ["MITM_CAPTURE_URL"] = f"http://127.0.0.1:{collector_port}/mcp/charles/capture"
    os.environ["MITM_CAPTURE_SYNC"] = "1"
    try:
        QuizCapture().response(flow)
    finally:
        os.environ.pop("MITM_CAPTURE_SYNC", None)
    assert received, "插件没有把流量推到收题地址"
    assert received[0]["response"]["body"] == built["response"]["body"]
    print(f"   插件已把这条流量推到本地收题口，path={received[0]['request']['path']}")

    _proxy_roundtrip(sample, upstream_port, collector, received)
    upstream.shutdown()
    collector.shutdown()


def _proxy_roundtrip(sample, upstream_port: int, collector, received) -> None:
    """启动 mitmdump，把这一条历史响应从本地上游经代理再推一次。"""
    import subprocess
    import textwrap
    import time

    import requests

    received.clear()
    rewrite = Path(tempfile.mkdtemp()) / "rewrite_upstream.py"
    rewrite.write_text(
        textwrap.dedent(
            f"""
            def request(flow):
                flow.request.scheme = "http"
                flow.request.host = "127.0.0.1"
                flow.request.port = {upstream_port}
                flow.request.headers["Host"] = "quiz-activity.eastmoney.com"
            """
        ),
        encoding="utf-8",
    )
    mitmdump = _mitmdump_path()
    port = _free_port()
    env = os.environ.copy()
    env["MITM_CAPTURE_URL"] = f"http://127.0.0.1:{collector.server_address[1]}/mcp/charles/capture"
    env.pop("MITM_CAPTURE_SYNC", None)
    process = subprocess.Popen(
        [
            mitmdump,
            "--listen-host", "127.0.0.1",
            "--listen-port", str(port),
            "-s", str(ROOT / "mitm_capture.py"),
            "-s", str(rewrite),
            "--set", "termlog_verbosity=error",
        ],
        cwd=str(ROOT),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        deadline = time.time() + 20
        while time.time() < deadline:
            if process.poll() is not None:
                output = process.stdout.read() if process.stdout else ""
                raise SystemExit(f"mitmdump 提前退出:\n{output}")
            try:
                requests.get(f"http://127.0.0.1:{port}/", timeout=0.5)
            except Exception:
                pass
            import socket
            with socket.socket() as sock:
                sock.settimeout(0.3)
                if sock.connect_ex(("127.0.0.1", port)) == 0:
                    break
            time.sleep(0.3)
        else:
            raise SystemExit("mitmdump 没有在 20 秒内开始监听")

        url = sample["url"] or "http://quiz-activity.eastmoney.com/qa/api/quiz/zjsb12_cs"
        # 历史记录是 https。测试改走 http，避免为本机再装一次证书。
        url = "http://" + url.split("://", 1)[-1]
        response = requests.request(
            sample["method"] or "GET",
            url,
            data=sample["request_body_json"] or None,
            proxies={"http": f"http://127.0.0.1:{port}", "https": f"http://127.0.0.1:{port}"},
            timeout=20,
        )
        assert response.status_code == 200, response.status_code
        deadline = time.time() + 15
        while time.time() < deadline and not received:
            time.sleep(0.2)
        assert received, "代理跑通了，但插件没有推送这条历史响应"
        assert received[-1]["metadata"]["ingest_source"] == "mitmproxy"
        print(f"   mitmdump 已用这条历史响应完成一次真实代理，端口 {port}")
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()


def _mitmdump_path() -> str:
    import shutil
    import sys

    found = shutil.which("mitmdump")
    if found:
        return found
    scripts = Path(sys.executable).resolve().parent / "Scripts" / "mitmdump.exe"
    if scripts.exists():
        return str(scripts)
    raise SystemExit("找不到 mitmdump，先执行 python -m pip install mitmproxy")


def _free_port() -> int:
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


if __name__ == "__main__":
    main()
