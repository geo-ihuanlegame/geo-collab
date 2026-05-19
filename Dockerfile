# Geo Collab Docker 镜像
# React 前端构建 + FastAPI + Playwright + Chromium + noVNC 远程浏览器

FROM node:22-bookworm-slim AS web-build

WORKDIR /app

COPY package.json pnpm-lock.yaml pnpm-workspace.yaml ./
COPY web/package.json web/package.json
RUN corepack enable && corepack prepare pnpm@10.4.0 --activate
RUN pnpm install --frozen-lockfile

COPY web ./web
RUN pnpm --filter @geo/web build

FROM python:3.12-slim

# 换阿里云 apt 镜像（国内服务器加速）
RUN sed -i 's|http://deb.debian.org/debian|http://mirrors.aliyun.com/debian|g' /etc/apt/sources.list.d/debian.sources 2>/dev/null || \
    sed -i 's|http://deb.debian.org/debian|http://mirrors.aliyun.com/debian|g' /etc/apt/sources.list 2>/dev/null || true

# 系统依赖：Chromium 浏览器、Xvfb 虚拟显示、VNC 远程桌面、noVNC Web 客户端、
# 中文字体、Chromium/Playwright 运行时库
RUN apt-get update && apt-get install -y --no-install-recommends \
    xvfb \
    x11vnc \
    websockify \
    novnc \
    chromium \
    fonts-noto-cjk \
    libnss3 \
    libnspr4 \
    libatk-bridge2.0-0 \
    libdrm2 \
    libxkbcommon0 \
    libgbm1 \
    libasound2 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 安装 Python 依赖（清华镜像加速）
COPY requirements.txt .
RUN pip install --no-cache-dir \
    -i https://pypi.tuna.tsinghua.edu.cn/simple \
    -r requirements.txt

# 复制项目源码和前端构建产物
COPY . .
COPY --from=web-build /app/web/dist ./web/dist

# 安装 Playwright 所需的 Chromium 浏览器（npmmirror 国内加速）
RUN PLAYWRIGHT_DOWNLOAD_HOST=https://npmmirror.com/mirrors/playwright \
    playwright install chromium

EXPOSE 8000

CMD ["sh", "-c", "alembic upgrade head && uvicorn server.app.main:app --host 0.0.0.0 --port 8000"]
