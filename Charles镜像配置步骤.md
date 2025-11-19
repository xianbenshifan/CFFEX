# Charles 镜像模式配置步骤

## 目标

让 Charles **正常转发**所有请求到真实服务器，同时**复制一份响应数据**到 Flask 服务进行搜题。

## 配置步骤

### 方案一：使用 Rewrite 规则（推荐）

#### 1. 移除 Map Remote 配置

**不要配置 Map Remote**，让请求正常转发到真实服务器。

#### 2. 配置 Rewrite 规则复制响应

1. **Tools** → **Rewrite** → **Enable Rewrite**

2. 添加规则组，命名为 "复制题目响应到Flask"

3. 添加规则：

   **规则1：匹配题目响应**
   - **Type**: `Body`
   - **Match**:
     - Protocol: `https`
     - Host: `quiz-activity.eastmoney.com`
     - Path: `qa/api/quiz/*` (注意：不要加 `*`，Charles会自动匹配)
     - Method: `GET`
   
   **规则2：发送到Flask（使用Script）**
   
   由于Charles的Rewrite功能限制，需要使用**Script**功能：

4. **Tools** → **Scripts** → **Enable Scripts**

5. 创建脚本：

```javascript
function onResponse(req, res) {
    if (req.host.indexOf('quiz-activity.eastmoney.com') !== -1 && 
        req.path.indexOf('/qa/api/quiz/') === 0 && 
        req.method === 'GET') {
        
        // 复制响应数据到Flask
        var responseBody = res.body;
        if (responseBody) {
            // 发送到Flask（异步，不阻塞）
            var http = new com.xavax.http.HttpClient();
            http.post('http://localhost:8889/qa/api/quiz/receive', {
                'Content-Type': 'application/json'
            }, JSON.stringify(responseBody));
        }
    }
}
```

### 方案二：手动方式（最简单）

1. **让 Charles 正常转发**（不配置任何Map Remote）

2. **在 Charles 中查看响应**
   - 找到 `quiz-activity.eastmoney.com/qa/api/quiz/*` 的响应
   - 右键 → **Copy Response**

3. **发送到 Flask**
   
   使用 curl 或 Postman：
   ```bash
   curl -X POST http://localhost:8889/qa/api/quiz/receive \
     -H "Content-Type: application/json" \
     -d '{"data": {粘贴复制的响应数据}}'
   ```

### 方案三：使用 Breakpoint（半自动）

1. **Tools** → **Breakpoints** → **Enable Breakpoints**

2. 添加规则：
   - Protocol: `https`
   - Host: `quiz-activity.eastmoney.com`
   - Path: `qa/api/quiz/*`
   - Method: `GET`

3. 当请求被拦截时：
   - 点击 **Execute** 让请求继续
   - 当响应被拦截时，复制响应数据
   - 手动发送到 Flask（或使用脚本）

## 推荐：最简单的配置

### 步骤1：不配置 Map Remote

让所有请求正常转发，不经过Flask。

### 步骤2：在Charles中查看并复制

1. 打开东方财富APP开始答题
2. 在Charles中查看 `quiz-activity.eastmoney.com/qa/api/quiz/*` 的响应
3. 复制响应JSON数据

### 步骤3：发送到Flask

创建一个简单的脚本或使用curl：

```bash
# 将响应数据保存到文件 response.json
# 然后发送到Flask
curl -X POST http://localhost:8889/qa/api/quiz/receive \
  -H "Content-Type: application/json" \
  -d @response.json
```

或者创建一个Python脚本自动完成。

## Flask服务接口

Flask服务现在提供以下接口（只接收，不发送）：

1. **POST /qa/api/quiz/receive**
   - 接收题目响应数据
   - 自动搜题并显示答案
   - 返回简单确认

2. **POST /qa/api/quiz/answer**
   - 接收答案提交数据
   - 显示答案对比
   - 返回简单确认

3. **GET /cache**
   - 查看缓存的题目和答案

## 自动化脚本示例

创建一个Python脚本，从Charles导出的JSON中提取响应并发送到Flask：

```python
import requests
import json

# 从Charles导出的JSON文件读取
with open('charles_export.json', 'r', encoding='utf-8') as f:
    charles_data = json.load(f)

# 找到题目响应
for session in charles_data.get('sessions', []):
    if 'quiz-activity.eastmoney.com' in session.get('host', ''):
        if '/qa/api/quiz/' in session.get('path', ''):
            response_data = session.get('response', {}).get('body', {})
            
            # 发送到Flask
            requests.post(
                'http://localhost:8889/qa/api/quiz/receive',
                json=response_data,
                timeout=5
            )
            print(f"已发送题目响应到Flask")
```

## 当前架构

```
手机 → Charles → 真实服务器（正常转发，返回响应）
              ↓
          Flask服务（只接收响应数据，搜题，不发送）
```

## 重要提示

- **Flask服务不参与实际的请求-响应流程**
- **Charles正常转发所有请求**
- **Flask只接收数据包进行搜题**
- **答案显示在控制台日志中**

