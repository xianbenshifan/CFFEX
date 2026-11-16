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
LLM_TIMEOUT=30
SUBMIT_INDEX_BASE=1
```

`CHARLES_MCP_URL` 与 `CHARLES_MCP_SUBMIT_TOOL` 同样可写进 `.env`。
真实环境变量优先于 `.env`，临时覆盖无需改文件。

大模型只接 DeepSeek 官方一个平台（`LLM_API_BASE` 默认
`https://api.deepseek.com/v1`）。

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
