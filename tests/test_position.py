"""PositionGetter 定位测试：256 坐标转换 + 合成小地图模板定位链。

- 坐标转换：纯数学，始终跑。
- 合成定位链：从地图裁一个区域模拟小地图（resize → IImageBuffer），
  set_prev_position(目标) → ``_match`` → 应还原目标附近坐标。
  需 avc + 地图图（缺则 skip）；验证的是"小地图↔地图匹配"机械链，
  真实游戏小地图渲染的精确标定仍需实机帧。
"""

from __future__ import annotations

from unittest.mock import MagicMock

import numpy as np  # noqa: F401
import pytest

from abilities.navigation.position import (
    MAP256_ORIGIN_X,
    MAP256_ORIGIN_Y,
    MAP256_SCALE,
    PositionGetter,
)


def _has_avc_and_map() -> bool:
    try:
        import avc  # noqa: F401
        from framework.resources import res

        # 检查模板匹配资源或 256 地图
        return (
            res.map("Assets/Map/Teyvat/MapBack_0_color.webp").exists()
            or res.map("Assets/Map/Teyvat/Teyvat_0_256.png").exists()
        )
    except Exception:
        return False


class TestMap256Coordinate:
    """256 缩放坐标转换（纯数学，始终跑）。"""

    def test_roundtrip(self):
        pg = PositionGetter(MagicMock())
        for gx, gy in [(2000.0, 1000.0), (-500.0, 3000.0), (0.0, 0.0)]:
            px, py = pg._game_to_map256(gx, gy)
            rx, ry = pg._map256_to_game(px, py)
            assert abs(rx - gx) < 0.01 and abs(ry - gy) < 0.01

    def test_known_origin(self):
        pg = PositionGetter(MagicMock())
        # 游戏原点 (0,0) → 256 地图 (4096, 2048)
        px, py = pg._game_to_map256(0, 0)
        assert abs(px - MAP256_ORIGIN_X) < 0.01 and abs(py - MAP256_ORIGIN_Y) < 0.01
        # 游戏 (1000, 0) → x 减少 1000*0.25=250
        px2, _ = pg._game_to_map256(1000.0, 0.0)
        assert abs(px2 - (MAP256_ORIGIN_X - 250.0)) < 0.01

    def test_consistent_with_2048_scale(self):
        # 与既有 2048 静态转换一致：2048px = 8 × 256px
        gx, gy = 2000.0, -1000.0
        ix, iy = PositionGetter.game_to_image_coords(gx, gy)  # 2048 缩放
        px, py = PositionGetter(MagicMock())._game_to_map256(gx, gy)
        assert abs(ix - px * 8) < 0.1 and abs(iy - py * 8) < 0.1


pytestmark = pytest.mark.skipif(
    not _has_avc_and_map(), reason="需 avc + 地图图（MapBack_0_color.webp 或 Teyvat_0_256.png）"
)


class TestMapPositioning:
    """合成小地图 → 还原位置（使用 avc IMapMatcher）。"""

    def _load_map(self):
        import cv2

        from framework.resources import res

        # 用 MapBack 彩图（= IMapMatcher 的 coarseMap）；小地图源与 coarse 同图 → 1:1 匹配。
        # 回退 256 地图（精度低，仅验证机械链）。
        color_path = res.map("Assets/Map/Teyvat/MapBack_0_color.webp")
        if not color_path.exists():
            color_path = res.map("Assets/Map/Teyvat/Teyvat_0_256.png")
        img = cv2.imread(str(color_path))  # BGR
        assert img is not None, "地图加载失败"
        return img

    @staticmethod
    def _pick_textured_topn(map_img, n=12):
        """返回纹理最强的前 n 个候选（48×48 块 std 降序），地图像素中心列表。

        单个候选在粗匹配尺度上未必唯一（纹理高 ≠ 粗尺度可区分），
        故返回多个，由调用方逐一尝试。步长 150 与 test_features.f12 一致。
        """
        h, w = map_img.shape[:2]
        cands = []
        for y in range(100, h - 100, 150):
            for x in range(100, w - 100, 150):
                cands.append((float(map_img[y : y + 48, x : x + 48].std()), x + 24, y + 24))
        cands.sort(reverse=True)
        return [(cx, cy) for _std, cx, cy in cands[:n]]

    def _build_minimap(self, map_img, cx, cy):
        """从 color webp 裁 ~70 color-px → resize 212×212 → bgra8 IImageBuffer。

        取 70 color-px 是为让 ``_match`` 中心裁 156 后 ≈ 52 color-px = coarseSize，
        与 coarseMap 1:1 匹配（exactSize 260 = 52×5 gray-px 同步 1:1）。
        """
        import cv2

        import avc
        from avc._core import ImageType

        h, w = map_img.shape[:2]
        half_px = 35  # 70px extent
        x0, y0 = max(0, cx - half_px), max(0, cy - half_px)
        x1, y1 = min(w, cx + half_px), min(h, cy + half_px)
        patch = map_img[y0:y1, x0:x1]
        resized = cv2.resize(patch, (212, 212), interpolation=cv2.INTER_AREA)
        bgra = cv2.cvtColor(resized, cv2.COLOR_BGR2BGRA)
        buf = avc.Image.IImageBuffer()
        buf.setFormat(212, 212, ImageType.bgra8)
        buf.from_bytes(bgra.tobytes())
        return buf

    def test_chain_recovery(self):
        """合成小地图（无旋转）→ _match 还原目标位置。

        纹理高的候选在粗匹配尺度上未必唯一，故取前 N 个逐一试，
        任一稳定匹配即过（验证的是"小地图↔地图匹配"机械链本身）。
        """
        map_img = self._load_map()
        pg = PositionGetter(MagicMock())
        for px, py in self._pick_textured_topn(map_img):
            gx, gy = pg._coarse_to_game(float(px), float(py))
            mini = self._build_minimap(map_img, px, py)
            pg.set_prev_position(gx, gy)
            r = pg._match(mini)
            if r is not None and abs(r[0] - gx) < 500.0 and abs(r[1] - gy) < 500.0:
                return
        raise AssertionError("前 N 个高纹理候选都未能稳定模板定位")

    def test_get_position_end_to_end(self):
        """造 1920×1080 帧（小地图贴到 (62,19)）→ get_position 走完整链路。

        同 test_chain_recovery，取前 N 个候选逐一试。
        """
        import avc
        from avc._core import ImageType

        map_img = self._load_map()
        pg = PositionGetter(MagicMock())
        for px, py in self._pick_textured_topn(map_img):
            gx, gy = pg._coarse_to_game(float(px), float(py))
            mini = self._build_minimap(map_img, px, py)
            # 1920×1080 黑帧, 小地图 bgra8 贴到 (62,19)
            frame = bytearray(1920 * 1080 * 4)
            mb = mini.to_bytes()
            for yy in range(212):
                src = yy * 212 * 4
                dst = ((19 + yy) * 1920 + 62) * 4
                frame[dst : dst + 212 * 4] = mb[src : src + 212 * 4]
            buf = avc.Image.IImageBuffer()
            buf.setFormat(1920, 1080, ImageType.bgra8)
            buf.from_bytes(bytes(frame))
            pg.set_prev_position(gx, gy)
            r = pg.get_position(buf)
            if r is not None and abs(r[0] - gx) < 500.0 and abs(r[1] - gy) < 500.0:
                return
        raise AssertionError("前 N 个高纹理候选都未能端到端定位")
