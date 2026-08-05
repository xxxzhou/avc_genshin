"""摄像机朝向检测与旋转控制（Phase B）。

对照 BetterGI:
- CameraOrientationFromGia.cs: 极坐标展开 + Scharr 边缘检测 + 峰值卷积
- CameraRotateTask.cs: 旋转摄像机到目标角度
- CameraOrientationCalculator.cs: V2 算法（色调直方图，更复杂，暂不用）

1080p 小地图区域: (62, 19, 212, 212)（对照 BGI MapAssets.MimiMapRect1080P）
小地图中心: (168, 125) 在 1080p 下

朝向算法（CameraOrientationFromGia.ComputeMiniMap）：
1. 灰度 + GaussianBlur(3,3)
2. WarpPolar(360,360) 极坐标展开
3. ROI: Rect(10, 0, 70, 360) → Rotate90CCW
4. Scharr(dx=1, dy=0) 边缘检测
5. FindPeaks → left[]/right[] 数组
6. 优化: left2 = max(left-right, 0), right2 = max(right-left, 0)
7. 卷积: left2 × Shift(right2, -90±2) 加权求和
8. 二次卷积: sum ± 2 平滑
9. angle = maxIndex + 45 (mod 360)

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

import cv2
import numpy as np

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


# ── 朝向算法（对照 BGI CameraOrientationFromGia）──


def _find_peaks(data: np.ndarray) -> list[int]:
    """查找数组中的峰值索引（对照 BGI FindPeaks）。

    峰值定义: data[i] > data[i-1] 且 data[i] > data[i+1]
    """
    peaks: list[int] = []
    for i in range(1, len(data) - 1):
        if data[i] > data[i - 1] and data[i] > data[i + 1]:
            peaks.append(i)
    return peaks


def _shift(array: np.ndarray, k: int) -> np.ndarray:
    """循环移位数组（对照 BGI Shift）。

    k > 0: 右移; k < 0: 左移。
    """
    n = len(array)
    if n == 0:
        return array
    k = k % n
    if k == 0:
        return array.copy()
    return np.concatenate([array[-k:], array[:-k]])


def compute_orientation(minimap_gray: np.ndarray) -> float:
    """从小地图灰度图计算摄像机朝向角度（0-360 度）。

    对照 BGI CameraOrientationFromGia.ComputeMiniMap:
    1. GaussianBlur(3,3)
    2. WarpPolar(360,360) 极坐标展开
    3. ROI(10,0,70,360) → Rotate90CCW
    4. Scharr(dx=1, dy=0)
    5. FindPeaks → left[]/right[]
    6. 优化 + 卷积
    7. angle = maxIndex + 45 (mod 360)

    Args:
        minimap_gray: 小地图灰度图 (H, W)，uint8

    Returns:
        角度 (0-360)
    """
    mat = minimap_gray.copy()

    # 1. 高斯模糊
    mat = cv2.GaussianBlur(mat, (3, 3), 0)

    # 2. 极坐标展开
    center = (mat.shape[1] / 2.0, mat.shape[0] / 2.0)
    polar_mat = cv2.warpPolar(
        mat, (360, 360), center, 360.0,
        cv2.INTER_LINEAR | cv2.WARP_POLAR_LINEAR,
    )

    # 3. ROI + 旋转
    # OpenCV WarpPolar 输出: 行=角度(0-359), 列=半径(0-359)
    # BGI: Rect(10, 0, 70, polarMat.Height) → 取半径 10-80, 全角度
    # 在 numpy 中: polar_mat[0:360, 10:80] (行=角度, 列=半径)
    polar_roi = polar_mat[0:360, 10:80].copy()
    # Rotate90Counterclockwise: 转置 + 垂直翻转
    polar_roi = cv2.rotate(polar_roi, cv2.ROTATE_90_COUNTERCLOCKWISE)

    # 4. Scharr 边缘检测 (dx=1, dy=0)
    scharr_result = cv2.Scharr(polar_roi, cv2.CV_32F, 1, 0)

    # 5. FindPeaks
    scharr_array = scharr_result.flatten().astype(np.float32)
    left = np.zeros(360, dtype=np.int32)
    right = np.zeros(360, dtype=np.int32)

    left_peaks = _find_peaks(scharr_array)
    for idx in left_peaks:
        left[idx % 360] += 1

    # 反转数组找负峰值
    reversed_array = -scharr_array
    right_peaks = _find_peaks(reversed_array)
    for idx in right_peaks:
        right[idx % 360] += 1

    # 6. 优化: left2 = max(left-right, 0), right2 = max(right-left, 0)
    left2 = np.maximum(left - right, 0)
    right2 = np.maximum(right - left, 0)

    # 7. 卷积: left2 × Shift(right2, -90±2) 加权
    total = np.zeros(360, dtype=np.int32)
    for i in range(-2, 3):
        weight = (3 - abs(i)) / 3.0
        shifted = _shift(right2, -90 + i)
        total += (left2 * shifted * weight).astype(np.int32)

    # 8. 二次卷积: sum ± 2 平滑
    result = np.zeros(360, dtype=np.int32)
    for i in range(-2, 3):
        weight = (3 - abs(i)) / 3.0
        shifted = _shift(total, i)
        result += (shifted * weight).astype(np.int32)

    # 9. 计算角度
    max_index = int(np.argmax(result))
    angle = max_index + 45
    if angle > 360:
        angle -= 360

    return float(angle)


class CameraControl:
    """摄像机朝向检测与旋转控制。

    朝向检测使用 CameraOrientationFromGia 算法（纯 OpenCV，无需外部数据）。
    旋转控制使用鼠标水平移动。
    """

    def __init__(self, ctx: GameContext):
        self.ctx = ctx
        self._dpi: float = ctx._dpi_scale  # Windows DPI 缩放比，从 GameContext 读取
        self._od = None  # avc IOrientationDetector（懒加载）

    def get_orientation(self, frame: IImageBuffer | None = None) -> float | None:
        """从截图获取摄像机朝向角度。

        优先走 avc ``IOrientationDetector``（BGI CameraOrientationFromGia 忠实移植，
        小地图缓冲直传）；无 avc 时回退纯 Python ``compute_orientation``（同算法）。
        输出为 BGI 原始角度约定（0=右/东，顺时针，实际取值 [45,360]），
        与 ``target_orientation``（0=北，逆时针）的换算待实机标定。
        """
        if frame is None:
            frame = self.ctx.capture()
        if frame is None:
            return None
        od = self._get_od()
        if od is not None:
            minimap = self._extract_minimap_buffer(frame)
            if minimap is None:
                return None
            ang = od.compute(minimap)
            return ang if ang >= 0 else None
        # 回退: 无 avc → 纯 Python（同款 BGI 峰卷积）
        gray = self._extract_minimap_gray(frame)
        if gray is None:
            return None
        return compute_orientation(gray)

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

    def _extract_minimap_gray(self, frame: IImageBuffer) -> np.ndarray | None:
        """回退用：裁剪小地图 → 灰度 numpy（喂纯 Python compute_orientation）。"""
        cropped = self._extract_minimap_buffer(frame)
        if cropped is None:
            return None
        try:
            raw = cropped.to_bytes()
            if raw is None:
                return None
            arr = np.frombuffer(raw, dtype=np.uint8).reshape(
                (MINIMAP_H, MINIMAP_W, 4),
            )
            # BGRA → 灰度
            return cv2.cvtColor(arr, cv2.COLOR_BGRA2GRAY)
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
