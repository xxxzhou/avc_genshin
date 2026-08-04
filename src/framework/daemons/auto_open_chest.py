"""auto_open_chest —— 自动开宝箱（对应 BetterGI AutoOpenChest，Phase A 新增）。

检测宝箱/地脉花 F 交互图标 → 按 F 开启。
owns_keys={INTERACT}、scenes={MAIN_UI, DOMAIN}：仅在可交互场景活跃。

对照 BGI AutoOpenChestTask：
- 检测 chest_F_icon（宝箱 F 键图标）→ 按 F
- 检测 flower_F_icon（地脉花 F 键图标）→ 按 F
- 远距离宝箱图标（chest.png）→ 走向宝箱（Phase B 导航能力后实现）
"""

from __future__ import annotations

import asyncio

from framework.authority import InputChannel
from framework.daemons.base import Daemon, DaemonCtx, daemon
from framework.scene import Scene


@daemon(
    name="auto_open_chest",
    owns_keys={InputChannel.INTERACT},
    scenes={Scene.MAIN_UI, Scene.DOMAIN},
    interval=0.3,
)
class AutoOpenChestDaemon(Daemon):
    async def step(self, dctx: DaemonCtx) -> None:
        from avc._core import KeyCode

        from abilities.game_state import has_chest_f_icon, has_flower_f_icon

        frame = dctx.shared.frame
        if frame is None:
            return

        # 检测宝箱/地脉花 F 交互图标
        if has_chest_f_icon(dctx.ctx, frame) or has_flower_f_icon(dctx.ctx, frame):
            dctx.ctx.press(KeyCode.f)
            dctx.observe.event("action", action="auto_open_chest")
            await asyncio.sleep(0.5)  # 开箱动画略长
