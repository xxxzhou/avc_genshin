"""auto_skip —— 自动跳过对话/过场（对应 BetterGI AutoSkipTrigger，docs/design/05 §5）。

scenes={DIALOG}：仅在对话中活跃。检测对话选项 → 选橙色选项/按空格跳过/关弹出页。

Phase A 升级：
1. 检测对话选项气泡（icon_option）→ OCR 选项文本 → 选橙色/默认
2. 检测感叹号选项（icon_exclamation）→ 点击
3. 检测关闭页面按钮（page_close）→ 按 ESC
4. 无选项时按空格推进
"""

from __future__ import annotations

import asyncio

from framework.authority import InputChannel
from framework.daemons.base import Daemon, DaemonCtx, daemon
from framework.scene import Scene


@daemon(
    name="auto_skip",
    owns_keys={InputChannel.INTERACT},
    scenes={Scene.DIALOG},
    interval=0.3,
)
class AutoSkipDaemon(Daemon):
    async def step(self, dctx: DaemonCtx) -> None:
        from avc._core import KeyCode

        from abilities.game_state import (
            has_icon_exclamation,
            has_icon_option,
            has_page_close,
        )

        frame = dctx.shared.frame
        if frame is None:
            return

        ctx = dctx.ctx

        # 1. 检测关闭页面按钮 → 按 ESC 关闭弹出页
        if has_page_close(ctx, frame):
            ctx.press(KeyCode.esc)
            dctx.observe.event("action", action="auto_skip", reason="close_popup")
            await asyncio.sleep(0.3)
            return

        # 2. 检测感叹号选项 → 点击（BGI: 直接点击感叹号选项）
        if has_icon_exclamation(ctx, frame):
            # 感叹号选项通常在对话选项区域，按交互键选择
            ctx.press(KeyCode.f)
            dctx.observe.event("action", action="auto_skip", reason="exclamation_option")
            await asyncio.sleep(0.3)
            return

        # 3. 检测对话选项气泡 → 选橙色选项或默认选项
        if has_icon_option(ctx, frame):
            # Phase A 简化：检测到选项时按交互键选择最后一个选项
            # 精细的 OCR + 橙色检测由 abilities/dialog.py 的 talk() 提供
            # 这里守护只做快速跳过：按 W 下移 + F 选择
            ctx.press(KeyCode.f)
            dctx.observe.event("action", action="auto_skip", reason="dialog_option")
            await asyncio.sleep(0.3)
            return

        # 4. 无选项：按空格推进对话
        ctx.press(KeyCode.space)
        await asyncio.sleep(0.1)
