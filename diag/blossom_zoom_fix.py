"""实机测试：find_blossom_and_nearest_tp 分步调试（打印中间值）。

用法：以管理员身份运行，确保原神在前台且在主界面。
"""

import sys
import time

sys.path.insert(0, "src")

from framework.context import GameContext
from framework.high_level_api import HighLevelApi
from framework.runtime import Runtime

# ── 初始化 ──
print("=== 初始化 ===")
ctx = GameContext()
ctx.ensure_foreground()
print("原神窗口已激活")

runtime = Runtime(ctx)

def _test(ctx, g):
    from framework.scene import Scene
    from avc._core import KeyCode
    from abilities.navigation.map_ops import MapController, MAP_CENTER_X, MAP_CENTER_Y, MAP_SCALE_FACTOR
    from abilities.navigation.position import PositionGetter
    from abilities.navigation.tp import TpDatabase
    from framework import utils

    # ── 1. 打开大地图 ──
    scene_state = g.scene
    scene = scene_state.scene if scene_state else None
    print(f"当前场景: {scene}")

    if scene != Scene.MAP:
        print("按 M 打开大地图...")
        ctx.press(KeyCode.m)
        g.wait_scene(Scene.MAP, timeout=5.0)
        utils.sleep(1.0)  # 等地图渲染稳定
        print(f"场景: {g.scene}")

    mc = MapController(ctx, g)

    # ── 2. 记录初始 zoom ──
    frame = ctx.capture()
    initial_zoom = mc.measure_zoom_level(frame)
    print(f"\n初始 zoom: {initial_zoom}")

    # ── 3. 在初始 zoom 下检测花 ──
    blossoms = mc.find_blossom_on_map(frame)
    print(f"初始 zoom 下检测到 {len(blossoms)} 朵花")
    for i, b in enumerate(blossoms):
        type_cn = "启示之花" if b.blossom_type == "revelation" else "藏金之花"
        print(f"  #{i}: {type_cn} @屏幕({b.screen_x:.0f}, {b.screen_y:.0f}) score={b.score:.3f}")

    if blossoms:
        # ── 4. 拖花到中心 ──
        best = blossoms[0]
        north_delta = (MAP_CENTER_Y - best.screen_y) * initial_zoom / MAP_SCALE_FACTOR
        west_delta = (MAP_CENTER_X - best.screen_x) * initial_zoom / MAP_SCALE_FACTOR
        print(f"\n拖拽偏移: north={north_delta:.1f}, west={west_delta:.1f}")
        if abs(north_delta) > 100 or abs(west_delta) > 100:
            mc.drag_map(north_delta, west_delta, initial_zoom)
            utils.sleep(0.3)
            frame = ctx.capture()
            blossoms2 = mc.find_blossom_on_map(frame)
            print(f"拖拽后检测到 {len(blossoms2)} 朵花")
            for i, b in enumerate(blossoms2):
                type_cn = "启示之花" if b.blossom_type == "revelation" else "藏金之花"
                print(f"  #{i}: {type_cn} @屏幕({b.screen_x:.0f}, {b.screen_y:.0f})")

    # ── 5. 缩放到 3.0 ──
    mc.set_zoom_level(3.0, frame)
    utils.sleep(0.3)
    frame = ctx.capture()
    zoom_after = mc.measure_zoom_level(frame)
    print(f"\n缩放后 zoom: {zoom_after}")

    # ── 6. 重新检测花 ──
    blossoms3 = mc.find_blossom_on_map(frame)
    print(f"zoom=3.0 下检测到 {len(blossoms3)} 朵花")
    for i, b in enumerate(blossoms3):
        type_cn = "启示之花" if b.blossom_type == "revelation" else "藏金之花"
        print(f"  #{i}: {type_cn} @屏幕({b.screen_x:.0f}, {b.screen_y:.0f}) score={b.score:.3f}")

    if not blossoms3:
        print("未检测到花，退出")
        ctx.press(KeyCode.m)
        return

    # ── 7. SIFT 定位视口中心 ──
    pg = PositionGetter(ctx)
    viewport = pg.get_position_from_big_map(frame)
    print(f"\nSIFT 视口中心: {viewport}")

    # ── 8. 坐标转换 ──
    best = blossoms3[0]
    zoom = zoom_after or 3.0
    game_pos = mc.screen_to_game(best.screen_x, best.screen_y, viewport, zoom)
    type_cn = "启示之花" if best.blossom_type == "revelation" else "藏金之花"
    print(f"\n花: {type_cn}")
    print(f"  屏幕坐标: ({best.screen_x:.0f}, {best.screen_y:.0f})")
    print(f"  游戏坐标: ({game_pos[0]:.1f}, {game_pos[1]:.1f})")
    print(f"  距视口中心屏幕偏移: dx={best.screen_x - MAP_CENTER_X:.0f}, dy={best.screen_y - MAP_CENTER_Y:.0f}")
    print(f"  距视口中心游戏偏移: dn={(MAP_CENTER_Y - best.screen_y) * zoom / MAP_SCALE_FACTOR:.1f}, dw={(MAP_CENTER_X - best.screen_x) * zoom / MAP_SCALE_FACTOR:.1f}")

    # ── 9. 最近传送点 ──
    db = TpDatabase()
    top5 = db.find_nearest(game_pos[0], game_pos[1], n=5)
    print(f"\n距花最近的 5 个传送点:")
    for i, tp in enumerate(top5):
        d = ((tp.x - game_pos[0])**2 + (tp.y - game_pos[1])**2) ** 0.5
        print(f"  #{i}: {tp.name} ({tp.x:.1f}, {tp.y:.1f}) type={tp.type} dist={d:.0f}")

    # ── 10. 关闭地图 ──
    print("\n=== 关闭地图 ===")
    ctx.press(KeyCode.m)
    time.sleep(1)
    print("测试完成！")

runtime.run_callable(_test, task_name="blossom_debug")
