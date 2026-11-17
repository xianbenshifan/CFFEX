# Charles MCP 抓包与题目归档管线

## 改造结果

现在的流程为：

```text
Charles / Charles MCP
        ↓ 标准 JSON 信封
POST /mcp/charles/capture
        ↓
QuizAutomation
        ├─ 本地精确匹配
        ├─ 本地语义匹配
        ├─ LLM API 兜底
        └─ Charles MCP 提交答案
        ↓
CapturePipeline
        ├─ 保存原始抓包
        ├─ 提取题目、选项、题型
        ├─ 保存服务器 correct / analysis / rightIndex
        ├─ 调用现有语义搜题引擎
        └─ 保存答案、答案索引、相似度和来源
        ↓
capture_data/quiz_capture.db
```

## 启动

```powershell
python proxy_server.py
python watch_charles_mirror.py
```

`watch_charles_mirror.py` 仍兼容旧版 Mirror 文件，同时优先把原始响应送到
`/mcp/charles/capture`，不会丢失服务器字段。

## Charles MCP bridge 信封

```json
{
  "request": {
    "method": "POST",
    "url": "https://TARGET/qa/api/quiz/answer",
    "headers": {},
    "body": {
      "questionId": "QUESTION_ID",
      "quizId": "QUIZ_ID",
      "answerIndex": 0
    }
  },
  "response": {
    "status_code": 200,
    "headers": {
      "Content-Type": "application/json"
    },
    "body": {
      "success": true,
      "data": {
        "correct": true,
        "analysis": "解析文本",
        "nextQuestion": {}
      }
    }
  },
  "metadata": {
    "session_id": "SESSION_ID"
  }
}
```

## 查询接口

- `GET /captures?limit=100`：抓包索引
- `GET /questions?limit=100&offset=0`：题目、答案、解析
- `GET /questions/QUESTION_ID`：单题完整归档
- `GET /automation/next`：读取最近一道已解析题目
- `POST /automation/submission`：记录答题端提交及服务器结果

## MCP JSON-RPC 客户端配置

如果本地 Charles MCP bridge 提供 JSON-RPC `tools/call`，可设置：

```powershell
$env:CHARLES_MCP_URL = "http://127.0.0.1:PORT/mcp"
$env:CHARLES_MCP_START_TOOL = "charles_start_capture"
$env:CHARLES_MCP_STOP_TOOL = "charles_stop_capture"
$env:CHARLES_MCP_EXPORT_TOOL = "charles_export_capture"
```

项目中的 `charles_mcp_adapter.py` 已提供对应客户端，业务代码只依赖标准信封，
后续更换 Charles 版本或 bridge 时无需改动题目解析和存储逻辑。

## 答案路由配置

配置写在项目根 `.env`（模板见 `.env.example`），改完重启服务生效：

```text
LOCAL_MATCH_THRESHOLD=0.82
DEEPSEEK_API_KEY=你的 DeepSeek API Key
LLM_MODEL=deepseek-flash
LLM_TIMEOUT=8
LLM_REASONING_EFFORT=low
SUBMIT_INDEX_BASE=1
```

`CHARLES_MCP_URL` 同样可写进 `.env`。
真实环境变量优先于 `.env`，临时覆盖无需改文件。

## 大模型响应时间

目标：收到题目后 8 秒内给出大模型侧答案。实测 15 道样例题（单选 / 多选 / 判断各 5 道）：

| 配置 | 平均 | 最慢 | 准确率 |
|---|---|---|---|
| 平台默认推理深度 | 2.4s | 8.3s | 12/12 |
| `reasoning_effort=low` | **1.7s** | **2.6s** | **15/15** |
| `max_tokens=200` 限长 | 1.6s | 1.9s | 7/12 ✗ |

结论：用 `reasoning_effort=low` 而不是压缩输出长度 —— 后者会把多选题截断成单选。
`LLM_TIMEOUT=8` 是硬上限，超时就不等大模型，只保留题库侧答案并在面板上标出。

大模型只接 DeepSeek 官方一个平台（`LLM_API_BASE` 默认
`https://api.deepseek.com/v1`）。

## 抓包与答案分工

- **收题**：`charles_mcp_http.py` 把 Charles MCP 以 streamable-http 暴露在
  `http://127.0.0.1:8931/mcp`，服务通过 `CHARLES_MCP_URL` 连接。这条路不需要 Agent。
- **给答案**：每题同时给出**题库侧**与**大模型侧**两份结论，并列呈现、互相校验。
  题库侧走精确匹配与语义检索，大模型侧走 DeepSeek。
- **不自动提交**：任何答案都不会自动发出，由人对照两份结论自己点选。

两份答案的来源标记：

| 标记 | 含义 |
|---|---|
| `local_exact` | 题库精确命中（相似度 1.0） |
| `local_semantic` | 语义检索达到阈值（默认 0.82） |
| `local_semantic_low` | 语义检索低于阈值，可信度偏低但仍呈现 |
| `llm_api` | 大模型结论 |

面板 `http://<本机IP>:8889/dashboard` 与答题记录 `answers.md` 都会展示两份答案及一致性判断。

## 收题靠轮询

Charles Web API 与 `charles-mcp` 都只提供查询接口，没有 websocket / webhook / 回调，
因此发现新题只能轮询。实测单次 `query_entries` 约 0.21 秒（本地 HTTP + 已有抓包会话），
所以间隔可以设得很短。

间隔在运行时可调，改完立即生效、无需重启：

```powershell
# 查看当前间隔与实测轮询次数
curl http://127.0.0.1:8889/mcp/capture/status

# 调到 0.5 秒
curl -X POST http://127.0.0.1:8889/mcp/capture/interval -H "Content-Type: application/json" -d '{"interval":0.5}'
```

允许范围 0.2–60 秒，超出会被钳制；非法值回落到 1 秒。

实际周期 = 间隔 + 单次查询耗时。设 0.5 秒时实测约 0.75 秒一次。

`.env` 里的 `MCP_POLL_INTERVAL` 只在启动收题时读取，运行中改它不生效。

每道题的 `answer_source` 会标记为：

- `local_exact`：题干精确命中本地题库；
- `local_semantic`：本地向量检索命中并达到阈值；
- `llm_api`：本地匹配不足时调用大模型 API。

提交返回后，服务器的 `correct`、`analysis`、`rightIndex` 和原始响应会关联
回写到对应的 `question_id`。

## 答题记录 Markdown

每收到一条抓包（题目或服务器判定），`capture_pipeline` 都会按数据库当前内容
整体重写一份 Markdown：

```text
capture_data/答题记录.md
```

- 路径可用 `ANSWER_MD_PATH` 覆盖；
- 每道题同时有 `### 我们的推断`（题库 / 语义检索 / 大模型的答案、来源、相似度、理由）
  和 `### 服务器标准答案`（标准答案、提交判定、服务器解析）；
- 同一题重复抓包不会产生两份记录，Markdown 始终是数据库的投影。

手动刷新或下载：

```powershell
curl http://localhost:8889/answers.md            # 直接下载
curl -X POST http://localhost:8889/answers.md/export   # 仅刷新并返回统计
```
