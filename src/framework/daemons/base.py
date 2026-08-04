"""守护任务基类 + @daemon 装饰器 + 注册表（docs/design/01 §4.2、02 §2.3、05 §6）。

守护 = 自治响应器（对应 BetterGI ITaskTrigger，但从"被调度器回调"变成框架驱动的循环）。

**框架驱动循环（关键设计，优于 daemon 自循环）**：守护只实现 ``step(dctx)``（一步
检测+响应）；框架的循环统一保证 **取消 / 场景门控 / 输入权属 / 频率**（02 §2.3）。
这避免每个守护作者重写门控/租约样板（易错），也让安全由框架可证明地保证。

> 与 01 §4.2 / 05 §6 的 ``async def run(self, ctx, token)`` 自循环写法的差异：那里是
> 理想化示意（且示例在 async 内调 sync g.* 会桥接死锁）。本实现把循环收归框架，daemon
> 只写 step；语义（自治响应、自管逻辑）不变。守护在 loop 线程，**用 dctx.ctx（avc）
> + abilities 直接操作，不走 g.* 同步桥**（桥是给工作线程主脚本的）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from framework.authority import InputChannel
from framework.scene import Scene

if TYPE_CHECKING:
    from framework.authority import InputAuthority
    from framework.cancellation import CancellationToken
    from framework.config import Config
    from framework.context import GameContext
    from framework.observe import Observe
    from framework.scene import SceneState
    from framework.shared import SharedState


@dataclass
class DaemonCtx:
    """守护一步执行所需的服务包（由 Runtime 按当前 run 组装）。"""

    ctx: "GameContext"
    shared: "SharedState"
    authority: "InputAuthority"
    observe: "Observe"
    token: "CancellationToken"
    cfg: "Config"

    @property
    def scene(self) -> "SceneState | None":
        return self.shared.scene

    @property
    def detections(self) -> dict:
        return self.shared.detections


class Daemon:
    """守护基类。子类实现 ``step``；元数据由 ``@daemon`` 设置。"""

    name: str = ""
    owns_keys: set[InputChannel] = set()
    scenes: set[Scene] = set()  # 空 = 所有场景活跃
    priority: int = 0
    interval: float = 0.2  # 步进间隔（秒）；守护频率 = 1/interval
    is_daemon: bool = True

    async def step(self, dctx: DaemonCtx) -> None:  # noqa: D401
        """一步检测+响应。框架在循环中按 interval 反复调用，并已保证场景/权属/取消。"""
        raise NotImplementedError


# ── 注册表 ──

_REGISTRY: dict[str, type[Daemon]] = {}


def daemon(
    *,
    name: str,
    owns_keys: set[InputChannel] | None = None,
    scenes: set[Scene] | None = None,
    priority: int = 0,
    interval: float = 0.2,
):
    """类装饰器：设置守护元数据并注册。``owns_keys``/``scenes`` 声明并发权属与场景门控。"""

    def deco(cls: type[Daemon]) -> type[Daemon]:
        cls.name = name
        cls.owns_keys = set(owns_keys or ())
        cls.scenes = set(scenes or ())
        cls.priority = priority
        cls.interval = interval
        cls.is_daemon = True
        if name in _REGISTRY:
            # 命名冲突：后者覆盖（与 TaskRegistry 一致，warn 由加载方记日志）
            pass
        _REGISTRY[name] = cls
        return cls

    return deco


def get_daemon_class(name: str) -> type[Daemon] | None:
    return _REGISTRY.get(name)


def list_daemons() -> list[str]:
    return list(_REGISTRY)


def clear_registry() -> None:
    """测试用：清空注册表。"""
    _REGISTRY.clear()
