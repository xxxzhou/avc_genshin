"""角色面朝（朝向）检测 —— 小地图箭头对称轴法（Phase B）。

背景（2026-08-08 实机，cache/diag_arrowsave.py / diag_facecal3.py）：
- 原神小地图固定北朝上，``avc IOrientationDetector``（get_orientation 旧实现）读的是
  小地图像素差异的放大值，非相机偏航，不可用。
- 小地图**玩家箭头**（~30px 刻痕箭头，青/橙/白多色渲染）是可靠的朝向传感器；
  但箭头紧凑、质心偏底，凸包「最远点=尖端」会间歇误选底角（±100° 跳变）。
- 本模块改用**对称轴法**：箭头为对称刻痕箭头 → 镜像重叠最大的轴=对称轴=朝向；
  尖端更窄消 180° 歧义；对称分 <0.35 判为非箭头（描边碎片/小图标）返回 None。
- 实机验证：连续 6×+600px 旋转，读数 Δ≈+26.5°/步（投递正确），非箭头被拒。

说明：箭头只反映**角色面朝**。原神空闲时面朝与相机偏航独立（转相机箭头不动），
须移动（轻推 W）才同步 → rotate_to 在旋转后轻推同步（见 camera.py）。
"""

from __future__ import annotations

import math
from collections import defaultdict

import numpy as np

# 小地图区域（1080p，实机标定 2026-08-08：环心 (169,154) r≈108）
MINIMAP_X = 61
MINIMAP_Y = 46
MINIMAP_W = 216
MINIMAP_H = 216

# 检测参数
_AREA_MIN = 50      # 箭头填充面积下限（白描边碎片约 56，箭头约 265）
_AREA_MAX = 2000    # 上限（防与附近色块粘连成大团）
_DIST_MAX = 45.0    # 候选质心距小地图中心的最大距离
_SYM_THRESH = 0.35  # 对称分阈值：低于=非箭头
_MIN_PTS = 12       # 候选最少像素数


def _sym_heading(pts: np.ndarray) -> tuple[float | None, float]:
    """对 blob 像素求对称轴朝向。

    在 PCA 主轴 ±50° 内扫 101 个角，镜像重叠分数最大的角=对称轴；
    尖端更窄消 180° 歧义。返回 (heading, score)；score<阈值 返回 (None, score)。
    """
    if len(pts) < _MIN_PTS:
        return None, 0.0
    pts = np.asarray(pts, dtype=np.float64)
    cx0, cy0 = float(pts[:, 0].mean()), float(pts[:, 1].mean())
    centered = pts - np.array([cx0, cy0])
    _, eigvec = np.linalg.eigh(np.cov(centered.T))
    base = math.degrees(math.atan2(eigvec[1, -1], eigvec[0, -1]))
    best_deg, best_score = base, -1.0
    for deg in np.linspace(base - 50, base + 50, 101):
        th = math.radians(deg)
        ux, uy = math.cos(th), math.sin(th)
        t = centered[:, 0] * ux + centered[:, 1] * uy
        p = -centered[:, 0] * uy + centered[:, 1] * ux
        dct: dict[int, set] = defaultdict(set)
        for tt, pp in zip(t, p):
            dct[round(tt / 2) * 2].add(round(pp))
        mirror = sum(1 for ps in dct.values() for pp in ps if -pp in ps)
        score = mirror / len(pts)
        if score > best_score:
            best_score, best_deg = score, deg
    if best_score < _SYM_THRESH:
        return None, best_score
    th = math.radians(best_deg)
    ux, uy = math.cos(th), math.sin(th)
    t = centered[:, 0] * ux + centered[:, 1] * uy
    perp = -centered[:, 0] * uy + centered[:, 1] * ux
    tmin, tmax = t.min(), t.max()
    span = max(tmax - tmin, 1e-6)
    w_min = np.ptp(perp[np.abs(t - tmin) < 0.2 * span]) if np.sum(np.abs(t - tmin) < 0.2 * span) > 3 else 1e9
    w_max = np.ptp(perp[np.abs(t - tmax) < 0.2 * span]) if np.sum(np.abs(t - tmax) < 0.2 * span) > 3 else 1e9
    tip_t = tmin if w_min < w_max else tmax  # 尖端更窄
    tip_x = cx0 + ux * tip_t
    tip_y = cy0 + uy * tip_t
    h = (90 - math.degrees(math.atan2(-(tip_y - cy0), tip_x - cx0))) % 360
    return h, best_score


def heading_from_crop(crop: np.ndarray, cx: float = 108, cy: float = 108) -> tuple[float | None, int, float, str | None]:
    """从小地图 BGR crop 检测角色面朝（罗盘角：0=北，90=东，顺时针）。

    逐色(青/橙/白)连通块收集候选，按距中心排序，取第一个对称分达标的候选
    （描边碎片/小图标对称分低被跳过）。返回 (heading, area, dist, color) 或
    (None, 0, 0, None)。
    """
    # avc to_bytes 实机标定返回 RGBA8（首字节=R），crop 即 (R,G,B)
    R = crop[:, :, 0].astype(np.int16)
    G = crop[:, :, 1].astype(np.int16)
    B = crop[:, :, 2].astype(np.int16)
    colors = {
        "cyan": (B > 170) & (G > 150) & (B - R > 40),
        "orange": (R > 170) & (G > 150) & (B < 100),
        "white": (B > 190) & (G > 190) & (R > 190),
    }
    cands: list[tuple[float, int, str, int, np.ndarray]] = []  # (dist, area, color, i, labels)
    for cname, mask in colors.items():
        # cv2 连接组件在 `import cv2` 延迟导入，避免纯数学测试拉重依赖
        import cv2

        n, labels, stats, cent = cv2.connectedComponentsWithStats(mask.astype(np.uint8))
        for i in range(1, n):
            area = int(stats[i, cv2.CC_STAT_AREA])
            if area < _AREA_MIN or area > _AREA_MAX:
                continue
            cxx, cyy = cent[i]
            d = math.hypot(cxx - cx, cyy - cy)
            if d > _DIST_MAX:
                continue
            cands.append((d, area, cname, i, labels))
    if not cands:
        return None, 0, 0, None
    cands.sort(key=lambda c: (c[0], -c[1]))
    for d, area, cname, i, labels in cands:
        ys, xs = np.where(labels == i)
        h, _score = _sym_heading(np.column_stack([xs, ys]))
        if h is not None:
            return h, area, d, cname
    return None, 0, 0, None


def heading_from_frame(frame) -> tuple[float | None, int, float, str | None]:
    """从 avc 截图对象裁剪小地图并检测朝向。返回 (heading, area, dist, color)。

    ``frame``: 有 ``height/width/to_bytes()`` 的 avc IImageBuffer（BGRA8 紧凑布局）。
    """
    import numpy as np

    raw = np.frombuffer(frame.to_bytes(), np.uint8)
    arr = raw.reshape(frame.height, frame.width, 4)
    crop = arr[MINIMAP_Y : MINIMAP_Y + MINIMAP_H, MINIMAP_X : MINIMAP_X + MINIMAP_W, :3]
    return heading_from_crop(crop)
