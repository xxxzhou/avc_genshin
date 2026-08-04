"""导航器 —— 角色移动 + 位置追踪 + 卡死脱困（Phase B）。

对照 BetterGI PathExecutor.MoveTo（核心移动循环）:
1. 按下 W 持续前进
2. 循环:
   a. 获取当前位置（小地图匹配）
   b. 计算目标朝向角度
   c. 旋转摄像机朝向目标
   d. 检查距离 < 阈值 → 到达
   e. 卡死检测 → 脱困
   f. 超时检测 → 放弃
3. 松开 W

关键参数（对照 BGI）:
- 到达距离阈值: 4（PathExecutor.MoveTo 中 distance < 4）
- 过远阈值: 500（distance > 500 → 可能识别错误）
- 卡死检测: 8 个采样, |delta.X| + |delta.Y| < 3
- 移动超时: 240 秒
- 旋转精度: 5 度
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from abilities.navigation.camera import CameraControl
from abilities.navigation.path_executor import Waypoint
from abilities.navigation.position import PositionGetter
from abilities.navigation.trap_escaper import TrapEscaper

if TYPE_CHECKING:
    from avc._core import KeyCode

    from framework.context import GameContext
    from framework.high_level_api import HighLevelApi


# ── 常量 ──

_ARRIVE_DISTANCE = 4.0  # 到达距离阈值
_TOO_FAR_DISTANCE = 500.0  # 过远距离阈值
_MOVE_TIMEOUT = 240.0  # 移动超时(秒)
_ROTATE_MAX_DIFF = 5.0  # 旋转精度(度)
_POSITION_RECORD_INTERVAL = 1.0  # 位置记录间隔(秒)


class Navigator:
    """角色移动控制器：位置追踪 + 朝向旋转 + 卡死脱困。

    对照 BGI PathExecutor.MoveTo 的移动循环:
    1. 获取位置 → 计算朝向 → 旋转 → 检查到达 → 卡死检测
    """

    def __init__(self, ctx: GameContext, g: HighLevelApi):
        self.ctx = ctx
        self.g = g
        self._position_getter = PositionGetter(ctx)
        self._camera = CameraControl(ctx)
        self._trap_escaper = TrapEscaper(ctx)

    def go_to(
        self,
        waypoint: Waypoint,
        *,
        tolerance: float = _ARRIVE_DISTANCE,
        timeout: float = _MOVE_TIMEOUT,
    ) -> bool:
        """走到指定路径点。返回是否到达。

        对照 BGI PathExecutor.MoveTo:
        1. 获取当前位置
        2. 旋转朝向目标
        3. 按住 W 前进
        4. 循环检测距离/卡死/超时
        5. 到达后松开 W
        """
        from avc._core import KeyCode

        start_time = time.monotonic()
        last_record_time = start_time
        too_far_count = 0

        try:
            # 初始朝向
            position = self._position_getter.get_position()
            if position is not None:
                target_angle = CameraControl.target_orientation(position, (waypoint.x, waypoint.y))
                self._camera.rotate_to(target_angle, max_diff=_ROTATE_MAX_DIFF)

            # 按住 W 开始移动
            try:
                self.ctx.ic.keyDown(KeyCode.w)
            except Exception:
                pass

            while time.monotonic() - start_time < timeout:
                # 获取当前位置
                position = self._position_getter.get_position()
                if position is None:
                    time.sleep(0.1)
                    continue

                # 计算距离
                dist = CameraControl.distance(position, (waypoint.x, waypoint.y))

                # 到达
                if dist < tolerance:
                    return True

                # 过远
                if dist > _TOO_FAR_DISTANCE:
                    too_far_count += 1
                    if too_far_count > 50:
                        return False
                    time.sleep(0.05)
                    continue
                else:
                    too_far_count = 0

                # 位置记录（卡死检测）
                now = time.monotonic()
                if now - last_record_time >= _POSITION_RECORD_INTERVAL:
                    self._trap_escaper.record_position(*position)
                    last_record_time = now

                # 卡死检测
                if self._trap_escaper.is_stuck():
                    # 释放 W
                    try:
                        self.ctx.ic.keyUp(KeyCode.w)
                    except Exception:
                        pass
                    # 脱困
                    self._trap_escaper.escape(waypoint)
                    if self._trap_escaper.should_abort:
                        return False
                    # 重新按住 W
                    try:
                        self.ctx.ic.keyDown(KeyCode.w)
                    except Exception:
                        pass
                    continue

                # 旋转朝向目标
                target_angle = CameraControl.target_orientation(position, (waypoint.x, waypoint.y))
                self._camera.rotate_to_approach(target_angle)

                time.sleep(0.05)

            return False  # 超时

        finally:
            # 释放 W
            try:
                self.ctx.ic.keyUp(KeyCode.w)
            except Exception:
                pass

    def get_position(self) -> tuple[float, float] | None:
        """获取当前玩家位置。"""
        return self._position_getter.get_position()

    def get_orientation(self) -> float | None:
        """获取当前摄像机朝向（度）。"""
        return self._camera.get_orientation()

    def set_prev_position(self, x: float, y: float) -> None:
        """设置上次位置（用于局部匹配优化）。"""
        self._position_getter.set_prev_position(x, y)
