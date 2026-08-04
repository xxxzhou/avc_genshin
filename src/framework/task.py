"""@task 契约 + TaskDescriptor（docs/design/04 §2、§4）。

任务是 avc_genshin 的一等公民。AI 写一个 ``@task`` 装饰的 ``main(ctx, g, **params)``，
落到 ``src/tasks/``，框架即接纳、即暴露、即可按名调用、可组合（04 §0）。

``@task`` 把元数据附在 ``main`` 函数上（``fn.task_descriptor``），TaskRegistry 加载时
提取并补全 source/kind。降级形态（无装饰器的 ``def main(ctx,g)``）由 Registry 推断。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable


@dataclass
class TaskDescriptor:
    """注册表里的任务描述（04 §4）。供执行器调用、供规划器枚举。"""

    name: str  # 全局唯一注册名
    desc: str  # 一句话描述（AI 规划据此判断是否复用）
    main: Callable[..., Any]  # def main(ctx, g, **params)
    daemons: list[str] = field(default_factory=list)  # 运行时自动挂载的守护名
    requires: list[str] = field(default_factory=list)  # 依赖的领域能力（规划校验）
    params: dict = field(default_factory=dict)  # 参数 schema（04 §2.3）
    preconditions: list[str] = field(default_factory=list)
    postconditions: list[str] = field(default_factory=list)
    needs_confirm: bool = False  # 高风险，执行前人类确认（接 Policy）
    tags: list[str] = field(default_factory=list)
    version: str = "0.1"

    # ── 框架内部（Registry 填充）──
    source: Path | None = None
    kind: str = "persistent"  # persistent | ephemeral
    loaded_at: float = field(default_factory=time.time)

    def view(self) -> dict:
        """精简视图（去掉 main/source，供 AI 规划器枚举，04 §4）。"""
        return {
            "name": self.name,
            "desc": self.desc,
            "params": self.params,
            "tags": self.tags,
            "requires": self.requires,
            "daemons": self.daemons,
            "version": self.version,
            "needs_confirm": self.needs_confirm,
        }


def task(
    *,
    name: str,
    desc: str,
    daemons: tuple[str, ...] | list[str] = (),
    requires: tuple[str, ...] | list[str] = (),
    params: dict | None = None,
    preconditions: tuple[str, ...] | list[str] = (),
    postconditions: tuple[str, ...] | list[str] = (),
    needs_confirm: bool = False,
    tags: tuple[str, ...] | list[str] = (),
    version: str = "0.1",
):
    """任务装饰器。设置元数据并附在 ``main`` 上（``fn.task_descriptor``）。

    契约见 04 §2。AI 生成任务一次命中规范（模式固定、字段自描述）。
    """

    def deco(fn: Callable[..., Any]) -> Callable[..., Any]:
        fn.task_descriptor = TaskDescriptor(  # type: ignore[attr-defined]
            name=name,
            desc=desc,
            main=fn,
            daemons=list(daemons),
            requires=list(requires),
            params=dict(params or {}),
            preconditions=list(preconditions),
            postconditions=list(postconditions),
            needs_confirm=needs_confirm,
            tags=list(tags),
            version=version,
        )
        return fn

    return deco
