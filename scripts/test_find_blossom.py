"""独立测 find_blossom：开图 → 检测花图标 → SIFT 定位 → 关图。

绕过 MapController（它需要 runtime.loop），直接调底层 find_all_templates。
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, "src")

import cv2  # noqa: E402
import numpy as np  # noqa: E402


def main() -> int:
    from abilities.game_state import make_classifier
    from abilities.navigation.map_ops import _BLOSSOM_TEMPLATES, _BLOSSOM_THRESHOLD
    from abilities.navigation.position import PositionGetter
    from abilities import vision_utils as vu
    from avc._core import KeyCode
    from framework.context import GameContext
    from framework.scene import set_classifier

    ctx = GameContext(window_title="原神")
    set_classifier(make_classifier(ctx))

    ctx.sc.activateWindow("原神")
    time.sleep(0.3)
    print("[1/5] 按 M 开图")
    ctx.ic.press(KeyCode.m)
    time.sleep(2.5)

    frame = ctx.capture()
    if frame is None:
        print("NO_FRAME")
        return 1
    raw = frame.to_bytes()
    arr = np.frombuffer(raw, dtype=np.uint8).reshape((frame.height, frame.width, 4))
    bgr = cv2.cvtColor(arr, cv2.COLOR_RGBA2BGR)
    Path("debug").mkdir(exist_ok=True)
    cv2.imwrite("debug/test_find_blossom_map.png", bgr)
    print(f"[2/5] 地图截图保存 debug/test_find_blossom_map.png ({frame.width}x{frame.height})")

    # 测花检测（直接调 find_all_templates）
    paths = list(_BLOSSOM_TEMPLATES.values())
    print(f"   模板: {paths}, threshold={_BLOSSOM_THRESHOLD}")
    found = vu.find_all_templates(ctx, paths, threshold=_BLOSSOM_THRESHOLD, frame=frame)
    total = sum(len(v) for v in found.values())
    print(f"[3/5] find_all_templates: 找到 {total} 个匹配")
    for tpl_name, rects in found.items():
        for r in rects:
            print(f"   {tpl_name}: ({r.cx:.0f},{r.cy:.0f}) {r.w}x{r.h} score={r.score:.3f}")

    # 测 SIFT 定位
    pg = PositionGetter(ctx)
    pos = pg.get_position_from_big_map(frame)
    print(f"[4/5] SIFT 定位: {pos}")

    # 关图
    print("[5/5] 按 M 关图")
    ctx.ic.press(KeyCode.m)
    time.sleep(0.5)
    ctx.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
