"""实机诊断：长距地图拖拽是否生效（auto_boss tp.navigate dist=2739 死循环复现）。

复现 Teleporter._navigate_map_to_target 的 MoveMapToCore 循环，但每轮额外记录：
- 拖拽前/后视口中心（SIFT）→ 实际视口位移 vs 期望位移
- zoom、期望屏幕像素、拖拽是否触边（cursor 位置）
- 每轮存图 debug/diag_drag_far/iter_N.png

用法（管理员）：python diag/diag_drag_far.py [target_x target_y] [max_iter]
默认目标 = 爆炎树传送点 (北 235.1, 西 837.5)。F9 全局取消。
"""

from __future__ import annotations

import math
import sys
import time

sys.path.insert(0, "src")


def run(ctx, g, tx: float, ty: float, max_iter: int) -> dict:
    from abilities.navigation.map_ops import MapController
    from abilities.navigation.position import PositionGetter
    from avc._core import KeyCode
    from framework.scene import Scene

    ctx.ensure_foreground()
    time.sleep(0.3)
    if not g.wait_main_ui(timeout=5.0):
        ctx.ic.press(KeyCode.esc)
        time.sleep(0.5)
        g.wait_main_ui(timeout=5.0)

    # 开图
    ctx.ic.press(KeyCode.m)
    g.wait_scene(Scene.MAP, timeout=10.0)
    time.sleep(0.8)

    mc = MapController(ctx, g)
    pg = PositionGetter(ctx)
    ic = ctx.ic
    rows: list[dict] = []
    last_center = None

    for i in range(max_iter):
        frame = ctx.capture()
        zoom = mc.measure_zoom_level(frame)
        center = pg.get_position_from_big_map(last_center)
        row: dict = {"iter": i, "zoom": zoom, "center": center}
        if center is None:
            row["note"] = "sift_fail"
            rows.append(row)
            print(f"[d{iter}] iter={i} zoom={zoom} SIFT_FAIL")
            time.sleep(0.3)
            continue
        last_center = center
        dx = tx - center[0]
        dy = ty - center[1]
        dist = math.hypot(dx, dy)
        # 期望屏幕像素（对照 drag_map 公式）
        exp_px_x = 3.57 * abs(dy) / max(zoom or 4.0, 0.1)
        exp_px_y = 3.57 * abs(dx) / max(zoom or 4.0, 0.1)
        cursor = ic.getCursorPos()
        row.update(
            dist=round(dist), exp_px=(round(exp_px_x), round(exp_px_y)),
            cursor_before=cursor,
        )
        print(
            f"[drag] iter={i} zoom={zoom} center=({center[0]:.0f},{center[1]:.0f}) "
            f"dist={dist:.0f} exp_px=({exp_px_x:.0f},{exp_px_y:.0f}) cursor={cursor}"
        )
        if dist < 200:
            row["note"] = "reached"
            rows.append(row)
            break

        # 远距先缩小（与 tp.py 相同）
        if dist > 1500 and (zoom or 0) < 6.0:
            mc.set_zoom_level(min(6.0, (zoom or 4.0) + 1.5), frame)
            time.sleep(0.2)
            frame = ctx.capture()
            zoom = mc.measure_zoom_level(frame) or zoom
            row["zoom_after_out"] = zoom

        mc.drag_map(dx, dy, zoom or 4.0)
        time.sleep(0.4)
        cursor2 = ic.getCursorPos()
        after = pg.get_position_from_big_map(center)
        row["cursor_after"] = cursor2
        row["center_after"] = after
        if after is not None:
            moved = (after[0] - center[0], after[1] - center[1])
            row["moved"] = (round(moved[0]), round(moved[1]))
            want = (round(dx), round(dy))
            eff = (
                round(moved[0] / dx, 2) if abs(dx) > 50 else None,
                round(moved[1] / dy, 2) if abs(dy) > 50 else None,
            )
            row["eff_ratio"] = eff
            print(
                f"[drag]   → moved=({moved[0]:.0f},{moved[1]:.0f}) "
                f"want=({dx:.0f},{dy:.0f}) eff={eff} cursor_after={cursor2}"
            )
        try:
            ctx.save_debug(f"diag_drag_far/iter_{i:02d}.png")
        except Exception:
            pass
        rows.append(row)

    # 关图复位
    ctx.ic.press(KeyCode.m)
    time.sleep(0.8)
    return {"rows": rows}


if __name__ == "__main__":
    from framework.runtime import Runtime

    tx = float(sys.argv[1]) if len(sys.argv) > 1 else 235.1
    ty = float(sys.argv[2]) if len(sys.argv) > 2 else 837.5
    max_iter = int(sys.argv[3]) if len(sys.argv) > 3 else 8

    rt = Runtime()
    try:
        result = rt.run_callable(
            lambda ctx, g: run(ctx, g, tx, ty, max_iter),
            task_name="diag_drag_far",
            timeout=240,
        )
        print(f"\n[result] {result}", file=sys.stderr)
    except Exception as e:
        print(f"\n[drag] 异常退出: {e!r}", file=sys.stderr)
    finally:
        rt.shutdown()
