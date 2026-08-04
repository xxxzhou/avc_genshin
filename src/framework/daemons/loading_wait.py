"""loading_wait —— 加载等待（对应 BetterGI GameLoadingTrigger，docs/design/05 §5）。

加载场景由 SceneEstimator 判定写入 SharedState.scene；主流程用 ``g.wait_loading`` /
``g.wait_main_ui`` 等待场景离开 LOADING。故本守护 owns_keys 空、step 无操作——它的
"阻塞主流程直到加载完成"语义由 SceneEstimator + g.wait_* 组合实现，守护本身只是注册项。
"""

from __future__ import annotations

from framework.daemons.base import Daemon, DaemonCtx, daemon
from framework.scene import Scene


@daemon(name="loading_wait", scenes={Scene.LOADING}, interval=0.5)
class LoadingWaitDaemon(Daemon):
    async def step(self, dctx: DaemonCtx) -> None:
        # 场景门控保证仅在 LOADING 活跃；加载判定与等待由 SceneEstimator + g.wait_* 负责。
        return
