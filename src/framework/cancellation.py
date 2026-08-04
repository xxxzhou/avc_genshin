"""CancellationToken + RunContext（docs/design/01 §4.4）。

统一取消模型：Ctrl+C / 用户停止 / 超时 / 异常 → ``token.cancel()`` →
主脚本在检查点抛 ``CancelledError``；守护 asyncio 任务被框架取消。
Runtime 保证取消后**卸载所有守护 + 释放按键**（01 §8.4）。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable

from framework.errors import CancelledError


class CancellationToken:
    """一次性取消信号。同步检查点 + 异步任务取消回调。"""

    def __init__(self):
        self._cancelled = False
        self.reason: str | None = None
        self._cbs: list[Callable[[], None]] = []

    def cancel(self, reason: str = "") -> None:
        if self._cancelled:
            return
        self._cancelled = True
        self.reason = reason or "cancelled"
        for cb in self._cbs:
            try:
                cb()
            except Exception:
                pass  # 取消回调失败不应阻断取消传播

    @property
    def cancelled(self) -> bool:
        return self._cancelled

    def check(self) -> None:
        """同步检查点：在 g.* / sleep / wait 等处调用；已取消则抛 CancelledError。"""
        if self._cancelled:
            raise CancelledError(self.reason or "cancelled")

    def on_cancel(self, cb: Callable[[], None]) -> None:
        """注册取消回调（如取消某 asyncio task）。已取消则立即触发。"""
        if self._cancelled:
            try:
                cb()
            except Exception:
                pass
            return
        self._cbs.append(cb)


@dataclass
class RunContext:
    """单次运行的状态（01 §4.4）：取消令牌、已挂守护、统计、嵌套深度。

    SharedState（scene/frame）在整次运行共享（同一游戏会话）；RunContext 是"本次执行"
    的局部状态。``ctx.run`` 建子 RunContext（切换 task 名 / 加深度），见 04 §6.2。
    """

    token: CancellationToken
    run_id: str
    task: str = ""
    started_at: float = field(default_factory=time.monotonic)
    mounted: list[str] = field(default_factory=list)  # 本次挂载的守护名（卸载用）
    depth: int = 0  # ctx.run 嵌套深度（默认上限 8，04 §6.2）

    def child(self, task: str) -> "RunContext":
        """ctx.run 进入子任务时派生（共享 token/run_id，独立 mounted/task，depth+1）。"""
        return RunContext(
            token=self.token,
            run_id=self.run_id,
            task=task,
            started_at=time.monotonic(),
            mounted=[],
            depth=self.depth + 1,
        )

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self.started_at
