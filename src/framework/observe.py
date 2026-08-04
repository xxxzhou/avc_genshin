"""Observe —— 可观测性地基（docs/design/02 §4、03 §7）。

执行时间线 + 结构化失败分类 + 失败自动存证。这是 **AI 迭代能收敛的眼睛**：
AI 没法坐到屏幕前，系统必须抓全诊断信息让 AI 远程定位（02 §4.1）。

- ``event(kind, **fields)``：追加一条时间线 + 写 JSONL（自动带 ts/scene）。
- ``failure(failure_type, **fields)``：记 failure 事件 + 自动存证（截图 + 期望 + timeline_tail）。
- ``save_evidence(ctx, tag)``：存当前帧到 ``debug/<run_id>/``，返回路径。

任务作者**不手写日志**——g.* 每次调用框架已记录；失败由框架自动存证。
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from framework.errors import FAILURE_TYPES
from framework.logging import JsonlLogger

if TYPE_CHECKING:
    from framework.context import GameContext
    from framework.shared import SharedState


class Observe:
    """运行期观测器：内存时间线（供 failure 上下文）+ JSONL 持久化 + 存证。"""

    TIMELINE_CAP = 500  # 内存保留最近 N 条（failure 的 timeline_tail 从此取）

    def __init__(
        self,
        logger: JsonlLogger,
        shared: "SharedState",
        debug_dir: str | Path = "debug",
    ):
        self.logger = logger
        self.shared = shared
        self.debug_dir = Path(debug_dir) / logger.run_id
        self._timeline: list[dict[str, Any]] = []

    # ── 事件 ──

    def event(self, kind: str, *, level: str = "info", task: str | None = None, **fields: Any) -> None:
        """记一条事件。自动注入 scene（来自 SharedState）+ ts/run_id（在 logger 注入）。"""
        scene = self.shared.scene.scene.value if (self.shared and self.shared.scene) else None
        entry: dict[str, Any] = {"level": level, "event": kind, "scene": scene}
        if task:
            entry["task"] = task
        entry.update(fields)
        self._timeline.append(entry)
        if len(self._timeline) > self.TIMELINE_CAP:
            # 丢弃最旧的（保留尾部，failure 上下文用近期事件）
            del self._timeline[: len(self._timeline) - self.TIMELINE_CAP]
        self.logger.log(entry)

    def failure(self, failure_type: str, *, task: str | None = None, **fields: Any) -> None:
        """记 failure 事件 + 自动存证（截图路径写入 shot 字段）。

        failure_type 取 errors.FAILURE_TYPES 之一（02 §4.3）。附带最近 N 步时间线，
        让 AI 拿到 failure 行 + 上下文即可定向修正，不必整段重写。
        """
        if failure_type not in FAILURE_TYPES:
            failure_type = "TaskError"
        tail = self._timeline[-12:]  # 最近 12 步作为上下文
        self.event(
            "failure",
            level="error",
            task=task,
            failure_type=failure_type,
            timeline_tail=tail,
            **fields,
        )

    # ── 存证 ──

    def save_evidence(self, ctx: "GameContext", tag: str = "evidence") -> str | None:
        """存当前帧到 debug/<run_id>/<ts>_<tag>.png，返回路径（失败时 None）。"""
        try:
            buf = ctx.capture()
            if buf is None:
                return None
            self.debug_dir.mkdir(parents=True, exist_ok=True)
            ts = f"{self.logger.ts():.2f}".replace(".", "_")
            path = self.debug_dir / f"{ts}_{tag}.png"
            buf.save(str(path))
            return str(path)
        except Exception:
            return None  # 存证失败不应淹没原始 failure

    # ── 查询 ──

    def timeline(self) -> list[dict[str, Any]]:
        """整条时间线（供 AI 回流诊断）。"""
        return list(self._timeline)
