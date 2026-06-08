"""
LiteLLM Sidecar - Async Logger Callback

核心机制：
1. 每次模型调用完成后，先写本地 JSON 文件（保证数据不丢）
2. 后台线程异步 POST 到 LiteLLM 主服务
3. 主服务挂了 → 本地文件保留，等恢复后批量重推
4. 重推线程定期扫描 spool 目录，清理已成功推送的文件
"""

import hashlib
import json
import logging
import os
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from threading import Thread
from typing import Any

import requests

logger = logging.getLogger("litellm-sidecar.async_logger")

# 配置从环境变量读取
MAIN_URL = os.environ.get("LITELLM_MAIN_URL", "http://litellm-main:4000")
LOG_DIR = Path(os.environ.get("LITELLM_LOG_DIR", "/app/logs/spool"))
PUSH_TIMEOUT = int(os.environ.get("LITELLM_PUSH_TIMEOUT", "5"))
MAX_RETRY_HOURS = int(os.environ.get("LITELLM_MAX_RETRY_HOURS", "72"))
RESCAN_INTERVAL = int(os.environ.get("LITELLM_RESCAN_INTERVAL", "30"))
MASK_API_KEYS = os.environ.get("LITELLM_MASK_API_KEYS", "true").lower() == "true"

# 确保目录存在
LOG_DIR.mkdir(parents=True, exist_ok=True)


def _mask_sensitive(data: dict) -> dict:
    """脱敏 API Key 等敏感字段"""
    if not MASK_API_KEYS:
        return data

    sensitive_keys = ["api_key", "x-api-key", "authorization", "apikey"]
    masked = {}
    for k, v in data.items():
        if k.lower() in sensitive_keys and isinstance(v, str):
            masked[k + "_sha256"] = hashlib.sha256(v.encode()).hexdigest()[:16]
            masked[k] = "***MASKED***"
        elif isinstance(v, dict):
            masked[k] = _mask_sensitive(v)
        elif isinstance(v, list):
            masked[k] = [
                _mask_sensitive(item) if isinstance(item, dict) else item
                for item in v
            ]
        else:
            masked[k] = v
    return masked


def _extract_log_payload(
    kwargs: dict,
    response_obj: Any,
    start_time: datetime,
    end_time: datetime,
    is_failure: bool = False,
) -> dict:
    """从 LiteLLM callback 参数中提取结构化日志"""
    payload = {
        "id": str(uuid.uuid4()),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "start_time": start_time.isoformat() if start_time else None,
        "end_time": end_time.isoformat() if end_time else None,
        "duration_ms": None,
        "model": kwargs.get("model", None),
        "model_name": kwargs.get("model", None),
        "api_base": kwargs.get("api_base", None),
        "call_type": kwargs.get("call_type", "completion"),
        "is_failure": is_failure,
        "source": "sidecar",
        "metadata": kwargs.get("metadata", {}),
    }

    # 计算耗时
    if start_time and end_time:
        delta = (end_time - start_time).total_seconds() * 1000
        payload["duration_ms"] = round(delta, 2)

    # Token 用量
    if response_obj and hasattr(response_obj, "usage"):
        usage = response_obj.usage
        payload["token_usage"] = {
            "prompt_tokens": getattr(usage, "prompt_tokens", 0),
            "completion_tokens": getattr(usage, "completion_tokens", 0),
            "total_tokens": getattr(usage, "total_tokens", 0),
        }
    elif response_obj and isinstance(response_obj, dict):
        usage = response_obj.get("usage", {})
        payload["token_usage"] = {
            "prompt_tokens": usage.get("prompt_tokens", 0),
            "completion_tokens": usage.get("completion_tokens", 0),
            "total_tokens": usage.get("total_tokens", 0),
        }

    # 成功响应内容摘要
    if not is_failure and response_obj:
        if hasattr(response_obj, "choices") and response_obj.choices:
            content = response_obj.choices[0].message.content
            # 只保留前 500 字符，避免日志过大
            payload["response_preview"] = content[:500] if content else None
            payload["finish_reason"] = response_obj.choices[0].finish_reason
        elif isinstance(response_obj, dict):
            choices = response_obj.get("choices", [])
            if choices:
                content = choices[0].get("message", {}).get("content", "")
                payload["response_preview"] = content[:500] if content else None
                payload["finish_reason"] = choices[0].get("finish_reason", None)

    # 失败信息
    if is_failure and kwargs.get("exception"):
        exc = kwargs["exception"]
        payload["error"] = {
            "type": type(exc).__name__,
            "message": str(exc)[:500],
        }

    # 脱敏
    payload = _mask_sensitive(payload)

    return payload


