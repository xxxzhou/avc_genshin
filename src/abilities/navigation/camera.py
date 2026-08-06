"""摄像机朝向检测与旋转控制（Phase B）。

对照 BetterGI:
- CameraOrientationFromGia.cs: 极坐标展开 + Scharr 边缘检测 + 峰值卷积
- CameraRotateTask.cs: 旋转摄像机到目标角度

朝向算法已由 avc IOrientationDetector 忠实移植（C++ 实现，更快），
本模块仅做裁剪+调用+角度换算。旧纯 Python cv2 实现已删除。

旋转算法（BGI CameraRotateTask.RotateToApproach）：
1. 获取当前摄像机朝向角度
2. 计算与目标角度的差值 diff
3. 根据 diff 大小选择控制比例 controlRatio:
   |diff| > 90° → 4x, |diff| > 30° → 3x, |diff| > 5° → 2x, else 1x
4. MoveMouseBy(-controlRatio * diff * dpi, 0) 旋转摄像机
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from avc.image import IImageBuffer

    from framework.context import GameContext


# ── 1080p 常量 ──

# 小地图区域（对照 BGI MapAssets.MimiMapRect1080P = Rect(62, 19, 212, 212)）
MINIMAP_X = 62
MINIMAP_Y = 19
MINIMAP_W = 212
MINIMAP_H = 212
MINIMAP_SIZE = 212  # 正方形边长

# 小地图中心（1080p）
MINIMAP_CENTER_X = MINIMAP_X + MINIMAP_W // 2  # 168
MINIMAP_CENTER_Y = MINIMAP_Y + MINIMAP_H // 2  # 125

# 旋转控制参数（对照 BGI CameraRotateTask）
_ROTATE_CONTROL_RATIOS = [
    (90, 4.0),
    (30, 3.0),
    (5, 2.0),
    (0, 1.0),
]


class CameraControl:
    """摄像机朝向检测与旋转控制。

    朝向检测走 avc IOrientationDetector（BGI CameraOrientationFromGia 忠实 C++ 移植）。
    旋转控制使用鼠标水平移动。
    """

    def __init__(self, ctx: GameContext):
        self.ctx = ctx
        self._dpi: float = ctx._dpi_scale  # Windows DPI 缩放比，从 GameContext 读取
        self._od = None  # avc IOrientationDetector（懒加载）

    def get_orientation(self, frame: IImageBuffer | None = None) -> float | None:
        """从截图获取摄像机朝向角度。

        走 avc IOrientationDetector（BGI CameraOrientationFromGia 忠实 C++ 移植）。
        输出为 BGI 原始角度约定（0=右/东，顺时针，实际取值 [45,360]），
        与 ``target_orientation``（0=北，逆时针）的换算待实机标定。
        """
        if frame is None:
            frame = self.ctx.capture()
        if frame is None:
            return None
        od = self._get_od()
        if od is None:
            return None
        minimap = self._extract_minimap_buffer(frame)
        if minimap is None:
            return None
        ang = od.compute(minimap)
        return ang if ang >= 0 else None

    def _get_od(self):
        """懒建 avc IOrientationDetector（无 avc/插件未装返回 None）。"""
        if self._od is not None:
            return self._od
        try:
            from avc import Vision

            od = Vision.createOrientationDetector()
            self._od = od
            return od
        except Exception:
            return None

    def _extract_minimap_buffer(self, frame: IImageBuffer) -> IImageBuffer | None:
        """裁剪小地图区域，返回 avc IImageBuffer（供 avc IOrientationDetector 直传）。"""
        try:
            from avc import Image

            return Image.crop(frame, MINIMAP_X, MINIMAP_Y, MINIMAP_W, MINIMAP_H)
        except Exception:
            return None

    def rotate_to(
        self,
        target_angle: float,
        max_diff: float = 5.0,
        max_attempts: int = 50,
    ) -> bool:
        """旋转摄像机到目标角度。返回是否在 max_diff 范围内。

        对照 BGI CameraRotateTask.WaitUntilRotatedTo:
        循环: 获取当前角度 → 计算 diff → MoveMouseBy 旋转
        """
        for _ in range(max_attempts):
            current = self.get_orientation()
            if current is None:
                return False

            diff = self._angle_diff(current, target_angle)
            if abs(diff) < max_diff:
                return True

            # 计算鼠标移动量
            control_ratio = self._control_ratio(diff)
            move_x = int(round(-control_ratio * diff * self._dpi))
            self.ctx.ic.moveMouseBy(move_x, 0)

            import time

            time.sleep(0.05)

        return False

    def rotate_to_approach(self, target_angle: float) -> float:
        """单次旋转逼近。返回当前角度差。

        对照 BGI CameraRotateTask.RotateToApproach:
        1. 获取当前角度
        2. 计算 diff
        3. MoveMouseBy(-controlRatio * diff * dpi, 0)
        """
        current = self.get_orientation()
        if current is None:
            return 0.0

        diff = self._angle_diff(current, target_angle)
        if abs(diff) < 0.01:
            return 0.0

        control_ratio = self._control_ratio(diff)
        move_x = int(round(-control_ratio * diff * self._dpi))
        self.ctx.ic.moveMouseBy(move_x, 0)
        return diff

    @staticmethod
    def target_orientation(
        from_pos: tuple[float, float],
        to_pos: tuple[float, float],
    ) -> int:
        """计算从 from_pos 到 to_pos 的朝向角度（0-360 度）。

        对照 BGI Navigation.GetTargetOrientation:
        用 atan2 计算向量角度，转换到 0-360 度范围。
        """
        dx = to_pos[0] - from_pos[0]
        dy = to_pos[1] - from_pos[1]

        if dx == 0 and dy == 0:
            return 0

        # atan2(dy, dx) → 角度（弧度）
        angle = math.atan2(dy, dx)
        # 转换到 0-360 度
        degrees = math.degrees(angle)
        if degrees < 0:
            degrees += 360

        return int(degrees)

    @staticmethod
    def distance(
        p1: tuple[float, float],
        p2: tuple[float, float],
    ) -> float:
        """欧氏距离。"""
        return math.hypot(p2[0] - p1[0], p2[1] - p1[1])

    @staticmethod
    def _angle_diff(current: float, target: float) -> float:
        """计算两个角度的最短差值（-180 ~ 180 度）。"""
        diff = (current - target + 180) % 360 - 180
        if diff < -180:
            diff += 360
        return diff

    @staticmethod
    def _control_ratio(diff: float) -> float:
        """根据角度差选择控制比例（对照 BGI CameraRotateTask）。"""
        abs_diff = abs(diff)
        for threshold, ratio in _ROTATE_CONTROL_RATIOS:
            if abs_diff > threshold:
                return ratio
        return 1.0
