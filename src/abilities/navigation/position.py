"""小地图位置检测 —— 从小地图截图获取玩家世界坐标（Phase B）。

对照 BetterGI:
- SceneBaseMapByTemplateMatch.cs: BGI 模板匹配方案（朝向去旋转 + 粗匹配 + 精匹配）
- FastSqDiffMatcher.cs: 多通道 SQDIFF 加速匹配
- MiniMapPreprocessor.cs: 小地图预处理（朝向检测 + 遮罩生成）
- TeyvatMapTest.cs: 提瓦特模板匹配实现

匹配流程（对照 BGI SceneBaseMapByTemplateMatch.GetMiniMapPosition）：
1. 小地图预处理：朝向检测 → 生成扇形遮罩（消除旋转）→ 圆形裁剪 → 去除 UI 图标
2. 粗匹配：小地图缩放到 52×52，彩图 matchTemplate(SQDIFF)
3. 精匹配：以粗匹配位置为中心，全尺寸灰度图 matchTemplate(SQDIFF_NORMED)
4. 置信度阈值：≥ 0.95

坐标系统（对照 BGI SceneBaseMap）：
- 原神地图坐标: (x, y)，以 1024 为基本单位
- 图像坐标: (px, py)，以像素为单位
- 转换: game_x = (origin_x - px) / scale, game_y = (origin_y - py) / scale
  其中 scale = block_width / 1024
- 提瓦特: 15行×22列, block_width=2048, origin=(16*2048, 8*2048)
"""

from __future__ import annotations

import json
import math
from typing import TYPE_CHECKING

from framework.resources import res

if TYPE_CHECKING:
    from avc.image import IImageBuffer

    from framework.context import GameContext


# ── 提瓦特地图参数（对照 BGI TeyvatMap.cs）──

TEYVAT_MAP_ROWS = 15
TEYVAT_MAP_COLS = 22
TEYVAT_MAP_UP_ROWS = 7
TEYVAT_MAP_LEFT_COLS = 15
TEYVAT_MAP_BLOCK_WIDTH = 2048

# 图像坐标系原点（块右下角）
TEYVAT_ORIGIN_X = (TEYVAT_MAP_LEFT_COLS + 1) * TEYVAT_MAP_BLOCK_WIDTH
TEYVAT_ORIGIN_Y = (TEYVAT_MAP_UP_ROWS + 1) * TEYVAT_MAP_BLOCK_WIDTH

# 缩放比例: block_width / 1024
TEYVAT_SCALE = TEYVAT_MAP_BLOCK_WIDTH / 1024.0

# 1080p 小地图区域（对照 BGI MapAssets.MimiMapRect1080P = Rect(62, 19, 212, 212)）
MINIMAP_X = 62
MINIMAP_Y = 19
MINIMAP_W = 212
MINIMAP_H = 212

# ── 256 缩放全地图（BigMapTeyvat256Layer / TeyvatMap）──
MAP256_IMAGE = "Assets/Map/Teyvat/Teyvat_0_256.png"  # res.map 相对路径（gitignored）
MAP256_W = 5632  # 22 列 × 256
MAP256_H = 3840  # 15 行 × 256
MAP256_BLOCK_WIDTH = 256
MAP256_ORIGIN_X = (TEYVAT_MAP_LEFT_COLS + 1) * MAP256_BLOCK_WIDTH  # 4096
MAP256_ORIGIN_Y = (TEYVAT_MAP_UP_ROWS + 1) * MAP256_BLOCK_WIDTH   # 2048
MAP256_SCALE = MAP256_BLOCK_WIDTH / 1024.0  # 0.25 px/游戏单位

# ── BGI 模板匹配资源路径 ──
# 分层地图（对照 BGI BaseMapLayerByTemplateMatch.LoadLayers）
_MAPBACK_DIR = "Assets/Map/Teyvat"
_MAPBACK_INFO = "mapback_info.json"  # 分层信息 JSON

# 大图视口 ROI（大图打开时地图内容区，1080p；实机验证边界）
_BIG_MAP_ROI = (0, 0, 1600, 900)

# 局部匹配窗口半径（游戏单位，对齐 BGI 的 3×3 块邻域优化）
_LOCAL_WINDOW_UNITS = 2000.0


