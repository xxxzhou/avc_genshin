"""实机诊断：zoom 旋钮检测 + 各档位下 SIFT/拖拽标定。

1. M 复位地图（回玩家位置）
2. zoom 1→6 逐档：存图 + 测旋钮 + SIFT 定位 + 小拖 300 单位实测 px/unit
用法：python diag/diag_zoom_scan.py
"""

from __future__ import annotations

import sys
import time

sys.path.insert(0, "src")

import cv2
import numpy as np


def snap(ctx, name: str) -> None:
    buf = ctx.capture()
    if buf is None:
        return
    raw = buf.to_bytes()
    arr = np.frombuffer(raw, dtype=np.uint8).reshape((buf.height, buf.width, 4))
    cv2.imwrite(f"debug/zoom_scan/{name}.png", cv2.cvtColor(arr, cv2.COLOR_RGBA2BGR))


def run(ctx, g) -> dict:
    from abilities.navigation.map_ops import MapController
    from abilities.navigation.position import PositionGetter
    from avc._core import KeyCode
    from framework.scene import Scene

    out = {}
    ctx.ensure_foreground()
    time.sleep(0.3)
    # 复位：关图再开图（当前视口在稻妻海域，SIFT 死）
    ctx.ic.press(KeyCode.m)
    time.sleep(0.8)
    ctx.ic.press(KeyCode.m)
    g.wait_scene(Scene.MAP, timeout=10.0)
    time.sleep(1.0)

    mc = MapController(ctx, g)
    pg = PositionGetter(ctx)
    snap(ctx, "00_reset")

    for zt in (1.5, 3.0, 4.5, 6.0):
        got = mc.set_zoom_level(zt)
        time.sleep(0.5)
        z_meas = mc.measure_zoom_level()
        p = pg.get_position_from_big_map()
        snap(ctx, f"z{zt}")
        print(f"[zs] set={zt} measured={z_meas} got={got} sift={p}")

    # zoom=6 下小拖 300 单位标定 px/unit
    mc.set_zoom_level(6.0)
    time.sleep(0.5)
    z = mc.measure_zoom_level()
    p0 = pg.get_position_from_big_map()
    snap(ctx, "pre_drag_z6")
    if p0 is not None:
        mc.drag_map(300.0, 0.0, z or 6.0)
        time.sleep(0.5)
        p1 = pg.get_position_from_big_map()
        snap(ctx, "post_drag_z6")
        moved = None if p1 is None else (round(p1[0] - p0[0]), round(p1[1] - p0[1]))
        print(f"[zs] z={z} drag(north+300): {p0} → {p1} moved={moved}")
        out["drag_z6"] = moved
        # 拖回去
        if p1 is not None:
            mc.drag_map(p0[0] - p1[0], p0[1] - p1[1], z or 6.0)
            time.sleep(0.5)

    # 复位关图
    ctx.ic.press(KeyCode.m)
    time.sleep(0.5)
    return out


if __name__ == "__main__":
    from framework.runtime import Runtime

    rt = Runtime()
    try:
        result = rt.run_callable(run, task_name="diag_zoom_scan", timeout=180)
        print(f"\n[result] {result}", file=sys.stderr)
    except Exception as e:
        print(f"\n[zs] 异常退出: {e!r}", file=sys.stderr)
    finally:
        rt.shutdown()
