"""诊断脚本：验证大地图上地脉花图标匹配 + 传送点查找。"""

import sys
sys.path.insert(0, "src")

from framework.context import GameContext
from framework.resources import res
from abilities import vision_utils as vu
from abilities.navigation.map_ops import MapController
from abilities.navigation.position import PositionGetter
from abilities.navigation.tp import TpDatabase
from framework.scene import Scene, SceneClassifier, SceneState

# ── 初始化 avc ──
ctx = GameContext()
ctx.ensure_foreground()

# ── 1. 截图当前画面 ──
print("=== Step 0: 当前场景 ===")
buf = ctx.capture()
if buf is None:
    print("ERROR: 截图失败")
    sys.exit(1)
print(f"截图尺寸: {buf.width}x{buf.height}")

# 保存当前截图
from PIL import Image
import numpy as np
raw = buf.to_bytes()
arr = np.frombuffer(raw, dtype=np.uint8).reshape(buf.height, buf.width, 4)
img = Image.fromarray(arr[:, :, :3], "RGB")
img.save("debug_current_screen.png")
print("已保存 debug_current_screen.png")

# ── 2. 打开大地图 ──
print("\n=== Step 1: 打开大地图 ===")
from avc._core import KeyCode
ctx.press(KeyCode.m)
import time
time.sleep(2)

buf = ctx.capture()
if buf is None:
    print("ERROR: 地图截图失败")
    sys.exit(1)

# 保存地图截图
raw = buf.to_bytes()
arr = np.frombuffer(raw, dtype=np.uint8).reshape(buf.height, buf.width, 4)
img = Image.fromarray(arr[:, :, :3], "RGB")
img.save("debug_map_screen.png")
print(f"地图截图尺寸: {buf.width}x{buf.height}")
print("已保存 debug_map_screen.png")

# ── 3. 测试花图标匹配 ──
print("\n=== Step 2: 花图标匹配 ===")
mc = MapController(ctx, None)

# 先设置缩放
zoom = mc.measure_zoom_level(buf)
print(f"当前缩放等级: {zoom}")

# 设置到 zoom 3
if zoom is not None:
    target_zoom = 3.0
    new_zoom = mc.set_zoom_level(target_zoom, buf)
    print(f"设置缩放到 {target_zoom}, 实际: {new_zoom}")
    time.sleep(0.5)
    buf = ctx.capture()

# 匹配启示之花
revelation_path = str(res.template_map("Blossom_of_Revelation.png"))
wealth_path = str(res.template_map("Blossom_of_Wealth.png"))
print(f"启示之花模板: {revelation_path} (exists={res.template_map('Blossom_of_Revelation.png').exists()})")
print(f"藏金之花模板: {wealth_path} (exists={res.template_map('Blossom_of_Wealth.png').exists()})")

# 用不同阈值测试（BGR 模式）
print("--- BGR 模式 ---")
for threshold in [0.5, 0.6, 0.65, 0.7, 0.8]:
    found = vu.find_all_templates(
        ctx,
        ["map/Blossom_of_Revelation.png", "map/Blossom_of_Wealth.png"],
        threshold=threshold,
        frame=buf,
    )
    total = sum(len(r) for r in found.values())
    details = {k: len(v) for k, v in found.items()}
    print(f"  threshold={threshold}: 命中 {total} 个, 详情={details}")
    if total > 0:
        for tpl_name, rects in found.items():
            for i, r in enumerate(rects):
                print(f"    [{tpl_name}] #{i}: cx={r.cx:.0f}, cy={r.cy:.0f}, score={r.score:.3f}")

# 用灰度模式测试
print("\n--- 灰度模式 ---")
for threshold in [0.5, 0.6, 0.65, 0.7, 0.8, 0.9]:
    ctx.tm.setGrayscale(True)
    found = vu.find_all_templates(
        ctx,
        ["map/Blossom_of_Revelation.png", "map/Blossom_of_Wealth.png"],
        threshold=threshold,
        frame=buf,
    )
    ctx.tm.setGrayscale(False)
    total = sum(len(r) for r in found.values())
    details = {k: len(v) for k, v in found.items()}
    print(f"  threshold={threshold}: 命中 {total} 个, 详情={details}")
    if total > 0:
        for tpl_name, rects in found.items():
            for i, r in enumerate(rects):
                print(f"    [{tpl_name}] #{i}: cx={r.cx:.0f}, cy={r.cy:.0f}, score={r.score:.3f}")

# ── 4. 测试传送点图标匹配 ──
print("\n=== Step 3: 传送点图标匹配 ===")
# 先把缩放调到传送点可见的级别
if zoom is not None:
    new_zoom = mc.set_zoom_level(4.4, buf)
    print(f"设置缩放到 4.4, 实际: {new_zoom}")
    time.sleep(0.5)
    buf = ctx.capture()

tp_icons = mc.find_tp_icons("TeleportWaypoint", buf)
print(f"传送点图标数量: {len(tp_icons)}")
for i, r in enumerate(tp_icons[:5]):
    print(f"  传送点 #{i}: cx={r.cx}, cy={r.cy}")

goddess_icons = mc.find_tp_icons("Goddess", buf)
print(f"七天神像图标数量: {len(goddess_icons)}")
for i, r in enumerate(goddess_icons[:3]):
    print(f"  神像 #{i}: cx={r.cx}, cy={r.cy}")

# ── 5. 测试视口定位 ──
print("\n=== Step 4: 视口定位 ===")
pos_getter = PositionGetter(ctx)
viewport = pos_getter.get_position_from_big_map(buf)
print(f"视口中心坐标: {viewport}")

# ── 6. 测试传送点数据库查找 ──
print("\n=== Step 5: 传送点数据库 ===")
db = TpDatabase()
print(f"传送点总数: {len(db.waypoints)}")
if viewport is not None:
    nearest = db.find_nearest(viewport[0], viewport[1], n=5)
    print(f"距视口中心最近的 5 个传送点:")
    for i, tp in enumerate(nearest):
        print(f"  #{i}: {tp.name} ({tp.x:.1f}, {tp.y:.1f}) type={tp.type}")

# ── 7. 关闭地图 ──
print("\n=== Done: 关闭地图 ===")
ctx.press(KeyCode.m)
time.sleep(1)

print("\n诊断完成！请查看 debug_current_screen.png 和 debug_map_screen.png")
