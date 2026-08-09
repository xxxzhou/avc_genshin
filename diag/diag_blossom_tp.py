"""实机诊断：只做 找花→算坐标→找传送点→传送，每步详细打印。

用法：以管理员身份运行，原神在前台大世界主界面。
  python diag_blossom_tp.py

安全：F9 全局取消热键。
"""

from __future__ import annotations

import sys
import time

sys.path.insert(0, "src")


def run_blossom_tp(ctx, g) -> dict:
    from avc._core import KeyCode
    from framework.scene import Scene
    from abilities.navigation.map_ops import MapController
    from abilities.navigation.position import PositionGetter
    from abilities.navigation.tp import TpDatabase
    from abilities.navigation.camera import CameraControl

    results: dict[str, str] = {}

    # ── 1. 确保主界面 ──
    print("[btp] === Step 1: 确保主界面 ===")
    ctx.ensure_foreground()
    time.sleep(0.3)
    if not g.wait_main_ui(timeout=5.0):
        print("[btp] 不在主界面，按 Esc ...")
        ctx.ic.press(KeyCode.esc)
        time.sleep(0.5)
        g.wait_main_ui(timeout=5.0)

    # ── 2. 打开大地图 ──
    print("[btp] === Step 2: 打开大地图 ===")
    ctx.ic.press(KeyCode.m)
    time.sleep(2.0)

    # ── 3. 找地脉花 ──
    print("[btp] === Step 3: 找地脉花 ===")
    mc = MapController(ctx, g)
    buf = ctx.capture()

    # 先用当前缩放试一次
    blossoms = mc.find_blossom_on_map(buf)
    print(f"  当前缩放检测到 {len(blossoms)} 朵地脉花")

    # 缩放到花可见级别再试
    if not blossoms:
        zoom = mc.measure_zoom_level(buf)
        print(f"  当前缩放: {zoom}")
        if zoom is not None:
            new_zoom = mc.set_zoom_level(3.0, buf)
            print(f"  设置缩放到 3.0, 实际: {new_zoom}")
            time.sleep(0.5)
            buf = ctx.capture()
            blossoms = mc.find_blossom_on_map(buf)
            print(f"  zoom=3.0: 检测到 {len(blossoms)} 朵")

    for i, b in enumerate(blossoms):
        type_cn = "启示之花" if b.blossom_type == "revelation" else "藏金之花"
        print(f"    #{i}: {type_cn} @屏幕({b.screen_x:.0f}, {b.screen_y:.0f}) score={b.score:.3f}")

    if not blossoms:
        print("[btp] 未检测到地脉花，尝试更多缩放 ...")
        for try_zoom in [1.5, 2.0, 2.5, 4.0, 4.5, 5.0]:
            mc.set_zoom_level(try_zoom, buf)
            time.sleep(0.5)
            buf = ctx.capture()
            blossoms = mc.find_blossom_on_map(buf)
            print(f"  zoom={try_zoom}: 检测到 {len(blossoms)} 朵")
            if blossoms:
                    break

    if not blossoms:
        print("[btp] 无地脉花，关闭地图退出")
        ctx.ic.press(KeyCode.m)
        time.sleep(1)
        return {"error": "no_blossom_found"}

    # ── 4. SIFT 定位 + 花游戏坐标 ──
    print("[btp] === Step 4: SIFT 定位 + 花游戏坐标 ===")
    pg = PositionGetter(ctx)
    viewport = pg.get_position_from_big_map(buf)
    print(f"  视口中心(游戏坐标): {viewport}")

    zoom = mc.measure_zoom_level(buf)
    if zoom is None:
        zoom = 2.0
        print(f"  缩放测量失败，使用兜底值 {zoom}")
    else:
        print(f"  当前缩放: {zoom}")

    if viewport is None:
        print("[btp] SIFT 失败，关闭地图退出")
        ctx.ic.press(KeyCode.m)
        time.sleep(1)
        return {"error": "sift_failed"}

    best = blossoms[0]
    blossom_game_pos = mc.screen_to_game(best.screen_x, best.screen_y, viewport, zoom)
    type_cn = "启示之花" if best.blossom_type == "revelation" else "藏金之花"
    print(f"  {type_cn} 游戏坐标: ({blossom_game_pos[0]:.1f}, {blossom_game_pos[1]:.1f})")

    # ── 5. 查最近传送点 ──
    print("[btp] === Step 5: 查最近传送点 ===")
    db = TpDatabase()
    nearest_list = db.find_nearest(blossom_game_pos[0], blossom_game_pos[1], n=5)
    if not nearest_list:
        print("[btp] 无传送点，关闭地图退出")
        ctx.ic.press(KeyCode.m)
        time.sleep(1)
        return {"error": "no_tp_found"}

    for i, tp in enumerate(nearest_list):
        dist = CameraControl.distance((tp.x, tp.y), blossom_game_pos)
        print(f"  #{i}: {tp.name} pos=({tp.x:.1f},{tp.y:.1f}) tran=({tp.tran_x:.1f},{tp.tran_y:.1f}) "
              f"距花={dist:.0f} type={tp.type}")

    nearest_tp = nearest_list[0]
    tp_dist = CameraControl.distance((nearest_tp.x, nearest_tp.y), blossom_game_pos)
    print(f"\n  选择: {nearest_tp.name} ({nearest_tp.x:.1f}, {nearest_tp.y:.1f}) "
          f"type={nearest_tp.type} 距花={tp_dist:.0f}")

    # ── 6. 关闭地图 ──
    print("[btp] === Step 6: 关闭地图 ===")
    ctx.ic.press(KeyCode.m)
    time.sleep(1.5)

    # ── 7. 传送到最近传送点 ──
    print("[btp] === Step 7: 传送 ===")
    try:
        result = g.teleport_to((nearest_tp.x, nearest_tp.y))
        print(f"  传送结果: 到达 ({result[0]:.1f}, {result[1]:.1f})")
        results["teleport"] = f"OK 到达({result[0]:.1f},{result[1]:.1f})"
    except Exception as e:
        print(f"  传送失败: {e!r}")
        results["teleport"] = f"ERR {e!r}"
        return {"results": results, "error": "teleport_failed"}

    # 等主界面
    if g.wait_main_ui(timeout=30):
        print("  已回到主界面")
    else:
        print("  等待主界面超时")

    # ── 8. 验证传送后位置 ──
    print("[btp] === Step 8: 验证传送后位置 ===")
    nav_pg = PositionGetter(ctx)
    nav_pg.set_prev_position(nearest_tp.tran_x, nearest_tp.tran_y)
    pos = nav_pg.get_position()
    if pos is not None:
        dist_to_tp = CameraControl.distance(pos, (nearest_tp.tran_x, nearest_tp.tran_y))
        dist_to_blossom = CameraControl.distance(pos, blossom_game_pos)
        print(f"  当前位置: ({pos[0]:.1f}, {pos[1]:.1f})")
        print(f"  距传送点: {dist_to_tp:.0f}")
        print(f"  距花: {dist_to_blossom:.0f}")
    else:
        print("  位置获取失败")

    # ── 打印结果 ──
    print("\n[btp] === 结果汇总 ===")
    for k, v in results.items():
        print(f"  {k:30s} {v}")

    return {
        "results": results,
        "blossom_pos": blossom_game_pos,
        "nearest_tp": nearest_tp.name,
        "tp_dist": tp_dist,
        "current_pos": pos,
    }


if __name__ == "__main__":
    from framework.runtime import Runtime

    rt = Runtime()
    try:
        result = rt.run_callable(
            run_blossom_tp,
            task_name="diag_blossom_tp",
            timeout=180,
        )
        print(f"\n[result] {result}", file=sys.stderr)
    except Exception as e:
        print(f"\n[btp] 异常退出: {e!r}", file=sys.stderr)
    finally:
        rt.shutdown()
