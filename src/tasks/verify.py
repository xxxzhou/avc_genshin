"""verify —— 游戏内诊断任务（供实机验证/标定）。

进游戏后跑一遍各能力，逐项打印结果，一眼看出"哪些 OK / 哪些该调参"。
**默认只读**（不点击/不移动/不战斗）；``do_teleport=True`` 才会真传送（会移动角色）。

用法：``python main.py --task verify``（默认只读）
     ``python main.py --task verify do_teleport=true waypoint=七天神像-风``（测传送链）
     ``python main.py --task verify do_map_calib=true``（大地图标定：先按 M 开图，会缩放/拖动地图）
     ``python main.py --task verify --window 计算器``（在任意窗口上验证截图链路）
"""

from __future__ import annotations

import sys
import time

from framework import task
from framework.status import StatusLine


@task(
    name="verify",
    desc="游戏内诊断：逐项探测 截图/场景/定位/朝向/敌人/Q/OCR 并打印，供实机标定。默认只读。",
    daemons=["frame", "scene_estimator"],
    params={
        "waypoint": {
            "type": "str",
            "default": "七天神像-风",
            "desc": "按名传送测试目标（须在 tp.json 中，如 七天神像-风/北风之狼的庙宇）",
        },
        "do_teleport": {
            "type": "bool",
            "default": False,
            "desc": "是否真传送（会移动角色；默认只验证名字可查）",
        },
        "do_ocr": {
            "type": "bool",
            "default": True,
            "desc": "是否跑 OCR 探测（依赖 avc_ocr 插件 + 模型）",
        },
        "do_map_calib": {
            "type": "bool",
            "default": False,
            "desc": "大地图标定：SIFT 定位/zoom 测量/滚轮方向/拖拽方向 DPI/图标匹配/轴对齐。"
            "需先按 M 打开大地图；会缩放/拖动地图（非只读）",
        },
        "probe": {
            "type": "str",
            "default": "",
            "desc": "额外探针：'timeline' = 运行结束回放本次 Observe 时间线（按 ability 分组），"
            "验证可观测性事件流是否正常落地",
        },
    },
    tags=["diag"],
)
def main(ctx, g, waypoint: str = "七天神像-风", do_teleport: bool = False, do_ocr: bool = True, do_map_calib: bool = False, probe: str = "") -> dict:
    """逐项探测并打印。返回 ``{"results": {探测名: 结果串}}``。"""
    results: dict[str, str] = {}
    status = StatusLine()

    def _probe(name: str, fn) -> None:
        """跑一个探测：异常也记录（ERR ...），不中断整体。"""
        status.show(f"[verify] {name} ...")
        try:
            v = fn()
            results[name] = f"OK  {v!r}"
        except Exception as e:  # noqa: BLE001 — 诊断任务要吞掉所有异常逐个报告
            results[name] = f"ERR {type(e).__name__}: {e}"
        tag = "OK" if results[name].startswith("OK") else "ERR"
        status.show(f"[verify] {name}: {tag}")

    # ── 1. 截图基础 ──
    _probe("capture", lambda: _capture_info(ctx))

    # ── 2. 截图速率 ──
    def _fps():
        N = 10
        t0 = time.perf_counter()
        for _ in range(N):
            ctx.capture()
        t1 = time.perf_counter()
        return f"{N / (t1 - t0):.1f} fps ({N} frames in {t1 - t0:.2f}s)"
    _probe("capture_fps", _fps)

    # ── 3. SourcePlayer 状态 ──
    _probe("source_player", lambda: "active" if ctx._player is not None else "fallback (IScreenCapture)")

    # ── 4. 场景检测 ──
    def _scene_detect():
        from abilities import game_state as gs
        frame = ctx.capture()
        if frame is None:
            return "capture None"
        checks = {
            "paimon_menu": gs.has_paimon_menu,
            "in_domain": gs.has_in_domain,
            "disabled_ui": gs.has_disabled_ui_btn,
            "map_scale_btn": gs.has_map_scale_btn,
            "map_settings_btn": gs.has_map_settings_btn,
            "map_close_btn": gs.has_map_close_btn,
        }
        found = []
        for name, fn in checks.items():
            try:
                if fn(ctx, frame):
                    found.append(name)
            except Exception as e:
                found.append(f"{name}(ERR:{e})")
        return found if found else "none detected"
    _probe("scene_detect", _scene_detect)

    # ── 5. 场景分类器 ──
    _probe("scene", lambda: g.scene.scene.name if g.scene and g.scene.scene else None)
    _probe("is_loading", lambda: g.is_loading())

    # ── 6. 传送（默认只查名，不真传）──
    def _tp_lookup():
        from abilities.navigation.tp import TpDatabase
        p = TpDatabase().find_by_name(waypoint)
        return f"{waypoint!r} → {p.name if p else '未找到（名字不在 tp.json / 非 Teyvat）'}"
    _probe("tp_lookup", _tp_lookup)
    if do_teleport:
        # 传送涉及鼠标/键盘操作，先确保原神窗口在前台（终端/浏览器在前台则操作落空）
        try:
            ctx.sc.activateWindow("原神")
        except Exception:
            pass
        time.sleep(0.3)
        _probe("teleport_to", lambda: g.teleport_to(waypoint))
        _probe("after_tp_wait_main_ui", lambda: g.wait_main_ui(timeout=30))

    # ── 7. 定位 ──
    def _position():
        from abilities.navigation.position import PositionGetter
        from framework.scene import Scene
        from avc._core import KeyCode
        pg = PositionGetter(ctx)
        # 全局匹配（无 prev_position；冷启动，地形自相似区域可能歧义）
        global_pos = pg.get_position()
        # SIFT 实测真值：确保大地图打开（以玩家为中心）→ SIFT → 关图回世界
        ctx.sc.activateWindow("原神")
        time.sleep(0.3)
        if g.scene is None or g.scene.scene is not Scene.MAP:
            ctx.ic.press(KeyCode.m)
            time.sleep(1.5)
            for _ in range(10):
                if g.scene and g.scene.scene is Scene.MAP:
                    break
                time.sleep(0.3)
        sift_truth = pg.get_position_from_big_map()
        ctx.ic.press(KeyCode.m)  # 关图
        if not g.wait_main_ui(timeout=10.0):
            time.sleep(1.5)
        else:
            time.sleep(0.5)
        if sift_truth is None:
            return f"global={global_pos}, sift=None(开图定位失败)"
        # 局部匹配（以 SIFT 真值为 prev_position，验证小地图局部锚定精度）
        pg.set_prev_position(*sift_truth)
        local_pos = pg.get_position()
        if local_pos is None:
            return f"global={global_pos}, sift={tuple(round(v, 1) for v in sift_truth)}, local=None"
        dx = round(local_pos[0] - sift_truth[0], 1)
        dy = round(local_pos[1] - sift_truth[1], 1)
        dist = round((dx * dx + dy * dy) ** 0.5)
        return (f"global={global_pos}, sift={tuple(round(v, 1) for v in sift_truth)}, "
                f"local={tuple(round(v, 1) for v in local_pos)} dist={dist}")
    _probe("position", _position)

    # ── 8. 朝向 ──
    def _orientation():
        from abilities.navigation.camera import CameraControl
        return CameraControl(ctx).get_orientation()
    _probe("orientation", _orientation)

    # ── 9. 敌人/血条 ──
    _probe("has_enemy", lambda: g.has_enemy())
    _probe(
        "nearest_enemy",
        lambda: (lambda r: None if r is None else f"({r.x},{r.y},{r.w},{r.h})")(
            g.find_nearest_enemy()
        ),
    )

    # ── 10. Q 就绪 ──
    _probe("is_q_ready", lambda: g.is_q_ready())

    # ── 11. OCR ──
    if do_ocr:
        def _ocr_boxes():
            ocr = getattr(ctx, "ocr", None)
            if ocr is None:
                return "no avc_ocr（Vision.createTextRecognizer 返回 None）"
            frame = ctx.capture()
            if frame is None:
                return "capture None"
            n = ocr.recognize(frame)
            texts = []
            for i in range(n):
                t, r = ocr.getMatch(i)
                texts.append(f"{t!r}@({r.x:.0f},{r.y:.0f})")
            return f"{n} 个文字框: {', '.join(texts[:10])}"
        _probe("ocr_boxes", _ocr_boxes)

    # ── 12. 大地图标定（do_map_calib；需先按 M 打开大地图，会缩放/拖动地图）──
    # 逐项回填 map_ops.py 标定常量：_ZOOM_WHEEL_SIGN / _DRAG_X_SIGN / _DRAG_Y_SIGN /
    # _DRAG_DPI_MULT / _ZOOM_PER_NOTCH / 旋钮 Y→zoom 斜率 / SIFT 精度耗时 / 坐标轴对齐。
    if do_map_calib:
        # 自动按 M 打开大地图（若当前不在 MAP 场景）
        from framework.scene import Scene

        # 标定操作需要窗口在前台
        ctx.sc.activateWindow("原神")
        time.sleep(0.3)

        if g.scene is None or g.scene.scene is not Scene.MAP:
            print("[calib] 当前不在大地图，激活窗口并按 M 开图...")
            ctx.sc.activateWindow("原神")
            time.sleep(0.3)
            from avc._core import KeyCode
            ctx.ic.press(KeyCode.m)
            time.sleep(1.5)
            for _ in range(10):
                if g.scene and g.scene.scene is Scene.MAP:
                    break
                time.sleep(0.3)
            else:
                results["calib_open_map"] = "ERR 按 M 后仍未进入 MAP 场景"
        if g.scene and g.scene.scene is Scene.MAP:
            results["calib_open_map"] = "OK  已进入 MAP 场景"

        def _require_map(fn):
            """每项探测调用时确认仍在 Scene.MAP（不在则记 ERR）。"""

            def wrapped():
                if g.scene is None or g.scene.scene is not Scene.MAP:
                    return "ERR not in MAP"
                return fn()

            return wrapped

        # 12a. SIFT 大图视口重定位 —— 精度 + 耗时（回填 SIFT 阈值/性能预期）
        def _bigmap_sift():
            from abilities.navigation.position import PositionGetter

            pg = PositionGetter(ctx)
            t0 = time.perf_counter()
            pos = pg.get_position_from_big_map()
            dt = time.perf_counter() - t0
            return f"pos={pos}, {dt * 1000:.0f}ms"
        _probe("calib_bigmap_sift", _require_map(_bigmap_sift))

        # 12b. 缩放等级测量 —— 旋钮 Y → zoom_level 实际值（回填 _ZOOM_START_Y/END_Y 斜率）
        def _zoom_measure():
            from abilities.navigation.map_ops import MapController

            return f"zoom_level={MapController(ctx, g).measure_zoom_level()}"
        _probe("calib_zoom_measure", _require_map(_zoom_measure))

        # 12c. 滚轮缩放方向/步长 —— scroll dy 前后 zoom 差 → 推断 _ZOOM_WHEEL_SIGN / _ZOOM_PER_NOTCH
        def _scroll_zoom():
            from abilities.navigation.map_ops import MapController

            mc = MapController(ctx, g)
            z0 = mc.measure_zoom_level()
            # avc scroll(0, dy) 的 dy 是滚轮格数（内部 dy*WHEEL_DELTA），不是 WHEEL_DELTA 单位
            # 测试：1格×5次、3格×3次、-3格×3次
            parts = [f"zoom0={z0}"]
            for label, dy, count in [("dy=+1×5", 1, 5), ("dy=+3×3", 3, 3), ("dy=-3×3", -3, 3)]:
                for _ in range(count):
                    ctx.ic.scroll(0, dy)
                    time.sleep(0.15)
                z = mc.measure_zoom_level()
                delta = None if z0 is None or z is None else round(z - z0, 3)
                parts.append(f"{label}: zoom={z} delta={delta}")
                z0 = z
            return "; ".join(parts)
        _probe("calib_scroll_zoom", _require_map(_scroll_zoom))

        # 12d. 拖拽方向/DPI —— 拖已知向量前后 SIFT 位置差 → 验证 _DRAG_X_SIGN/_DRAG_Y_SIGN/_MAP_SCALE_FACTOR
        # ⚠ 约定（2026-08-08 修正）：drag_map(200,0)=北向+200，drag_map(0,200)=西向+200
        def _drag_probe():
            from abilities.navigation.map_ops import MapController
            from abilities.navigation.position import PositionGetter
            from framework.scene import Scene
            from avc._core import KeyCode

            pg = PositionGetter(ctx)
            mc = MapController(ctx, g)

            # 若视口定位不到（前次失败可能把地图停在海洋/未开放区）→ M 关/开图复位到玩家
            if pg.get_position_from_big_map() is None:
                ctx.sc.activateWindow("原神")
                time.sleep(0.3)
                ctx.ic.press(KeyCode.m)  # 关图
                time.sleep(0.6)
                ctx.ic.press(KeyCode.m)  # 重开（以玩家为中心）
                time.sleep(1.5)
                for _ in range(10):
                    if g.scene and g.scene.scene is Scene.MAP:
                        break
                    time.sleep(0.3)
                time.sleep(0.5)

            zoom = mc.measure_zoom_level() or 4.0
            # 先放大地图到 zoom≈4（传送点可见级别），避免拖太远 SIFT 丢失
            mc.set_zoom_level(4.0)
            time.sleep(0.3)
            zoom = mc.measure_zoom_level() or 4.0

            # 用较小的偏移量（200 游戏单位），避免拖出 SIFT 底图
            # X 方向
            p0 = pg.get_position_from_big_map()
            mc.drag_map(200.0, 0.0, zoom)
            time.sleep(0.3)
            p1 = pg.get_position_from_big_map()
            dx = None if p0 is None or p1 is None else round(p1[0] - p0[0], 1)
            # Y 方向（+200）
            mc.drag_map(0.0, 200.0, zoom)
            time.sleep(0.3)
            p2 = pg.get_position_from_big_map()
            dy_pos = None if p1 is None or p2 is None else round(p2[1] - p1[1], 1)
            # Y 方向（-200，反向测试）
            mc.drag_map(0.0, -200.0, zoom)
            time.sleep(0.3)
            p3 = pg.get_position_from_big_map()
            dy_neg = None if p2 is None or p3 is None else round(p3[1] - p2[1], 1)
            return (f"drag(north+200)@zoom{zoom:.2f}: Δnorth={dx}; "
                    f"drag(west+200): Δwest={dy_pos}; drag(west-200): Δwest={dy_neg}; "
                    f"pos: {p0}→{p1}→{p2}→{p3}")
        _probe("calib_drag", _require_map(_drag_probe))

        # 12d2. 原始 moveBy 诊断 —— 用小步长 moveBy 测试拖拽是否生效
        def _raw_moveby_probe():
            from abilities.navigation.position import PositionGetter

            pg = PositionGetter(ctx)
            ic = ctx.ic
            btn = ctx._MouseButton["left"]
            dpi = ctx._dpi_scale
            # 拖拽起点：视口中心 → 屏幕坐标
            sx, sy = ctx.to_screen(960, 540)

            # 激活窗口（确保前台）
            ctx.sc.activateWindow("原神")
            time.sleep(0.3)

            # 诊断：moveTo 后检查实际鼠标位置
            ic.moveTo(int(sx), int(sy))
            time.sleep(0.1)
            actual_pos = ic.getCursorPos()
            screen_bounds = ic.screenBounds()

            results_parts = [
                f"to_screen(960,540)=({sx},{sy})",
                f"sc.size={ctx.sc.width()}x{ctx.sc.height()}",
                f"border=({ctx._border_left},{ctx._border_top})",
                f"cursor_after_moveTo={actual_pos}",
                f"screen_bounds={screen_bounds}",
                f"dpi={dpi:.2f}",
            ]

            # 基线 SIFT
            p0 = pg.get_position_from_big_map()
            if p0 is None:
                return "; ".join(results_parts) + "; SIFT failed at baseline"
            results_parts.append(f"baseline_sift={p0}")

            # 测试1: moveTo → mouseDown → moveBy(50,0) → mouseUp（小步长，乘DPI）
            ic.moveTo(int(sx), int(sy))
            time.sleep(0.05)
            ic.mouseDown(btn)
            time.sleep(0.05)
            ic.moveBy(int(round(50 * dpi)), 0)
            time.sleep(0.05)
            cursor_during = ic.getCursorPos()
            ic.mouseUp(btn)
            time.sleep(0.5)
            p1 = pg.get_position_from_big_map()
            ddx = round(p1[0] - p0[0], 1) if p1 else None
            ddy = round(p1[1] - p0[1], 1) if p1 else None
            results_parts.append(f"drag_X+50buf*dpi: cursor={cursor_during}, sift_dx={ddx}, sift_dy={ddy}")

            # 测试2: moveTo → mouseDown → moveBy(50,0) → mouseUp（不乘DPI）
            ic.moveTo(int(sx), int(sy))
            time.sleep(0.05)
            ic.mouseDown(btn)
            time.sleep(0.05)
            ic.moveBy(50, 0)
            time.sleep(0.05)
            cursor_during2 = ic.getCursorPos()
            ic.mouseUp(btn)
            time.sleep(0.5)
            p2 = pg.get_position_from_big_map()
            ddx2 = round(p2[0] - (p1[0] if p1 else p0[0]), 1) if p2 else None
            ddy2 = round(p2[1] - (p1[1] if p1 else p0[1]), 1) if p2 else None
            results_parts.append(f"drag_X+50nodpi: cursor={cursor_during2}, sift_dx={ddx2}, sift_dy={ddy2}")

            # 测试3: 用 avc drag() API 代替手动 mouseDown/moveBy/mouseUp
            # drag(x1,y1,x2,y2) 从 (sx,sy) 拖到 (sx+125, sy)（屏幕像素125=50buf*dpi）
            ic.drag(int(sx), int(sy), int(sx + 125), int(sy))
            time.sleep(0.5)
            p3 = pg.get_position_from_big_map()
            prev_x = p2[0] if p2 else (p1[0] if p1 else p0[0])
            prev_y = p2[1] if p2 else (p1[1] if p1 else p0[1])
            ddx3 = round(p3[0] - prev_x, 1) if p3 else None
            ddy3 = round(p3[1] - prev_y, 1) if p3 else None
            results_parts.append(f"drag_api(125px): sift_dx={ddx3}, sift_dy={ddy3}")

            return "; ".join(results_parts)
        _probe("calib_raw_moveby", _require_map(_raw_moveby_probe))

        # 12e. 传送点图标匹配 —— 当前视口可见图标（验证模板/阈值就绪）
        def _tp_icon_match():
            from abilities.navigation.map_ops import MapController

            mc = MapController(ctx, g)
            frame = ctx.capture()
            out = {}
            for t in ("TeleportWaypoint", "Goddess", "OneTimeDomain"):
                r = mc.find_tp_icon(t, frame)
                if r is not None:
                    out[t] = (round(r.cx), round(r.cy))
            return out if out else "no icon matched（视口内无传送点或需放大）"
        _probe("calib_tp_icon", _require_map(_tp_icon_match))

        # 12f. 坐标轴对齐 —— SIFT 定位 vs 最近 tp.json 点（人工核对轴/符号一致性）
        def _axis_check():
            import math as _m

            from abilities.navigation.position import PositionGetter
            from abilities.navigation.tp import TpDatabase

            pg = PositionGetter(ctx)
            pos = pg.get_position_from_big_map()
            if pos is None:
                return "SIFT 定位失败，无法比对"
            near = TpDatabase().find_nearest(pos[0], pos[1], "Teyvat", n=1)
            if not near:
                return f"pos={pos}, tp.json 无点"
            p = near[0]
            d = _m.hypot(p.x - pos[0], p.y - pos[1])
            return f"sift={pos}, nearest={p.name!r}@({p.x},{p.y}) dist={d:.0f}"
        _probe("calib_axis_check", _require_map(_axis_check))

        # 12g. zoom 扫描 SIFT 定位 —— 在不同缩放档测定位稳定性（确定"如何确定正确位置"）
        def _zoom_sift_scan():
            from abilities.navigation.map_ops import MapController
            from abilities.navigation.position import PositionGetter

            mc = MapController(ctx, g)
            pg = PositionGetter(ctx)
            parts = []
            for target_z in (2.5, 3.5, 4.4, 5.5):
                mc.set_zoom_level(target_z)
                time.sleep(0.3)
                z = mc.measure_zoom_level()
                if z is None:
                    parts.append(f"z{target_z}: zoom 测不到")
                    continue
                t0 = time.perf_counter()
                pos = pg.get_position_from_big_map()
                dt = time.perf_counter() - t0
                parts.append(f"z{z:.2f}: pos={pos} ({dt * 1000:.0f}ms)")
            return "; ".join(parts)
        _probe("calib_zoom_sift", _require_map(_zoom_sift_scan))

    # ── 打印 + 返回 ──
    ok_count = sum(1 for v in results.values() if v.startswith("OK"))
    err_count = len(results) - ok_count
    status.show(f"[verify] 完成: {ok_count} OK / {err_count} ERR / {len(results)} total")
    # 详细结果写 stderr（不占状态行），供事后查看
    for k, v in results.items():
        print(f"  {k:24s} {v}", file=sys.stderr)

    # ── probe=timeline：回放本次 Observe 时间线（按 ability 分组）──
    if "timeline" in probe:
        from framework.report import summarize, summary_text

        timeline = ctx.observe.timeline()
        summary = summarize(timeline)
        print("\n[verify] Observe 时间线摘要（按 ability 分组）：", file=sys.stderr)
        print(summary_text(summary), file=sys.stderr)
        print(f"[verify] 共 {len(timeline)} 条事件；最近 30 条：", file=sys.stderr)
        for e in timeline[-30:]:
            print(f"  {str(e.get('event')):22s} ability={e.get('ability')} "
                  f"ok={e.get('ok')} reason={e.get('reason')}", file=sys.stderr)

    status.finish()
    return {"results": results}


def _capture_info(ctx) -> str:
    """截图信息：尺寸 + SourcePlayer 状态。"""
    frame = ctx.capture()
    if frame is None:
        return "capture returned None"
    w, h = frame.width, frame.height
    player = "SourcePlayer" if ctx._player is not None else "IScreenCapture"
    return f"{w}x{h} via {player}"
