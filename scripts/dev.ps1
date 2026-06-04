<#
.SYNOPSIS
    本地开发：启动前端 (Vite:5173)，可选连本地或外网后端。

.DESCRIPTION
    - 前端跑在 5173（CORS 只放行 5173），Vite 自带 HMR，改源码保存即热更新。
    - Vite 把 /api 代理到后端。代理目标由环境变量 GEO_API_TARGET 决定，
      默认 http://127.0.0.1:8000。指定 -ApiTarget 可临时指向外网后端。
    - 仅当连本地后端、且检测到 conda 时，才会新开窗口启动 uvicorn；
      否则只起前端（纯 UI 改动可实时预览，接口走代理）。

.PARAMETER ApiTarget
    后端代理地址。给了它就走「纯前端 + 远程后端」模式，不在本地起后端。
    例：-ApiTarget "https://your-backend.example.com"

.PARAMETER FrontendOnly
    只起前端，不尝试本地后端（连本地 8000，需另行启动后端时用）。

.EXAMPLE
    .\scripts\dev.ps1                                            # 前端 + 本地后端(若有 conda)
    .\scripts\dev.ps1 -FrontendOnly                             # 只起前端，连本地 8000
    .\scripts\dev.ps1 -ApiTarget "https://api.example.com"      # 前端本地 + 外网后端
#>
param(
    [string]$ApiTarget,
    [switch]$FrontendOnly
)

$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot

# 给了 -ApiTarget 就走远程后端模式：设置代理目标，且不在本地起后端。
if ($ApiTarget) {
    $env:GEO_API_TARGET = $ApiTarget
    $FrontendOnly = $true
    Write-Host "[dev] /api 代理 -> $ApiTarget （纯前端 + 远程后端模式）" -ForegroundColor Cyan
} elseif ($env:GEO_API_TARGET) {
    Write-Host "[dev] /api 代理 -> $($env:GEO_API_TARGET) （来自环境变量 GEO_API_TARGET）" -ForegroundColor Cyan
}

# 确保前端依赖已安装
if (-not (Test-Path (Join-Path $repo "web\node_modules"))) {
    Write-Host "[dev] 安装前端依赖 (pnpm install)..." -ForegroundColor Cyan
    Push-Location $repo
    pnpm install
    Pop-Location
}

# 没装 conda 就跑不了本地后端，自动降级为只起前端。
$condaFound = $null -ne (Get-Command conda -ErrorAction SilentlyContinue)
if (-not $FrontendOnly -and -not $condaFound) {
    Write-Host "[dev] 未检测到 conda，跳过本地后端，仅起前端。" -ForegroundColor Yellow
    Write-Host "[dev] (要连外网后端请用 -ApiTarget；纯 UI 改动现在就能实时预览。)" -ForegroundColor Yellow
    $FrontendOnly = $true
}

if (-not $FrontendOnly) {
    # ---- 后端：新开一个窗口，激活 conda 环境后启动 uvicorn ----
    # 必填环境变量：未设置时 create_app() 会抛 RuntimeError / 数据库连不上。
    # 按需修改下面三项，或改为在 .env 里配置后删掉这几行。
    if (-not $env:GEO_JWT_SECRET)   { $env:GEO_JWT_SECRET   = "dev-only-change-me-please-use-a-long-random-string" }
    if (-not $env:GEO_DATA_DIR)     { $env:GEO_DATA_DIR     = (Join-Path $repo "data") }
    if (-not $env:GEO_DATABASE_URL) { $env:GEO_DATABASE_URL = "mysql+pymysql://geo_user:password@127.0.0.1:3306/geo_dev" }

    Write-Host "[dev] 启动后端 uvicorn (新窗口, :8000)..." -ForegroundColor Cyan
    $backendCmd = @"
conda activate geo_xzpt
`$env:GEO_JWT_SECRET   = '$($env:GEO_JWT_SECRET)'
`$env:GEO_DATA_DIR     = '$($env:GEO_DATA_DIR)'
`$env:GEO_DATABASE_URL = '$($env:GEO_DATABASE_URL)'
Set-Location '$repo'
uvicorn server.app.main:app --reload --host 127.0.0.1 --port 8000
"@
    Start-Process powershell -ArgumentList "-NoExit", "-Command", $backendCmd
}

# ---- 前端：在当前窗口跑 Vite dev server ----
Write-Host "[dev] 启动前端 Vite (:5173) -> http://127.0.0.1:5173" -ForegroundColor Green
Push-Location $repo
pnpm --filter @geo/web dev
Pop-Location

