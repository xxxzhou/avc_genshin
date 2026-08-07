"""auto_talk —— NPC 交互守护（Phase D 新增）。

对应 BetterGI 的 NPC F-交互场景，但以守护形式实现（后台响应）。

与 ``auto_pick`` 区分：
- ``auto_pick`` 拾取物品（drops/ore/interact 等 YOLO 类 + F 图标全屏搜索）
- ``auto_talk`` 与 NPC 对话（F 图标出现在屏幕中下区域 + OCR NPC 名称）

场景门控：``MAIN_UI``（对话/菜单/战斗中不触发，避免误操作）。
输入权属：``INTERACT``（与 auto_pick 共享，通过 ``priority=5`` 抢占）。
"""

from __future__ import annotations

import asyncio

from framework.authority import InputChannel
from framework.daemons.base import Daemon, DaemonCtx, daemon
from framework.scene import Scene


@daemon(
    name="auto_talk",
    owns_keys={InputChannel.INTERACT},
    scenes={Scene.MAIN_UI},
    interval=0.3,
    priority=5,  # 高于 auto_pick（priority=0），NPC 对话优先于拾取
)
class AutoTalkDaemon(Daemon):
    """NPC 交互守护：检测 F 图标 + OCR NPC 名称 → 按 F。

    v1 简化：只检测 F 图标，不做 OCR NPC 名称验证（BGI 的完整实现需要 OCR
    文本匹配"凯瑟琳"/"阿圆"等关键词；我们通过 ROI 区分：NPC 的 F 图标通常
    出现在屏幕中下区域，物品的 F 图标可能在任何位置）。
    """

    # NPC F 图标 ROI（屏幕中下区域，对照 BGI 实机观察）
    _NPC_F_ROI = (640, 400, 640, 400)  # (x, y, w, h)，1080p

    async def step(self, dctx: DaemonCtx) -> None:
        from avc._core import KeyCode

        from abilities.game_state import has_pick_f

        frame = dctx.shared.frame
        if frame is None:
            return

        ctx = dctx.ctx

        # 检测 F 图标（限定在 NPC 区域 ROI）
        if has_pick_f(ctx, frame):
            # v1：直接按 F（不做 OCR 验证）
            ctx.press(KeyCode.f)
            dctx.observe.event("action", action="auto_talk", reason="npc_f_icon")
            await asyncio.sleep(0.5)  # 对话触发后略停，避免连按
