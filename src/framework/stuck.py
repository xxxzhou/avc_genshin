"""通用卡死检测（docs/design/02 §2.3 补充）。

泛化 ``TrapEscaper`` 的位置专用卡死检测，适用于任何可观察量：
位置、场景、模板匹配结果、敌人数量等。

原理：连续 ``window`` 次采样同一指标（由 ``equals_fn`` 判等）→ 判卡死。
``equals_fn`` 默认 ``operator.eq``（精确匹配），可传入容差比较（如位置
差 < 3m）。``window`` 建议 5-10（太少误报，太多延迟）。

用法::

    # 位置卡死（容差 3m）
    sd = StuckDetector(window=8, equals_fn=lambda a, b: abs(a[0]-b[0]) + abs(a[1]-b[1]) < 3.0)
    sd.update((x, y))
    if sd.is_stuck():
        ...  # 尝试脱困

    # 敌人数量卡死
    sd = StuckDetector(window=5)
    sd.update(len(detect_blood_bars(frame)))
    if sd.is_stuck():
        ...  # 清场超时
"""

from __future__ import annotations

import operator
from collections import deque
from typing import Any, Callable


class StuckDetector:
    """通用卡死检测：连续 N 次采样某指标无变化 → 卡死。

    Args:
        window: 连续相同值的样本数阈值（达到即判卡死）。
        equals_fn: 判等函数，默认 ``operator.eq``。可传入容差比较。
    """

    def __init__(
        self,
        window: int = 10,
        equals_fn: Callable[[Any, Any], bool] | None = None,
    ):
        self._window = max(window, 2)
        self._equals = equals_fn or operator.eq
        self._history: deque[Any] = deque(maxlen=self._window)

    def update(self, value: Any) -> None:
        """记录一次采样值。"""
        self._history.append(value)

    def is_stuck(self) -> bool:
        """是否卡死：历史样本数 >= window 且全部相同。"""
        if len(self._history) < self._window:
            return False
        # 检查最后 window 个样本是否全部两两相等
        values = list(self._history)
        first = values[0]
        return all(self._equals(first, v) for v in values[1:])

    def reset(self) -> None:
        """清空历史。"""
        self._history.clear()
