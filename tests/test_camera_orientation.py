"""摄像机朝向/旋转数学正确性测试（纯数学，无 avc/游戏/mock 依赖）。

锁定 2026-08-07 的核对结论：
  get_orientation（avc / BGI FromGia 移植）与 target_orientation（atan2，≡ BGI
  Navigation.GetTargetOrientation）**同一坐标系**，_angle_diff 直接相减即可，无需换算。
依据：BGI CameraRotateTask.RotateToApproach 里 (cao - targetOrientation) 直接相减；
FromGia 回退输出 / PredictRotation / GetTargetOrientation 三者在 BGI 走同一相减。

本文件只测纯数学（target_orientation / _angle_diff / _control_ratio）——这些是静态方法，
不需实例化 CameraControl，不需 avc。get_orientation 的数值正确性（avc 端）依赖真实小地图图，
归「离线能力验证套件」/ 实机标定，不在本文件。
"""

from __future__ import annotations

import math

from abilities.navigation.camera import CameraControl


# ── BGI 公式独立重算（对照基线）──


def _bgi_get_target_orientation(dx: float, dy: float) -> int:
    """BGI Navigation.GetTargetOrientation 的 acos 实现（源码逐行照搬，作对照基线）。

    源码：angle = acos(dx/len)；dy<0 → 2π-angle；int(angle*180/π)。
    """
    length = math.hypot(dx, dy)
    if length == 0:
        return 0
    angle = math.acos(dx / length)
    if dy < 0:
        angle = 2 * math.pi - angle
    return int(angle * (180.0 / math.pi))


# ── target_orientation ≡ BGI GetTargetOrientation ──


class TestTargetOrientation:
    """target_orientation(from, to) 必须数值等于 BGI Navigation.GetTargetOrientation。"""

    def test_cardinal_directions(self):
        """四正方向：+x→0 / +y→90 / -x→180 / -y→270。"""
        assert CameraControl.target_orientation((0, 0), (1, 0)) == 0
        assert CameraControl.target_orientation((0, 0), (0, 1)) == 90
        assert CameraControl.target_orientation((0, 0), (-1, 0)) == 180
        assert CameraControl.target_orientation((0, 0), (0, -1)) == 270

    def test_diagonal_directions(self):
        assert CameraControl.target_orientation((0, 0), (1, 1)) == 45
        assert CameraControl.target_orientation((0, 0), (1, -1)) == 315
        assert CameraControl.target_orientation((0, 0), (-1, 1)) == 135
        assert CameraControl.target_orientation((0, 0), (-1, -1)) == 225

    def test_equivalent_to_bgi_acos_formula(self):
        """我们的 atan2 实现 ≡ BGI acos 实现（多组向量，含各象限）。"""
        vectors = [
            (3, 4), (-3, 4), (3, -4), (-3, -4),
            (5, 0), (0, 5), (-5, 0), (0, -5),
            (1, 100), (-100, 1), (7, -7), (-7, -7),
            (8, 15), (-8, 15), (8, -15), (-8, -15),
        ]
        for dx, dy in vectors:
            ours = CameraControl.target_orientation((0, 0), (dx, dy))
            bgi = _bgi_get_target_orientation(dx, dy)
            assert ours == bgi, f"({dx},{dy}): ours={ours} bgi_acos={bgi}"

    def test_uses_delta_from_nonzero_origin(self):
        """非原点起点按差向量算。"""
        assert CameraControl.target_orientation((10, 10), (11, 10)) == 0   # 向 +x
        assert CameraControl.target_orientation((10, 10), (10, 11)) == 90  # 向 +y

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
