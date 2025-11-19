# 使用Charles代理工具（推荐方案）

由于Flask作为HTTP代理服务器无法完全处理HTTPS请求，**强烈建议使用Charles等专业代理工具**。

## 为什么需要Charles？

1. **HTTPS支持**：Charles可以处理HTTPS的CONNECT请求，建立安全隧道
2. **更稳定**：专业的代理工具经过充分测试
3. **功能强大**：支持请求修改、重定向、断点等功能

## 使用Charles的步骤

### 1. 安装Charles

下载并安装Charles Proxy（免费版即可）

### 2. 配置Charles转发规则

1. 打开Charles
2. 菜单：**Tools** → **Rewrite**（或**Tools** → **Map Remote**）
3. 启用**Enable Rewrite**
4. 添加规则：
   - **Type**: Map Remote
   - **Protocol**: https
   - **Host**: `quiz-activity.eastmoney.com`
   - **Port**: 443
   - **Map to**: `http://localhost:8889`
   - **Path**: `/qa/api/quiz/*`（只转发题目相关请求）

或者使用**Map Remote**功能：
- **From**: `https://quiz-activity.eastmoney.com/qa/api/quiz/*`
- **To**: `http://localhost:8889/qa/api/quiz/*`

### 3. 配置手机代理

1. 在手机上设置HTTP代理指向Charles
   - 代理地址：你的电脑IP
   - 代理端口：`8888`（Charles默认端口）

2. 安装Charles证书（用于HTTPS）
   - 在手机上访问：`chls.pro/ssl`
   - 下载并安装证书
   - 在系统设置中信任证书

### 4. 启动搜题服务

```bash
python proxy_server.py
```

### 5. 开始使用

1. 打开东方财富APP
2. 开始答题
3. 答案会显示在 `proxy_server.py` 的控制台日志中

## 使用Fiddler（替代方案）

如果你使用Fiddler：

1. 打开Fiddler
2. **Tools** → **Options** → **Connections**
3. 勾选**Allow remote computers to connect**
4. 记住端口号（默认8888）

5. 配置AutoResponder规则：
   - **Rule**: `https://quiz-activity.eastmoney.com/qa/api/quiz/*`
   - **Action**: `http://localhost:8889/qa/api/quiz/*`

6. 手机配置代理指向Fiddler

## 优势

✅ **完全支持HTTPS**  
✅ **不影响其他网络连接**  
✅ **更稳定可靠**  
✅ **可以查看所有请求详情**

## 如果仍然无法连接

1. **检查防火墙**：确保Charles/Fiddler的端口没有被阻止
2. **检查证书**：确保已安装并信任Charles证书
3. **检查网络**：确保手机和电脑在同一网络
4. **查看日志**：检查Charles/Fiddler的日志，看是否有错误