def _write_to_spool(payload: dict) -> Path:
    """写日志到本地 spool 目录，保证数据不丢"""
    filename = f"{payload['timestamp'].replace(':', '-').replace('.', '_')}_{payload['id'][:8]}.json"
    filepath = LOG_DIR / filename
    filepath.write_text(json.dumps(payload, default=str, ensure_ascii=False))
    logger.debug(f"Log written to spool: {filepath.name}")
    return filepath


def _push_to_main(log_file: Path) -> bool:
    """推单条日志到主服务，成功返回 True"""
    try:
        data = json.loads(log_file.read_text())
        url = f"{MAIN_URL}/log/spend"
        resp = requests.post(url, json=data, timeout=PUSH_TIMEOUT)
        if resp.status_code in (200, 201):
            log_file.unlink()
            logger.debug(f"Log pushed & cleaned: {log_file.name}")
            return True
        else:
            logger.warning(
                f"Push failed [{resp.status_code}]: {log_file.name}, will retry later"
            )
            return False
    except requests.RequestException as e:
        logger.warning(f"Push error: {log_file.name} - {e}, will retry later")
        return False


def _async_push(log_file: Path):
    """后台线程：立即尝试推送一次"""
    _push_to_main(log_file)


def _rescan_spool():
    """定期扫描 spool 目录，重推失败的日志，清理过期文件"""
    now = time.time()
    for log_file in sorted(LOG_DIR.glob("*.json")):
        try:
            stat = log_file.stat()
            age_hours = (now - stat.st_mtime) / 3600

            # 超过最大重试时间的日志，归档到 dead letter
            if age_hours > MAX_RETRY_HOURS:
                dead_dir = LOG_DIR / "dead_letter"
                dead_dir.mkdir(exist_ok=True)
                dead_file = dead_dir / log_file.name
                log_file.rename(dead_file)
                logger.warning(f"Log expired, moved to dead_letter: {log_file.name}")
                continue

            # 尝试重推
            _push_to_main(log_file)
        except Exception as e:
            logger.error(f"Rescan error on {log_file.name}: {e}")


def _rescan_loop():
    """后台循环：定期重推"""
    while True:
        try:
            _rescan_spool()
        except Exception as e:
            logger.error(f"Rescan loop error: {e}")
        time.sleep(RESCAN_INTERVAL)


# 启动重推循环线程
_rescan_thread = Thread(target=_rescan_loop, daemon=True, name="sidecar-rescan")
_rescan_thread.start()
logger.info(f"Rescan thread started, interval={RESCAN_INTERVAL}s")


def custom_async_logger(
    kwargs: dict,
    response_obj: Any,
    start_time: datetime,
    end_time: datetime,
):
    """LiteLLM success_callback / failure_callback 入口"""
    is_failure = kwargs.get("exception") is not None

    payload = _extract_log_payload(kwargs, response_obj, start_time, end_time, is_failure)
    log_file = _write_to_spool(payload)

    # 后台线程异步推送（不阻塞主调用）
    Thread(
        target=_async_push,
        args=(log_file,),
        daemon=True,
        name=f"sidecar-push-{payload['id'][:8]}",
    ).start()


# 注册到 litellm
import litellm
litellm.success_callback = [custom_async_logger]
litellm.failure_callback = [custom_async_logger]
logger.info("LiteLLM sidecar async logger registered")