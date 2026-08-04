"""SceneEstimator —— 持续判定当前场景，写 SharedState.scene（docs/design/02 §1）。

所有读取方（g.wait_main_ui / 守护场景门控 / Observe）问 SharedState.scene，不再各自匹配
——单一事实源。分类规则起步可组合（abilities/game_state 特征）；默认分类器返回 UNKNOWN，
由 set_classifier 注入真实规则（02 §1.5：UNKNOWN 比例高时回流补充）。
"""

from __future__ import annotations

import time
from dataclasses import replace

from framework.daemons.base import Daemon, DaemonCtx, daemon
from framework.scene import classify_scene


@daemon(name="scene_estimator", interval=0.1)
class SceneEstimatorDaemon(Daemon):
    async def step(self, dctx: DaemonCtx) -> None:
        frame = dctx.shared.frame  # 复用 FrameDaemon 的帧（共享帧，02 §3）
        if frame is None:
            return
        state = classify_scene(frame)
        prev = dctx.shared.scene
        # 稳定判定：场景变化时刷新 since，否则保留原 since
        since = prev.since if (prev and prev.scene == state.scene) else time.monotonic()
        dctx.shared.scene = replace(state, since=since)
