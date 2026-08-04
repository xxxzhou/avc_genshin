"""avc_genshin 框架核心包（L2）。

任务侧一行 import 拿到写任务的全部公共 API：

    from framework import task, KeyCode  # 写 @task + 按键常量

``task`` / ``Runtime`` / 地基模块**无 avc 依赖**，``import framework`` 轻量、不启动 avc
运行时。avc 常量（``KeyCode``/``MouseButton``/...）经 ``__getattr__`` **真懒加载**——
首次访问才 ``import avc._core``（任务运行时本就在 avc 之上，此时启动合理）。
avc 真正需要的能力（截图/输入/识别）在 ``GameContext`` 实例化处懒导入。
"""

from __future__ import annotations

__version__ = "0.1.0"

# ── 任务契约（无 avc 依赖）──
from .task import TaskDescriptor, task

# ── 地基模块（无 avc 依赖，可独立 import / 单测）──
from . import config, errors, utils
from .resources import res

# ── 引擎与 API（无 avc 依赖；avc 在 GameContext 实例化时懒导入）──
from .context import GameContext
from .high_level_api import HighLevelApi
from .registry import TaskRegistry
from .runtime import Runtime

__all__ = [
    "task",
    "TaskDescriptor",
    "GameContext",
    "HighLevelApi",
    "Runtime",
    "TaskRegistry",
    "res",
    "config",
    "errors",
    "utils",
    "KeyCode",
    "MouseButton",
    "TemplateMatchMethod",
    "MatchOrderBy",
]

# avc 常量再导出（03 §10）的延迟键：首次访问时才 import avc._core（启动 avc 运行时）。
_AVC_LAZY = ("KeyCode", "MouseButton", "TemplateMatchMethod", "MatchOrderBy")


def __getattr__(name):
    if name in _AVC_LAZY:
        from avc._core import (  # type: ignore
            KeyCode,
            MatchOrderBy,
            MouseButton,
            TemplateMatchMethod,
        )
        globals().update(
            KeyCode=KeyCode,
            MouseButton=MouseButton,
            TemplateMatchMethod=TemplateMatchMethod,
            MatchOrderBy=MatchOrderBy,
        )
        return globals()[name]
    raise AttributeError(f"module 'framework' has no attribute {name!r}")
