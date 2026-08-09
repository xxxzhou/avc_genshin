"""实机测试：找地脉花 → 传送到最近传送点 → 走到花的位置。

验证修复后的 Navigator.go_to（大角度差预转向）能否正确走向地脉花。

用法：以管理员身份运行，原神在前台大世界主界面。
  python diag_blossom_walk.py

安全：F9 全局取消热键。
"""

from __future__ import annotations

import sys
import time

sys.path.insert(0, "src")


def run_blossom_walk(ctx, g) -> dict:
    """地脉花导航全流程：找花 → 传送 → 走到花。"""
    from avc._core import KeyCode
    from framework.scene import Scene
    from abilities.navigation.map_ops import MapController
    from abilities.navigation.position import PositionGetter
    from abilities.navigation.navigator import Navigator
    from abilities.navigation.path_executor import Waypoint
    from abilities.navigation.camera import CameraControl

    results: dict[str, str] = {}

    def step(name: str, fn) -> bool:
        t0 = time.monotonic()
        try:
            v = fn()
            dt = time.monotonic() - t0
            results[name] = f"OK  {v!r}  ({dt:.1f}s)"
            print(f"  [blossom_walk] {name}: OK  {v!r}  ({dt:.1f}s)")
            return True
        except Exception as e:
            dt = time.monotonic() - t0
            results[name] = f"ERR {type(e).__name__}: {e}  ({dt:.1f}s)"
            print(f"  [blossom_walk] {name}: ERR {e!r}  ({dt:.1f}s)")
            return False

    # ── 1. 确保主界面 ──
    print("[blossom_walk] === Step 1: 确保主界面 ===")
    ctx.ensure_foreground()
    time.sleep(0.3)
    if not g.wait_main_ui(timeout=5.0):
        print("[blossom_walk] 不在主界面，按 Esc 恢复 ...")
        ctx.ic.press(KeyCode.esc)
        time.sleep(0.5)
        g.wait_main_ui(timeout=5.0)

    # ── 2. 打开大地图 ──
    print("[blossom_walk] === Step 2: 打开大地图 ===")
    ctx.ic.press(KeyCode.m)
    time.sleep(2.0)

    # ── 3. 找地脉花 ──
    print("[blossom_walk] === Step 3: 找地脉花 ===")
    mc = MapController(ctx, g)
    buf = ctx.capture()

    # 缩放到花可见级别
    zoom = mc.measure_zoom_level(buf)
    print(f"  当前缩放: {zoom}")
    if zoom is not None:
        new_zoom = mc.set_zoom_level(3.0, buf)
        print(f"  设置缩放到 3.0, 实际: {new_zoom}")
        time.sleep(0.5)
        buf = ctx.capture()

    blossoms = mc.find_blossom_on_map(buf)
    print(f"  检测到 {len(blossoms)} 朵地脉花")
    for i, b in enumerate(blossoms):
        type_cn = "启示之花" if b.blossom_type == "revelation" else "藏金之花"
        print(f"    #{i}: {type_cn} @屏幕({b.screen_x:.0f}, {b.screen_y:.0f}) score={b.score:.3f}")

    if not blossoms:
        print("[blossom_walk] 未检测到地脉花，尝试其他缩放 ...")
        for try_zoom in [2.0, 4.0]:
            if zoom is not None:
                mc.set_zoom_level(try_zoom, buf)
                time.sleep(0.5)
                buf = ctx.capture()
                blossoms = mc.find_blossom_on_map(buf)
                print(f"  zoom={try_zoom}: 检测到 {len(blossoms)} 朵")
                if blossoms:
                    break

    if not blossoms:
        print("[blossom_walk] 无地脉花，关闭地图退出")
        ctx.ic.press(KeyCode.m)
        time.sleep(1)
        return {"results": results, "error": "no_blossom_found"}

    # ── 4. SIFT 定位 + 花游戏坐标 ──
    print("[blossom_walk] === Step 4: SIFT 定位 + 花游戏坐标 ===")
    pg = PositionGetter(ctx)
    viewport = pg.get_position_from_big_map(buf)
    print(f"  视口中心: {viewport}")

    zoom = mc.measure_zoom_level(buf)
    # zoom 可能在新截图上测不到（地图 UI 变化），用之前的值兜底
    if zoom is None:
        zoom = 2.0  # 花在 zoom=2 时检测到的
        print(f"  缩放测量失败，使用兜底值 {zoom}")
    else:
        print(f"  当前缩放: {zoom}")

    if viewport is None:
        print("[blossom_walk] SIFT 失败，关闭地图退出")
        ctx.ic.press(KeyCode.m)
        time.sleep(1)
        return {"results": results, "error": "sift_failed"}

    best = blossoms[0]
    blossom_game_pos = mc.screen_to_game(best.screen_x, best.screen_y, viewport, zoom)
    type_cn = "启示之花" if best.blossom_type == "revelation" else "藏金之花"
    print(f"  {type_cn} 游戏坐标: ({blossom_game_pos[0]:.1f}, {blossom_game_pos[1]:.1f})")

    # ── 5. 查最近传送点 ──
    print("[blossom_walk] === Step 5: 查最近传送点 ===")
    from abilities.navigation.tp import TpDatabase

    db = TpDatabase()
    nearest_list = db.find_nearest(blossom_game_pos[0], blossom_game_pos[1], n=3)
    if not nearest_list:
        print("[blossom_walk] 无传送点，关闭地图退出")
        ctx.ic.press(KeyCode.m)
        time.sleep(1)
        return {"results": results, "error": "no_tp_found"}

    nearest_tp = nearest_list[0]
    tp_dist = CameraControl.distance(
        (nearest_tp.x, nearest_tp.y), blossom_game_pos
    )
    print(f"  最近传送点: {nearest_tp.name} ({nearest_tp.x:.1f}, {nearest_tp.y:.1f}) "
          f"type={nearest_tp.type} 距花={tp_dist:.0f}")
    print(f"  传送后位置: ({nearest_tp.tran_x:.1f}, {nearest_tp.tran_y:.1f})")

    # ── 6. 关闭地图 ──
    print("[blossom_walk] === Step 6: 关闭地图 ===")
    ctx.ic.press(KeyCode.m)
    time.sleep(1.5)

    # ── 7. 传送到最近传送点 ──
    print("[blossom_walk] === Step 7: 传送 ===")
    step("teleport", lambda: g.teleport_to((nearest_tp.x, nearest_tp.y)))
    step("wait_main_ui", lambda: g.wait_main_ui(timeout=30))

    # ── 8. 走到地脉花位置 ──
    print("[blossom_walk] === Step 8: 走到地脉花 ===")
    nav = Navigator(ctx, g)
    # 传送后锚定 prev
    nav.set_prev_position(nearest_tp.tran_x, nearest_tp.tran_y)

    # 先 seed prev（小地图定位需要）
    pg2 = PositionGetter(ctx)
    pg2.set_prev_position(nearest_tp.tran_x, nearest_tp.tran_y)

    # 获取当前位置
    p0 = nav.get_position()
    if p0 is not None:
        dist_to_blossom = CameraControl.distance(p0, blossom_game_pos)
        print(f"  当前位置: ({p0[0]:.1f}, {p0[1]:.1f})")
        print(f"  距花距离: {dist_to_blossom:.1f}")

        # 计算初始朝向差
        target_angle = CameraControl.target_orientation(p0, blossom_game_pos)
        current_angle = nav.get_orientation()
        if current_angle is not None:
            angle_diff = CameraControl._angle_diff(current_angle, target_angle)
            print(f"  当前朝向: {current_angle:.1f}°  目标朝向: {target_angle}°  差: {angle_diff:.1f}°")
    else:
        print("  当前位置获取失败（继续尝试行走）")

    # 走到花的位置（手动循环，每步打印诊断）
    blossom_wp = Waypoint(
        x=blossom_game_pos[0],
        y=blossom_game_pos[1],
        type="target",
        move_mode="walk",
    )
    t0 = time.monotonic()

    # 直接用 Navigator.go_to（已修复：大角度差停步闭环转向）
    arrived = nav.go_to(blossom_wp, tolerance=8.0, timeout=120.0)
    elapsed = time.monotonic() - t0

    # 最终位置
    pf = nav.get_position()
    final_dist = None if pf is None else CameraControl.distance(pf, blossom_game_pos)
    print(f"  go_to 结果: arrived={arrived}  耗时={elapsed:.1f}s")
    if pf is not None:
        print(f"  最终位置: ({pf[0]:.1f}, {pf[1]:.1f})  距花: {final_dist:.1f}")
    results["go_to_blossom"] = f"{'OK' if arrived else 'FAIL'} arrived={arrived} dist={final_dist} elapsed={elapsed:.1f}s"

    # ── 9. 检查花 F 图标 ──
    print("[blossom_walk] === Step 9: 检查花 F 图标 ===")
    from abilities.game_state import has_flower_f_icon

    found_f = False
    for _ in range(10):
        frame = ctx.capture()
        if frame is not None and has_flower_f_icon(ctx, frame):
            found_f = True
            break
        time.sleep(0.3)

    if found_f:
        print("  检测到花 F 图标！走到花位置成功！")
        results["flower_f_icon"] = "OK  检测到花 F 图标"
    else:
        print("  未检测到花 F 图标（可能距花稍远，或花不在视野内）")
        results["flower_f_icon"] = "FAIL  未检测到花 F 图标"

    # ── 打印结果 ──
    print("\n[blossom_walk] === 结果汇总 ===")
    for k, v in results.items():
        print(f"  {k:30s} {v}")

    return {"results": results, "arrived": arrived, "final_dist": final_dist}


if __name__ == "__main__":
    from framework.runtime import Runtime

    rt = Runtime()
    try:
        result = rt.run_callable(
            run_blossom_walk,
            task_name="diag_blossom_walk",
            timeout=300,
        )
        print(f"\n[result] {result}", file=sys.stderr)
    except Exception as e:
        print(f"\n[blossom_walk] 异常退出: {e!r}", file=sys.stderr)
    finally:
        rt.shutdown()
