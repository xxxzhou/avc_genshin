"""实机测试：地脉花完整流程（地图找花 → 传送 → 打怪 → 拾取）。

用法：以管理员身份运行，确保原神在前台且在主界面。
      python diag_fight_test.py

流程：
1. 打开大地图，缩放到花图标可见级别，检测地脉花
2. SIFT 定位视口 → 算花的游戏坐标 → 找最近传送点（失败则拖地图重试）
3. 关闭地图，用 Teleporter 传送到传送点（或直接在地图上点图标传送）
4. 旋转视角搜索敌人/花
5. 如果有花，走过去触发战斗
6. 战斗到清场
7. 拾取掉落物

注意：此脚本绕过 Runtime，直接使用底层 API，仅用于实机诊断。
"""

import sys
import time
import math

sys.path.insert(0, "src")

from framework.context import GameContext
from framework.scene import Scene, SceneState, set_classifier, classify_scene
from abilities.game_state import make_classifier, has_flower_f_icon, has_chest_f_icon

# ── 配置 ──
FIGHT_TIMEOUT = 120
PICK_TIMEOUT = 15
SEEK_TIMEOUT = 90
WALK_SECS_PER_STEP = 5  # 每次行走秒数（原 3s 不够，花可能 70+ 单位远）

# ── 初始化 ──
print("=== 初始化 ===")
ctx = GameContext()
ctx.ensure_foreground()
print("原神窗口已激活")

set_classifier(make_classifier(ctx))

# ── 场景检测 ──
print("\n=== Step 0: 场景检测 ===")
frame = ctx.capture()
if frame is None:
    print("ERROR: 截图失败")
    sys.exit(1)
print(f"截图尺寸: {frame.width}x{frame.height}")

scene = classify_scene(frame)
print(f"当前场景: {scene.scene.name}")

# 回到主界面
if scene.scene != Scene.MAIN_UI:
    print(f"不在主界面 (scene={scene.scene.name})，尝试回到主界面...")
    from avc._core import KeyCode
    for _ in range(5):
        if scene.scene == Scene.MAP:
            ctx.press(KeyCode.m)
        else:
            ctx.press(KeyCode.esc)
        time.sleep(1)
        frame = ctx.capture()
        scene = classify_scene(frame)
        if scene.scene == Scene.MAIN_UI:
            break

