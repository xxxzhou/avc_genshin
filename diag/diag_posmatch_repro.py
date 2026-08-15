"""离线复现走路段 pos.match 的 avc cv::crossCorr 崩溃。

用法: py -3.12 diag/diag_posmatch_repro.py <frame.png> [prev_x prev_y]
默认用 debug/r_20260815_083640/timeline/0001_15.8_main_ui.png + prev=(-653,267)。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from framework.observe import _NullObserve  # noqa: E402


class _StubCtx:
    observe = _NullObserve()


def main() -> None:
    frame_path = sys.argv[1] if len(sys.argv) > 1 else (
        "debug/r_20260815_083640/timeline/0001_15.8_main_ui.png")
    prev = (float(sys.argv[2]), float(sys.argv[3])) if len(sys.argv) > 3 else (-653.0, 267.0)

    from avc.image import loadImage
    from abilities.navigation import position as P

    frame = loadImage(frame_path)
    print(f"frame {frame.width}x{frame.height}, prev={prev}")

    pg = P.PositionGetter(_StubCtx())
    pg.set_prev_position(*prev)

    from avc import Image
    mini = Image.crop(frame, P.MINIMAP_X, P.MINIMAP_Y, P.MINIMAP_W, P.MINIMAP_H)
    print("minimap cropped:", mini.width, mini.height)

    # 先试不裁 156 直接过 _match（内部会裁）——与实机 nav.step 路径一致
    for i in range(3):
        r = pg._match(mini)
        print(f"round {i}: {r}")
    print("no crash")


if __name__ == "__main__":
    main()
