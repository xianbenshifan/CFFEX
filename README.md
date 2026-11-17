# CFFEX 答题辅助

手机流量经过代理。本机服务收题后给出题库和大模型两份答案，不自动提交。

## 启动

Charles：双击 `start.bat`。
mitmproxy：双击 `start_mitm.bat`。代理端口与 Charles 相同，都是 `8888`，手机不用改端口。只解密答题域名，签发用的是手机里已经信任的 Charles 根证书。同一时刻只能开其中一套。

两条路用同一个答题服务。窗口里会打印面板地址，形如 `http://<本机IP>:8889/dashboard`。

停止用 `stop.bat`。

## 目录

| 路径 | 作用 |
|---|---|
| `start.bat` / `stop.bat` | Charles 收题的启动与停止 |
| `start_mitm.bat` | mitmproxy 收题的启动 |
| `mitm_capture.py` | mitmproxy 插件，命中题目后推给答题服务 |
| `proxy_server.py` | 答题服务，端口 8889 |
| `charles_mcp_http.py` | Charles MCP，端口 8931 |
| `database.json` | 题库 |
| `bank_source/` | 题库原文和生成脚本 |
| `capture_data/` | 抓包库和答题记录，不入库 |
| `README_CAPTURE.md` | 收题、归档和答案路由说明 |

配置在项目根 `.env`，模板是 `.env.example`。
