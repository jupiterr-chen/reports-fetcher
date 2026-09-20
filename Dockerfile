# reports-fetcher 容器化交付（主力运行方式，见 ARCHITECTURE §3）
#
# 目标：
#   dev    : I0 来源契约探测与开发（当前阶段）
#   runtime: CLI 运行（I1 起启用）
#   server : HTTP 服务（I5 起启用）
#
# 可覆盖构建参数（配合 docker-compose.yml）：
#   BASE_IMAGE    基础镜像（默认 python:3.12-slim，满足 3.11+ 基线；国内网络可用镜像仓库前缀覆盖）
#   PIP_INDEX_URL pip 索引（默认清华源；海外构建用 PIP_INDEX_URL=https://pypi.org/simple 覆盖）

ARG BASE_IMAGE=python:3.12-slim
FROM ${BASE_IMAGE} AS base
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1
WORKDIR /app

# ---- dev / probe：来源契约探测与开发 ----
FROM base AS dev
ARG PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
RUN pip install --index-url "$PIP_INDEX_URL" --no-cache-dir "requests>=2.31,<3"
# 源码经 bind mount 覆盖（compose 中挂载 .:/app），COPY 仅保证镜像可独立运行
COPY . /app
CMD ["python", "tools/probe/probe_sources.py", "--market", "all"]

# ---- runtime：CLI（I1 起启用，届时安装本包） ----
# FROM base AS runtime
# COPY --from=build /app/dist/*.whl .
# RUN pip install --no-cache-dir . && rm -f *.whl
# ENTRYPOINT ["python", "-m", "reports_fetcher"]

# ---- server：HTTP 服务（I5 起启用，server 安装组） ----
# FROM runtime AS server
# RUN pip install --no-cache-dir ".[server]"
# EXPOSE 8000
# ENTRYPOINT ["python", "-m", "reports_fetcher", "serve"]
