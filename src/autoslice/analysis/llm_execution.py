"""话题分析 LLM 并发与进度回调执行策略。"""

from __future__ import annotations

import os
import threading
from collections.abc import Callable

LLM_DEFAULT_CONCURRENCY = 3
LLM_MAX_CONCURRENCY = 4

ProgressCallback = Callable[[str, int, int], None]


FACADE_EXPORTS = {
    "LLM_DEFAULT_CONCURRENCY": "LLM_DEFAULT_CONCURRENCY",
    "LLM_MAX_CONCURRENCY": "LLM_MAX_CONCURRENCY",
    "_configured_llm_concurrency": "configured_llm_concurrency",
    "_serialized_progress_callback": "serialized_progress_callback",
}


def configured_llm_concurrency() -> int:
    """读取并裁剪 LLM 并发数，避免向上游发起过量请求。"""

    raw_value = os.environ.get(
        "AUTOSLICE_LLM_CONCURRENCY",
        str(LLM_DEFAULT_CONCURRENCY),
    )
    try:
        value = int(raw_value)
    except (TypeError, ValueError):
        value = LLM_DEFAULT_CONCURRENCY
    return max(1, min(LLM_MAX_CONCURRENCY, value))


def serialized_progress_callback(
    progress_callback: ProgressCallback | None,
) -> ProgressCallback | None:
    """用互斥锁包装进度回调，保证并发消息以完整调用为单位写入。"""

    if not progress_callback:
        return None
    lock = threading.Lock()

    def report(message: str, step: int, total: int) -> None:
        with lock:
            progress_callback(message, step, total)

    return report
