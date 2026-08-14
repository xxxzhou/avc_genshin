"""实机诊断：真实传送链到爆炎树坐标（auto_boss 卡点 tp.navigate 复现+验证）。

走 g.teleport_to((北,西)) 全链（开图→导航拖拽→点图标→OCR 确认→落点锚定）。
用法：python diag/diag_tp_boss.py [x y]（默认爆炎树 235.1 837.5）
"""

from __future__ import annotations

import sys
import time

sys.path.insert(0, "src")

import cv2
import numpy as np


def snap(ctx, name: str) -> None:
    buf = ctx.capture()
    if buf is None:
        return
    raw = buf.to_bytes()
    arr = np.frombuffer(raw, dtype=np.uint8).reshape((buf.height, buf.width, 4))
    cv2.imwrite(f"debug/tp_boss/{name}.png", cv2.cvtColor(arr, cv2.COLOR_RGBA2BGR))


def run(ctx, g, tx: float, ty: float) -> dict:
    t0 = time.time()
    snap(ctx, "00_before")
    try:
        landed = g.teleport_to((tx, ty))
    except Exception as e:
        snap(ctx, "99_fail")
        return {"error": repr(e), "dt": round(time.time() - t0, 1)}
    snap(ctx, "01_after")
    ok = g.wait_main_ui(timeout=30)
    return {"landed": landed, "main_ui": ok, "dt": round(time.time() - t0, 1)}


if __name__ == "__main__":
    from framework.runtime import Runtime

    tx = float(sys.argv[1]) if len(sys.argv) > 1 else 235.1
    ty = float(sys.argv[2]) if len(sys.argv) > 2 else 837.5

    rt = Runtime()
    try:
        result = rt.run_callable(
            lambda ctx, g: run(ctx, g, tx, ty), task_name="diag_tp_boss", timeout=300
        )
        print(f"\n[result] {result}", file=sys.stderr)
    except Exception as e:
        print(f"\n[tpb] 异常退出: {e!r}", file=sys.stderr)
    finally:
        rt.shutdown()
