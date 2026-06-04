# Docker 本地集成测试环境 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 Windows 上用一套独立的开发 Docker 编排跑后端（FastAPI + MySQL + MinIO），前端在宿主 Vite 热更新，实现本地前后端集成测试。

**Architecture:** 混合拓扑——后端三件套在 Docker（WSL2 后端），前端 Vite dev server 跑在宿主 5173 并把 `/api` 代理到 Docker 暴露的 `127.0.0.1:8000`。后端用精瘦镜像（无 Chromium/noVNC），源码挂载实现 `--reload`。生产配置完全不动。

**Tech Stack:** Docker Desktop (WSL2)、docker compose、python:3.12-slim、MySQL 8.0、MinIO、Vite/pnpm、PowerShell。

**关键约束：**
- 宿主机当前只有 Windows Store 的 python stub、无 conda、无 docker；因此**配置文件的静态校验手段有限**，真正的功能校验集中在「Phase 2」（装完 Docker 后）。
- 所有 `.ps1` 文件必须存为 **UTF-8 BOM**（Windows PowerShell 5.1 解析中文要求）。
- 不改 `docker-compose.yml` / `Dockerfile` / `Dockerfile.nginx` / `nginx.conf`。

---

## 文件结构

| 文件 | 动作 | 职责 |
|---|---|---|
| `Dockerfile.app` | 新建 | 精瘦后端镜像（仅 API 进程，无浏览器自动化） |
| `docker-compose.dev.yml` | 新建 | 开发编排：mysql + minio + app（无 worker/nginx） |
| `scripts/dev-docker.ps1` | 新建 | 一键编排：生成 .env → compose up → 等就绪 → 起 Vite |
| `.env` | 新建（git 忽略） | 由脚本生成，开发默认凭据 + 随机 JWT secret |
| `docs/.../2026-06-03-docker-local-dev-design.md` | 已存在 | 设计稿 |

---

## Phase 0 — 创建配置文件（无需 Docker，现在就能做）

### Task 1: 精瘦后端镜像 `Dockerfile.app`

**Files:**
- Create: `Dockerfile.app`

- [ ] **Step 1: 写文件**

```dockerfile
# 精瘦后端镜像：仅 FastAPI/API 进程，无浏览器自动化。
# 不含 Chromium / noVNC / 中文字体，不跑 playwright install。
# 发布 worker 仍用根目录的 Dockerfile（重型镜像）。
FROM python:3.12-slim

WORKDIR /app

# 清华 pip 镜像加速；requirements 先装以利用层缓存
COPY requirements.txt .
RUN pip install --no-cache-dir \
    -i https://pypi.tuna.tsinghua.edu.cn/simple \
    -r requirements.txt

# 复制源码（.dockerignore 已排除 node_modules/.git/data 等）
COPY . .

EXPOSE 8000

# 默认命令（compose 会覆盖为带 --reload 的版本）
CMD ["sh", "-c", "alembic upgrade head && uvicorn server.app.main:app --host 0.0.0.0 --port 8000"]
```

- [ ] **Step 2: 校验文件存在且非空**

Run: `Get-Item Dockerfile.app | Select-Object Length`
Expected: Length > 0。

- [ ] **Step 3: Commit**

```bash
git add Dockerfile.app
git commit -m "feat(docker): 精瘦后端开发镜像 Dockerfile.app"
```

---

### Task 2: 开发编排 `docker-compose.dev.yml`

**Files:**
- Create: `docker-compose.dev.yml`

- [ ] **Step 1: 写文件**

