"""实机探针（分步）：当前画面状态 —— capture 尺寸/场景/OCR 面板 ROI。

用法：py -3.12 diag/diag_step.py
"""

from __future__ import annotations

import sys

sys.path.insert(0, "src")

import numpy as np
from PIL import Image

from framework.context import GameContext
from framework.scene import SceneClassifier


def save(ctx, buf, name: str) -> None:
    raw = buf.to_bytes()
    arr = np.frombuffer(raw, dtype=np.uint8).reshape(buf.height, buf.width, 4)
    Image.fromarray(arr[:, :, :3], "RGB").save(name)
    print(f"saved {name}")


ctx = GameContext()
ctx.ensure_foreground()
buf = ctx.capture()
if buf is None:
    print("ERROR: capture failed")
    sys.exit(1)
print(f"capture size: {buf.width}x{buf.height}")
save(ctx, buf, "debug/diag_step_now.png")

from framework.scene import classify_scene

state = classify_scene(buf)
print(f"scene: {state}")

# 几何映射体检：1080p(1689,1007) → 物理
print(f"to_screen(1689,1007) = {ctx.to_screen(1689, 1007)}")
print(f"to_screen(960,540)   = {ctx.to_screen(960, 540)}")
