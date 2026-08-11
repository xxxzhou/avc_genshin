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
_STEER_SLEEP_S = 0.1  # rotate_camera_to_target 内部已含 _SETTLE_S 等待，此处仅补短间隔
_POSITION_RECORD_INTERVAL = 1.0  # 位置记录间隔(秒)
_JUMP_INTERVAL_S = 2.0  # jump 移动模式周期跳间隔
_REORIENT_THRESHOLD = 15.0  # 角度差超过此值时停步闭环转向（避免移动中转向不足导致偏航/卡地形）


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
        # 注入 position_getter：脱困前后采样判定 escaped（nav.stuck 事件）
        self._trap_escaper = TrapEscaper(ctx, position_getter=self._position_getter)

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
        ob = self.ctx.observe
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

            # 初始转向：用 rotate_to 闭环收敛到目标朝向（避免大角度差时边走边转向
            # 导致角色在错误方向越走越远）。max_diff=5° 足够小，后续移动循环里
            # rotate_camera_to_target 会继续微调。
            start_pos = self._position_getter.get_position()
            if start_pos is not None:
                target_angle = CameraControl.target_orientation(start_pos, (waypoint.x, waypoint.y))
                current_angle = self._camera.get_orientation()
                if current_angle is not None:
                    angle_diff = self._camera._angle_diff(current_angle, target_angle)
                    if abs(angle_diff) > 30:  # 大角度差才预转向
                        self._camera.rotate_to(target_angle, max_diff=5.0)

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

            target_xy = (round(waypoint.x), round(waypoint.y))
            while time.monotonic() - start_time < timeout:
                # 获取当前位置
                position = self._position_getter.get_position()
                if position is None:
                    # 定位缺失（SIFT 未中）：节流观察，不 abort（可能瞬时）；持续缺失→末轮 timeout 兜底
                    ob.event("nav.step", ability="nav", phase="observe",
                             target=target_xy, reason="pos_fail", mode=mode,
                             throttle_key="nav.step")
                    time.sleep(0.1)
                    continue

                # 计算距离
                dist = CameraControl.distance(position, (waypoint.x, waypoint.y))

                # 到达
                if dist < tolerance:
                    ob.event("nav.step", ability="nav", phase="decide", ok=True, reason="arrived",
                             target=target_xy, pos=(round(position[0]), round(position[1])),
                             dist=round(dist), mode=mode)
                    return True

                # 朝向（提前算，供观察事件 + 转向决策复用，避免双读 compass）
                target_angle = CameraControl.target_orientation(position, (waypoint.x, waypoint.y))
                current_angle = self._camera.get_orientation()
                heading_diff = (
                    round(self._camera._angle_diff(current_angle, target_angle), 1)
                    if current_angle is not None else None
                )

                # 过远（定位可能错乱 / 视口漂移）
                if dist > _TOO_FAR_DISTANCE:
                    too_far_count += 1
                    ob.event("nav.step", ability="nav", phase="observe",
                             target=target_xy, reason="too_far", dist=round(dist),
                             too_far_count=too_far_count, mode=mode,
                             throttle_key="nav.step")
                    if too_far_count > 50:
                        ob.event("nav.step", ability="nav", phase="decide", ok=False,
                                 reason="abort_too_far", dist=round(dist),
                                 too_far_count=too_far_count)
                        return False
                    time.sleep(0.05)
                    continue
                else:
                    too_far_count = 0

                # 移动观察（节流 ~1/s）：让 AI 看「角色位置随时间逼近/偏离目标」+ 朝向偏差
                ob.event("nav.step", ability="nav", phase="observe",
                         target=target_xy, pos=(round(position[0]), round(position[1])),
                         dist=round(dist), heading_diff=heading_diff, mode=mode,
                         throttle_key="nav.step")

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
                        # 脱困（trap_escaper 内发 nav.stuck）
                        self._trap_escaper.escape(waypoint)
                        if self._trap_escaper.should_abort:
                            ob.event("nav.step", ability="nav", phase="decide", ok=False,
                                     reason="abort_stuck", pos=(round(position[0]), round(position[1])))
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

                # 转向：复用上方算的 current_angle/target_angle/heading_diff
                if current_angle is not None:
                    if abs(heading_diff) > _REORIENT_THRESHOLD:
                        # 角度差过大：松开 W → 闭环转向 → 重新按住 W
                        # 避免移动中 rotate_camera_to_target 单次转向不足导致偏航/卡地形
                        try:
                            self.ctx.ic.keyUp(KeyCode.w)
                        except Exception:
                            pass
                        # 闭环转向：rotate_to 内部每轮 move_by_rel + settle + nudge_sync
                        # nudge_sync 会推动角色前进，位置漂移后 target_angle 变化，
                        # 故 rotate_to 完成后须重新读位置算 target_angle，
                        # 若仍 > REORIENT 则继续转向（而非回主循环触发又一轮停步-转向）
                        for _reorient in range(10):  # 最多 10 轮闭环
                            self._camera.rotate_to(target_angle, max_diff=5.0)
                            # 重新读位置算 target_angle
                            new_pos = self._position_getter.get_position()
                            if new_pos is not None:
                                position = new_pos
                                target_angle = CameraControl.target_orientation(position, (waypoint.x, waypoint.y))
                            new_angle = self._camera.get_orientation()
                            if new_angle is None:
                                break
                            if abs(self._camera._angle_diff(new_angle, target_angle)) <= _REORIENT_THRESHOLD:
                                break
                        try:
                            self.ctx.ic.keyDown(KeyCode.w)
                        except Exception:
                            pass
                    else:
                        # 小角度差：移动中纯相机旋转（不 nudge、不打断 W）
                        self._camera.rotate_camera_to_target(position, (waypoint.x, waypoint.y))

                time.sleep(_STEER_SLEEP_S)

            ob.event("nav.step", ability="nav", phase="decide", ok=False, reason="timeout",
                     target=target_xy, mode=mode)
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
