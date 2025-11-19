# Charles 镜像模式配置指南

## 目标

让 Charles 正常转发所有请求到真实服务器，同时**复制一份**到 Flask 服务进行搜题。Flask 只接收数据，不发送响应。

## 配置方法

### 方法一：使用 Rewrite 规则（推荐）

#### 1. 配置 Map Remote（正常转发）

**Tools** → **Map Remote** → **Enable Map Remote**

**不要配置** Map Remote！让请求正常转发到真实服务器。

#### 2. 配置 Rewrite 规则（复制响应到Flask）

**Tools** → **Rewrite** → **Enable Rewrite**

添加规则：

**Rule 1: 复制题目响应到Flask**

- **Type**: Body
- **Match**: 
  - Protocol: `https`
  - Host: `quiz-activity.eastmoney.com`
  - Path: `qa/api/quiz/*`
  - Method: `GET`
- **Action**: 
  - Type: `Body`
  - Action: `Replace`
  - Value: 保持原值（不修改）
  - **同时添加**: 
    - Type: `URL`
    - Action: `Add Header` 或使用 `Script` 功能

**更简单的方法：使用 Breakpoint**

#### 方法二：使用 Breakpoint（更简单）

1. **Tools** → **Breakpoints**
2. 勾选 **Enable Breakpoints**
3. 添加规则：
   - **Protocol**: `https`
   - **Host**: `quiz-activity.eastmoney.com`
   - **Path**: `qa/api/quiz/*`
   - **Method**: `GET`

4. 当请求被拦截时：
   - 在 Breakpoint 窗口中，点击 **Execute** 让请求继续
   - 在响应被拦截时，复制响应数据
   - 手动发送一份到 Flask（或使用脚本）

#### 方法三：使用 External Proxy（最佳方案）

1. **Proxy** → **External Proxy Settings**
2. 勾选 **Use external proxy servers**
3. 添加规则：
   - **Protocol**: `http`
   - **Host**: `localhost`
   - **Port**: `8889`
   - **Type**: `Mirror`（镜像模式）

但 Charles 可能不支持镜像模式，所以使用下面的方法。

## 推荐方案：使用 Map Remote + 手动复制

### 步骤1：正常转发（不配置Map Remote）

让所有请求正常转发到真实服务器，不经过Flask。

### 步骤2：在Charles中查看响应，手动触发搜题

1. 在 Charles 中查看 `quiz-activity.eastmoney.com/qa/api/quiz/*` 的响应
2. 复制响应 JSON 数据
3. 使用 curl 或 Postman 发送到 Flask：

```bash
curl -X POST http://localhost:8889/qa/api/quiz/receive \
  -H "Content-Type: application/json" \
  -d '{"data": {复制的响应数据}}'
```

### 步骤3：自动化脚本（可选）

创建一个 Charles 脚本，自动将响应复制到 Flask。

## 最简单的方案：修改架构

由于 Charles 的限制，建议修改架构：

### 新架构

```
手机 → Charles → 真实服务器（正常转发）
              ↓
          Flask服务（只接收，不响应）
```

### 配置步骤

1. **移除 Map Remote 配置**（让请求正常转发）

2. **使用 Charles 的 Script 功能**（如果有）

3. **或者手动方式**：
   - 在 Charles 中查看响应
   - 复制响应数据
   - 发送到 `http://localhost:8889/qa/api/quiz/receive`

## 当前Flask服务已支持

Flask 服务现在支持以下接口：

1. **GET /qa/api/quiz/<id>** - 接收题目请求（返回确认，不转发）
2. **POST /qa/api/quiz/receive** - 接收题目响应数据（用于搜题）
3. **POST /qa/api/quiz/answer** - 接收答案提交（只记录，不转发）

## 实际使用建议

由于 Charles 的限制，最简单的方式是：

1. **让 Charles 正常转发所有请求**（不配置 Map Remote）
2. **在 Charles 中查看响应**
3. **手动或使用脚本将响应数据发送到 Flask**

或者，如果你有编程能力，可以写一个 Charles 脚本自动完成。

## 测试

1. 启动 Flask 服务：`python proxy_server.py`
2. 在 Charles 中查看题目响应
3. 复制响应 JSON
4. 发送到 Flask：
```bash
curl -X POST http://localhost:8889/qa/api/quiz/receive \
  -H "Content-Type: application/json" \
  -d @response.json
```