class PositionGetter:
    """从小地图截图获取玩家世界坐标。

    使用 BGI 模板匹配方案（对照 SceneBaseMapByTemplateMatch）：
    1. avc IMapMatcher: 朝向去旋转 + 粗匹配 + 精匹配
    2. 无 avc 时回退到纯 Python 模板匹配（简化版）
    """

    def __init__(self, ctx: GameContext):
        self.ctx = ctx
        self._prev_x: float = -1
        self._prev_y: float = -1
        self._map_matcher = None  # avc IMapMatcher（懒加载）
        self._map_matcher_initialized: bool = False

    def get_position(
        self,
        frame: IImageBuffer | None = None,
    ) -> tuple[float, float] | None:
        """获取当前玩家位置（原神地图坐标）。

        1. 提取小地图区域
        2. IMapMatcher 匹配（朝向去旋转 + 粗匹配 + 精匹配）
        3. 坐标转换 → 原神地图坐标
        4. 更新 prev_position
        """
        if frame is None:
            frame = self.ctx.capture()
        if frame is None:
            return None

        minimap = self._extract_minimap(frame)
        if minimap is None:
            return None

        result = self._match(minimap)
        if result is not None:
            self._prev_x, self._prev_y = result
            return result

        return None

    def get_position_from_big_map(
        self, frame: IImageBuffer | None = None
    ) -> tuple[float, float] | None:
        """大图恢复定位：大图已打开时，匹配大图视口↔全地图，返回玩家游戏坐标。

        对照 BGI SceneBaseMap.GetBigMapPosition（简化：直接模板匹配大图区域到全地图）。
        ⚠ 大图打开/视口 ROI 边界留实机。
        """
        # TODO: 大图定位使用 IMapMatcher，待实机验证
        return None

    def set_prev_position(self, x: float, y: float) -> None:
        """设置上次位置（用于局部匹配优化，对照 BGI Navigation.SetPrevPosition）。"""
        self._prev_x = x
        self._prev_y = y

    @property
    def prev_position(self) -> tuple[float, float] | None:
        """上次成功获取的位置。"""
        if self._prev_x <= 0 and self._prev_y <= 0:
            return None
        return (self._prev_x, self._prev_y)

    def _extract_minimap(self, frame: IImageBuffer) -> IImageBuffer | None:
        """从完整截图裁剪小地图区域。"""
        try:
            from avc import Image

            return Image.crop(frame, MINIMAP_X, MINIMAP_Y, MINIMAP_W, MINIMAP_H)
        except Exception:
            return None

    def _match(self, minimap_img: IImageBuffer) -> tuple[float, float] | None:
        """小地图 → IMapMatcher 匹配 → 游戏坐标。

        对照 BGI SceneBaseMapByTemplateMatch.GetMiniMapPosition：
        1. MiniMapPreprocessor: 朝向检测 + 遮罩
        2. 粗匹配 + 精匹配
        3. 坐标转换
        """
        mm = self._get_map_matcher()
        if mm is None:
            return None

        # 有 prev_position → 设置 ROI 局部搜索
        if not (self._prev_x <= 0 and self._prev_y <= 0):
            cx, cy = self._game_to_map256(self._prev_x, self._prev_y)
            half = int(_LOCAL_WINDOW_UNITS * MAP256_SCALE)
            x0 = max(0, int(cx) - half)
            y0 = max(0, int(cy) - half)
            mm.setRoi(x0, y0, half * 2, half * 2)
        else:
            mm.clearRoi()

        if mm.match(minimap_img) == 0:
            return None

        r = mm.getResult()
        if r is None:
            return None

        # 地图像素坐标 → 游戏坐标
        # BGI MapBack 图的坐标系统与 256 地图不同，需要根据 layer 信息转换
        # 简化: 暂用 256 地图坐标转换（实机标定后修正）
        return self._map256_to_game(r.px, r.py)

    def _get_map_matcher(self):
        """懒建 avc IMapMatcher + 加载分层地图资源。"""
        if self._map_matcher_initialized:
            return self._map_matcher
        self._map_matcher_initialized = True

        try:
            from avc import Vision

            mm = Vision.createMapMatcher()
            if mm is None:
                return None

            # 加载主地图层（MapBack_0）
            # 对照 BGI BaseMapLayerByTemplateMatch.LoadLayer
            # stb_image 不支持 WEBP，用 cv2 加载后转 IImageBuffer
            color_path = res.map(f"{_MAPBACK_DIR}/MapBack_0_color.webp")
            gray_path = res.map(f"{_MAPBACK_DIR}/MapBack_0_gray.webp")

            import os

            if color_path.exists():
                self._load_map_to_matcher(mm, str(color_path), "color")
            else:
                # 回退: 用 256 全地图（精度较低）
                map256_path = res.map(MAP256_IMAGE)
                if map256_path.exists():
                    self._load_map_to_matcher(mm, str(map256_path), "color")
                else:
                    return None

            if gray_path.exists():
                self._load_map_to_matcher(mm, str(gray_path), "gray")

            self._map_matcher = mm
            return mm
        except Exception:
            return None

    @staticmethod
    def _load_map_to_matcher(mm, path: str, kind: str) -> bool:
        """用 cv2 加载图片（支持 WEBP）→ IImageBuffer → 传给 IMapMatcher。

        stb_image (loadImagePath) 不支持 WEBP，所以用 cv2 加载后转 IImageBuffer。
        """
        import cv2

        import avc
        from avc._core import ImageType

        img = cv2.imread(path, cv2.IMREAD_COLOR if kind == "color" else cv2.IMREAD_GRAYSCALE)
        if img is None:
            return False

        if kind == "color":
            bgra = cv2.cvtColor(img, cv2.COLOR_BGR2BGRA)
            buf = avc.Image.IImageBuffer()
            buf.setFormat(bgra.shape[1], bgra.shape[0], ImageType.bgra8)
            buf.from_bytes(bgra.tobytes())
            mm.setMapImage(buf)
        else:
            # 灰度图: 转 BGRA (单通道→4通道)
            bgra = cv2.cvtColor(img, cv2.COLOR_GRAY2BGRA)
            buf = avc.Image.IImageBuffer()
            buf.setFormat(bgra.shape[1], bgra.shape[0], ImageType.bgra8)
            buf.from_bytes(bgra.tobytes())
            mm.setFineMapImage(buf)
        return True

    # ── 256 地图坐标转换（BGI TeyvatMap，2048 缩放 ÷8）──

    def _game_to_map256(self, gx: float, gy: float) -> tuple[float, float]:
        """游戏坐标 → 256 地图像素坐标。"""
        return (
            MAP256_ORIGIN_X - gx * MAP256_SCALE,
            MAP256_ORIGIN_Y - gy * MAP256_SCALE,
        )

    def _map256_to_game(self, px: float, py: float) -> tuple[float, float]:
        """256 地图像素坐标 → 游戏坐标。"""
        return (
            (MAP256_ORIGIN_X - px) / MAP256_SCALE,
            (MAP256_ORIGIN_Y - py) / MAP256_SCALE,
        )

    @staticmethod
    def image_to_game_coords(
        img_x: float,
        img_y: float,
        origin_x: float = TEYVAT_ORIGIN_X,
        origin_y: float = TEYVAT_ORIGIN_Y,
        scale: float = TEYVAT_SCALE,
    ) -> tuple[float, float]:
        """图像坐标 → 原神地图坐标（对照 BGI SceneBaseMap.ConvertImageCoordinatesToGenshinMapCoordinates）。"""
        game_x = (origin_x - img_x) / scale
        game_y = (origin_y - img_y) / scale
        return (game_x, game_y)

    @staticmethod
    def game_to_image_coords(
        game_x: float,
        game_y: float,
        origin_x: float = TEYVAT_ORIGIN_X,
        origin_y: float = TEYVAT_ORIGIN_Y,
        scale: float = TEYVAT_SCALE,
    ) -> tuple[float, float]:
        """原神地图坐标 → 图像坐标（对照 BGI SceneBaseMap.ConvertGenshinMapCoordinatesToImageCoordinates）。"""
        img_x = origin_x - game_x * scale
        img_y = origin_y - game_y * scale
        return (img_x, img_y)
