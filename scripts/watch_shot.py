"""cron 监控截图：保存当前游戏画面到 debug/cron_watch.png。

供 5 分钟周期监控用：截图 → 主 agent 启动 subagent Read 分析。
走 GameContext（与正式任务同链路，确保 1080p 归一化 + buffer API 一致）。
"""
from __future__ import annotations

import sys
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, "src")

OUT = Path("debug/cron_watch.png")


def main() -> int:
    from framework.context import GameContext

    ctx = GameContext(window_title="原神")
    try:
        buf = ctx.capture()
        if buf is None:
            print("NO_FRAME")
            return 1
        raw = buf.to_bytes()
        w, h = buf.width, buf.height
        arr = np.frombuffer(raw, dtype=np.uint8).reshape((h, w, 4))
        bgr = cv2.cvtColor(arr, cv2.COLOR_RGBA2BGR)
        OUT.parent.mkdir(parents=True, exist_ok=True)
        cv2.imwrite(str(OUT), bgr)
        print(f"SAVED {OUT} ({w}x{h})")
        return 0
    finally:
        try:
            ctx.close()
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())
