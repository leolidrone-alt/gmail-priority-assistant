"""通用工具：日志、重试、文本清洗。

刻意不引入 tenacity / rich 之类的额外依赖，项目要保持轻量（后面要上 OrangePi）。
"""

from __future__ import annotations

import functools
import logging
import re
import sys
import time
from typing import Callable

logger = logging.getLogger("aide")


def setup_logging(verbose: bool = False) -> logging.Logger:
    """配置一次性日志输出，顺便修掉 Windows GBK 控制台打不出中文的问题。"""
    from config import LOG_LEVEL  # 延迟导入，避免 config ↔ utils 循环依赖

    level = logging.DEBUG if verbose else getattr(logging, LOG_LEVEL, logging.INFO)
    root = logging.getLogger()
    root.setLevel(level)

    if not root.handlers:
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (AttributeError, ValueError):  # 重定向到文件时可能不支持
                pass

        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)-7s %(name)s | %(message)s", "%H:%M:%S")
        )
        root.addHandler(handler)

    # 第三方库太吵，压掉（openai 新版走 httpx2/httpcore2，名字可能变）
    for noisy in (
        "googleapiclient.discovery_cache",
        "urllib3",
        "httpx",
        "httpx2",
        "httpcore",
        "httpcore2",
        "openai._base_client",
    ):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    return logger


def retry(
    attempts: int = 3,
    base_delay: float = 0.8,
    max_delay: float = 8.0,
    retry_on: Callable[[Exception], bool] | None = None,
    description: str = "",
):
    """指数退避重试装饰器。

    retry_on 返回 False 的异常会立刻抛出（比如 400 这类重试也没用的错误）。
    """

    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            last_exc: Exception | None = None
            for attempt in range(1, attempts + 1):
                try:
                    return func(*args, **kwargs)
                except Exception as exc:  # noqa: BLE001 —— 重试层需要兜住所有异常
                    last_exc = exc
                    retryable = retry_on(exc) if retry_on else True
                    if attempt >= attempts or not retryable:
                        raise
                    delay = min(max_delay, base_delay * 2 ** (attempt - 1))
                    logger.warning(
                        "%s failed (attempt %d/%d): %s: %s — retrying in %.1fs",
                        description or func.__name__,
                        attempt,
                        attempts,
                        type(exc).__name__,
                        exc,
                        delay,
                    )
                    time.sleep(delay)
            raise last_exc  # pragma: no cover - 逻辑上不可达

        return wrapper

    return decorator


def is_retryable_http_error(exc: Exception) -> bool:
    """判断 googleapiclient 的 HttpError / google.auth 的 RefreshError 是否值得重试。"""
    # invalid_grant / invalid_client 是确定性错误，重试多少次都一样
    if type(exc).__name__ == "RefreshError":
        return "invalid_grant" not in str(exc).lower()

    status = getattr(exc, "status_code", None) or getattr(getattr(exc, "resp", None), "status", None)
    try:
        status = int(status)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        status = None

    if status in (429, 500, 502, 503, 504):
        return True
    if status in (400, 401, 403, 404):
        # 403 里有配额问题也有权限问题，只对限流原因重试
        text = str(exc).lower()
        return "rate limit" in text or "ratelimitexceeded" in text or "backend error" in text
    return status is None


def is_retryable_api_error(exc: Exception) -> bool:
    """判断 openai SDK 异常是否值得重试（按类名判断，避免强依赖具体版本）。"""
    name = type(exc).__name__
    if name in {"RateLimitError", "APIConnectionError", "APITimeoutError", "InternalServerError"}:
        return True
    if name in {"BadRequestError", "AuthenticationError", "PermissionDeniedError", "NotFoundError"}:
        return False
    # 其余网络类错误（httpx 的 ConnectError / ReadTimeout 等）也重试
    return "Connection" in name or "Timeout" in name


_WS_RE = re.compile(r"[ \t\u00a0\u3000]+")
_MULTI_NL_RE = re.compile(r"\n{3,}")
_ZW_RE = re.compile(r"[\u200b-\u200d\ufeff]")


def clean_text(text: str) -> str:
    """压缩空白、去掉零宽字符，保留段落结构。"""
    if not text:
        return ""
    text = _ZW_RE.sub("", text.replace("\r\n", "\n").replace("\r", "\n"))
    text = _WS_RE.sub(" ", text)
    text = "\n".join(line.strip() for line in text.split("\n"))
    return _MULTI_NL_RE.sub("\n\n", text).strip()


def truncate_middle(text: str, limit: int) -> str:
    """超长文本保留头尾，尾部常常是签名/截止时间等关键信息。"""
    if len(text) <= limit:
        return text
    head = int(limit * 0.7)
    tail = limit - head - 20
    omitted = len(text) - head - tail
    return f"{text[:head]}\n\n...[{omitted} characters omitted]...\n\n{text[-tail:]}"
