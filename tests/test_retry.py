"""retry.py + stuck.py 测试。

覆盖：retry_on 装饰器（成功/重试/不重试/退避/Retry异常覆盖）、
retry_until（成功/超时/异常容忍）、StuckDetector（检测/重置/自定义比较）。
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock

import pytest

from framework.errors import CancelledError, NormalEnd, PolicyViolation, Retry, TaskError
from framework.retry import retry_on, retry_until
from framework.stuck import StuckDetector


# ══════════════════════════════════════════════════════════════════
# retry_on
# ══════════════════════════════════════════════════════════════════


class TestRetryOn:
    def test_succeeds_first_try(self):
        """首次成功，不重试。"""
        call_count = [0]

        @retry_on(TaskError, max_attempts=3, delay=0.01)
        def fn():
            call_count[0] += 1
            return 42

        assert fn() == 42
        assert call_count[0] == 1

    def test_retries_on_specified_error(self):
        """指定异常 → 重试直到成功。"""
        call_count = [0]

        @retry_on(TaskError, max_attempts=3, delay=0.01)
        def fn():
            call_count[0] += 1
            if call_count[0] < 3:
                raise TaskError("transient")
            return "ok"

        assert fn() == "ok"
        assert call_count[0] == 3

    def test_max_attempts_exceeded_raises(self):
        """超过 max_attempts → 抛出最后一个异常。"""
        @retry_on(TaskError, max_attempts=2, delay=0.01)
        def fn():
            raise TaskError("persistent")

        with pytest.raises(TaskError, match="persistent"):
            fn()

    def test_non_retryable_normal_end(self):
        """NormalEnd 不重试，直接抛出。"""
        call_count = [0]

        @retry_on(Exception, max_attempts=5, delay=0.01)
        def fn():
            call_count[0] += 1
            raise NormalEnd("done")

        with pytest.raises(NormalEnd):
            fn()
        assert call_count[0] == 1  # 没重试

    def test_non_retryable_cancelled(self):
        """CancelledError 不重试。"""
        call_count = [0]

        @retry_on(Exception, max_attempts=5, delay=0.01)
        def fn():
            call_count[0] += 1
            raise CancelledError()

        with pytest.raises(CancelledError):
            fn()
        assert call_count[0] == 1

    def test_non_retryable_policy_violation(self):
        """PolicyViolation 不重试。"""
        call_count = [0]

        @retry_on(Exception, max_attempts=5, delay=0.01)
        def fn():
            call_count[0] += 1
            raise PolicyViolation("no")

        with pytest.raises(PolicyViolation):
            fn()
        assert call_count[0] == 1

    def test_unspecified_error_not_retried(self):
        """指定了特定异常时，其他异常不重试。"""
        call_count = [0]

        @retry_on(TaskError, max_attempts=5, delay=0.01)
        def fn():
            call_count[0] += 1
            raise ValueError("wrong type")

        with pytest.raises(ValueError):
            fn()
        assert call_count[0] == 1

    def test_empty_errors_retries_all(self):
        """不指定 errors → 重试所有 Exception（除 NON_RETRYABLE）。"""
        call_count = [0]

        @retry_on(max_attempts=3, delay=0.01)
        def fn():
            call_count[0] += 1
            if call_count[0] < 3:
                raise RuntimeError("any error")
            return "ok"

        assert fn() == "ok"
        assert call_count[0] == 3

    def test_retry_exception_overrides_max_attempts(self):
        """Retry(attempts=N) 覆盖装饰器的 max_attempts。"""
        call_count = [0]

        @retry_on(Retry, max_attempts=1, delay=0.01)  # 装饰器设1次
        def fn():
            call_count[0] += 1
            if call_count[0] < 3:
                raise Retry("try again", attempts=3)  # 覆盖为3次
            return "ok"

        assert fn() == "ok"
        assert call_count[0] == 3

    def test_backoff_delays_increase(self):
        """退避延迟递增。"""
        delays = []

        def fake_sleep(s):
            delays.append(s)

        call_count = [0]

        @retry_on(TaskError, max_attempts=4, delay=0.1, backoff=2.0, max_delay=1.0)
        def fn():
            call_count[0] += 1
            if call_count[0] < 4:
                raise TaskError("retry")
            return "ok"

        import framework.retry
        orig = time.sleep
        time.sleep = fake_sleep
        try:
            fn()
        finally:
            time.sleep = orig

        assert len(delays) == 3  # 3 次重试
        assert delays[0] == pytest.approx(0.1, abs=0.01)  # delay * 2^0
        assert delays[1] == pytest.approx(0.2, abs=0.01)  # delay * 2^1
        assert delays[2] == pytest.approx(0.4, abs=0.01)  # delay * 2^2

    def test_on_retry_callback(self):
        """on_retry 回调被调用。"""
        callbacks = []

        @retry_on(TaskError, max_attempts=3, delay=0.01, on_retry=lambda a, e: callbacks.append((a, str(e))))
        def fn():
            raise TaskError("fail")

        with pytest.raises(TaskError):
            fn()
        assert len(callbacks) == 2  # 2 次重试（3 次尝试 - 1）
        assert callbacks[0][0] == 1
        assert "fail" in callbacks[0][1]


# ══════════════════════════════════════════════════════════════════
# retry_until
# ══════════════════════════════════════════════════════════════════


class TestRetryUntil:
    def test_succeeds_immediately(self):
        assert retry_until(lambda: True, timeout=1.0) is True

    def test_succeeds_after_retries(self):
        call_count = [0]

        def pred():
            call_count[0] += 1
            return call_count[0] >= 3

        assert retry_until(pred, timeout=5.0, interval=0.01) is True
        assert call_count[0] >= 3

    def test_timeout_returns_false(self):
        assert retry_until(lambda: False, timeout=0.1, interval=0.05) is False

    def test_exception_in_pred_tolerated(self):
        """谓词异常视为 False，继续轮询。"""
        call_count = [0]

        def pred():
            call_count[0] += 1
            if call_count[0] == 1:
                raise RuntimeError("boom")
            return True

        assert retry_until(pred, timeout=2.0, interval=0.01) is True


# ══════════════════════════════════════════════════════════════════
# StuckDetector
# ══════════════════════════════════════════════════════════════════


class TestStuckDetector:
    def test_not_stuck_initially(self):
        sd = StuckDetector(window=5)
        assert sd.is_stuck() is False

    def test_not_stuck_with_few_samples(self):
        sd = StuckDetector(window=3)
        sd.update(10)
        sd.update(10)
        assert sd.is_stuck() is False  # 只有 2 个样本，< window=3

    def test_stuck_when_same_value_repeated(self):
        sd = StuckDetector(window=3)
        sd.update(10)
        sd.update(10)
        sd.update(10)
        assert sd.is_stuck() is True

    def test_not_stuck_when_value_changes(self):
        sd = StuckDetector(window=3)
        sd.update(10)
        sd.update(20)
        sd.update(10)
        assert sd.is_stuck() is False

    def test_stuck_with_custom_equals(self):
        """自定义 equals_fn：位置容差 ±2 内视为相同。"""
        sd = StuckDetector(
            window=3,
            equals_fn=lambda a, b: abs(a - b) < 2.0,
        )
        sd.update(10.0)
        sd.update(10.5)  # 容差内
        sd.update(11.0)  # 容差内
        assert sd.is_stuck() is True

    def test_not_stuck_with_custom_equals_beyond_tolerance(self):
        sd = StuckDetector(
            window=3,
            equals_fn=lambda a, b: abs(a - b) < 2.0,
        )
        sd.update(10.0)
        sd.update(11.5)  # 容差内
        sd.update(13.0)  # 10→13 差3，超出容差
        assert sd.is_stuck() is False

    def test_reset_clears_history(self):
        sd = StuckDetector(window=3)
        sd.update(10)
        sd.update(10)
        sd.reset()
        assert sd.is_stuck() is False

    def test_reset_then_stuck_again(self):
        sd = StuckDetector(window=2)
        sd.update(10)
        sd.update(10)
        assert sd.is_stuck() is True
        sd.reset()
        sd.update(20)
        sd.update(20)
        assert sd.is_stuck() is True
