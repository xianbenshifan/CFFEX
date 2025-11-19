# 搜题API服务使用说明

## 功能说明

这是一个基于Flask的HTTP API服务，可以接收题目查询请求，自动使用语义搜索在题库中查找相似题目并返回答案。

## 安装依赖

```bash
pip install -r requirements.txt
```

## 启动服务

```bash
python api_server.py
```

服务默认运行在 `http://localhost:5000`

可以通过环境变量修改配置：
- `API_HOST`: 监听地址（默认: 0.0.0.0）
- `API_PORT`: 监听端口（默认: 5000）

示例：
```bash
API_PORT=8080 python api_server.py
```

## API接口

### 1. 健康检查

**GET** `/health`

检查服务是否就绪。

**响应示例：**
```json
{
  "status": "ready",
  "message": "搜题引擎运行正常",
  "database_size": 1000,
  "embeddings_size": 1000
}
```

### 2. 单题搜索

**POST** `/search`

搜索单个题目。

**请求体：**
```json
{
  "query": "题目文本或关键词",
  "top_k": 5
}
```

**参数说明：**
- `query` (必需): 要搜索的题目文本
- `top_k` (可选): 返回结果数量，默认5，最多20

**响应示例：**
```json
{
  "success": true,
  "query": "债期货合约最后交易日之前的交割结算价...",
  "count": 5,
  "results": [
    {
      "number": "485",
      "type": "judgment",
      "question": "国债期货合约最后交易日之前的交割结算价...",
      "options": {
        "A": "正确",
        "B": "错误"
      },
      "answer": "A",
      "similarity": 0.9462
    },
    ...
  ]
}
```

### 3. 批量搜索

**POST** `/search/batch`

批量搜索多个题目。

**请求体：**
```json
{
  "queries": ["题目1", "题目2", "题目3"],
  "top_k": 5
}
```

**参数说明：**
- `queries` (必需): 题目文本数组
- `top_k` (可选): 每个查询返回的结果数量

**响应示例：**
```json
{
  "success": true,
  "total": 3,
  "results": [
    {
      "success": true,
      "query": "题目1",
      "count": 5,
      "results": [...]
    },
    ...
  ]
}
```

## 使用示例

### Python示例

```python
import requests

# 单题搜索
response = requests.post(
    "http://localhost:5000/search",
    json={
        "query": "政府对新能源汽车实施生产厂家补贴政策",
        "top_k": 5
    }
)

result = response.json()
if result["success"]:
    for item in result["results"]:
        print(f"题号: {item['number']}, 相似度: {item['similarity']}")
        print(f"答案: {item['answer']}")
```

### curl示例

```bash
# 健康检查
curl http://localhost:5000/health

# 单题搜索
curl -X POST http://localhost:5000/search \
  -H "Content-Type: application/json" \
  -d '{
    "query": "政府对新能源汽车实施生产厂家补贴政策",
    "top_k": 5
  }'

# 批量搜索
curl -X POST http://localhost:5000/search/batch \
  -H "Content-Type: application/json" \
  -d '{
    "queries": ["题目1", "题目2"],
    "top_k": 3
  }'
```

### JavaScript示例

```javascript
// 单题搜索
fetch('http://localhost:5000/search', {
  method: 'POST',
  headers: {
    'Content-Type': 'application/json',
  },
  body: JSON.stringify({
    query: '政府对新能源汽车实施生产厂家补贴政策',
    top_k: 5
  })
})
.then(response => response.json())
.then(data => {
  if (data.success) {
    data.results.forEach(item => {
      console.log(`题号: ${item.number}, 答案: ${item.answer}`);
    });
  }
});
```

## 测试

运行测试脚本：

```bash
# 先启动API服务
python api_server.py

# 在另一个终端运行测试
python test_api.py
```

## 注意事项

1. 首次启动时会自动加载模型和生成向量，可能需要几分钟时间
2. 服务启动后，模型和向量会常驻内存，后续查询会很快
3. 如果修改了 `semantic_search.py` 中的模型或向量生成逻辑，建议删除缓存文件重新生成
4. API支持跨域请求（CORS），可以在网页中直接调用

## 错误处理

所有接口在出错时会返回相应的HTTP状态码和错误信息：

- `400`: 请求参数错误
- `500`: 服务器内部错误
- `503`: 服务未就绪（模型未加载完成）

错误响应格式：
```json
{
  "success": false,
  "error": "错误描述"
}
```

