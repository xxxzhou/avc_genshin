"""auto_pick —— 自动拾取（对应 BetterGI AutoPickTrigger，docs/design/05 §5）。

读共享检测结果（FrameDaemon 推理），命中可交互物 → 按 F。
owns_keys={INTERACT}、scenes={MAIN_UI, DOMAIN}：对话/菜单/战斗里自动静默（场景门控，
02 §2.3），不会误开宝箱面板/NPC 对话。

Phase A 升级：
- 白名单改为可配置（从 ONNX 模型元数据读真实类名，或用默认列表）
- 增加 F 键模板检测作为 YOLO 的补充
"""

from __future__ import annotations

import asyncio

from framework.authority import InputChannel
from framework.daemons.base import Daemon, DaemonCtx, daemon
from framework.scene import Scene


# 默认可交互物类别（视 bgi_world 模型 labels 而定；可由 Runtime 注入真实类名）
_DEFAULT_WHITELIST = frozenset({
    "drops", "ore", "interact", "item", "collect",
    "artifact", "material", "food", "furnishing",
})


@daemon(
    name="auto_pick",
    owns_keys={InputChannel.INTERACT},
    scenes={Scene.MAIN_UI, Scene.DOMAIN},
    interval=0.15,
)
class AutoPickDaemon(Daemon):
    # 可由 Runtime/配置注入真实类名白名单
    whitelist: frozenset = _DEFAULT_WHITELIST

    async def step(self, dctx: DaemonCtx) -> None:
        from avc._core import KeyCode

        from abilities.game_state import has_pick_f

        # 方法一：YOLO 检测结果（FrameDaemon 推理）
        for cls, items in dctx.shared.detections.items():
            if cls in self.whitelist and items:
                dctx.ctx.press(KeyCode.f)
                dctx.observe.event("action", task=dctx.token and "", action="press", key="f", reason=f"auto_pick:{cls}")
                await asyncio.sleep(0.4)  # 拾取后略停，避免连按
                return

        # 方法二：F 键模板检测（YOLO 未检测到但屏幕有 F 图标）
        frame = dctx.shared.frame
        if frame is not None and has_pick_f(dctx.ctx, frame):
            dctx.ctx.press(KeyCode.f)
            dctx.observe.event("action", action="press", key="f", reason="auto_pick:f_icon")
            await asyncio.sleep(0.4)
