"""小地图位置检测 —— 从小地图截图获取玩家世界坐标（Phase B）。

对照 BetterGI:
- NavigationInstance.cs: 获取位置（SIFT 特征匹配）
- SceneBaseMap.cs: 全地图匹配 + 局部匹配 + 坐标转换
- TeyvatMap.cs: 提瓦特地图参数（15行×22列, 2048px块宽）

两种模式：
1. SIFT 模式（需要全地图特征数据，50-500MB）: 精确匹配
2. 模板匹配模式（无需大文件）: 简化回退

坐标系统（对照 BGI SceneBaseMap）：
- 原神地图坐标: (x, y)，以 1024 为基本单位
- 图像坐标: (px, py)，以像素为单位
- 转换: game_x = (origin_x - px) / scale, game_y = (origin_y - py) / scale
  其中 scale = block_width / 1024
- 提瓦特: 15行×22列, block_width=2048, origin=(16*2048, 8*2048)
"""

from __future__ import annotations

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


class PositionGetter:
    """从小地图截图获取玩家世界坐标。

    两种匹配模式:
    1. SIFT: 特征匹配（需要全地图 keypoint 数据，精确）
    2. 模板匹配: 模板匹配（无需大文件，简化回退）

    v1: SIFT 模式依赖大文件（不在 git），先实现骨架。
    当 SIFT 数据不可用时，自动降级到模板匹配。
    """

    def __init__(self, ctx: GameContext):
        self.ctx = ctx
        self._prev_x: float = -1
        self._prev_y: float = -1
        self._method: str = "template"  # "sift" | "template"
        self._sift_available: bool = False  # 懒检测

    def get_position(
        self,
        frame: IImageBuffer | None = None,
    ) -> tuple[float, float] | None:
        """获取当前玩家位置（原神地图坐标）。

        1. 提取小地图区域
        2. 匹配（SIFT 或模板）
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

        # 尝试 SIFT
        if self._sift_available:
            result = self._match_sift(minimap)
            if result is not None:
                self._prev_x, self._prev_y = result
                return result

        # 回退到模板匹配
        result = self._match_template(minimap)
        if result is not None:
            self._prev_x, self._prev_y = result
            return result

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

    def _match_sift(
        self,
        minimap_img: IImageBuffer,
    ) -> tuple[float, float] | None:
        """SIFT 特征匹配（需要全地图 keypoint 数据）。

        对照 BGI SceneBaseMap.GetMiniMapPosition:
        1. 从小地图提取 SIFT 特征
        2. 与全地图特征进行 KNN 匹配
        3. 如果有 prev_position，先做局部匹配（加速）
        4. 坐标转换: 图像坐标 → 原神地图坐标

        v1: 需要加载 SIFT 数据文件，暂返回 None。
        """
        # 懒检测 SIFT 数据可用性
        if not self._sift_available:
            self._check_sift_availability()
        if not self._sift_available:
            return None

        # v1: SIFT 匹配需要完整的 OpenCV 特征匹配链
        # 在数据文件就绪后实现
        return None

    def _match_template(
        self,
        minimap_img: IImageBuffer,
    ) -> tuple[float, float] | None:
        """模板匹配回退（简化，无需大文件）。

        v1: 模板匹配需要地图切片图，暂返回 None。
        """
        return None

    def _check_sift_availability(self) -> None:
        """检查 SIFT 数据文件是否存在。"""
        # SIFT 数据文件: *_SIFT.kp.bin + *_SIFT.mat.png
        # 在 resources/map/ 目录下
        sift_dir = res.map("feature")
        if sift_dir.exists():
            kp_files = list(sift_dir.glob("*_SIFT.kp.bin"))
            if kp_files:
                self._sift_available = True
                self._method = "sift"
                return
        self._sift_available = False

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
