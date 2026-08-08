"""auto_eat —— 自动吃药/复活（对应 BetterGI AutoEatTrigger，Phase A 新增）。

检测红血 → 使用便携营养袋；检测复活图标 → 自动复活。
owns_keys={GADGET}、scenes={MAIN_UI, COMBAT, DOMAIN}：仅在可操作场景活跃。

对照 BGI AutoEatTrigger：
- 红血检测：像素 (808, 1010) 为红色 (R≈255, G≈90, B≈90)
- Recovery 图标：便携营养袋不在 CD
- Resurrection 图标：角色死亡待复活
- 吃药键：Z（快捷使用小道具，BGI GIActions.QuickUseGadget）
- 检测间隔：150ms（对齐 BGI CheckInterval）
- 吃药间隔：1s（对齐 BGI EatInterval）
- 优先级：25（对齐 BGI AutoEatTrigger.Priority）
"""

from __future__ import annotations

import asyncio
import time

from framework.authority import InputChannel
from framework.daemons.base import Daemon, DaemonCtx, daemon
from framework.scene import Scene


@daemon(
    name="auto_eat",
    owns_keys={InputChannel.GADGET},
    scenes={Scene.MAIN_UI, Scene.COMBAT, Scene.DOMAIN},
    interval=0.15,
    priority=25,
)
class AutoEatDaemon(Daemon):
    # 吃药间隔（秒），防止频繁吃药（对齐 BGI EatInterval 1000ms）
    eat_interval: float = 1.0
    _last_eat_time: float = 0.0
    # Recovery 缓存（30 秒内不重复检测）
    _recovery_cache_time: float = 0.0
    _recovery_cached: bool = False

    async def step(self, dctx: DaemonCtx) -> None:
        from avc._core import KeyCode

        from abilities.game_state import (
            has_recovery_icon,
            has_resurrection_icon,
            is_low_hp,
        )

        frame = dctx.shared.frame
        if frame is None:
            return

        now = time.monotonic()

        # 写入红血状态供 fighter 读取（单写多读，GIL 下布尔引用读原子）
        low = is_low_hp(dctx.ctx, frame)
        dctx.shared.low_hp = low

        # 检测复活图标（最高优先）
        if has_resurrection_icon(dctx.ctx, frame):
            dctx.ctx.press(KeyCode.z)  # QuickUseGadget
            dctx.observe.event("action", action="auto_eat", reason="resurrection")
            self._last_eat_time = now
            await asyncio.sleep(2.0)
            return

        # 检测红血
        if low:
            # Recovery 缓存：30 秒内不重复检测
            if now - self._recovery_cache_time >= 30:
                self._recovery_cached = has_recovery_icon(dctx.ctx, frame)
                self._recovery_cache_time = now

            if self._recovery_cached:
                # 检查吃药间隔
                if now - self._last_eat_time >= self.eat_interval:
                    dctx.ctx.press(KeyCode.z)  # QuickUseGadget
                    dctx.observe.event("action", action="auto_eat", reason="low_hp")
                    self._last_eat_time = now
                    await asyncio.sleep(0.5)
