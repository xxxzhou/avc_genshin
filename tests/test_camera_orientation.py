"""摄像机朝向/旋转数学正确性测试（纯数学，无 avc/游戏/mock 依赖）。

2026-08-08 实机定论更新：avc getOrientation（BGI FromGia 移植）不可用（原神小地图固定
北朝上，把微差放大成假角度），改走小地图箭头传感器（arrow.py，**真罗盘 0=北/90=东**）。
因此 target_orientation 从 BGI 帧（atan2(Δ西,Δ北)，西=90°）改为**真罗盘帧**
（atan2(-Δ西,Δ北)，东=90°），与 get_orientation（箭头）同系、_angle_diff 直接相减。

本文件只测纯数学（target_orientation / _angle_diff / _control_ratio）——这些是静态方法，
不需实例化 CameraControl，不需 avc。get_orientation 的数值正确性（箭头端）归离线套件/
实机标定，不在本文件。
"""

from __future__ import annotations

import math

from abilities.navigation.camera import CameraControl


# ── acos 角参考（对照基线）──


def _bgi_get_target_orientation(dx: float, dy: float) -> int:
    """BGI Navigation.GetTargetOrientation 的 acos 数学式（作纯角参考，非 BGI 轴序）。

    源码：angle = acos(dx/len)；dy<0 → 2π-angle；int(angle*180/π)。
    本测试把它当「atan2(dy,dx) 的角度」参考；调用方按目标帧传 (Δ北, Δ东)。
    """
    length = math.hypot(dx, dy)
    if length == 0:
        return 0
    angle = math.acos(dx / length)
    if dy < 0:
        angle = 2 * math.pi - angle
    return int(angle * (180.0 / math.pi))


# ── target_orientation ≡ 真罗盘（0=北，90=东）──


class TestTargetOrientation:
    """target_orientation(from, to) 输出真罗盘角（0=北/90=东/180=南/270=西）。

    坐标序 (北,西)：+y=+西→270，-y=+东→90（与 get_orientation 箭头同帧）。
    """

    def test_cardinal_directions(self):
        """四正方向：+北→0 / +东→90 / +南→180 / +西→270。"""
        assert CameraControl.target_orientation((0, 0), (1, 0)) == 0      # +北
        assert CameraControl.target_orientation((0, 0), (0, -1)) == 90    # +东
        assert CameraControl.target_orientation((0, 0), (-1, 0)) == 180   # +南
        assert CameraControl.target_orientation((0, 0), (0, 1)) == 270    # +西

    def test_diagonal_directions(self):
        assert CameraControl.target_orientation((0, 0), (1, -1)) == 45    # 东北
        assert CameraControl.target_orientation((0, 0), (1, 1)) == 315    # 西北
        assert CameraControl.target_orientation((0, 0), (-1, -1)) == 135  # 西南
        assert CameraControl.target_orientation((0, 0), (-1, 1)) == 225   # 东南

    def test_matches_compass_reference(self):
        """罗盘角 = atan2(Δ东, Δ北) = atan2(-Δ西, Δ北)；acos 数学式作参考（第二轴取负）。"""
        vectors = [
            (3, 4), (-3, 4), (3, -4), (-3, -4),
            (5, 0), (0, 5), (-5, 0), (0, -5),
            (1, 100), (-100, 1), (7, -7), (-7, -7),
            (8, 15), (-8, 15), (8, -15), (-8, -15),
        ]
        for dx, dy in vectors:  # dx=Δ北, dy=Δ西
            ours = CameraControl.target_orientation((0, 0), (dx, dy))
            ref = _bgi_get_target_orientation(dx, -dy)  # 换轴：(Δ北, Δ东=-Δ西)
            assert ours == ref, f"({dx},{dy}): ours={ours} ref={ref}"

    def test_uses_delta_from_nonzero_origin(self):
        """非原点起点按差向量算。"""
        assert CameraControl.target_orientation((10, 10), (11, 10)) == 0    # 北
        assert CameraControl.target_orientation((10, 10), (10, 11)) == 270  # 西

    def test_same_point_is_zero(self):
        """同点 → 0（不除零）。"""
        assert CameraControl.target_orientation((5, 5), (5, 5)) == 0


# ── _angle_diff：最短角差 ∈ [-180,180]，与 BGI RotateToApproach 同公式 ──


class TestAngleDiff:
    """_angle_diff(current, target) = (current-target+180)%360-180（BGI 同款）。"""

    def test_zero(self):
        assert CameraControl._angle_diff(90, 90) == 0

    def test_signed_shortest(self):
        assert CameraControl._angle_diff(0, 45) == -45
        assert CameraControl._angle_diff(45, 0) == 45

    def test_wraparound_across_zero(self):
        """跨 0/360 边界取最短：350 与 10 相差 20。"""
        assert CameraControl._angle_diff(350, 10) == -20
        assert CameraControl._angle_diff(10, 350) == 20

    def test_opposite_is_180(self):
        """反向边界 = ±180。"""
        assert abs(CameraControl._angle_diff(0, 180)) == 180

    def test_always_in_range(self):
        """任意 (current,target) 结果 ∈ [-180,180]。"""
        for c in range(0, 360, 17):
            for t in range(0, 360, 23):
                d = CameraControl._angle_diff(c, t)
                assert -180 <= d <= 180, f"({c},{t})→{d} 越界"


# ── _control_ratio：|diff| 分档倍率（对照 BGI CameraRotateTask）──


class TestControlRatio:
    """|diff|>90→4x | >30→3x | >5→2x | else 1x（BGI CameraRotateTask 阈值）。"""

    def test_threshold_boundaries(self):
        assert CameraControl._control_ratio(91) == 4.0
        assert CameraControl._control_ratio(90) == 3.0    # 不 >90
        assert CameraControl._control_ratio(31) == 3.0
        assert CameraControl._control_ratio(30) == 2.0    # 不 >30
        assert CameraControl._control_ratio(6) == 2.0
        assert CameraControl._control_ratio(5) == 1.0     # 不 >5
        assert CameraControl._control_ratio(0) == 1.0

    def test_large_diff(self):
        assert CameraControl._control_ratio(180) == 4.0
        assert CameraControl._control_ratio(100) == 4.0

    def test_negative_uses_abs(self):
        """负 diff 按 |diff| 选档（旋转方向由 move_x 符号处理，倍率取绝对值）。"""
        assert CameraControl._control_ratio(-100) == 4.0
        assert CameraControl._control_ratio(-31) == 3.0
        assert CameraControl._control_ratio(-6) == 2.0
        assert CameraControl._control_ratio(-3) == 1.0
