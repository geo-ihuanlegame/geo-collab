# GEO MCP 接入指南

让 Claude Code 直接操作 GEO 平台（读写文章 / 生文 / 配图 / 送审 / 分发 / 拉数据）。
连的是**远端后端** `geo.huanchanghuyu.com`，所以**不需要**本地起后端、数据库或 AI Key。
前提：能跑 Python + 能访问该后端 + 有团队 token。

---

## 4 步接入

### 1. 确认你要用的 Python
```
python -c "import sys; print(sys.executable)"
```
记下打印的**绝对路径**，下面两步用同一个。

### 2. 用这个 Python 装依赖（最关键）
```
python -m pip install -r E:\geo\requirements-mcp.txt
```
> 只有 3 个轻依赖：`mcp[cli]` + `httpx` + `pydantic`。

### 3. 配 `~/.claude.json`（Windows：`C:\Users\<你>\.claude.json`）
在 `mcpServers` 里加：
```json
"geo": {
  "command": "C:\\Users\\<你>\\miniconda3\\python.exe",
  "args": ["-m", "server.mcp"],
  "env": {
    "GEO_MCP_TOKEN": "b602df64a11563377617870174618a5a1b6f8adc06e58c2cd8ee7b1b991a6892",
    "GEO_API_BASE_URL": "http://geo.huanchanghuyu.com",
    "PYTHONPATH": "E:\\geo"
  }
}
```
**三处必须对**：
- `command` → 填**第 1 步的绝对路径**（别用裸 `"python"`，会漂到没装包的环境）。
- `PYTHONPATH` → 你本机 geo 仓库根目录。
- `GEO_MCP_TOKEN` → 团队共享 service token（内部用，别外传）。

### 4. 重启 Claude Code → `/mcp`
看到 geo 变 **connected** 即成功。

---

## 排错对照表

| `/mcp` 现象 | 原因 | 解决 |
|---|---|---|
| `-32000` / `Failed to reconnect` | spawn 的 python 没装 `mcp[cli]` | 回第 2 步，**用 command 里那个 python** 装 |
| `ModuleNotFoundError: server` | `PYTHONPATH` 没指对仓库根 | 改 env 里的 `PYTHONPATH` |
| 连上但 tool 调用报 5xx / 超时 | 后端不通 | 查网络 / VPN，确认能访问 `geo.huanchanghuyu.com` |
| 启动即退、提示 token 为空 | 没填 token | 补 env 里的 `GEO_MCP_TOKEN` |

接入前可先单测环境（不依赖后端）：
```
set GEO_MCP_TOKEN=stub && <你的python绝对路径> -c "import server.mcp.server as s; print('tools=', len(s.mcp._tool_manager._tools))"
```
打印 `tools= 17` 即环境就绪。

---

## 一句话原理

执行 `python -m server.mcp` 的那个 Python **必须装了 `mcp[cli]`**。
裸 `python` 在不同机器解析到的解释器不同，容易漂到没装包的环境 → `-32000`。
把 `command` 钉死成绝对路径、并保证它装了依赖，就一劳永逸。