```yaml
# 开发用编排：后端在 Docker，前端在宿主 Vite (5173)。
# 不含 worker / nginx（本地不测发布流程）。生产请用 docker-compose.yml。
services:
  mysql:
    image: mysql:8.0
    restart: unless-stopped
    environment:
      MYSQL_ROOT_PASSWORD: ${MYSQL_ROOT_PASSWORD:?required}
      MYSQL_DATABASE: ${MYSQL_DATABASE:-geo_collab}
      MYSQL_USER: ${MYSQL_USER:-geo_user}
      MYSQL_PASSWORD: ${MYSQL_PASSWORD:?required}
    ports:
      - "127.0.0.1:3306:3306"
    volumes:
      - mysql_data:/var/lib/mysql
    healthcheck:
      test: ["CMD", "mysqladmin", "ping", "-h", "localhost"]
      interval: 10s
      timeout: 5s
      retries: 5

  minio:
    image: minio/minio
    restart: unless-stopped
    command: server /data --console-address ":9001"
    environment:
      MINIO_ROOT_USER: ${MINIO_ROOT_USER:?required}
      MINIO_ROOT_PASSWORD: ${MINIO_ROOT_PASSWORD:?required}
    ports:
      - "127.0.0.1:9000:9000"
      - "127.0.0.1:9001:9001"
    volumes:
      - minio_data:/data

  app:
    build:
      context: .
      dockerfile: Dockerfile.app
    restart: unless-stopped
    depends_on:
      mysql:
        condition: service_healthy
      minio:
        condition: service_started
    env_file:
      - .env
    environment:
      GEO_DB_HOST: mysql
      GEO_DB_PORT: 3306
      GEO_DB_USER: ${MYSQL_USER:-geo_user}
      GEO_DB_PASS: ${MYSQL_PASSWORD}
      GEO_DB_NAME: ${MYSQL_DATABASE:-geo_collab}
      GEO_DATA_DIR: /app/data
      GEO_MINIO_ENDPOINT: minio:9000
      GEO_MINIO_ACCESS_KEY: ${MINIO_ROOT_USER}
      GEO_MINIO_SECRET_KEY: ${MINIO_ROOT_PASSWORD}
      # Windows bind mount 文件事件不可靠，强制 uvicorn --reload 轮询
      WATCHFILES_FORCE_POLLING: "true"
    command: >
      sh -c "alembic upgrade head &&
             python -m server.scripts.seed_users &&
             uvicorn server.app.main:app --host 0.0.0.0 --port 8000 --reload"
    ports:
      - "127.0.0.1:8000:8000"
    volumes:
      # 挂载源码实现后端热重载（alembic.ini 仍用镜像内烤好的）
      - ./server:/app/server
      - app_data:/app/data

volumes:
  mysql_data:
  minio_data:
  app_data:
```

- [ ] **Step 2: 校验文件存在**

Run: `Get-Item docker-compose.dev.yml | Select-Object Length`
Expected: Length > 0。

> 注：真正的 `docker compose config` 语法校验在 Phase 2（装完 Docker）执行。

- [ ] **Step 3: Commit**

```bash
git add docker-compose.dev.yml
git commit -m "feat(docker): 开发编排 docker-compose.dev.yml（mysql+minio+app）"
```

---

### Task 3: 编排脚本 `scripts/dev-docker.ps1`

**Files:**
- Create: `scripts/dev-docker.ps1`

- [ ] **Step 1: 写文件**

