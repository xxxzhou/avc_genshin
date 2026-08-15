"""逐帧子进程跑 pos.match，找 avc cv::crossCorr 崩溃复现帧。

崩溃走 OpenCV terminate → 进程硬死 → 非零退出码即命中。
用法: py -3.12 diag/diag_repro_sweep.py
"""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PREV_POINTS = [(-653.0, 267.0), (-288.0, 614.0), (2000.0, -900.0), (-600.0, 450.0)]


def main() -> None:
    frames = Path(ROOT / "diag/repro_frames.txt").read_text().splitlines()
    print(f"{len(frames)} frames x {len(PREV_POINTS)} prevs")
    hits = []
    for i, f in enumerate(frames):
        for px, py in PREV_POINTS:
            r = subprocess.run(
                [sys.executable, str(ROOT / "diag/diag_posmatch_repro.py"), f, str(px), str(py)],
                capture_output=True, text=True, cwd=str(ROOT), timeout=120)
            if r.returncode != 0 or "terminate handler" in (r.stdout + r.stderr):
                print(f"HIT: {f} prev=({px},{py}) rc={r.returncode}")
                hits.append((f, px, py))
                break
        if i % 25 == 0:
            print(f"...{i}/{len(frames)} done, hits={len(hits)}")
    print("HITS:", hits if hits else "none")


if __name__ == "__main__":
    main()
