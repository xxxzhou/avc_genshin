"""实机测试：大地图找地脉花 + 最近传送点。

用法：以管理员身份运行，确保原神在前台且在大地图界面（或主界面自动按 M 打开）。

流程：
1. 截图当前画面，判断场景
2. 若不在地图，按 M 打开大地图
3. 缩放到花图标可见级别（zoom ~3）
4. find_blossom_on_map() 检测花图标
5. SIFT 定位视口中心
6. screen_to_game() 转换花的游戏坐标
7. TpDatabase.find_nearest() 查最近传送点
8. 关闭地图
"""

import sys
import time

sys.path.insert(0, "src")

from framework.context import GameContext
from abilities.navigation.map_ops import MapController, BlossomCandidate
from abilities.navigation.position import PositionGetter
from abilities.navigation.tp import TpDatabase

# ── 初始化 avc ──
print("=== 初始化 ===")
ctx = GameContext()
ctx.ensure_foreground()
print("原神窗口已激活")

# ── 1. 截图当前画面 ──
print("\n=== Step 0: 当前场景 ===")
buf = ctx.capture()
if buf is None:
    print("ERROR: 截图失败，请确认原神窗口在前台")
    sys.exit(1)
print(f"截图尺寸: {buf.width}x{buf.height}")

# 保存当前截图
try:
    from PIL import Image
    import numpy as np
    raw = buf.to_bytes()
    arr = np.frombuffer(raw, dtype=np.uint8).reshape(buf.height, buf.width, 4)
    img = Image.fromarray(arr[:, :, :3], "RGB")
    img.save("debug_blossom_test_before_map.png")
    print("已保存 debug_blossom_test_before_map.png")
except Exception as e:
    print(f"保存截图失败: {e}")

# ── 2. 打开大地图 ──
print("\n=== Step 1: 打开大地图 ===")
from avc._core import KeyCode

ctx.press(KeyCode.m)
time.sleep(2)

buf = ctx.capture()
if buf is None:
    print("ERROR: 地图截图失败")
    sys.exit(1)
print(f"地图截图尺寸: {buf.width}x{buf.height}")

# ── 3. 缩放到花图标可见级别 ──
print("\n=== Step 2: 缩放调整 ===")
mc = MapController(ctx, None)
zoom = mc.measure_zoom_level(buf)
print(f"当前缩放等级: {zoom}")

# 花图标在 zoom ~3 时可见（缩小级别）
if zoom is not None:
    target_zoom = 3.0
    new_zoom = mc.set_zoom_level(target_zoom, buf)
    print(f"设置缩放到 {target_zoom}, 实际: {new_zoom}")
    time.sleep(0.5)
    buf = ctx.capture()

# ── 4. 检测地脉花图标 ──
print("\n=== Step 3: 检测地脉花 ===")
blossoms = mc.find_blossom_on_map(buf)
print(f"检测到 {len(blossoms)} 朵地脉花")
for i, b in enumerate(blossoms):
    type_cn = "启示之花" if b.blossom_type == "revelation" else "藏金之花"
    print(f"  #{i}: {type_cn} @屏幕({b.screen_x:.0f}, {b.screen_y:.0f}) score={b.score:.3f}")

if not blossoms:
    print("未检测到地脉花，尝试不同缩放级别...")
    # 尝试 zoom 2 和 zoom 4
    for try_zoom in [2.0, 4.0]:
        if zoom is not None:
            new_zoom = mc.set_zoom_level(try_zoom, buf)
            print(f"  尝试 zoom={try_zoom}, 实际: {new_zoom}")
            time.sleep(0.5)
            buf = ctx.capture()
            blossoms = mc.find_blossom_on_map(buf)
            print(f"  检测到 {len(blossoms)} 朵")
            for i, b in enumerate(blossoms):
                type_cn = "启示之花" if b.blossom_type == "revelation" else "藏金之花"
                print(f"    #{i}: {type_cn} @屏幕({b.screen_x:.0f}, {b.screen_y:.0f}) score={b.score:.3f}")
            if blossoms:
                break

# ── 5. SIFT 视口定位 ──
print("\n=== Step 4: SIFT 视口定位 ===")
pg = PositionGetter(ctx)
viewport = pg.get_position_from_big_map(buf)
print(f"视口中心游戏坐标: {viewport}")

# ── 6. 屏幕坐标→游戏坐标 ──
print("\n=== Step 5: 花的游戏坐标 ===")
zoom = mc.measure_zoom_level(buf)
print(f"当前缩放: {zoom}")

if blossoms and viewport is not None and zoom is not None:
    for i, b in enumerate(blossoms):
        game_pos = mc.screen_to_game(b.screen_x, b.screen_y, viewport, zoom)
        type_cn = "启示之花" if b.blossom_type == "revelation" else "藏金之花"
        print(f"  #{i}: {type_cn} 游戏({game_pos[0]:.1f}, {game_pos[1]:.1f})")

    # ── 7. 查最近传送点 ──
    print("\n=== Step 6: 最近传送点 ===")
    db = TpDatabase()
    best = blossoms[0]
    game_pos = mc.screen_to_game(best.screen_x, best.screen_y, viewport, zoom)
    nearest_list = db.find_nearest(game_pos[0], game_pos[1], n=5)
    print(f"距花最近的 5 个传送点:")
    for i, tp in enumerate(nearest_list):
        dist = ((tp.x - game_pos[0])**2 + (tp.y - game_pos[1])**2) ** 0.5
        print(f"  #{i}: {tp.name} ({tp.x:.1f}, {tp.y:.1f}) type={tp.type} 距离={dist:.0f}")
else:
    if not blossoms:
        print("  跳过：未检测到花")
    if viewport is None:
        print("  跳过：SIFT 定位失败")
    if zoom is None:
        print("  跳过：缩放测量失败")

# ── 8. 关闭地图 ──
print("\n=== Done: 关闭地图 ===")
ctx.press(KeyCode.m)
time.sleep(1)

print("\n测试完成！")