# ── 最小化 g 桥接 ──
class _MinimalG:
    def __init__(self, ctx):
        self.ctx = ctx
        self._scene = scene
        self.runtime = None

    @property
    def scene(self):
        return self._scene

    def _update_scene(self):
        frame = ctx.capture()
        if frame:
            self._scene = classify_scene(frame)

    def wait_scene(self, target_scene, timeout=10.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            self._update_scene()
            if self._scene.scene == target_scene:
                return True
            time.sleep(0.2)
        return False

    def wait_main_ui(self, timeout=60.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            self._update_scene()
            if self._scene.scene == Scene.MAIN_UI:
                return True
            time.sleep(0.3)
        return False

    def click(self, x, y, button="left"):
        ctx.click_at(x, y, button)

    def press(self, key, hold=0.0):
        ctx.press(key, hold)

g = _MinimalG(ctx)

# ── Step 1: 打开大地图找花 ──
print("\n=== Step 1: 打开大地图找地脉花 ===")
from avc._core import KeyCode
from abilities.navigation.map_ops import MapController, DISPLAY_TP_ZOOM
from abilities.navigation.position import PositionGetter
from abilities.navigation.tp import TpDatabase, Teleporter

ctx.press(KeyCode.m)
if not g.wait_scene(Scene.MAP, timeout=10.0):
    print("ERROR: 等待地图场景超时！")
    sys.exit(1)
print(f"场景: {g.scene.scene.name}")
time.sleep(0.5)

frame = ctx.capture()
mc = MapController(ctx, g)

# 缩放到花图标可见级别
zoom = mc.measure_zoom_level(frame)
print(f"当前缩放: {zoom}")
if zoom is not None:
    new_zoom = mc.set_zoom_level(3.0, frame)
    print(f"设置缩放到 3.0, 实际: {new_zoom}")
    time.sleep(0.5)
    frame = ctx.capture()

# 检测地脉花
blossoms = mc.find_blossom_on_map(frame)
print(f"检测到 {len(blossoms)} 朵地脉花")
for i, b in enumerate(blossoms):
    type_cn = "启示之花" if b.blossom_type == "revelation" else "藏金之花"
    print(f"  #{i}: {type_cn} @屏幕({b.screen_x:.0f}, {b.screen_y:.0f}) score={b.score:.3f}")

if not blossoms:
    for try_zoom in [2.0, 4.0]:
        mc.set_zoom_level(try_zoom, frame)
        time.sleep(0.5)
        frame = ctx.capture()
        blossoms = mc.find_blossom_on_map(frame)
        print(f"  尝试 zoom={try_zoom}, 检测到 {len(blossoms)} 朵")
        if blossoms:
            break

if not blossoms:
    print("ERROR: 未检测到地脉花！关闭地图退出。")
    ctx.press(KeyCode.m)
    sys.exit(1)

# 选启示之花优先
best = blossoms[0]
for b in blossoms:
    if b.blossom_type == "revelation" and b.score > 0.7:
        best = b
        break
type_cn = "启示之花" if best.blossom_type == "revelation" else "藏金之花"
print(f"\n选择目标: {type_cn} @屏幕({best.screen_x:.0f}, {best.screen_y:.0f}) score={best.score:.3f}")

# ── Step 2: 定位花的游戏坐标，找最近传送点 ──
print("\n=== Step 2: 定位花的游戏坐标 ===")
pg = PositionGetter(ctx)

# SIFT 定位当前视口
viewport = pg.get_position_from_big_map(frame)
zoom = mc.measure_zoom_level(frame)
print(f"当前视口: {viewport}, 缩放: {zoom}")

target_tp = None
game_pos = None  # 花的游戏坐标（SIFT 成功时设置）
if viewport is not None and zoom is not None:
    # 算花的游戏坐标
    game_pos = mc.screen_to_game(best.screen_x, best.screen_y, viewport, zoom)
    print(f"花的游戏坐标: ({game_pos[0]:.1f}, {game_pos[1]:.1f})")

    # 找最近传送点
    db = TpDatabase()
    nearest = db.find_nearest(game_pos[0], game_pos[1], n=3)
    for i, tp in enumerate(nearest):
        dist = math.hypot(tp.x - game_pos[0], tp.y - game_pos[1])
        print(f"  #{i}: {tp.name} ({tp.x:.0f}, {tp.y:.0f}) type={tp.type} dist={dist:.0f}")
    target_tp = nearest[0] if nearest else None
else:
    print("SIFT 定位失败，拖地图让花到中心后重试...")
    if zoom is None:
        zoom = 3.0
    north_delta = (best.screen_y - 540) * zoom / 3.57
    west_delta = (best.screen_x - 960) * zoom / 3.57
    print(f"  拖拽: north={north_delta:.0f}, west={west_delta:.0f}")
    mc.drag_map(north_delta, west_delta, zoom)
    time.sleep(0.5)
    frame = ctx.capture()

    # 重试 SIFT
    viewport = pg.get_position_from_big_map(frame)
    zoom = mc.measure_zoom_level(frame)
    print(f"  重试 SIFT: viewport={viewport}, zoom={zoom}")

    if viewport is not None and zoom is not None:
        game_pos = mc.screen_to_game(best.screen_x, best.screen_y, viewport, zoom)
        print(f"  花的游戏坐标: ({game_pos[0]:.1f}, {game_pos[1]:.1f})")
        db = TpDatabase()
        nearest = db.find_nearest(game_pos[0], game_pos[1], n=3)
        target_tp = nearest[0] if nearest else None

if target_tp is not None:
    print(f"\n目标传送点: {target_tp.name} ({target_tp.x:.0f}, {target_tp.y:.0f}) type={target_tp.type}")

    # 关闭地图，用 Teleporter 传送到传送点坐标
    ctx.press(KeyCode.m)
    time.sleep(1)
    g._update_scene()

    # ── Step 3: 传送 ──
    print(f"\n=== Step 3: 传送到 {target_tp.name} ===")
    try:
        teleporter = Teleporter(ctx, g)
        result = teleporter.teleport_to((target_tp.x, target_tp.y))
        print(f"传送完成！到达坐标: ({result[0]:.0f}, {result[1]:.0f})")
    except Exception as e:
        print(f"传送失败: {e}")
        print("尝试继续测试...")
else:
    # 无法定位花的游戏坐标 → 放大找传送点图标，直接在地图上操作
    print("\n无法定位花坐标，放大找传送点图标...")
    mc.set_zoom_level(DISPLAY_TP_ZOOM, frame)
    time.sleep(0.5)
    frame = ctx.capture()
    icons = mc.find_tp_icons("TeleportWaypoint", frame) + mc.find_tp_icons("Goddess", frame)
    if not icons:
        print("  未找到传送点图标！关闭地图退出。")
        ctx.press(KeyCode.m)
        sys.exit(1)
    # 选距视口中心最近的图标
    best_icon = min(icons, key=lambda ic: math.hypot(ic.cx - 960, ic.cy - 540))
    print(f"  找到传送点图标 @({best_icon.cx:.0f}, {best_icon.cy:.0f})")
    # 点击并确认传送（处理标记面板）
    from abilities.tp_panel import detect_tp_panel, TeleportPanelKind, find_teleport_button, close_marker_panel
    teleported = False
    for icon in icons[:3]:
        g.click(icon.cx, icon.cy)
        time.sleep(1)
        for _ in range(10):
            frame = ctx.capture()
            kind = detect_tp_panel(ctx, frame)
            if kind is TeleportPanelKind.TELEPORT:
                btn = find_teleport_button(ctx, frame)
                if btn:
                    g.click(btn.cx, btn.cy)
                else:
                    ctx.press(KeyCode.f)
                teleported = True
                break
            if kind is TeleportPanelKind.MARKER:
                print("  命中标记面板，Esc 关闭后换下一个图标")
                close_marker_panel(ctx)
                break
            time.sleep(0.3)
        if teleported:
            break
    # 等传送完成
    g.wait_main_ui(timeout=60)

# 等场景稳定
time.sleep(2)
g._update_scene()
print(f"当前场景: {g.scene.scene.name}")

# 提前初始化搜索状态（Step 3.5 可能设置 found_flower）
found_enemy = False
found_flower = False

# ── Step 3.5: 如果有花的游戏坐标，先朝花方向走 ──
# 传送后可能离花 70+ 单位，需要先走过去再搜索
if target_tp is not None and game_pos is not None:
    print(f"\n=== Step 3.5: 朝花方向走 ===")
    from abilities.navigation.position import PositionGetter as _PG
    from abilities.navigation.camera import CameraControl

    walk_pg = _PG(ctx)
    cam = CameraControl(ctx)

    # 获取当前位置
    cur_pos = walk_pg.get_position()
    if cur_pos is not None:
        dist = math.hypot(game_pos[0] - cur_pos[0], game_pos[1] - cur_pos[1])
        print(f"当前位置: ({cur_pos[0]:.0f}, {cur_pos[1]:.0f}), 距花: {dist:.0f}")

        if dist > 30:
            # 先闭环转向花方向（rotate_to 内含轻推 W 同步面朝）
            target_angle = CameraControl.target_orientation(cur_pos, game_pos)
            print(f"  目标朝向: {target_angle}°")
            ok = cam.rotate_to(target_angle, max_diff=5.0)
            print(f"  转向结果: {'成功' if ok else '失败'}")
            time.sleep(0.3)

            # 走向花（每步走 3 秒，约 60 单位）
            walk_steps = max(1, int(dist / 60))
            for step in range(min(walk_steps, 5)):
                ctx.ic.keyDown(KeyCode.w)
                time.sleep(3)
                ctx.ic.keyUp(KeyCode.w)
                time.sleep(0.5)

                # 检查是否已到花附近
                frame = ctx.capture()
                if frame is not None:
                    if has_flower_f_icon(ctx, frame):
                        found_flower = True
                        print(f"  走了 {step+1} 步后发现花 F 图标！")
                        break
                    if has_chest_f_icon(ctx, frame):
                        print(f"  走了 {step+1} 步后发现宝箱 F 图标！")
                        break

                # 更新位置和朝向
                new_pos = walk_pg.get_position()
                if new_pos is not None:
                    new_dist = math.hypot(game_pos[0] - new_pos[0], game_pos[1] - new_pos[1])
                    print(f"  走了 {step+1} 步, 剩余距离: {new_dist:.0f}")
                    if new_dist < 30:
                        print("  已到达花附近！")
                        break
                    # 重新转向
                    new_angle = CameraControl.target_orientation(new_pos, game_pos)
                    cam.rotate_to(new_angle, max_diff=10.0)  # 行走中粗调即可
                    time.sleep(0.3)
    else:
        print("  无法获取当前位置，跳过定向行走")

# ── Step 4: 索敌 + 找花 ──
print(f"\n=== Step 4: 搜索敌人/地脉花 (超时 {SEEK_TIMEOUT}s) ===")
from abilities.fighter import SimpleFighter

fighter = SimpleFighter(ctx, g)

seek_deadline = time.time() + SEEK_TIMEOUT
found_enemy = False

has_enemy = fighter.has_enemy()
has_world_enemy = fighter.has_enemy_in_world()
print(f"血条检测: {has_enemy}, 世界敌人检测: {has_world_enemy}")

if has_enemy or has_world_enemy:
    found_enemy = True
    print("视野内已有敌人！")

frame = ctx.capture()
if frame is not None:
    if has_flower_f_icon(ctx, frame):
        found_flower = True
        print("视野内已有地脉花 F 图标！")

if not found_enemy and not found_flower:
    print("未发现目标，旋转视角搜索...")
    turn_count = 0
    while time.time() < seek_deadline:
        ctx.move_by_rel(0, 600)
        time.sleep(1.5)

        if fighter.has_enemy():
            found_enemy = True
            print(f"  发现敌人！(转了 {turn_count} 次)")
            break
        if fighter.has_enemy_in_world():
            found_enemy = True
            print(f"  发现世界敌人！(转了 {turn_count} 次)")
            break

        frame = ctx.capture()
        if frame is not None and has_flower_f_icon(ctx, frame):
            found_flower = True
            print(f"  发现地脉花 F 图标！(转了 {turn_count} 次)")
            break

        turn_count += 1
        if turn_count >= 6:
            print("  转了一圈没找到，走几步...")
            ctx.ic.keyDown(KeyCode.w)
            time.sleep(WALK_SECS_PER_STEP)
            ctx.ic.keyUp(KeyCode.w)
            time.sleep(1)
            turn_count = 0

if not found_enemy and not found_flower:
    print("未找到敌人或花。")

# ── Step 5: 如果有花，走过去触发战斗 ──
if found_flower and not found_enemy:
    print("\n=== Step 5: 走向花触发战斗 ===")
    ctx.press(KeyCode.f)
    time.sleep(1)
    # 等敌人出现
    wait_deadline = time.time() + 15
    while time.time() < wait_deadline:
        if fighter.has_enemy() or fighter.has_enemy_in_world():
            found_enemy = True
            print("  花的敌人出现了！")
            break
        time.sleep(0.5)

# ── Step 6: 战斗 ──
cleared = False
if found_enemy:
    print(f"\n=== Step 6: 战斗 (超时 {FIGHT_TIMEOUT}s) ===")
    try:
        cleared = fighter.fight_until_clear(timeout=FIGHT_TIMEOUT)
        print("战斗清场完成！" if cleared else "战斗超时，未完全清场。")
    except Exception as e:
        print(f"战斗异常: {e}")
        ctx.release_all_keys()
else:
    print("\n跳过战斗（无敌人）")

# ── Step 7: 拾取 ──
print(f"\n=== Step 7: 拾取掉落 (超时 {PICK_TIMEOUT}s) ===")
picked = fighter.pick_drops(timeout=PICK_TIMEOUT)
print(f"拾取完成，按 F {picked} 次")

frame = ctx.capture()
if frame is not None:
    if has_flower_f_icon(ctx, frame):
        print("  仍有花 F 图标 → 按 F")
        ctx.press(KeyCode.f)
        time.sleep(1)
    if has_chest_f_icon(ctx, frame):
        print("  仍有宝箱 F 图标 → 按 F")
        ctx.press(KeyCode.f)
        time.sleep(1)

# ── 完成 ──
print("\n=== 测试完成 ===")
print(f"发现敌人: {'是' if found_enemy else '否'}")
print(f"发现花: {'是' if found_flower else '否'}")
print(f"战斗清场: {'是' if cleared else '否'}")
print(f"拾取次数: {picked}")

try:
    from PIL import Image
    import numpy as np
    frame = ctx.capture()
    if frame:
        raw = frame.to_bytes()
        arr = np.frombuffer(raw, dtype=np.uint8).reshape(frame.height, frame.width, 4)
        img = Image.fromarray(arr[:, :, :3], "RGB")
        img.save("debug_fight_test_result.png")
        print("已保存 debug_fight_test_result.png")
except Exception as e:
    print(f"保存截图失败: {e}")

ctx.close()
