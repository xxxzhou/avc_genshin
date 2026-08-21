"""角色朝向（面朝）检测与旋转控制（Phase B）。

实机定论（2026-08-08，cache/diag_* 系列探针）：
- 旧朝向传感器 ``avc IOrientationDetector``（BGI FromGia 移植）不可用：原神小地图
  固定北朝上，它把小地图像素微差放大成巨大角度跳变（±600px → ±136°），非相机偏航。
- 小地图**玩家箭头**是可靠的朝向传感器：对称轴法检测（见 ``arrow.py``），
  实测连续 ±600px 旋转读数稳定 Δ≈±26.5°/步。
- 箭头只反映**角色面朝**；原神空闲时面朝与相机偏航独立（转相机箭头不动），
  须轻推 W 同步 → rotate_to 每轮旋转后轻推再读（闭环收敛）。
- 旋转标定：move_by_rel 相对移动 +600px → 面朝 +26.5°（facecal3），即
  **1° ≈ 22.6px**；单次移动封顶 ±600px（大正移 +2000 投递不稳）。
"""

from __future__ import annotations

import math
import time
from typing import TYPE_CHECKING

from abilities.navigation import arrow

if TYPE_CHECKING:
    from avc.image import IImageBuffer

    from framework.context import GameContext


# ── 1080p 常量 ──

# 小地图区域（实机标定 2026-08-08：环心 (169,154) r≈108；旧 BGI (62,19,212,212) 偏上 29px，
# 箭头不在中心 → 朝向检测坏。见 position.py 同注释）
MINIMAP_X = arrow.MINIMAP_X
MINIMAP_Y = arrow.MINIMAP_Y
MINIMAP_W = arrow.MINIMAP_W
MINIMAP_H = arrow.MINIMAP_H
MINIMAP_SIZE = arrow.MINIMAP_W  # 正方形边长

# 小地图中心（1080p，实机径向剖面测得）
MINIMAP_CENTER_X = MINIMAP_X + MINIMAP_W // 2  # 169
MINIMAP_CENTER_Y = MINIMAP_Y + MINIMAP_H // 2  # 154

# 旋转标定与控制参数（实机 2026-08-08）
_PX_PER_DEG = 22.6      # 1° ≈ 22.6px（facecal3：+600px → +26.5°）
_MAX_MOVE_PX = 600      # 单次移动封顶（≈26.5°）；大正移 +2000 投递不稳，用已验证的 ±600
_SETTLE_S = 1.5         # 相机旋转惯性等待（转完停稳再轻推/再读）
_NUDGE_S = 0.25         # 轻推 W 同步面朝=相机偏航（facecal3 用 0.25s 达 +26.5°）
_SYNC_S = 0.7           # 轻推后等面朝稳定再读数

# 旧旋转控制比例表（保留：_control_ratio 兼容旧调用方/测试；新 rotate_to 用线性 PX_PER_DEG）
_ROTATE_CONTROL_RATIOS = [
    (90, 4.0),
    (30, 3.0),
    (5, 2.0),
    (0, 1.0),
]

# 朝向连续读取失败达此次数 → 发 cam.heading_fail（诊断「盲走」：罗盘读不到仍导航）
_HEADING_FAIL_THRESHOLD = 5

# 失速检测（2026-08-15 实机：角色被地形挡住时 nudge W 无效，面朝读数不变，
# +300px×5 → Δ≈0°，max_attempts 空转 30s+ 拖死走路段）：连续 N 轮大位移后
# 面朝变化 < 此角度 → 判定卡住，快速中止（reason=stalled）交给走路段
# rotate_camera_to_target（移动中面朝自动同步，diag_moveto 已证可靠）收敛。
_STALL_DELTA_DEG = 1.5
_STALL_LIMIT = 2


