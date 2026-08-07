"""通用重试工具（docs/design/02 §4.3 补充）。

提供两个可组合的原语：

1. ``retry_on`` —— 装饰器/调用器，指数退避重试指定异常。
   ``NormalEnd``/``CancelledError``/``PolicyViolation`` **不重试**（这些是语义终止）。
   ``Retry`` 异常的 ``attempts`` 字段可覆盖装饰器默认值。

2. ``retry_until`` —— 轮询谓词直到为 True 或超时。
   替代任务中散落的 ``while+sleep`` 循环，统一超时+日志。

用法::

    # 装饰器
    @retry_on(TaskError, max_attempts=3, delay=1.0)
    def do_something(ctx, g):
        ...

    # 直接调用
    result = retry_on(TimeoutError)(lambda: risky_operation(), max_attempts=5)

    # 轮询
    ok = retry_until(lambda: g.find_text("确认") is not None, timeout=10.0)
"""

from __future__ import annotations

import functools
import time
from typing import Any, Callable, Sequence, TypeVar

from framework.errors import CancelledError, NormalEnd, PolicyViolation

T = TypeVar("T")

# 不重试的异常（语义终止，非瞬态故障）
_NON_RETRYABLE = (NormalEnd, CancelledError, PolicyViolation)


def retry_on(
    *errors: type[Exception],
    max_attempts: int = 3,
    delay: float = 1.0,
    backoff: float = 2.0,
    max_delay: float = 30.0,
    on_retry: Callable[[int, Exception], None] | None = None,
) -> Callable:
    """装饰器/调用器：指数退避重试指定异常。

    Args:
        *errors: 可重试的异常类型。为空时重试所有 Exception（除 NON_RETRYABLE）。
        max_attempts: 最大尝试次数（含首次）。
        delay: 首次重试延迟（秒）。
        backoff: 退避倍数（delay * backoff^attempt）。
        max_delay: 单次延迟上限（秒）。
        on_retry: 重试回调 (attempt, exception)。

    Returns:
        装饰器（用于函数）或可直接调用 ``retry_on(TimeoutError)(fn, *args)``。

    注意：
        - NormalEnd / CancelledError / PolicyViolation 永不重试
        - 如果函数抛 Retry(attempts=N)，N 覆盖 max_attempts
    """
    retryable = tuple(errors) if errors else (Exception,)

    def decorator(fn: Callable[..., T]) -> Callable[..., T]:
        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> T:
            effective_max = max_attempts
            attempt = 0
            while True:
                attempt += 1
                try:
                    return fn(*args, **kwargs)
                except _NON_RETRYABLE:
                    raise
                except Exception as e:
                    # Retry 异常的 attempts 字段覆盖
                    from framework.errors import Retry

                    if isinstance(e, Retry) and e.attempts is not None:
                        effective_max = e.attempts

                    # 检查是否可重试
                    if not isinstance(e, retryable):
                        raise
                    if attempt >= effective_max:
                        raise

                    # 退避
                    sleep_time = min(delay * (backoff ** (attempt - 1)), max_delay)
                    if on_retry:
                        on_retry(attempt, e)
                    time.sleep(sleep_time)

        return wrapper

    return decorator


def retry_until(
    pred: Callable[[], bool],
    timeout: float = 30.0,
    interval: float = 0.5,
    label: str = "",
) -> bool:
    """轮询谓词直到为 True 或超时。

    Args:
        pred: 无参谓词，返回 True 表示成功。
        timeout: 超时秒数。
        interval: 轮询间隔（秒）。
        label: 描述标签（用于日志/错误信息）。

    Returns:
        True = 谓词在超时前为 True；False = 超时。
    """
    deadline = time.monotonic() + timeout
    while True:
        try:
            if pred():
                return True
        except Exception:
            pass  # 谓词异常视为 False，继续轮询
        if time.monotonic() >= deadline:
            return False
        remaining = deadline - time.monotonic()
        time.sleep(min(interval, max(0.0, remaining)))
