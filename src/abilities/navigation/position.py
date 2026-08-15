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
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from framework.resources import res

if TYPE_CHECKING:
    from avc.image import IImageBuffer
    from avc.vision import IMapMatcher

    from framework.context import GameContext


@dataclass
class _MapLayer:
    """一个 MapBack 分层（粗匹配+精匹配+坐标转换）。

    对照 BGI BaseMapLayerByTemplateMatch: LayerId/Left/Top/Scale +
    CoarseColorMatcher (FastSqDiffMatcher) + FineGrayMap (Mat)。

    坐标系（对照 BGI BaseMapLayerByTemplateMatch.WorldToMap/MapToWorld）：
    - 图像坐标增大 → 游戏坐标减小（翻转）
    - 粗匹配: game_x(西) = Left - px * RoughZoom / Scale, RoughZoom=5
    - 精匹配: game_x(西) = Left - px * ExactZoom / Scale, ExactZoom=1
    - ⚠ 换轴：Left 在西轴（BGI X = position[2]），Top 在北轴（BGI Y = position[0]）；
      coarse_to_game 返回 (北, 西) 序，与 get_position_from_big_map/_map256_to_game 一致
    - IMapMatcher 返回的 (px, py) 已是中心坐标（MapMatcher.cpp 加了 coarseSize/2）
    """

    layer_id: str
    left: float  # 西轴原点（mapback_info.json Left，= BGI X = position[2]）
    top: float  # 北轴原点（mapback_info.json Top，= BGI Y = position[0]）
    scale: float = 1.0  # 缩放（mapback_info.json Scale）
    mm: IMapMatcher | None = field(default=None, repr=False)  # avc IMapMatcher 实例
    # 覆盖范围（西轴 right / 北轴 bottom，从彩图尺寸 + BGI 翻转公式计算）
    right: float = 0.0
    bottom: float = 0.0

    def contains(self, north: float, west: float) -> bool:
        """玩家坐标 (北向, 西向) 是否在该层覆盖范围内。

        left/right 在西轴、top/bottom 在北轴。BGI 翻转坐标系：px=0 → West=Left
        （最大），px=width → West 最小；所以 West 范围是 [right, left]。
        """
        if self.right == self.left and self.bottom == self.top:
            return True  # 无范围信息时不排除
        west_ok = min(self.right, self.left) <= west <= max(self.right, self.left)
        north_ok = min(self.bottom, self.top) <= north <= max(self.bottom, self.top)
        return west_ok and north_ok

    def coarse_to_game(self, px: float, py: float) -> tuple[float, float]:
        """coarseMap 像素坐标（中心）→ 玩家坐标 (北向, 西向)。

        对照 BGI MapToWorld: game_x(西) = Left - px * zoom / Scale,
        game_y(北) = Top - py * zoom / Scale。⚠ 返回 (北, 西) 序（换轴），
        与 get_position_from_big_map 的 (position[0], position[2]) 一致。
        IMapMatcher 返回的 px/py 已含 coarseSize/2 偏移，无需再加。
        """
        west = self.left - px * _ROUGH_ZOOM / self.scale
        north = self.top - py * _ROUGH_ZOOM / self.scale
        return (north, west)

    def game_to_coarse(self, north: float, west: float) -> tuple[float, float]:
        """玩家坐标 (北向, 西向) → coarseMap 像素坐标（coarse_to_game 的逆）。

        对照 BGI WorldToMap: map_x(西→px) = (Left - West) * Scale / zoom,
        map_y(北→py) = (Top - North) * Scale / zoom。
        """
        px = (self.left - west) * self.scale / _ROUGH_ZOOM
        py = (self.top - north) * self.scale / _ROUGH_ZOOM
        return (px, py)


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

# 1080p 小地图区域。
# 实机标定(2026-08-08)：原 BGI Rect(62,19,212,212) 中心 (168,125) 在本版本偏上 ~29px——
# 对 2 帧(cache/map_t0, map_p1)做径向剖面(hue/V)均测得环心 (169,154)、环带 r≈105-111；
# 玩家箭头(青色三角)在环心处。旧 rect 顶部裁进顶部 UI 条(0..44)、底部切掉圆圈，
# 中心裁 156 后箭头偏离中心 34px(旧) vs 9px(新) → 朝向检测与小地图匹配全坏。
MINIMAP_X = 61
MINIMAP_Y = 46
MINIMAP_W = 216
MINIMAP_H = 216

# ── 256 缩放全地图（BigMapTeyvat256Layer / TeyvatMap）──
MAP256_IMAGE = "Assets/Map/Teyvat/Teyvat_0_256.png"  # res.map 相对路径（gitignored）
MAP256_W = 5632  # 22 列 × 256
MAP256_H = 3840  # 15 行 × 256
MAP256_BLOCK_WIDTH = 256
MAP256_ORIGIN_X = (TEYVAT_MAP_LEFT_COLS + 1) * MAP256_BLOCK_WIDTH  # 4096
MAP256_ORIGIN_Y = (TEYVAT_MAP_UP_ROWS + 1) * MAP256_BLOCK_WIDTH   # 2048
MAP256_SCALE = MAP256_BLOCK_WIDTH / 1024.0  # 0.25 px/游戏单位

