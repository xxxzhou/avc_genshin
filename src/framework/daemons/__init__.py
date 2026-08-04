"""守护任务库（docs/design/01 §7、02 §1-3、05 §5）。

导入本包即注册内置守护：frame / scene_estimator / auto_pick / auto_skip / loading_wait /
auto_eat / quick_teleport / auto_open_chest。
任务侧用 ``ctx.mount("auto_pick")`` 挂载；框架保证并发安全（02 §2）。
"""

from framework.daemons.base import Daemon, DaemonCtx, clear_registry, daemon, get_daemon_class, list_daemons

# 导入内置守护模块（@daemon 装饰时自动注册到 base._REGISTRY）
from framework.daemons import frame as _frame  # noqa: F401
from framework.daemons import scene_estimator as _scene_estimator  # noqa: F401
from framework.daemons import auto_pick as _auto_pick  # noqa: F401
from framework.daemons import auto_skip as _auto_skip  # noqa: F401
from framework.daemons import loading_wait as _loading_wait  # noqa: F401
from framework.daemons import auto_eat as _auto_eat  # noqa: F401
from framework.daemons import quick_teleport as _quick_teleport  # noqa: F401
from framework.daemons import auto_open_chest as _auto_open_chest  # noqa: F401

__all__ = [
    "Daemon",
    "DaemonCtx",
    "daemon",
    "get_daemon_class",
    "list_daemons",
    "clear_registry",
]
