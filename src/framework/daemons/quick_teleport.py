"""quick_teleport —— 快速传送确认（对应 BetterGI QuickTeleport，Phase A 新增）。

地图打开时自动确认传送：OCR 检测传送面板 → 点击 '传送' 按钮确认。
scenes={MAP}：仅在大地图界面活跃。

传送流程（由 g.teleport_to 驱动，本守护自动确认）：
1. g.teleport_to 打开地图 → 搜索 → 选点
2. 本守护 OCR 检测传送面板（'传送' 按钮）→ 点击确认（找不到文字则按 F）
3. 检测传送确认弹窗 → 点击确认

⚠ 不处理标记面板（OCR '追踪'/'总标记' 等）：标记面板的 '确认' 按钮带 F 快捷键，
  按 F 会确认标记编辑而非传送；关闭标记面板 + 换点重试由 tp 主流程负责，守护不介入。
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
        from avc._core import KeyCode

        from abilities import vision_utils as vu
        from abilities.tp_panel import (
            TeleportPanelKind,
            detect_tp_panel,
            find_teleport_button,
        )

        frame = dctx.shared.frame
        if frame is None:
            return

        ctx = dctx.ctx

        # OCR 检测传送面板（'传送' 按钮）→ 点击确认
        kind = detect_tp_panel(ctx, frame)
        if kind is TeleportPanelKind.TELEPORT:
            btn = find_teleport_button(ctx, frame)
            if btn is not None:
                ctx.click_at(btn.cx, btn.cy)
                dctx.observe.event("action", action="quick_teleport", reason="teleport_panel")
                await asyncio.sleep(0.5)
                return
            # OCR 未定位到按钮文字，按 F 兜底（BGI HandleTeleportPanel 按 F）
            ctx.press(KeyCode.f)
            dctx.observe.event("action", action="quick_teleport", reason="teleport_f")
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