```powershell
<#
.SYNOPSIS
    本地集成测试：后端 (FastAPI+MySQL+MinIO) 跑 Docker，前端 Vite 跑宿主 (5173)。

.DESCRIPTION
    - 首次运行自动生成 .env（含随机 JWT secret、种子 admin）。
    - docker compose -f docker-compose.dev.yml up -d --build 起后端。
    - 等 :8000 就绪后在宿主启动 Vite（/api 已代理到 127.0.0.1:8000）。
    - 不含 worker/nginx；不测发布流程。

.PARAMETER Down       停止并移除容器（保留数据卷）。
.PARAMETER Logs       跟随查看 app 容器日志。
.PARAMETER Rebuild    强制重建镜像后再起。
.PARAMETER NoFrontend 只起后端 Docker，不在宿主启动 Vite。

.EXAMPLE
    .\scripts\dev-docker.ps1              # 起后端 + 前端
    .\scripts\dev-docker.ps1 -NoFrontend # 只起后端
    .\scripts\dev-docker.ps1 -Logs       # 看后端日志
    .\scripts\dev-docker.ps1 -Down       # 停
#>
param(
    [switch]$Down,
    [switch]$Logs,
    [switch]$Rebuild,
    [switch]$NoFrontend
)

$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
$compose = Join-Path $repo "docker-compose.dev.yml"
$envFile = Join-Path $repo ".env"

# docker 是否就绪
if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    Write-Host "[dev-docker] 未找到 docker。请先安装 Docker Desktop 并启动它。" -ForegroundColor Red
    exit 1
}

function Invoke-Compose { docker compose -f $compose @args }

if ($Down) { Invoke-Compose down; return }
if ($Logs) { Invoke-Compose logs -f app; return }

# 1. 确保 .env
if (-not (Test-Path $envFile)) {
    Write-Host "[dev-docker] 生成 .env（随机 JWT secret，种子 admin/admin12345）..." -ForegroundColor Cyan
    $jwt = -join ((1..64) | ForEach-Object { '{0:x}' -f (Get-Random -Maximum 16) })
    $content = @"
MYSQL_ROOT_PASSWORD=geo_root_dev
MYSQL_DATABASE=geo_collab
MYSQL_USER=geo_user
MYSQL_PASSWORD=geo_pass_dev
GEO_JWT_SECRET=$jwt
GEO_SEED_USERS=[{"username":"admin","password":"admin12345","role":"admin"}]
MINIO_ROOT_USER=minioadmin
MINIO_ROOT_PASSWORD=minioadmin123
"@
    # ASCII 编码，避免 BOM 干扰 docker compose 读取
    [System.IO.File]::WriteAllText($envFile, $content, [System.Text.Encoding]::ASCII)
}

# 2. 起后端
Write-Host "[dev-docker] 启动后端容器 (compose up -d --build)..." -ForegroundColor Cyan
$buildFlag = if ($Rebuild) { "--build" } else { "--build" }  # 总是 --build；compose 用缓存
Invoke-Compose up -d $buildFlag

# 3. 等 app 就绪
Write-Host "[dev-docker] 等待后端 http://127.0.0.1:8000 就绪..." -ForegroundColor Cyan
$ready = $false
for ($i = 0; $i -lt 90; $i++) {
    try {
        Invoke-WebRequest -Uri "http://127.0.0.1:8000/openapi.json" -UseBasicParsing -TimeoutSec 2 | Out-Null
        $ready = $true; break
    } catch {
        # 拿到任何 HTTP 响应（含 4xx/5xx）也算服务起来了
        if ($_.Exception.Response) { $ready = $true; break }
    }
    Start-Sleep -Seconds 2
}
if (-not $ready) {
    Write-Host "[dev-docker] 后端 90s 内未就绪。看日志：.\scripts\dev-docker.ps1 -Logs" -ForegroundColor Red
    exit 1
}
Write-Host "[dev-docker] 后端就绪。登录账号 admin / admin12345" -ForegroundColor Green

# 4. 起前端（宿主 Vite）
if ($NoFrontend) {
    Write-Host "[dev-docker] -NoFrontend：跳过前端。前端请自行 pnpm --filter @geo/web dev" -ForegroundColor Yellow
    return
}
if (-not (Test-Path (Join-Path $repo "web\node_modules"))) {
    Write-Host "[dev-docker] 安装前端依赖 (pnpm install)..." -ForegroundColor Cyan
    Push-Location $repo
    pnpm install
    Pop-Location
}
Write-Host "[dev-docker] 启动前端 Vite (:5173) -> http://127.0.0.1:5173" -ForegroundColor Green
Push-Location $repo
pnpm --filter @geo/web dev
Pop-Location
```

- [ ] **Step 2: 存为 UTF-8 BOM 并做语法校验**

Run:
```powershell
$p = "scripts\dev-docker.ps1"
$c = Get-Content -Raw -Encoding UTF8 $p
$c | Out-File -FilePath $p -Encoding UTF8   # PS5.1 的 -Encoding UTF8 = UTF-8 with BOM
$errs = $null
[System.Management.Automation.Language.Parser]::ParseFile((Resolve-Path $p), [ref]$null, [ref]$errs) | Out-Null
if ($errs) { $errs | ForEach-Object { $_.Message } } else { "OK: no parse errors" }
```
Expected: `OK: no parse errors`。

- [ ] **Step 3: Commit**

```bash
git add scripts/dev-docker.ps1
git commit -m "feat(docker): dev-docker.ps1 一键编排（compose up + 宿主 Vite）"
```

---

### Task 4: 确认 `.env` 被忽略 + 文档说明

**Files:**
- Verify: `.dockerignore`（已含 `.env` / `.env.*`，无需改）
- Verify: `.gitignore`（确认含 `.env`；若无则补）

- [ ] **Step 1: 确认 .env 不会进 git**

Run: `git check-ignore .env`
Expected: 输出 `.env`（表示已被忽略）。

- [ ] **Step 2: 若上一步无输出，则补到 .gitignore**

