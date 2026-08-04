"""阶段一基础链路原型（IMPLEMENTATION §10 阶段一）。

三个 probe，验证「截图 → 识别 → 操作」链路的各段：
    python main.py --proto capture [--window 原神]
    python main.py --proto vision  --window 原神 --template <模板路径或 res 名>
    python main.py --proto detect  --window 原神 --model bgi_world.onnx

均为只读探针（vision/detect 不点击），用于在真实游戏上验证 avc 接线与模型解码。
"""

from __future__ import annotations

import sys

from framework import utils
from framework.config import Config


def _make_ctx(window: str):
    from framework.context import GameContext

    return GameContext(window_title=window, cfg=Config.load())


def proto_capture(window: str) -> int:
    """截图并落盘到 debug/，打印尺寸/格式。验证 avc 截图链路。"""
    ctx = _make_ctx(window)
    buf = ctx.capture()
    if buf is None:
        print(f"[capture] ✗ 截图为空（窗口「{window}」是否在前台/可见？）", file=sys.stderr)
        return 1
    debug_path = utils.ensure_dir("debug") / "proto_capture.png"
    buf.save(str(debug_path))
    print(
        f"[capture] ✓ {buf.width}×{buf.height} imageType={_imagetype_name(buf.imageType)} "
        f"→ {debug_path}"
    )
    return 0


def proto_vision(window: str, template: str) -> int:
    """模板匹配探针：截图 → find_template → 打印命中框（不点击）。"""
    from abilities import vision_utils as vu

    ctx = _make_ctx(window)
    buf = ctx.capture()
    if buf is None:
        print("[vision] ✗ 截图为空", file=sys.stderr)
        return 1
    rect = vu.find_template(ctx, template, threshold=0.8, frame=buf)
    if rect is None:
        print(f"[vision] ✗ 未命中模板 {template!r}（阈值 0.8）")
        return 1
    sp = ctx.to_screen(rect.cx, rect.cy)
    print(
        f"[vision] ✓ 命中 {template!r}  buf=({rect.x},{rect.y},{rect.w}×{rect.h}) "
        f"score={rect.score:.3f} center=({rect.cx:.0f},{rect.cy:.0f}) → 屏幕{sp}"
    )
    return 0


def proto_detect(window: str, model: str) -> int:
    """YOLO 检测探针：截图 → GenshinDetector.detect → 打印各类检测数与 top 框。"""
    from abilities.detector import GenshinDetector
    from framework.resources import res

    model_path = res.model(model)
    if not model_path.exists():
        print(
            f"[detect] ✗ 模型不存在: {model_path}\n"
            f"  放到 resources/models/{model}，或设 BGI_ROOT 指向本地 BetterGI 仓库复用。",
            file=sys.stderr,
        )
        return 1

    ctx = _make_ctx(window)
    buf = ctx.capture()
    if buf is None:
        print("[detect] ✗ 截图为空", file=sys.stderr)
        return 1

    det = GenshinDetector(str(model_path))
    result = det.detect(buf)
    total = sum(len(v) for v in result.values())
    print(f"[detect] 模型={model} task={det.task} imgsz={det.imgsz} 类别={det.names}")
    if not result:
        print("[detect] ✓ 推理完成，未检测到目标")
        return 0
    print(f"[detect] ✓ 共 {total} 个目标，{len(result)} 类：")
    for name, dets in result.items():
        d = dets[0]
        print(
            f"  · {name}: {len(dets)} 个  top框=({d.x1},{d.y1})-({d.x2},{d.y2}) "
            f"score={d.score:.3f}"
        )
    return 0


def _imagetype_name(it) -> str:
    try:
        from avc._core import ImageType

        return ImageType(int(it)).name
    except Exception:
        return str(it)
