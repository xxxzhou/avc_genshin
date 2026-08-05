"""简单通知（对齐 BGI ``Notify.Event``）：任务开始/结束/出错时的日志 + 可插桌面推送。

默认打印到控制台；可在 ``main`` 里 ``notify.register(handler)`` 挂桌面推送
（如 win10 toast / 系统托盘）。纯框架、零游戏依赖。

用法：
    from framework.notify import notify
    notify("task_start", task="auto_boss")
    # 自定义处理器
    notify.register(lambda event, fields: print("PUSH", event, fields))
"""

from __future__ import annotations

from typing import Any, Callable

# 处理器签名：handler(event: str, fields: dict)
Handler = Callable[[str, dict], None]


class _Notify:
    """轻量发布订阅：``emit(event, **fields)`` 广播给所有 handler。"""

    def __init__(self) -> None:
        self._handlers: list[Handler] = []

    def register(self, handler: Handler) -> None:
        """注册处理器（可多次，都收到）。"""
        self._handlers.append(handler)

    def emit(self, event: str, **fields: Any) -> None:
        for h in self._handlers:
            try:
                h(event, fields)
            except Exception:
                pass  # 通知失败不影响任务


_notify = _Notify()


def notify(event: str, **fields: Any) -> None:
    """模块级入口：``notify("task_start", task="auto_boss")``。"""
    _notify.emit(event, **fields)


def register(handler: Handler) -> None:
    """注册通知处理器（桌面推送等）。"""
    _notify.register(handler)


def _console(event: str, fields: dict) -> None:
    parts = [f"[notify] {event}"]
    parts += [f"{k}={v}" for k, v in fields.items()]
    print(" ".join(parts))


register(_console)  # 默认：打控制台