class CameraControl:
    """角色朝向（面朝）检测与旋转控制。

    朝向检测走小地图玩家箭头对称轴检测（``arrow.py``）；旋转控制使用相对鼠标移动
    + 轻推 W 同步面朝（见模块 docstring 实机定论）。
    """

    def __init__(self, ctx: GameContext):
        self.ctx = ctx
        # 朝向连续 None 计数（cam.heading_fail 状态机：达阈值发一次失败，恢复发一次）
        self._heading_none_streak = 0

    def get_orientation(self, frame: IImageBuffer | None = None) -> float | None:
        """从截图获取角色朝向（罗盘角 0=北，90=东，顺时针）。

        走小地图玩家箭头的对称轴检测（``arrow.heading_from_frame``）。
        **角度约定 = 0=北，顺时针（罗盘）**，与 ``target_orientation`` 同系，
        ``_angle_diff`` 直接相减即可。

        注意：读的是**角色面朝**。原神空闲时面朝与相机偏航独立（转相机箭头不动），
        须轻推 W 同步后才等于相机偏航 → 配对使用 ``rotate_to``（内部同步）。

        可观测性：连续 N 次读不到朝向 → ``cam.heading_fail``（盲走诊断，痛点②⑤）。
        """
        if frame is None:
            frame = self.ctx.capture()
        if frame is None:
            self._record_heading_none()
            return None
        h, _area, _dist, _color = arrow.heading_from_frame(frame)
        if h is None:
            self._record_heading_none()
            return None
        self._record_heading_ok()
        return h

    def _record_heading_none(self) -> None:
        """朝向读取失败计数；达阈值发 cam.heading_fail（进入失败态仅发一次）。"""
        self._heading_none_streak += 1
        if self._heading_none_streak == _HEADING_FAIL_THRESHOLD:
            self.ctx.observe.event(
                "cam.heading_fail", ability="cam", phase="observe",
                ok=False, reason="heading_none", streak=self._heading_none_streak,
            )

    def _record_heading_ok(self) -> None:
        """朝向恢复；此前处于失败态（≥阈值）则发 cam.heading_fail ok=True。"""
        if self._heading_none_streak >= _HEADING_FAIL_THRESHOLD:
            self.ctx.observe.event(
                "cam.heading_fail", ability="cam", phase="observe",
                ok=True, reason="recovered", streak=self._heading_none_streak,
            )
        self._heading_none_streak = 0

    def rotate_to(
        self,
        target_angle: float,
        max_diff: float = 5.0,
        max_attempts: int | None = None,
    ) -> bool:
        """旋转角色面朝到目标角度。返回是否在 max_diff 范围内。

        闭环（实机验证 cache/diag_rotfix2.py）：读箭头面朝 → 算 diff →
        ``move_by_rel(-diff*22.6px)`` 转相机 → 等相机停 → 轻推 W 同步面朝=相机 → 再读。
        - 旋转必须用**相对**鼠标移动（avc moveBy 绝对坐标不触发原神 raw-input 视角）。
        - 单次移动量 = ``diff * PX_PER_DEG``，封顶 ±600px（≈±26.5°）；大角度多轮收敛。
        - 每轮轻推 W 会带一点前进位移（~1m/轮），大角度需多轮 → 位置会有漂移，
          导航层应在旋转后重定位（多功能联调阶段处理）。
        - max_attempts 缺省按 |diff| 估算（每轮≈26.5°），另 +5 冗余。实机(2026-08-08)：
          175° 恰好需要 9 步才收敛，且单步偶发 ±200° 异常跳变（撞地形/检测误读），
          冗余不足会耗尽步数返回 False（diag_rotloop 证实）。+5 缓冲异常。

        可观测性：入口/出口各一条 ``cam.rotate``（diff, attempts, max_attempts, converged）。
        痛点②：不收敛（reason=max_attempts）或读不到朝向（read_fail）= 转向失败 → 偏航/卡地形。
        """
        ob = self.ctx.observe
        self._center_cursor()
        # 入口：读当前朝向算初始 diff + 确定 max_attempts
        current = self._read_stable()
        if current is None:
            ob.event("cam.rotate", ability="cam", phase="act",
                     target=target_angle, ok=False, reason="read_fail")
            return False
        initial_diff = self._angle_diff(current, target_angle)
        if max_attempts is None:
            # 入口 diff 估算 max_attempts，但加最小下限 8（subagent 2026-08-12 报告
            # Bug 1：入口 diff=-0.1 时 max_attempts=5 不够循环噪声收敛——180 度歧义
            # 噪声让循环内 diff 跳到 60+，剩余 4 次不够）。
            max_attempts = max(int(abs(initial_diff) / 26.0) + 5, 8)
        ob.event("cam.rotate", ability="cam", phase="act",
                 target=target_angle, diff=round(initial_diff, 1), max_attempts=max_attempts)

        used = 0
        # 自适应增益：px→角度映射今天实测不稳定（同 +600px 在 +4°~+131° 间波动，
        # 2026-08-15 diag_rot_speed；两份历史标定 22.6 vs 9.9 px/° 互相矛盾）。
        # 每步实测 Δ角/px 在线修正估计，夹在 [8, 60] 防失控。
        px_per_deg = _PX_PER_DEG
        prev_angle: float | None = None
        prev_move: int | None = None
        stalled = 0
        for _ in range(max_attempts):
            used += 1
            current = self._read_stable()
            if current is None:
                ob.event("cam.rotate", ability="cam", phase="act", ok=False,
                         attempts=used, max_attempts=max_attempts, reason="read_fail")
                return False
            diff = self._angle_diff(current, target_angle)
            if prev_angle is not None and prev_move is not None and abs(prev_move) >= 100:
                actual = self._angle_diff(current, prev_angle)
                if actual * prev_move > 0 and abs(actual) > 2.0:
                    # 增益在线修正：方向对且角度变化显著才采信（有进展 → 失速清零）
                    measured = abs(prev_move) / abs(actual)
                    px_per_deg = max(8.0, min(60.0, 0.5 * px_per_deg + 0.5 * measured))
                    stalled = 0
                elif abs(actual) < _STALL_DELTA_DEG and abs(diff) >= max_diff:
                    # 失速：大位移后面朝纹丝不动且未收敛 → nudge 被地形挡死，
                    # 再转也是空转；stalled 连续 _STALL_LIMIT 轮 → 快速中止
                    stalled += 1
                    if stalled >= _STALL_LIMIT:
                        ob.event("cam.rotate", ability="cam", phase="act", ok=False,
                                 attempts=used, max_attempts=max_attempts,
                                 diff=round(diff, 1), reason="stalled")
                        return False
                else:
                    stalled = 0
            prev_angle = current
            if abs(diff) < max_diff:
                ob.event("cam.rotate", ability="cam", phase="act", ok=True,
                         attempts=used, max_attempts=max_attempts,
                         diff_final=round(diff, 1), reason="converged")
                return True
            move_x = int(round(-diff * px_per_deg))
            move_x = max(-_MAX_MOVE_PX, min(_MAX_MOVE_PX, move_x))
            if move_x == 0:
                move_x = -_MAX_MOVE_PX if diff > 0 else _MAX_MOVE_PX
            prev_move = move_x
            self._center_cursor()  # 光标每轮回中：防相对位移被屏幕边缘裁剪（见下）
            self.ctx.move_by_rel(move_x, 0)
            time.sleep(_SETTLE_S)  # 等相机旋转结束（惯性 ~1.5s）
            self._nudge_sync()     # 轻推 W 同步面朝=相机偏航
        # 最终校验：最后一次 move 可能已收敛但未读数，读一次确认
        current = self._read_stable()
        if current is None:
            ob.event("cam.rotate", ability="cam", phase="act", ok=False,
                     attempts=used, max_attempts=max_attempts, reason="read_fail")
            return False
        converged = abs(self._angle_diff(current, target_angle)) < max_diff
        ob.event("cam.rotate", ability="cam", phase="act", ok=converged,
                 attempts=used, max_attempts=max_attempts,
                 reason="converged" if converged else "max_attempts")
        return converged

    def _center_cursor(self) -> None:
        """光标回中：相对移动会在屏幕坐标上累加，光标若停在边缘（此前地图拖拽/
        旋转残留，或多轮旋转+nudge 漂移累计），相对位移被屏幕边界裁剪 → 实际
        旋转量远小于期望 → 被误判失速/不收敛。rotate_to 每轮 move 前调用。"""
        try:
            sx, sy = self.ctx.to_screen(960, 540)
            self.ctx.ic.moveTo(int(sx), int(sy))
        except Exception:
            pass

    def _read_stable(self, tries: int = 3) -> float | None:
        """连续读箭头朝向取中位数，滤掉噪声（单次读数会跳）。"""
        vals = []
        for _ in range(tries):
            v = self.get_orientation()
            if v is not None:
                vals.append(v)
            time.sleep(0.15)
        if not vals:
            return None
        return float(sorted(vals)[len(vals) // 2])

    def _nudge_sync(self) -> None:
        """轻推 W 同步面朝=相机偏航（原神空闲时二者独立，移动才同步）。

        用 0.25s 短按 W：面朝转到相机方向并前进一点（facecal3 标定同款）。
        """
        from avc._core import KeyCode

        ic = self.ctx.ic
        ic.keyDown(KeyCode.w)
        time.sleep(_NUDGE_S)
        ic.keyUp(KeyCode.w)
        time.sleep(_SYNC_S)

    def rotate_camera_to_target(
        self,
        from_pos: tuple[float, float],
        to_pos: tuple[float, float],
        max_diff: float = 3.0,
        current_angle: float | None = None,
    ) -> float:
        """移动中朝目标点转相机（纯 move_by_rel，**不 nudge**）。返回未收敛的角度差。

        前提：角色正在移动（W 已按住），**移动中面朝自动同步相机**（实机 diag_moveto [A]：
        按住 W 时 +600px → 面朝 +21.3°），无需轻推 W，也不打断持续行走。
        go_to 每轮调此方法持续转向（diag_moveto [B]：dist 单调下降、diff→0）。
        空闲态需用 ``rotate_to``（内含轻推同步）。

        current_angle: 可选朝向覆盖（navigator 传运动方向朝向，免疫箭头 180° 翻转，
        2026-08-22 实机山地蛇形案）；None 时读箭头。

        ⚠ 调用后须等相机旋转惯性结束再读朝向（约 1.5s，见 _SETTLE_S），
        否则读数是中间值 → diff 错误 → 过度旋转/振荡。
        """
        target_angle = CameraControl.target_orientation(from_pos, to_pos)
        current = current_angle if current_angle is not None else self.get_orientation()
        if current is None:
            return 0.0
        diff = self._angle_diff(current, target_angle)
        if abs(diff) < max_diff:
            return diff
        move_x = int(round(-diff * _PX_PER_DEG))
        move_x = max(-_MAX_MOVE_PX, min(_MAX_MOVE_PX, move_x))
        self.ctx.move_by_rel(move_x, 0)
        # 等相机旋转惯性结束（移动中面朝自动同步，无需 nudge，但须等相机停稳）
        time.sleep(_SETTLE_S)
        return diff

    @staticmethod
    def target_orientation(
        from_pos: tuple[float, float],
        to_pos: tuple[float, float],
    ) -> int:
        """计算从 from_pos 到 to_pos 的朝向角度（0-360 度）。

        输出**真罗盘角**（0=北，90=东，顺时针），与 ``get_orientation``（箭头传感器）同帧。
        坐标序 (北,西)：dx=Δ北、dy=Δ西；罗盘角 = atan2(-Δ西, Δ北)。

        ⚠ 对照 BGI GetTargetOrientation（2026-08-08 实机定论）：BGI 帧是 atan2(Δ西, Δ北)
        → 西=90°（逆时针罗盘），其 getOrientation 也用同帧、可相减；但我们弃用 avc
        getOrientation（小地图固定北朝上 ⇒ 假角度）改用小地图箭头（真罗盘），故此处把
        第二轴取负转成真罗盘，与箭头同系（见 arrow.py / rotate_to）。
        """
        dx = to_pos[0] - from_pos[0]      # Δ北
        dy = -(to_pos[1] - from_pos[1])   # Δ东 = -Δ西（换真罗盘帧）

        if dx == 0 and dy == 0:
            return 0

        # atan2(dy, dx) → 角度（弧度）→ 0-360 度
        degrees = math.degrees(math.atan2(dy, dx))
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
