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
