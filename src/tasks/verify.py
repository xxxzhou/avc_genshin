"""verify —— 游戏内诊断任务（供实机验证/标定，Phase D 配套）。

进游戏后跑一遍各能力，逐项打印结果，一眼看出"哪些 OK / 哪些该调参"。
**默认只读**（不点击/不移动/不战斗）；``do_teleport=True`` 才会真传送（会移动角色）。

用法：``python main.py --task verify``（默认只读）
     ``python main.py --task verify do_teleport=true waypoint=七天神像-风``（测传送链）

每项探测独立 try/except：一项失败不中断其余，错误以 ``ERR 类型: 信息`` 记录，
方便在实机上看具体卡在哪。打印结果也写入任务返回（JSONL task_return）。
"""

from __future__ import annotations

from framework import task


@task(
    name="verify",
    desc="游戏内诊断：逐项探测 场景/传送/定位/朝向/敌人/Q/OCR 并打印，供实机标定。默认只读。",
    daemons=["frame", "scene_estimator"],
    requires=["navigation", "fighter"],
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
    from abilities.navigation.camera import CameraControl
    from abilities.navigation.position import PositionGetter
    from abilities.navigation.tp import TpDatabase

    results: dict[str, str] = {}

    def probe(name: str, fn) -> None:
        """跑一个探测：异常也记录（ERR ...），不中断整体。"""
        try:
            v = fn()
            results[name] = f"OK  {v!r}"
        except Exception as e:  # noqa: BLE001 — 诊断任务要吞掉所有异常逐个报告
            results[name] = f"ERR {type(e).__name__}: {e}"

    # ── 1. 场景 ──
    probe("scene", lambda: g.scene.scene.name if g.scene and g.scene.scene else None)
    probe("is_loading", lambda: g.is_loading())
    probe("wait_main_ui(10s)", lambda: g.wait_main_ui(timeout=10))

    # ── 2. 传送（默认只查名，不真传）──
    def _tp_lookup():
        p = TpDatabase().find_by_name(waypoint)
        return f"{waypoint!r} → {p.name if p else '未找到（名字不在 tp.json / 非 Teyvat）'}"
    probe("tp_lookup", _tp_lookup)
    if do_teleport:
        probe("teleport_to", lambda: g.teleport_to(waypoint))
        probe("after_tp_wait_main_ui", lambda: g.wait_main_ui(timeout=30))

    # ── 3. 定位（小地图 SIFT ↔ 256 地图）──
    probe("position", lambda: PositionGetter(ctx).get_position())

    # ── 4. 朝向（BGI 峰卷积；注意与 target_orientation 约定换算待标定）──
    probe("orientation", lambda: CameraControl(ctx).get_orientation())

    # ── 5. 敌人/血条（avc IColorDetector）──
    probe("has_enemy", lambda: g.has_enemy())
    probe(
        "nearest_enemy",
        lambda: (lambda r: None if r is None else f"({r.x},{r.y},{r.w},{r.h})")(
            g.find_nearest_enemy()
        ),
    )

    # ── 6. Q 就绪（q_classify 分类）──
    probe("is_q_ready", lambda: g.is_q_ready())

    # ── 7. OCR（avc_ocr；奖励领用靠它认文案）──
    if do_ocr:
        def _ocr_boxes():
            ocr = getattr(ctx, "ocr", None)
            if ocr is None:
                return "no avc_ocr（Vision.createTextRecognizer 返回 None）"
            frame = ctx.capture()
            if frame is None:
                return "capture None"
            return f"{ocr.recognize(frame)} 个文字框"
        probe("ocr_boxes", _ocr_boxes)

    # ── 打印 + 返回 ──
    print("\n===== avc_genshin 实机诊断 =====")
    for k, v in results.items():
        print(f"  {k:24s} {v}")
    print("==============================")
    return {"results": results}
