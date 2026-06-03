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

# 2. 兜底：确保基础镜像 python:3.12-slim 在本地。
# 国内 DNS 常污染 docker.io 鉴权（auth.docker.io），且 Docker Desktop 开启
# containerd 镜像存储时 daemon.json 的 registry-mirrors 不生效。这里若本地缺
# 基础镜像，就从国内镜像源拉取再打回标准 tag，让后续 build 命中本地、不碰 docker.io。
$baseImg = "python:3.12-slim"
$mirrorImg = "docker.m.daocloud.io/library/python:3.12-slim"
if (-not (docker image inspect $baseImg 2>$null)) {
    Write-Host "[dev-docker] 本地缺 $baseImg，从镜像源拉取并打 tag..." -ForegroundColor Yellow
    docker pull $mirrorImg
    if ($LASTEXITCODE -eq 0) { docker tag $mirrorImg $baseImg }
    else { Write-Host "[dev-docker] 镜像源拉取失败，将尝试直连 docker.io（可能因网络失败）。" -ForegroundColor Yellow }
}

# 3. 起后端
Write-Host "[dev-docker] 启动后端容器 (compose up -d --build)..." -ForegroundColor Cyan
Invoke-Compose up -d --build

# 4. 等 app 就绪
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

# 5. 起前端（宿主 Vite）
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


