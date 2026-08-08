"""单行状态显示（终端覆盖式，5秒无新输出自动清除）。

用法：
    status = StatusLine()
    status.show("定位中... pos=(123, 456)")
    status.show("OK  capture 1920x1080")
    # 5秒无新 show() 调用 → 自动清除该行

    status.clear()   # 手动清除
    status.finish()  # 清除 + 停止自动清除线程
"""

from __future__ import annotations

import sys
import threading
import time


class StatusLine:
    """终端单行覆盖式状态显示。"""

    CLEAR_DELAY = 5.0  # 秒，无新输出后自动清除

    def __init__(self, stream=None) -> None:
        self._stream = stream or sys.stderr
        self._timer: threading.Timer | None = None
        self._last_len = 0
        self._finished = False

    def show(self, text: str) -> None:
        """覆盖当前行显示 text。5秒后自动清除。"""
        if self._finished:
            return
        # 取消之前的自动清除定时器
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None

        # 用回车覆盖当前行
        padded = text.ljust(self._last_len) if self._last_len > len(text) else text
        self._stream.write(f"\r{padded}")
        self._stream.flush()
        self._last_len = len(padded)

        # 启动自动清除定时器
        self._timer = threading.Timer(self.CLEAR_DELAY, self._do_clear)
        self._timer.daemon = True
        self._timer.start()

    def clear(self) -> None:
        """清除当前行。"""
        self._do_clear()

    def finish(self) -> None:
        """停止自动清除并清行。"""
        self._finished = True
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
        self._do_clear()

    def _do_clear(self) -> None:
        if self._last_len > 0:
            self._stream.write(f"\r{' ' * self._last_len}\r")
            self._stream.flush()
            self._last_len = 0
