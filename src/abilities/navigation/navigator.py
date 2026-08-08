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

# 到达距离阈值。实机（2026-08-08 diag_moveto）：移动中 get_position 相邻采样噪声 ~±4 单位，
# dist<4 会与噪声同量级、可能振荡不触发；取 ~2× 噪声余量。
_ARRIVE_DISTANCE = 8.0
_TOO_FAR_DISTANCE = 500.0  # 过远距离阈值
_MOVE_TIMEOUT = 240.0  # 移动超时(秒)
_STEER_SLEEP_S = 0.25  # 每轮转向后等待（相机转完 + 角色转向；diag_moveto 用 0.4s 收敛）
_POSITION_RECORD_INTERVAL = 1.0  # 位置记录间隔(秒)
_JUMP_INTERVAL_S = 2.0  # jump 移动模式周期跳间隔


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

        实机定论（2026-08-08 diag_moveto）：**移动中面朝自动同步相机**，故本循环
        「按住 W 持续走 + 每轮 ``rotate_camera_to_target`` 纯 move_by_rel 转相机转向」
        （不轻推 W、不打断行走）。对照 BGI PathExecutor.MoveTo:
        1. 获取当前位置
        2. 计算朝向（真罗盘，见 camera.target_orientation）
        3. 按住 W 前进，边走边转相机对准目标
        4. 循环检测距离/卡死/超时
        5. 到达后松开 W
        """
        from avc._core import KeyCode

        self.ctx.ensure_foreground()  # 移动全程 ic 直调，开头保证前台
        start_time = time.monotonic()
        last_record_time = start_time
        too_far_count = 0
        sprint = False  # run/dash 冲刺（finally 释放用）

        try:
            # 移动模式: fly 先跳起进滑翔; climb 跳过卡死脱困(攀爬中不脱困)
            mode = (getattr(waypoint, "move_mode", "") or "walk").strip() or "walk"
            skip_trap = mode == "climb"
            sprint = mode in ("run", "dash")  # 冲刺
            is_jump = mode == "jump"  # 周期跳
            last_jump = start_time

            # 不做初始 rotate_to（空闲大角度旋转会轻推走路撞地形）；移动循环里用
            # rotate_camera_to_target 边走边转向（移动中面朝自动同步相机，见 camera.py）。
            # fly: 先跳起进入滑翔（实机验证空格键进入滑翔）
            if mode == "fly":
                try:
                    self.ctx.ic.press(KeyCode.space, 50)
                except Exception:
                    pass
            # run/dash: 按住 shift 冲刺
            if sprint:
                try:
                    self.ctx.ic.keyDown(KeyCode.shift)
                except Exception:
                    pass

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

                # 位置记录 + 卡死检测（climb 模式跳过，避免攀爬中误判乱跳）
                if not skip_trap:
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

                # jump 模式: 周期跳（翻越/过坎，简化；实机验证）
                if is_jump and time.monotonic() - last_jump >= _JUMP_INTERVAL_S:
                    try:
                        self.ctx.ic.press(KeyCode.space, 30)
                    except Exception:
                        pass
                    last_jump = time.monotonic()

                # 转向：移动中纯相机旋转（不 nudge、不打断 W；面朝自动同步相机）
                self._camera.rotate_camera_to_target(position, (waypoint.x, waypoint.y))

                time.sleep(_STEER_SLEEP_S)

            return False  # 超时

        finally:
            # 释放 W (+ 冲刺 shift)
            try:
                self.ctx.ic.keyUp(KeyCode.w)
            except Exception:
                pass
            if sprint:
                try:
                    self.ctx.ic.keyUp(KeyCode.shift)
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