# ── 大图 SIFT 视口重定位（对照 BGI BigMapTeyvat256Layer：预存底图特征 + 切块局部 FLANN）──
_BIGMAP_VIEWPORT_SHRINK = 4  # 视口缩放比（BGI ResizeHelper 1/4，对齐 256 层尺度）
_BIGMAP_TRAIN_KP = "Assets/Map/Teyvat/Teyvat_0_256_SIFT.kp.bin"  # BGI 预存底图关键点（28B/个 cv::KeyPoint）
_BIGMAP_TRAIN_DESC = "Assets/Map/Teyvat/Teyvat_0_256_SIFT.mat.png"  # BGI 预存底图描述子（128×N 灰度 PNG）
_BIGMAP_BLOCK_ROWS = (MAP256_H // MAP256_BLOCK_WIDTH) * 4  # 60（BGI GameMapRows×4 = 15 行×4）
_BIGMAP_BLOCK_COLS = (MAP256_W // MAP256_BLOCK_WIDTH) * 4  # 88（BGI GameMapCols×4 = 22 列×4）
_BIGMAP_EXPAND_CELLS = 2  # 局部搜索外扩格数（BGI KnnMatchLocal 默认 expandCells）

# ── BGI 模板匹配资源路径 ──
# 分层地图（对照 BGI BaseMapLayerByTemplateMatch.LoadLayers）
_MAPBACK_DIR = "Assets/Map/Teyvat"
_MAPBACK_INFO_FILES = ["mapback_info.json", "mapback_6_0_info.json"]  # 分层信息 JSON（MapBack_0~5）

# 大图视口 ROI（大图打开时地图内容区，1080p；实机验证边界）
_BIG_MAP_ROI = (0, 0, 1600, 900)

# 粗匹配局部搜索半径（color-px 单位，对照 BGI MiniMapMatchConfig.RoughSearchRadius=50）
# ⚠ 2026-08-15 实机（走路段 avc corr.rows 崩溃）：局部搜索 = prev±50，当 prev 靠近
# coarseMap 边界时 avc 内部 autoRoi&full 后 searchRoi < coarseSize(52) → 粗匹配
# matchTemplate(52模板 > searchRoi) 触发 cv::crossCorr 断言崩溃（terminate 无法 catch）。
# 改 0 = 禁用局部搜索 = 全图搜索 → searchRoi 恒为全图(≥52)，不崩。粗匹配模板仅 52×52，
# 全图匹配开销可忽略；精匹配仍用 setSearchRadius 局部（prev 附近）。
_ROUGH_SEARCH_RADIUS = 0

# color-px → 世界单位（BGI: color webp 是 gray webp 的 1/5 缩略 → 1 color-px = 5 世界单位 at Scale=1）
_COARSE_PIXEL_TO_WORLD = 5.0
# BGI MiniMapMatchConfig: 粗匹配缩放比（color webp 相对 gray webp 的缩放倍数）
_ROUGH_ZOOM = 5

# ── 小地图预处理尺寸（BGI Process1：212 中心裁 156）──
PROC_MINIMAP_SIZE = 156  # 裁后边长（朝向/掩码/匹配统一用此尺寸）
_PROC_CENTER = PROC_MINIMAP_SIZE // 2  # 78

# ── BGI 掩码参数（上层视觉特征，留在 avc_genshin；对照 MiniMapPreprocessor/MaskCalculator）──
# 扇形：玩家朝向背后可见区 (排除正前方 ~91° UI/箭头)；angle 为 IOrientationDetector 输出 (0=东, 顺时针)
# 半轴用 int：buildSectorMask 的 rx/ry 是 int32_t，传 float 会被 SWIG 拒 (TypeError)
_MASK_SECTOR_HALF = PROC_MINIMAP_SIZE  # 椭圆半轴（满尺寸 → 整圆扇形）
_MASK_SECTOR_BACK = 45.5  # 朝向后偏移 (度)
_MASK_SECTOR_FRONT = 314.5  # 朝向前截止 (度)；arc = FRONT-BACK ≈ 269°
_MASK_CIRCLE_RADIUS = _PROC_CENTER  # 78：圆形裁剪半径
# 黄绿任务箭头 BGR 范围 (BGI)
_MASK_BG_LO = (165, 165, 55)
_MASK_BG_HI = (180, 180, 75)
# UI 图标：三通道接近(近灰) + 亮度 ∈ [50,127] (BGI MaskCalculator)
_MASK_ICON_CHAN_DIFF = 8  # max-min < 此值 = 近灰
_MASK_ICON_BRIGHT_LO = 50
_MASK_ICON_BRIGHT_HI = 127

# 置信度阈值（实机标定后调高）
# 合成图（无 UI/旋转/日夜变化）: 0.85+；真实小地图（有 UI/掩码/压缩）: 0.7+ 即可
_MIN_SCORE = 0.7


class PositionGetter:
    """从小地图截图获取玩家世界坐标。

    avc 是通用小地图定位引擎（IMapMatcher：粗匹配 + 精匹配 + 亚像素）；
    原神视觉特征（中心裁 156、朝向、BGI 掩码组合、坐标换算）全在本层处理：
    1. 提取小地图 212² → 中心裁 156²（BGI Process1，去 UI 环）
    2. 朝向：IOrientationDetector（独立、更准；失败当 0）
    3. 掩码：IMaskBuilder 组合（扇形 ∩ 圆 − UI 图标 − 黄绿箭头）→ IMapMatcher.setMask
    4. 多图层 IMapMatcher 匹配（BGI 6 层 MapBack）→ coarseMap 像素坐标 → 游戏坐标
    """

    def __init__(self, ctx: GameContext):
        self.ctx = ctx
        self._prev_x: float = 0
        self._prev_y: float = 0
        self._has_prev: bool = False
        self._layers: list[_MapLayer] = []  # 多图层（懒加载）
        self._layers_initialized: bool = False
        self._prev_layer_idx: int = -1  # 上次匹配到的图层索引
        self._od = None  # avc IOrientationDetector（懒加载）
        self._mask_builder = None  # avc IMaskBuilder（懒加载）
        self._mask_builder_initialized: bool = False
        self._fm = None  # avc IFeatureMatcher（大图 SIFT 重定位，懒加载）
        self._fm_initialized: bool = False
        self._train_loaded: bool = False  # BGI 预存底图特征是否已 loadTrainFeaturesPath

    def get_position(
        self,
        frame: IImageBuffer | None = None,
    ) -> tuple[float, float] | None:
        """获取当前玩家位置（原神地图坐标）。

        1. 提取小地图区域（212²）
        2. _match：中心裁 156 → 朝向 + 掩码 → IMapMatcher 匹配
        3. 坐标转换 → 原神地图坐标
        4. 更新 prev_position
        """
        if frame is None:
            frame = self.ctx.capture()
        if frame is None:
            return None

        # 场景守卫：大地图打开时画面无小地图，裁出的"伪小地图"会匹配出跨 4000
        # 单位的假位置（2026-08-15 实机 [3201,-967] 锁死）→ 直接判失败
        from framework.scene import Scene, classify_scene

        try:
            state = classify_scene(frame)
        except Exception:
            state = None
        if state is not None and state.scene is Scene.MAP:
            self.ctx.observe.event(
                "pos.match", ability="pos", phase="observe",
                ok=False, reason="scene_map_minimap_invalid",
                throttle_key="pos.match.scene_map",
            )
            return None

        minimap = self._extract_minimap(frame)
        if minimap is None:
            return None

        result = self._match(minimap)
        if result is not None:
            self._prev_x, self._prev_y = result
            self._has_prev = True
            return result

        return None

    def get_position_from_big_map(
        self,
        frame: IImageBuffer | None = None,
        expected_center: tuple[float, float] | None = None,
    ) -> tuple[float, float] | None:
        """大图视口重定位：地图打开时，用 SIFT 在 Teyvat_0_256 底图定位当前视口，
        返回视口中心（玩家位置）的游戏坐标。

        对照 BGI BigMapTeyvat256Layer.GetBigMapPosition：
        - 底图(train)特征离线预存（.kp.bin/.mat.png）→ loadTrainFeaturesPath 一次进内存 + 切块
        - 视口(query)灰度缩 1/4（对齐 256 层尺度）→ 实时 detectAndCompute
        - 有 expected_center → matchQueryLocal（切块局部 FLANN knnMatch，BGI KnnMatchLocal）
        - 无 expected_center → matchQueryFull（全图 FLANN match，BGI Match）
        - avc 内部：H(query→train) 投影视口中心 → 256 底图坐标 → _map256_to_game → 游戏坐标

        expected_center: 游戏坐标的预期视口中心（上轮定位结果），切块局部搜索用；None 走全图。
        """
        fm = self._ensure_feature_matcher()
        if fm is None or not self._load_train_features(fm):
            return None
        if frame is None:
            frame = self.ctx.capture()
        if frame is None:
            return None

        viewport = self._shrink_for_bigmap(frame)
        if viewport is None:
            return None

        try:
            mode = "local" if expected_center is not None else "full"
            fallback_used = False
            if expected_center is not None:
                rx, ry, rw, rh = self._build_search_rect(
                    expected_center, viewport.width, viewport.height
                )
                hit = fm.matchQueryLocal(viewport, rx, ry, rw, rh, _BIGMAP_EXPAND_CELLS)
                # 局部失败（典型：zoom 很高时 query=整图，装不进局部窗口；或视口漂移）
                # → 兜底全图匹配，避免连续失败触发 M 复位
                if hit <= 0:
                    hit = fm.matchQueryFull(viewport)
                    fallback_used = True
                    mode = "full(fallback)"
            else:
                hit = fm.matchQueryFull(viewport)
            if hit <= 0:
                self.ctx.observe.event("pos.bigmap", ability="pos", phase="observe",
                                       mode=mode, hit=0, ok=False, reason="sift_no_match",
                                       throttle_key="pos.bigmap")
                return None
            r = fm.getMatch(0)
            if r is None:
                self.ctx.observe.event("pos.bigmap", ability="pos", phase="observe",
                                       mode=mode, hit=hit, ok=False, reason="no_result",
                                       throttle_key="pos.bigmap")
                return None
            # 点结果：r.x/y = 视口中心在 256 底图的坐标（w=h=0 哨兵）
            cx256 = float(r.x)
            cy256 = float(r.y)
        except Exception as e:
            self.ctx.observe.event("pos.bigmap", ability="pos", phase="observe",
                                   ok=False, reason="exception", detail=repr(e),
                                   throttle_key="pos.bigmap")
            return None

        game = self._map256_to_game(cx256, cy256)
        self.ctx.observe.event("pos.bigmap", ability="pos", phase="observe",
                               mode=mode, fallback_used=fallback_used, hit=hit,
                               pos=(round(game[0]), round(game[1])), ok=True,
                               throttle_key="pos.bigmap")
        return game

    def _ensure_feature_matcher(self):
        """懒加载 avc IFeatureMatcher（大图 SIFT 重定位）。失败返回 None。

        BGI 定位走 loadTrainFeaturesPath/matchQueryLocal/matchQueryFull，内部强制 SIFT 无参
        + 内嵌 BGI 常量（Lowe 0.75 / good≥7 / RANSAC 3.0），不读 setMethod/setMaxFeatures 等
        配置，故此处不再设置（保持定位基线纯净）。
        """
        if not self._fm_initialized:
            self._fm_initialized = True
            try:
                from avc import Vision

                self._fm = Vision.createFeatureMatcher()
            except Exception:
                self._fm = None
        return self._fm

    def _load_train_features(self, fm) -> bool:
        """一次性加载 BGI 预存底图特征 + 切块缓存（fm.loadTrainFeaturesPath）。已加载则跳过。"""
        if self._train_loaded:
            return True
        try:
            kp = res.map(_BIGMAP_TRAIN_KP)
            desc = res.map(_BIGMAP_TRAIN_DESC)
            if not kp.exists() or not desc.exists():
                return False
            self._train_loaded = bool(
                fm.loadTrainFeaturesPath(
                    str(kp),
                    str(desc),
                    MAP256_W,
                    MAP256_H,
                    _BIGMAP_BLOCK_ROWS,
                    _BIGMAP_BLOCK_COLS,
                )
            )
        except Exception:
            self._train_loaded = False
        return self._train_loaded

    def _build_search_rect(
        self,
        expected_center: tuple[float, float],
        query_w: int,
        query_h: int,
    ) -> tuple[int, int, int, int]:
        """BGI BuildLocalSearchRect：以 expected_center(游戏坐标) 为中心的预期搜索矩形。

        坐标系=256 底图（train）；size = min(train, max(query×2, train/4))，中心钳边界。
        返回 (x, y, w, h) 供 matchQueryLocal 的 roi。
        """
        cx256, cy256 = self._game_to_map256(expected_center[0], expected_center[1])
        rw = min(MAP256_W, max(query_w * 2, MAP256_W // 4))
        rh = min(MAP256_H, max(query_h * 2, MAP256_H // 4))
        x = max(0, min(int(round(cx256 - rw / 2.0)), MAP256_W - rw))
        y = max(0, min(int(round(cy256 - rh / 2.0)), MAP256_H - rh))
        return (x, y, rw, rh)

    @staticmethod
    def _shrink_for_bigmap(frame: IImageBuffer) -> IImageBuffer | None:
        """视口截图缩 1/4（对齐 256 层尺度，BGI ResizeHelper），返回新 IImageBuffer 或 None。"""
        try:
            from avc import Image

            w = max(1, frame.width // _BIGMAP_VIEWPORT_SHRINK)
            h = max(1, frame.height // _BIGMAP_VIEWPORT_SHRINK)
            return Image.resize(frame, w, h)
        except Exception:
            return None

    def set_prev_position(self, x: float, y: float) -> None:
        """设置上次位置（用于局部匹配优化，对照 BGI Navigation.SetPrevPosition）。"""
        self._prev_x = x
        self._prev_y = y
        self._has_prev = True

    @property
    def prev_position(self) -> tuple[float, float] | None:
        """上次成功获取的位置。"""
        if not self._has_prev:
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
        """小地图（≥156）→ 中心裁 156 → 朝向+掩码 → 多图层 IMapMatcher 匹配 → 游戏坐标。

        对照 BGI SceneBaseMapByTemplateMatch:
        - 有 prev_position → 先搜上次成功的层（LocalMatch），失败则遍历其他层
        - 无 prev_position → 遍历所有层 GlobalMatch，取 score 最高的
        """
        layers = self._get_layers()
        if not layers:
            return None

        # 1. 中心裁 156 (BGI Process1: 去 UI 环, 统一朝向/掩码/匹配尺寸)
        mini156 = self._center_crop_proc(minimap_img)
        if mini156 is None:
            return None

        # 2. 朝向 (失败当 0) + 3. 掩码 (失败则全参与)
        angle = self._get_orientation(mini156)
        mask = self._build_mask(mini156, angle)

        # 4. 多图层匹配
        has_prev = self._has_prev
        best_result = None  # (game_x, game_y, layer_idx)
        sel_score = 0.0
        candidate_scores: list[float] = []  # 全局模式下各层候选分数（诊断层歧义用）

        if has_prev:
            # 局部匹配：先搜覆盖 prev_position 的层（local 模式），再搜其他层（global 模式）
            # 覆盖 prev 的层优先，且有局部搜索半径加速
            local_layers = [i for i, l in enumerate(layers) if l.contains(self._prev_x, self._prev_y)]
            other_layers = [i for i in range(len(layers)) if i not in local_layers]
            # 上次成功的层如果还在 local 列表中就排第一
            if 0 <= self._prev_layer_idx < len(layers) and self._prev_layer_idx in local_layers:
                local_layers.remove(self._prev_layer_idx)
                local_layers.insert(0, self._prev_layer_idx)
            order = local_layers + other_layers
            # has_prev 模式下，先搜 local_layers（覆盖 prev_position 的层）。
            # local_layers 失败回退到 other_layers（global 模式）时，加合理性检查：
            # 全局匹配容易 false positive（subagent 2026-08-12 报告 Bug 3：
            # MapBack_0 大层 score 0.856 假阳性，prev 锁死在错位置 dist=13km）。
            # 修：global 回退时收集所有 other_layers 候选，取最高分；且
            # 距 prev_position > 1000 单位的结果要求 score > 0.95。
            used_fallback = False  # 追踪是否走了 local→global 回退（可观测性）
            for idx in order:
                layer = layers[idx]
                is_local = idx in local_layers
                result = self._match_layer(layer, mini156, mask, local=is_local)
                if result is not None:
                    gx, gy, score = result
                    # local 匹配（prev 附近）直接采纳
                    if is_local:
                        best_result = (gx, gy, idx)
                        sel_score = score
                        break
                    # global 回退（other_layers）：合理性检查
                    used_fallback = True
                    dist_to_prev = (
                        (gx - self._prev_x) ** 2 + (gy - self._prev_y) ** 2
                    ) ** 0.5
                    if dist_to_prev > 1000.0 and score < 0.95:
                        # 距 prev 太远且分数不够高 → 嫌疑假阳性，继续找
                        candidate_scores.append(score)
                        continue
                    best_result = (gx, gy, idx)
                    sel_score = score
                    break

        if best_result is None:
            # 全局匹配：遍历所有层，收集候选，优先选位置在层覆盖范围内的
            candidates: list[tuple[float, float, int, float]] = []  # (gx, gy, idx, score)
            for idx, layer in enumerate(layers):
                result = self._match_layer(layer, mini156, mask, local=False)
                if result is not None:
                    gx, gy, score = result
                    candidates.append((gx, gy, idx, score))

            candidate_scores = [c[3] for c in candidates]
            if candidates:
                # 按 score 降序
                candidates.sort(key=lambda c: c[3], reverse=True)
                best_score = candidates[0][3]
                # 在 top 候选中（score 差距 <0.1），优先选位置在层覆盖范围内的
                for gx, gy, idx, score in candidates:
                    if score < best_score - 0.1:
                        break
                    if layers[idx].contains(gx, gy):
                        best_result = (gx, gy, idx)
                        sel_score = score
                        break
                # 全不在覆盖范围内时回退最高 score
                if best_result is None:
                    gx, gy, idx, score = candidates[0]
                    best_result = (gx, gy, idx)
                    sel_score = score

        # 可观测性：pos.match —— 痛点②（纳塔歧义=全局选错层后 _prev_layer_idx 锁死）
        if best_result is not None:
            self._prev_layer_idx = best_result[2]
            in_cov = layers[best_result[2]].contains(best_result[0], best_result[1])
            # mode 区分 local / local_fallback_global / global（subagent 2026-08-12 建议）：
            # local = local_layers 命中；local_fallback_global = local 失败后全局回退；
            # global = 无 prev。让 AI 一眼看出"局部锚定失败"危险态。
            if has_prev:
                mode_str = "local_fallback_global" if used_fallback else "local"
            else:
                mode_str = "global"
            # prev_distance：结果距 prev_position 的距离（仅 has_prev 时有意义）。
            # AI 据此判断锚定是否合理（>1000 + score<0.95 = 嫌疑假阳性，已被合理性检查过滤）。
            prev_dist = (
                round(((best_result[0] - self._prev_x) ** 2 + (best_result[1] - self._prev_y) ** 2) ** 0.5)
                if has_prev else None
            )
            self.ctx.observe.event(
                "pos.match", ability="pos", phase="decide",
                has_prev=has_prev, mode=mode_str,
                selected_layer=layers[best_result[2]].layer_id,
                selected_score=round(sel_score, 3),
                candidate_count=len(candidate_scores),
                top_scores=[round(s, 3) for s in sorted(candidate_scores, reverse=True)[:4]],
                in_coverage=in_cov,
                pos=(round(best_result[0]), round(best_result[1])),
                prev_distance=prev_dist,
                ok=True, throttle_key="pos.match",
            )
            return (best_result[0], best_result[1])

        self.ctx.observe.event(
            "pos.match", ability="pos", phase="decide",
            has_prev=has_prev, mode=("local" if has_prev else "global"),
            candidate_count=len(candidate_scores),
            ok=False, reason="no_match", throttle_key="pos.match",
        )
        return None

    def _match_layer(
        self, layer: _MapLayer, mini156: IImageBuffer, mask: IImageBuffer | None, local: bool
    ) -> tuple[float, float, float] | None:
        """在单层上执行匹配，返回 (game_x, game_y, score) 或 None。"""
        mm = layer.mm
        if mm is None:
            return None

        # 设置掩码
        if mask is not None:
            mm.setMask(mask)
        else:
            mm.clearMask()
        mm.setSubPixel(True)
        mm.setMinScore(_MIN_SCORE)

        # ROI 局部搜索: prev_position 在该层覆盖范围内时用局部匹配，否则全局
        use_local = (
            local
            and self._has_prev
            and layer.contains(self._prev_x, self._prev_y)
        )
        if use_local:
            cx, cy = layer.game_to_coarse(self._prev_x, self._prev_y)
            mm.setPrevPosition(cx, cy)
            # BGI: RoughSearchRadius=50 (color-px)
            mm.setRoughSearchRadius(_ROUGH_SEARCH_RADIUS)
        else:
            mm.clearPrevPosition()
            mm.setRoughSearchRadius(0)

        # 匹配
        if mm.match(mini156) == 0:
            return None
        r = mm.getResult()
        if r is None:
            return None

        gx, gy = layer.coarse_to_game(r.px, r.py)
        return (gx, gy, r.score)

    def _layer_search_order(self) -> list[int]:
        """返回图层搜索顺序：上次成功的层优先，再其他层。"""
        n = len(self._layers)
        if self._prev_layer_idx < 0 or self._prev_layer_idx >= n:
            return list(range(n))
        order = [self._prev_layer_idx]
        for i in range(n):
            if i != self._prev_layer_idx:
                order.append(i)
        return order

    # ── 预处理（裁剪 / 朝向 / 掩码）──

    def _center_crop_proc(self, buf: IImageBuffer) -> IImageBuffer | None:
        """中心裁剪到 PROC_MINIMAP_SIZE (156)；已 ≤156 时原样返回。"""
        try:
            from avc import Image

            w = buf.width
            h = buf.height
            if w <= PROC_MINIMAP_SIZE or h <= PROC_MINIMAP_SIZE:
                return buf
            off = (w - PROC_MINIMAP_SIZE) // 2
            offy = (h - PROC_MINIMAP_SIZE) // 2
            return Image.crop(buf, off, offy, PROC_MINIMAP_SIZE, PROC_MINIMAP_SIZE)
        except Exception:
            return None

    def _get_orientation(self, mini156: IImageBuffer) -> float:
        """朝向角（0=东/顺时针，[45,360]）；失败当 0（不阻塞匹配）。"""
        od = self._get_od()
        if od is None:
            return 0.0
        try:
            ang = od.compute(mini156)
            return float(ang) if ang >= 0 else 0.0
        except Exception:
            return 0.0

    def _get_od(self):
        """懒建 avc IOrientationDetector（无 avc/插件未装返回 None）。"""
        if self._od is not None:
            return self._od
        try:
            from avc import Vision

            self._od = Vision.createOrientationDetector()
            return self._od
        except Exception:
            return None

    def _build_mask(self, mini156: IImageBuffer, angle: float) -> IImageBuffer | None:
        """BGI 掩码组合：扇形 ∩ 圆 − UI 图标 − 黄绿箭头。失败返回 None。"""
        mb = self._get_mask_builder()
        if mb is None:
            return None
        try:
            from avc import Image
            from avc._core import ColorSpace

            def _wrap(raw):
                """SWIG buildXxxMask 返回 AvcWrapper.IImageBuffer，需包装为 avc.image.IImageBuffer。"""
                if raw is None:
                    return None
                return Image.IImageBuffer(raw) if not hasattr(raw, "width") else raw

            # 扇形：玩家可见区（排除正前方 UI）；椭圆半轴满尺寸 → 整圆扇形
            sector = _wrap(mb.buildSectorMask(
                PROC_MINIMAP_SIZE, PROC_MINIMAP_SIZE, _PROC_CENTER, _PROC_CENTER,
                _MASK_SECTOR_HALF, _MASK_SECTOR_HALF,
                angle + _MASK_SECTOR_BACK, angle + _MASK_SECTOR_FRONT,
            ))
            # 圆形裁剪（排除四角）
            circle = _wrap(mb.buildCircleMask(
                PROC_MINIMAP_SIZE, PROC_MINIMAP_SIZE, _PROC_CENTER, _PROC_CENTER,
                _MASK_CIRCLE_RADIUS,
            ))
            m = mb.maskAnd(sector, circle)
            if m is None:
                return None
            m = _wrap(m)
            # 减去 UI 图标（上层 cv2：近灰 + 中亮度）
            icon = self._build_icon_mask(mini156)
            if icon is not None:
                m = _wrap(mb.maskAnd(m, mb.maskNot(icon)))
            # 减去黄绿任务箭头
            bg = mb.buildColorRangeMask(
                mini156, ColorSpace.bgr,
                _MASK_BG_LO[0], _MASK_BG_LO[1], _MASK_BG_LO[2],
                _MASK_BG_HI[0], _MASK_BG_HI[1], _MASK_BG_HI[2],
            )
            if bg is not None:
                bg = _wrap(bg)
                m = _wrap(mb.maskAnd(m, mb.maskNot(bg)))
            return m
        except Exception:
            return None

    def _get_mask_builder(self):
        """懒建 avc IMaskBuilder（无 avc/插件未装返回 None）。"""
        if self._mask_builder_initialized:
            return self._mask_builder
        self._mask_builder_initialized = True
        try:
            from avc import Vision

            self._mask_builder = Vision.createMaskBuilder()
            return self._mask_builder
        except Exception:
            return None

    def _build_icon_mask(self, mini156: IImageBuffer) -> IImageBuffer | None:
        """UI 图标掩码（BGI MaskCalculator：三通道接近 + 亮度∈[50,127]）。

        纯 avc + bytes 实现，不依赖 numpy/cv2：
        1. to_bytes() 取 BGRA8 原始数据
        2. 逐像素判断：BGR 通道最大差 < _MASK_ICON_CHAN_DIFF 且最大通道 ∈ [_MASK_ICON_BRIGHT_LO, _MASK_ICON_BRIGHT_HI]
        3. 生成 r8 掩码（255=图标，0=非图标）
        """
        try:
            w = mini156.width
            h = mini156.height
            raw = mini156.to_bytes()
            if not raw or len(raw) < h * w * 4:
                return None
        except Exception:
            return None

        mask = bytearray(h * w)
        for y in range(h):
            row_off = y * w * 4
            for x in range(w):
                off = row_off + x * 4
                b, g, r = raw[off], raw[off + 1], raw[off + 2]
                ch_max = max(b, g, r)
                ch_min = min(b, g, r)
                if (ch_max - ch_min < _MASK_ICON_CHAN_DIFF
                        and _MASK_ICON_BRIGHT_LO <= ch_max <= _MASK_ICON_BRIGHT_HI):
                    mask[y * w + x] = 255

        try:
            import avc
            from avc._core import ImageType

            buf = avc.Image.IImageBuffer()
            buf.setFormat(w, h, ImageType.r8)
            buf.from_bytes(bytes(mask))
            return buf
        except Exception:
            return None

    @staticmethod
    def _gray_to_buffer(gray_w: int, gray_h: int, gray_bytes: bytes) -> IImageBuffer | None:
        """单通道 r8 bytes → IImageBuffer。"""
        try:
            import avc
            from avc._core import ImageType

            buf = avc.Image.IImageBuffer()
            buf.setFormat(gray_w, gray_h, ImageType.r8)
            buf.from_bytes(gray_bytes)
            return buf
        except Exception:
            return None

    def _get_layers(self) -> list[_MapLayer]:
        """懒加载所有 MapBack 分层（对照 BGI BaseMapLayerByTemplateMatch.LoadLayers）。

        从 mapback_info.json + mapback_6_0_info.json 读层信息，每层创建独立 IMapMatcher。
        """
        if self._layers_initialized:
            return self._layers
        self._layers_initialized = True

        try:
            from avc import Vision

            # 1. 读取所有 info JSON，合并层信息
            layer_infos: list[dict] = []
            for info_name in _MAPBACK_INFO_FILES:
                info_path = res.map(f"{_MAPBACK_DIR}/{info_name}")
                if info_path.exists():
                    with open(info_path, encoding="utf-8") as f:
                        data = json.load(f)
                        if isinstance(data, list):
                            layer_infos.extend(data)

            if not layer_infos:
                # 回退: 尝试直接加载 MapBack_0（硬编码最低保障）
                color_path = res.map(f"{_MAPBACK_DIR}/MapBack_0_color.webp")
                if color_path.exists():
                    mm = self._create_matcher(color_path, res.map(f"{_MAPBACK_DIR}/MapBack_0_gray.webp"))
                    if mm is not None:
                        self._layers = [_MapLayer(layer_id="MapBack_0", left=12384.0, top=1024.0, scale=1.0, mm=mm)]
                return self._layers

            # 2. 为每层创建 IMapMatcher + 加载资源
            for info in layer_infos:
                layer_id = info.get("LayerId", "")
                if not layer_id:
                    continue
                color_path = res.map(f"{_MAPBACK_DIR}/{layer_id}_color.webp")
                gray_path = res.map(f"{_MAPBACK_DIR}/{layer_id}_gray.webp")
                if not color_path.exists():
                    continue
                mm = self._create_matcher(color_path, gray_path)
                if mm is None:
                    continue
                # 从彩图文件计算覆盖范围（BGI 翻转坐标系）
                cw, ch = self._read_image_size(color_path)
                s = float(info.get("Scale", 1))
                left = float(info.get("Left", 0))
                top = float(info.get("Top", 0))
                # BGI: game_x = Left - px * zoom / Scale
                # px=0 → game=Left（最大），px=width → game=Left - width*zoom/Scale（最小）
                right = left - (cw * _ROUGH_ZOOM / s if cw else 0)
                bottom = top - (ch * _ROUGH_ZOOM / s if ch else 0)
                layer = _MapLayer(
                    layer_id=layer_id,
                    left=left,
                    top=top,
                    scale=s,
                    mm=mm,
                    right=right,
                    bottom=bottom,
                )
                self._layers.append(layer)

            # 3. 无层可用时回退 256 全地图
            if not self._layers:
                map256_path = res.map(MAP256_IMAGE)
                if map256_path.exists():
                    mm = Vision.createMapMatcher()
                    if mm is not None:
                        mm.setMapImageByPath(str(map256_path))
                        self._layers = [_MapLayer(layer_id="Teyvat_0_256", left=0, top=0, scale=1.0, mm=mm)]

            return self._layers
        except Exception:
            return self._layers

    @staticmethod
    def _read_image_size(path) -> tuple[int, int]:
        """读取图片尺寸（宽,高），失败返回 (0,0)。"""
        try:
            from PIL import Image as PILImage
            with PILImage.open(str(path)) as img:
                return img.size  # (width, height)
        except Exception:
            return (0, 0)

    @staticmethod
    def _create_matcher(color_path, gray_path) -> IMapMatcher | None:
        """创建并配置单个 IMapMatcher（BGI MiniMapMatchConfig: RoughSize=52, ExactSize=260）。"""
        try:
            from avc import Vision

            mm = Vision.createMapMatcher()
            if mm is None:
                return None
            mm.setMapImageByPath(str(color_path))
            if gray_path.exists():
                mm.setFineMapImageByPath(str(gray_path))
            mm.setCoarseSize(52)
            # ⚠ 2026-08-15 实机（走路段 avc corr.rows 崩溃）：原 ExactSize=260 会把
            # mini156(156×156) resize 放大到 260，精匹配 searchArea 需 ≥260，走路段
            # pos.match 高频触发时某边缘组合导致 cv::crossCorr 断言崩溃（terminate，
            # 无法 catch）。改为 156（= 实际小地图尺寸，不放大）：searchArea 只需 ≥156，
            # 大幅降低触发面；且尺寸匹配更稳。粗匹配仍 52（不变）。
            mm.setExactSize(156)
            return mm
        except Exception:
            return None

    # ── 256 地图坐标转换（BGI TeyvatMap，2048 缩放 ÷8）──

    def _game_to_map256(self, gx: float, gy: float) -> tuple[float, float]:
        """游戏坐标 → 256 地图像素坐标。

        坐标轴对照 BGI TeyvatMapCoordinate.GameToMain：
        - 游戏坐标第 0 轴（=position[0]，北向）映射底图 py
        - 游戏坐标第 1 轴（=position[2]，西向）映射底图 px
        即 px = ORIGIN_X - position[2]*SCALE, py = ORIGIN_Y - position[0]*SCALE。
        """
        return (
            MAP256_ORIGIN_X - gy * MAP256_SCALE,
            MAP256_ORIGIN_Y - gx * MAP256_SCALE,
        )

    def _map256_to_game(self, px: float, py: float) -> tuple[float, float]:
        """256 地图像素坐标 → 游戏坐标（_game_to_map256 的逆）。

        返回 (position[0], position[2]) 序，与 tp.py TpPosition.x/y 一致。
        """
        return (
            (MAP256_ORIGIN_Y - py) / MAP256_SCALE,
            (MAP256_ORIGIN_X - px) / MAP256_SCALE,
        )

    # ── MapBack 粗匹配坐标转换（委托 _MapLayer）──

    def _coarse_to_game(self, px: float, py: float, layer_idx: int = 0) -> tuple[float, float]:
        """coarseMap 像素坐标 → 游戏坐标（默认第 0 层，兼容旧接口）。"""
        if layer_idx < len(self._layers):
            return self._layers[layer_idx].coarse_to_game(px, py)
        # 回退: MapBack_0 硬编码（BGI 翻转公式；换轴返回 (北, 西)）
        return (1024.0 - py * _ROUGH_ZOOM, 12384.0 - px * _ROUGH_ZOOM)

    def _game_to_coarse(self, gx: float, gy: float, layer_idx: int = 0) -> tuple[float, float]:
        """游戏坐标 → coarseMap 像素坐标（默认第 0 层，兼容旧接口）。"""
        if layer_idx < len(self._layers):
            return self._layers[layer_idx].game_to_coarse(gx, gy)
        # 回退: MapBack_0 硬编码（(gx,gy)=(北,西)；换轴后 px 由西向 gy 算）
        return ((12384.0 - gy) / _ROUGH_ZOOM, (1024.0 - gx) / _ROUGH_ZOOM)

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
