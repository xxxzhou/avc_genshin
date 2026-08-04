"""结构化日志（JSONL，docs/design/03 §7、02 §4）。

每次运行开 ``logs/<run_id>.jsonl``，一行一个 JSON 事件（AI 可解析）。
``Observe.event`` 同时追加内存时间线 + 写本日志。任务作者**不手写日志**——
g.* 的每次调用、Runtime 的 task_start/task_return/failure 由框架自动记录。

字段约定见 03 §7.3：ts(相对秒)/run_id/task/scene/level/event/result/failure_type/shot/...。
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any


def new_run_id() -> str:
    """``r_YYYYMMDD_HHMMSS``（同一次运行所有事件共享）。"""
    return "r_" + time.strftime("%Y%m%d_%H%M%S", time.localtime())


class JsonlLogger:
    """JSONL 追加式日志。线程安全（同一 run 仅 loop 线程写，仍加锁以防 g.* 桥接写）。"""

    def __init__(self, run_id: str, logs_dir: str | Path = "logs", start: float | None = None):
        self.run_id = run_id
        self.start = start if start is not None else time.monotonic()
        self.path = Path(logs_dir) / f"{run_id}.jsonl"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fp = self.path.open("a", encoding="utf-8")
        self._closed = False

    def ts(self) -> float:
        """相对运行开始的秒数。"""
        return round(time.monotonic() - self.start, 3)

    def log(self, event: dict[str, Any]) -> None:
        """写一条事件。自动注入 ts/run_id（调用方可覆盖）。"""
        if self._closed:
            return
        entry = {"ts": self.ts(), "run_id": self.run_id, **event}
        self._fp.write(json.dumps(entry, ensure_ascii=False, default=_json_default) + "\n")
        self._fp.flush()

    def close(self) -> None:
        if not self._closed:
            self._fp.close()
            self._closed = True


def _json_default(o: Any) -> Any:
    """JSON 序列化兜底：路径/枚举/dataclass 转 JSON 友好形态。"""
    if isinstance(o, os.PathLike):
        return str(o)
    if isinstance(o, Enum):
        return o.value
    if hasattr(o, "__dict__"):
        # dataclass / 简单对象：取 __dict__
        d = {k: v for k, v in vars(o).items() if not k.startswith("_")}
        return d
    return repr(o)
