# CFFEX 答题辅助

手机流量经过 Charles。本机服务通过 HTTP MCP 收题，给出题库和大模型两份答案，不自动提交。

## 启动

双击 `start.bat`。窗口里会打印面板地址，形如 `http://<本机IP>:8889/dashboard`。

停止用 `stop.bat`。

## 目录

| 路径 | 作用 |
|---|---|
| `start.bat` / `stop.bat` | 启动与停止 |
| `proxy_server.py` | 答题服务，端口 8889 |
| `charles_mcp_http.py` | Charles MCP，端口 8931 |
| `database.json` | 题库 |
| `bank_source/` | 题库原文和生成脚本 |
| `capture_data/` | 抓包库和答题记录，不入库 |
| `README_CAPTURE.md` | 收题、归档和答案路由说明 |

配置在项目根 `.env`，模板是 `.env.example`。
