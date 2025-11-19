# Charles Mirror 自动搜题使用说明

## 功能说明

使用Charles的Mirror功能将请求保存到本地文件夹，然后通过文件监听脚本自动读取并搜题。

## 配置步骤

### 1. 配置Charles Mirror功能

1. 打开Charles
2. **Tools** → **Mirror Settings**（或 **Tools** → **Mirror**）
3. 启用 **Enable Mirror**
4. 配置镜像规则：
   - **Host**: `quiz-activity.eastmoney.com`
   - **Path**: `qa/api/quiz/*`
   - **Method**: `GET` 和 `POST`
   - **Save to**: 选择 `123321` 文件夹（或你指定的文件夹）

### 2. 安装依赖

```bash
pip install watchdog
```

或者安装所有依赖：

```bash
pip install -r requirements.txt
```

### 3. 启动服务

#### 方法一：使用批处理文件（Windows）

双击运行 `start_watch.bat`

#### 方法二：命令行启动

```bash
python watch_charles_mirror.py
```

### 4. 启动搜题服务

在另一个终端窗口启动Flask搜题服务：

```bash
python proxy_server.py
```

## 工作流程

```
手机 → Charles → 真实服务器（正常转发）
              ↓
          Mirror保存文件到 123321 文件夹
              ↓
          watch_charles_mirror.py 监听文件夹
              ↓
          自动读取新文件并发送到Flask服务
              ↓
          Flask服务搜题并显示答案
```

## 文件结构

Charles Mirror保存的文件结构：

```
123321/
└── quiz-activity.eastmoney.com/
    └── qa/
        └── api/
            └── quiz/
                ├── Mzpvia%3f_ts%3d...  (题目文件)
                └── answer%3f_ts%3d...   (答案文件)
```

## 功能特点

1. **自动监听**：实时监听文件夹变化，新文件自动处理
2. **智能识别**：自动区分题目文件和答案文件
3. **自动搜题**：题目文件自动发送到Flask服务进行搜题
4. **答案对比**：答案文件自动发送到Flask服务进行对比
5. **去重处理**：避免重复处理同一文件

## 日志输出

脚本会输出详细的日志信息：

```
============================================================
Charles Mirror 文件监听服务
============================================================
监听文件夹: C:\Users\...\123321
Flask服务地址: http://localhost:8889/qa/api/quiz/receive
============================================================

✓ Flask服务运行正常

正在扫描已存在的文件...
[检测到新题目] 文件: Mzpvia%3f_ts%3d...
题目ID: 54567
题目: 中证1000股指期权交割结算价采用到期日中证1000指数最后一小时所有指数点的算术平均价。...
============================================================

开始监听文件夹变化...
按 Ctrl+C 停止
```

## 注意事项

1. **确保Flask服务运行**：`watch_charles_mirror.py` 需要 `proxy_server.py` 正在运行
2. **文件夹路径**：默认监听 `123321` 文件夹，如需修改请编辑 `watch_charles_mirror.py` 中的 `MIRROR_FOLDER` 变量
3. **文件格式**：Charles Mirror保存的文件必须是有效的JSON格式
4. **网络连接**：确保可以访问 `localhost:8889`（Flask服务地址）

## 故障排查

### 1. 无法连接到Flask服务

- 检查 `proxy_server.py` 是否正在运行
- 检查端口 `8889` 是否被占用
- 检查防火墙设置

### 2. 文件没有被处理

- 检查文件是否在正确的文件夹路径下
- 检查文件是否是有效的JSON格式
- 查看日志输出，确认文件是否被检测到

### 3. 搜题失败

- 检查Flask服务的日志输出
- 确认模型和向量是否已加载完成
- 查看 `/health` 接口确认服务状态

## 高级配置

### 修改监听文件夹

编辑 `watch_charles_mirror.py`：

```python
MIRROR_FOLDER = "你的文件夹路径"
```

### 修改Flask服务地址

编辑 `watch_charles_mirror.py`：

```python
FLASK_URL = "http://localhost:8889/qa/api/quiz/receive"
FLASK_ANSWER_URL = "http://localhost:8889/qa/api/quiz/answer"
```

## 与Scripts功能的对比

| 功能 | Scripts功能 | Mirror + 监听脚本 |
|------|------------|-----------------|
| 需要Pro版本 | 是 | 否 |
| 自动化程度 | 高 | 高 |
| 配置复杂度 | 中等 | 简单 |
| 稳定性 | 高 | 高 |
| 适用场景 | 实时处理 | 文件保存后处理 |

## 优势

1. **不需要Pro版本**：免费版Charles即可使用
2. **配置简单**：只需配置Mirror功能，无需编写脚本
3. **稳定可靠**：文件保存后再处理，避免网络问题
4. **易于调试**：可以查看保存的文件内容

## 使用建议

1. 先启动 `proxy_server.py`，等待模型加载完成
2. 再启动 `watch_charles_mirror.py` 开始监听
3. 配置Charles Mirror功能
4. 开始答题，答案会自动显示在Flask服务的控制台

