"""verify —— 游戏内诊断任务（供实机验证/标定）。

进游戏后跑一遍各能力，逐项打印结果，一眼看出"哪些 OK / 哪些该调参"。
**默认只读**（不点击/不移动/不战斗）；``do_teleport=True`` 才会真传送（会移动角色）。

用法：``python main.py --task verify``（默认只读）
     ``python main.py --task verify do_teleport=true waypoint=七天神像-风``（测传送链）
     ``python main.py --task verify --window 计算器``（在任意窗口上验证截图链路）
"""

from __future__ import annotations

import time

from framework import task


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
    },
    tags=["diag"],
)
def main(ctx, g, waypoint: str = "七天神像-风", do_teleport: bool = False, do_ocr: bool = True) -> dict:
    """逐项探测并打印。返回 ``{"results": {探测名: 结果串}}``。"""
    results: dict[str, str] = {}

    def probe(name: str, fn) -> None:
        """跑一个探测：异常也记录（ERR ...），不中断整体。"""
        try:
            v = fn()
            results[name] = f"OK  {v!r}"
        except Exception as e:  # noqa: BLE001 — 诊断任务要吞掉所有异常逐个报告
            results[name] = f"ERR {type(e).__name__}: {e}"

    # ── 1. 截图基础 ──
    probe("capture", lambda: _capture_info(ctx))

    # ── 2. 截图速率 ──
    def _fps():
        N = 10
        t0 = time.perf_counter()
        for _ in range(N):
            ctx.capture()
        t1 = time.perf_counter()
        return f"{N / (t1 - t0):.1f} fps ({N} frames in {t1 - t0:.2f}s)"
    probe("capture_fps", _fps)

    # ── 3. SourcePlayer 状态 ──
    probe("source_player", lambda: "active" if ctx._player is not None else "fallback (IScreenCapture)")

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
    probe("scene_detect", _scene_detect)

    # ── 5. 场景分类器 ──
    probe("scene", lambda: g.scene.scene.name if g.scene and g.scene.scene else None)
    probe("is_loading", lambda: g.is_loading())

    # ── 6. 传送（默认只查名，不真传）──
    def _tp_lookup():
        from abilities.navigation.tp import TpDatabase
        p = TpDatabase().find_by_name(waypoint)
        return f"{waypoint!r} → {p.name if p else '未找到（名字不在 tp.json / 非 Teyvat）'}"
    probe("tp_lookup", _tp_lookup)
    if do_teleport:
        probe("teleport_to", lambda: g.teleport_to(waypoint))
        probe("after_tp_wait_main_ui", lambda: g.wait_main_ui(timeout=30))

    # ── 7. 定位 ──
    def _position():
        from abilities.navigation.position import PositionGetter
        pg = PositionGetter(ctx)
        # 全局匹配（无 prev_position）
        global_pos = pg.get_position()
        # 局部匹配（用传送锚点坐标做 prev_position）
        from abilities.navigation.tp import TpDatabase
        anchor = TpDatabase().find_by_name(waypoint)
        if anchor is not None:
            pg.set_prev_position(anchor.tran_x, anchor.tran_y)
            local_pos = pg.get_position()
            return f"global={global_pos}, local(prev={waypoint})={local_pos}"
        return f"global={global_pos}, no anchor for local"
    probe("position", _position)

    # ── 8. 朝向 ──
    def _orientation():
        from abilities.navigation.camera import CameraControl
        return CameraControl(ctx).get_orientation()
    probe("orientation", _orientation)

    # ── 9. 敌人/血条 ──
    probe("has_enemy", lambda: g.has_enemy())
    probe(
        "nearest_enemy",
        lambda: (lambda r: None if r is None else f"({r.x},{r.y},{r.w},{r.h})")(
            g.find_nearest_enemy()
        ),
    )

    # ── 10. Q 就绪 ──
    probe("is_q_ready", lambda: g.is_q_ready())

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
        probe("ocr_boxes", _ocr_boxes)

    # ── 打印 + 返回 ──
    print("\n===== avc_genshin 实机诊断 =====")
    for k, v in results.items():
        print(f"  {k:24s} {v}")
    print("==============================")
    return {"results": results}


def _capture_info(ctx) -> str:
    """截图信息：尺寸 + SourcePlayer 状态。"""
    frame = ctx.capture()
    if frame is None:
        return "capture returned None"
    w, h = frame.width, frame.height
    player = "SourcePlayer" if ctx._player is not None else "IScreenCapture"
    return f"{w}x{h} via {player}"
