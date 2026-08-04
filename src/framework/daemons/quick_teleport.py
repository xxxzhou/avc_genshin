"""quick_teleport —— 快速传送确认（对应 BetterGI QuickTeleport，Phase A 新增）。

地图打开时自动确认传送：检测传送按钮 → 点击确认。
scenes={MAP}：仅在大地图界面活跃。

传送流程（由 g.teleport_to 驱动，本守护自动确认）：
1. g.teleport_to 打开地图 → 搜索 → 选点
2. 本守护检测 "GoTeleport" 按钮 → 点击
3. 检测传送确认弹窗 → 点击确认
"""

from __future__ import annotations

import asyncio

from framework.authority import InputChannel
from framework.daemons.base import Daemon, DaemonCtx, daemon
from framework.scene import Scene


@daemon(
    name="quick_teleport",
    owns_keys={InputChannel.MOUSE_CLICK},
    scenes={Scene.MAP},
    interval=0.3,
)
class QuickTeleportDaemon(Daemon):
    async def step(self, dctx: DaemonCtx) -> None:
        from abilities.game_state import has_go_teleport
        from abilities import vision_utils as vu

        frame = dctx.shared.frame
        if frame is None:
            return

        ctx = dctx.ctx

        # 检测 "GoTeleport" 传送按钮
        if has_go_teleport(ctx, frame):
            rect = vu.find_template(ctx, "teleport/GoTeleport.png", frame=frame)
            if rect is not None:
                ctx.click_at(rect.cx, rect.cy)
                dctx.observe.event("action", action="quick_teleport", reason="go_teleport")
                await asyncio.sleep(0.5)
                return

        # 检测传送确认弹窗（黑白确认按钮）
        for btn in ("ui/btn_black_confirm.png", "ui/btn_white_confirm.png"):
            rect = vu.find_template(ctx, btn, threshold=0.75, frame=frame)
            if rect is not None:
                ctx.click_at(rect.cx, rect.cy)
                dctx.observe.event("action", action="quick_teleport", reason="confirm")
                await asyncio.sleep(0.5)
                return
