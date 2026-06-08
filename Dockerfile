FROM python:3.11-slim AS base

# 元信息
LABEL org.opencontainers.image.source="https://github.com/richer421/litellm-sidecar"
LABEL org.opencontainers.image.description="LiteLLM Sidecar Proxy - async observability for AI inference"
LABEL org.opencontainers.image.licenses="MIT"

WORKDIR /app

# 安装依赖（分层构建，依赖层可缓存）
RUN pip install --no-cache-dir \
    litellm[proxy]==1.76.0 \
    requests>=2.31.0

# 复制 sidecar 自定义模块
COPY callbacks/ /app/callbacks/
COPY config/ /app/config/
COPY scripts/ /app/scripts/

# 创建日志缓冲目录
RUN mkdir -p /app/logs/spool /app/logs/spool/dead_letter

# 环境变量默认值
ENV LITELLM_CONFIG=/app/config/litellm_config.yaml
ENV LITELLM_MAIN_URL=http://litellm-main:4000
ENV LITELLM_LOG_DIR=/app/logs/spool
ENV LITELLM_PUSH_TIMEOUT=5
ENV LITELLM_MAX_RETRY_HOURS=72
ENV LITELLM_RESCAN_INTERVAL=30
ENV LITELLM_MASK_API_KEYS=true
ENV PORT=4000

# 健康检查
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import requests; r = requests.get('http://localhost:4000/health', timeout=3); r.raise_for_status()" || exit 1

EXPOSE 4000

# 启动：先注册 callback，再启动 proxy
ENTRYPOINT ["bash", "/app/scripts/entrypoint.sh"]