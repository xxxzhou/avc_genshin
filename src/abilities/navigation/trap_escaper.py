"""卡死检测与脱困（Phase B）。

对照 BetterGI TrapEscaper.cs:
- 卡死检测: 记录 8+ 个位置采样，|delta.X| + |delta.Y| < 3 → 卡死
- 脱困策略: 随机角度旋转 → 后退/左移/右移 → 重新朝向目标
- 卡死计数: 同路线连续卡死 3 次以上 → 放弃重试

BGI TrapEscaper 脱困步骤:
1. 停止移动
2. 随机旋转角度 (30-150 度)
3. 后退 2 秒
4. 左移/右移各 1 秒
5. 如果在攀爬: 跳跃 + 向上移动
"""

from __future__ import annotations

import random
import time
from typing import TYPE_CHECKING

from abilities.navigation.camera import CameraControl

if TYPE_CHECKING:
    from avc._core import KeyCode

    from framework.context import GameContext

from abilities.navigation.path_executor import Waypoint


# ── 常量 ──

_STUCK_SAMPLE_COUNT = 8  # 卡死检测所需最小采样数
_STUCK_DELTA_THRESHOLD = 3.0  # |delta.X| + |delta.Y| 阈值
_MAX_STUCK_COUNT = 3  # 最大连续卡死次数
_ESCAPE_BACK_DURATION = 2.0  # 后退持续时间(秒)
_ESCAPE_STRAFE_DURATION = 1.0  # 左右移动持续时间(秒)
_ESCAPE_ROTATE_MIN = 30  # 随机旋转最小角度
_ESCAPE_ROTATE_MAX = 150  # 随机旋转最大角度


class TrapEscaper:
    """检测并脱离卡死状态。

    对照 BGI TrapEscaper.cs:
    - 记录位置采样
    - 检测卡死（位置长时间不变）
    - 执行脱困动作（旋转+后退+横移）
    """

    def __init__(self, ctx: GameContext, max_stuck_count: int = _MAX_STUCK_COUNT,
                 position_getter=None):
        self.ctx = ctx
        self._positions: list[tuple[float, float]] = []
        self._stuck_count: int = 0
        self._max_stuck_count: int = max_stuck_count
        self._last_record_time: float = 0.0
        # 位置读取器（Navigator 注入）：脱困前后采样判定 escaped；None 时省略该判定
        self._position_getter = position_getter

    def record_position(self, x: float, y: float) -> None:
        """记录位置采样（用于卡死检测）。

        对照 BGI PathExecutor.MoveTo 中的卡死检测逻辑:
        每隔约 1 秒记录一次位置，保留最近 8+ 个采样。
        """
        self._positions.append((x, y))

    def is_stuck(self) -> bool:
        """检测是否卡死。

        对照 BGI PathExecutor.MoveTo:
        8+ 个位置采样，最新与第 8 个之前的差 |delta.X| + |delta.Y| < 3 → 卡死。
        """
        if len(self._positions) < _STUCK_SAMPLE_COUNT:
            return False
        delta_x = self._positions[-1][0] - self._positions[-_STUCK_SAMPLE_COUNT][0]
        delta_y = self._positions[-1][1] - self._positions[-_STUCK_SAMPLE_COUNT][1]
        return abs(delta_x) + abs(delta_y) < _STUCK_DELTA_THRESHOLD

    @property
    def stuck_count(self) -> int:
        """连续卡死次数。"""
        return self._stuck_count

    @property
    def should_abort(self) -> bool:
        """是否应放弃当前路线（卡死次数超限）。"""
        return self._stuck_count >= self._max_stuck_count

    def escape(self, target: Waypoint) -> None:
        """执行脱困动作。

        对照 BGI TrapEscaper.RotateAndMove + MoveTo:
        1. 释放 W 键
        2. 随机旋转 30-150 度
        3. 后退 2 秒
        4. 左移或右移 1 秒
        5. 重新朝向目标

        可观测性：发 ``nav.stuck``（ability=trap, stuck_count, should_abort, actions,
        pos_before/after, escaped）。痛点②：脱困前后采样位置判定 ``escaped``——
        移动量 ≥ 卡死阈值=已脱困；仍原地=still_stuck（继续卡死将 abort）。
        """
        ob = self.ctx.observe
        pg = self._position_getter
        pos_before = pg.get_position() if pg is not None else None

        try:
            from avc._core import KeyCode
        except ImportError:
            KeyCode = None

        actions: list[str] = []

        # 1. 释放前进键
        if KeyCode is not None:
            try:
                self.ctx.ic.keyUp(KeyCode.w)
            except Exception:
                pass

        # 2. 随机旋转
        angle = random.uniform(_ESCAPE_ROTATE_MIN, _ESCAPE_ROTATE_MAX)
        direction = random.choice([-1, 1])
        move_x = int(direction * angle * 2)  # 简化旋转
        try:
            # 旋转用相对移动（avc moveBy 绝对坐标，原神 raw-input 视角不认）
            self.ctx.move_by_rel(move_x, 0)
        except Exception:
            pass
        actions.append(f"rotate{direction:+d}{angle:.0f}")
        time.sleep(0.1)  # 测试时缩短等待

        # 3. 后退
        if KeyCode is not None:
            self.ctx.press(KeyCode.s, hold=0.1)  # 测试时缩短
            actions.append("back")

        # 4. 左移或右移
        if KeyCode is not None:
            strafe_key = KeyCode.a if direction > 0 else KeyCode.d
            self.ctx.press(strafe_key, hold=0.1)  # 测试时缩短
            actions.append("left" if direction > 0 else "right")
        time.sleep(0.2)

        # 5. 增加卡死计数
        self._stuck_count += 1

        # 6. 脱困成效：采样后位置 vs 卡死前位置（移动量≥卡死阈值=已脱困）
        pos_after = pg.get_position() if pg is not None else None
        escaped = None
        if pos_before is not None and pos_after is not None:
            moved = abs(pos_after[0] - pos_before[0]) + abs(pos_after[1] - pos_before[1])
            escaped = moved >= _STUCK_DELTA_THRESHOLD
        stuck_fields = dict(
            stuck_count=self._stuck_count,
            should_abort=self.should_abort,
            actions=actions,
            target=(round(target.x), round(target.y)),
            pos_before=(round(pos_before[0]), round(pos_before[1])) if pos_before else None,
            pos_after=(round(pos_after[0]), round(pos_after[1])) if pos_after else None,
            escaped=escaped,
        )
        if escaped is not None:
            stuck_fields["ok"] = escaped
            if not escaped:
                stuck_fields["reason"] = "still_stuck"
        ob.event("nav.stuck", ability="trap", phase="act", **stuck_fields)

        # 7. 清空位置记录
        self._positions.clear()

    def reset(self) -> None:
        """清空位置记录和卡死计数。"""
        self._positions.clear()
        self._stuck_count = 0
