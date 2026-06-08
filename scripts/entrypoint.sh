#!/bin/bash
set -e

echo "=== LiteLLM Sidecar Proxy Starting ==="

# 注册自定义 callback 到 litellm
echo "Registering async logger callback..."
python -c "
import sys
sys.path.insert(0, '/app')
from callbacks.async_logger import custom_async_logger
import litellm
litellm.success_callback = [custom_async_logger]
litellm.failure_callback = [custom_async_logger]
print('✓ Callback registered')
"

# 显示配置
echo "Config: ${LITELLM_CONFIG}"
echo "Main URL: ${LITELLM_MAIN_URL}"
echo "Log Dir: ${LITELLM_LOG_DIR}"
echo "Port: ${PORT}"

# 启动 litellm proxy
echo "Starting LiteLLM proxy server..."
exec litellm \
    --config "${LITELLM_CONFIG}" \
    --port "${PORT}" \
    --host 0.0.0.0