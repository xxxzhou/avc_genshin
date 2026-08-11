"""测 cam.rotate：转 90/180/270 度，看角色实际朝向变化。

诊断 navigator 走反的根因：cam.rotate 没真转 / 转错方向 / 朝向读取错。
"""
from __future__ import annotations

import sys
import time

sys.path.insert(0, "src")


def main() -> int:
    from abilities.navigation.camera import CameraControl
    from framework.context import GameContext

    ctx = GameContext(window_title="原神")
    cam = CameraControl(ctx)

    ctx.sc.activateWindow("原神")
    time.sleep(0.5)

    # 1. 当前朝向
    h0 = cam.get_orientation()
    print(f"[init] heading={h0:.1f}°")

    # 2. 转到 90 度（东）
    print("[1] rotate target=90")
    cam.rotate_to(90.0, max_attempts=10)
    time.sleep(0.5)
    h1 = cam.get_orientation()
    print(f"    actual={h1:.1f}°  diff={h1 - 90:.1f}")

    # 3. 转到 180 度（南）
    print("[2] rotate target=180")
    cam.rotate_to(180.0, max_attempts=10)
    time.sleep(0.5)
    h2 = cam.get_orientation()
    print(f"    actual={h2:.1f}°  diff={h2 - 180:.1f}")

    # 4. 转到 270 度（西）
    print("[3] rotate target=270")
    cam.rotate_to(270.0, max_attempts=10)
    time.sleep(0.5)
    h3 = cam.get_orientation()
    print(f"    actual={h3:.1f}°  diff={h3 - 270:.1f}")

    # 5. 转到 0 度（北）
    print("[4] rotate target=0")
    cam.rotate_to(0.0, max_attempts=10)
    time.sleep(0.5)
    h4 = cam.get_orientation()
    print(f"    actual={h4:.1f}°  diff={h4 - 0:.1f}")

    ctx.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