仅当 Step 1 没有任何输出时执行：
```powershell
Add-Content -Path .gitignore -Value "`n.env"
```
然后再次 `git check-ignore .env` 确认输出 `.env`。

- [ ] **Step 3: Commit（仅当改了 .gitignore）**

```bash
git add .gitignore
git commit -m "chore: 忽略本地 .env"
```

---

## Phase 1 — 安装 Docker Desktop（交互，需提权 + 重启）

> ⚠️ 本阶段需管理员权限和**重启**，会中断当前会话。重启后回到仓库继续 Phase 2。
> 依赖 BIOS 已开启 CPU 虚拟化（VT-x / AMD-V）。当前 `HypervisorPresent=False`。

### Task 5: 安装 WSL2 + Docker Desktop

**Files:** 无（系统级操作）

- [ ] **Step 1: 以管理员身份装 WSL2**

在**管理员 PowerShell**中：
```powershell
wsl --install
```
Expected: 提示已启用「虚拟机平台」「适用于 Linux 的 Windows 子系统」并安装默认发行版，要求重启。

- [ ] **Step 2: 重启电脑**

重启后等 WSL 首次初始化完成（可能要求设置 Linux 用户名/密码，随意设）。

- [ ] **Step 3: 验证 WSL2**

```powershell
wsl --status
wsl --list --verbose
```
Expected: 默认版本为 2，至少一个发行版处于 Running/Stopped。

- [ ] **Step 4: 装 Docker Desktop**

```powershell
winget install --id Docker.DockerDesktop -e --accept-source-agreements --accept-package-agreements
```
Expected: 安装成功。

- [ ] **Step 5: 启动 Docker Desktop 并确认后端**

手动启动 Docker Desktop → Settings → General 勾选 “Use the WSL 2 based engine” → Apply & Restart。

- [ ] **Step 6: 验证 docker 可用**

```powershell
docker version
docker compose version
```
Expected: client 与 server 版本都打印出来（server 段说明 Docker 引擎已运行）。

> 若 `wsl --install` 报虚拟化未开启：进 BIOS 开启 Intel VT-x / AMD-V（及主板的 SVM/Virtualization），再重试 Step 1。

---

## Phase 2 — 起栈并端到端验证（装完 Docker 后）

### Task 6: 启动并验证全链路

**Files:** 无（运行 + 验证）

- [ ] **Step 1: 校验 compose 文件语法**

Run: `docker compose -f docker-compose.dev.yml config`
Expected: 打印展开后的完整配置、无错误（变量缺失会在此报错——首次没有 .env 时会提示，先执行 Step 2 生成 .env 再回来跑也可）。

- [ ] **Step 2: 一键起栈**

Run: `.\scripts\dev-docker.ps1 -NoFrontend`
Expected: 生成 `.env`；mysql/minio/app 容器起来；打印「后端就绪。登录账号 admin / admin12345」。

- [ ] **Step 3: 确认容器状态**

Run: `docker compose -f docker-compose.dev.yml ps`
Expected: mysql（healthy）、minio（up）、app（up）。

- [ ] **Step 4: 确认 app 日志无崩溃**

Run: `docker compose -f docker-compose.dev.yml logs app --tail 50`
Expected: 看到 `alembic upgrade head` 跑完、seed_users 输出（创建 admin 或 already exists）、`Uvicorn running on http://0.0.0.0:8000`。

- [ ] **Step 5: 起前端并人工验收**

Run: `.\scripts\dev-docker.ps1`（这次不带 -NoFrontend；或新开窗口 `pnpm --filter @geo/web dev`）
然后浏览器打开 `http://127.0.0.1:5173`：
- 用 `admin` / `admin12345` 登录成功。
- 文章列表页能加载（无 500/网络错误）。
- 随便改一处 `web/src/` 文案保存 → 页面热更新生效。

Expected: 以上全部通过 = 前端 ↔ 后端 ↔ MySQL 全链路打通。

- [ ] **Step 6: 收尾合并**

实现完成、验收通过后，使用 superpowers:finishing-a-development-branch 决定合并/PR/清理。

---

## Self-Review 备注

- **Spec 覆盖**：精瘦镜像(Task1)、dev 编排无 worker/nginx(Task2)、一键脚本+.env 生成(Task3)、.env 忽略(Task4)、Docker 安装(Task5)、端到端验收(Task6)、vite 无需改(设计 §3.5 已说明)——全部覆盖。
- **占位符**：无 TBD/TODO；所有文件内容完整给出。
- **一致性**：compose 中 `Dockerfile.app`、服务名 `mysql`/`minio`/`app`、端口 8000、卷名在各处一致；脚本里的 compose 文件名、就绪探测 URL 与 compose 暴露端口一致。
- **宿主工具限制**：Phase 0 的校验只做「文件存在 / ps1 语法」；compose/Dockerfile 的功能校验显式推迟到 Phase 2，已在 Task2/Task6 标注。
