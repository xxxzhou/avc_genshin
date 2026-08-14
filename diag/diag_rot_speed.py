"""实机标定：大步长相对旋转（move_by_rel ±600/±1200）的稳定性与实际角度。

每步：读朝向(中位3) → move_by_rel(dx) → 等 settle → 读朝向 → Δ角。
若 ±1200 稳定（Δ≈±53°，无丢步），rotate_to 可翻倍速度。
用法：python diag/diag_rot_speed.py
"""

from __future__ import annotations

import sys
import time

sys.path.insert(0, "src")


def run(ctx, g) -> dict:
    from abilities.navigation.camera import CameraControl

    cam = CameraControl(ctx)
    out = {}
    ctx.ensure_foreground()
    time.sleep(0.5)

    for px in (600, 900, 1200, 1500):
        deltas = []
        for _ in range(3):
            a = cam.get_orientation()
            if a is None:
                deltas.append(None)
                continue
            ctx.move_by_rel(px, 0)
            time.sleep(1.5)
            # 轻推 W 同步面朝=相机
            from avc._core import KeyCode

            ic = ctx.ic
            ic.keyDown(KeyCode.w)
            time.sleep(0.25)
            ic.keyUp(KeyCode.w)
            time.sleep(0.7)
            b = cam.get_orientation()
            if b is None:
                deltas.append(None)
            else:
                d = (b - a + 180) % 360 - 180
                deltas.append(round(d, 1))
        out[f"px{px}"] = deltas
        print(f"[rot] +{px}px → Δ角 {deltas}（期望 ≈{px / 22.6:.0f}°）")
    return out


if __name__ == "__main__":
    from framework.runtime import Runtime

    rt = Runtime()
    try:
        result = rt.run_callable(run, task_name="diag_rot_speed", timeout=120)
        print(f"\n[result] {result}", file=sys.stderr)
    except Exception as e:
        print(f"\n[rot] 异常退出: {e!r}", file=sys.stderr)
    finally:
        rt.shutdown()
