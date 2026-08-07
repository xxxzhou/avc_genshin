"""大地图操作原语 —— MapController（Phase B，对照 BGI TpTask.cs 的地图 UI 操作）。

封装地图打开后的视口操作：缩放测量/控制、拖拽平移、地面层切换、国家切换、
传送点图标匹配。供 ``tp.py`` 的 ``_navigate_map_to_target``（MoveMapToCore 循环）调用。

所有方法假设调用时已处于 ``Scene.MAP``（由调用方保证）。坐标均为 1080p buffer 坐标。

⚠ 多个方向/系数源自 BGI TpConfig/TpTask，因 avc 输入语义（moveBy/scroll 的 DPI、
正负方向）与 SendInput 不完全等同，下列符号常量需经 ``verify`` 实机标定回填：
``_ZOOM_WHEEL_SIGN`` / ``_DRAG_X_SIGN`` / ``_DRAG_Y_SIGN`` / ``_DRAG_DPI_MULT``。
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

from framework import utils

from abilities import vision_utils as vu

if TYPE_CHECKING:
    from framework.context import GameContext
    from framework.high_level_api import HighLevelApi

    from abilities.vision_utils import Rect


# ── 实机标定常量（源自 BGI TpConfig/TpTask；方向/系数待实机回填）──

# 缩放等级测量（BGI GetBigMapScale/GetBigMapZoomLevel）
_ZOOM_START_Y = 468  # 旋钮顶部 Y（地图最大缩小，scale=1, zoom=1）BGI ZoomStartY
_ZOOM_END_Y = 612  # 旋钮底部 Y（地图最大放大，scale=0, zoom=6）BGI ZoomEndY
_ZOOM_BTN_ROI = (30, 440, 40, 200)  # 旋钮搜索区（BGI MapScaleButton ROI）

# 滚轮缩放（BGI AdjustMapZoomLevel）
_ZOOM_PER_NOTCH = 0.085  # 每滚轮槽 zoom 步长（BGI DefaultMapZoomLevelPerWheelNotch）
_ZOOM_WHEEL_SIGN = -1  # scroll dy 与 zoom 增减关系（实机确认：+dy 缩小→zoom 减→sign=-1）
_MAX_ZOOM_NOTCHES = 16  # 单次缩放最大滚轮槽（BGI MaxMapZoomWheelBatchNotches）
_MAX_ZOOM_ATTEMPTS = 3  # 缩放重试轮数
_ZOOM_TOLERANCE = 0.3  # zoom 达标容差

# 拖拽平移（BGI GetMoveMapState / MouseMoveMap）
_MAP_SCALE_FACTOR = 2.361  # 游戏单位→像素（zoom=1）BGI MapScaleFactor
_MAP_DRAG_PIXELS_PER_STEP = 48  # 每步最大像素（BGI MapDragPixelsPerStep）
_MAP_DRAG_FAST_STEP_RATIO = 0.42  # 快速段步数占比（BGI MapDragFastStepRatio）
_MAP_DRAG_FAST_DISTANCE_RATIO = 0.85  # 快速段距离占比（BGI MapDragFastDistanceRatio）
_MAP_DRAG_STEP_MS = 20  # 每步间隔（ms）
_DRAG_X_SIGN = 1  # 拖拽 X 方向符号（实机确认：+x拖→SIFT x增大→同向→1）
_DRAG_Y_SIGN = 1  # 拖拽 Y 方向符号（实机确认：+y拖→SIFT y增大→同向→1）
_DRAG_DPI_MULT = 0.0  # 已弃用：moveTo 绝对坐标模式下不需要 DPI 乘子（buf_to_scr 由 sc.width/1920 计算）

# 图标 / 区域
_DISPLAY_TP_ZOOM = 4.4  # 传送点图标显示所需 zoom（BGI DisplayTpPointZoomLevel）
_TP_ICON_THRESHOLD = 0.65  # 传送点图标匹配阈值（BGI NearbyMapIconTemplateThreshold）
_MAP_CENTER_X = 960
_MAP_CENTER_Y = 540
_MAP_DRAG_START = (960, 540)  # 拖拽起点（视口中心，避开左上 UI 覆盖区 360×400）
_COUNTRY_BTN_POS = (1760, 1020)  # 右下"当前区域"按钮（BGI Width-160, Height-60）
_COUNTRY_OCR_ROI = (1280, 0, 640, 1080)  # 国家列表 OCR 区（右 1/3，BGI Width*2/3）
_SWITCH_AREA_RETRIES = 4  # 国家列表 OCR 重试次数（BGI SwitchAreaCandidateRetryCount）

# ── Teyvat 7 国中心坐标（avc x,y 系 = position[0], position[2]）──
# 源自 BGI MapLazyAssets.CountryPositions（BGI X,Y 系 = position[2], position[0]），
# 此处已换轴：avc_x = BGI_Y(pos0), avc_y = BGI_X(pos2)。⚠ 换轴待实机确认。
_COUNTRY_CENTERS: dict[str, tuple[float, float]] = {
    "蒙德": (2278.0, -876.0),  # BGI [-876, 2278]
    "璃月": (-666.0, 270.0),  # BGI [270, -666]
    "稻妻": (-3050.0, -4400.0),  # BGI [-4400, -3050]
    "须弥": (-374.0, 2877.0),  # BGI [2877, -374]
    "枫丹": (3631.0, 4515.0),  # BGI [4515, 3631]
    "纳塔": (-1879.1, 8973.5),  # BGI [8973.5, -1879.1]
    "挪德卡莱": (1661.84, 9542.25),  # BGI [9542.25, 1661.84]
}

# ── 传送点 type → 图标模板（resources/templates/teleport/，经 res.template 解析）──
_TP_ICON_BY_TYPE: dict[str, str] = {
    "TeleportWaypoint": "teleport/TeleportWaypoint.png",
    "Goddess": "teleport/StatueOfTheSeven.png",
    "OneTimeDomain": "teleport/Domain.png",
    "BlessDomain": "teleport/Domain.png",
    "ForgeryDomain": "teleport/Domain.png",
    "MasteryDomain": "teleport/Domain.png",
    "TrounceDomain": "teleport/Domain.png",
    "Mansion": "teleport/Mansion.png",
    "SubSpaceWaypoint": "teleport/SubSpaceWaypoint.png",
    "PortableWaypoint": "teleport/PortableWaypoint.png",
    "NatlanObsidianTotemPole": "teleport/ObsidianTotemPole.png",
    "TabletOfTona": "teleport/TabletOfTona.png",
    "NodKraiMeetingPoint": "teleport/NodKraiMeetingPoint.png",
}
_DOMAIN_TYPES = frozenset(
    {"OneTimeDomain", "BlessDomain", "ForgeryDomain", "MasteryDomain", "TrounceDomain"}
)


class MapController:
    """大地图 UI 操作原语（缩放/拖拽/切层/切国家/图标匹配）。

    依赖注入 ``ctx``（avc 输入）+ ``g``（高层 API）。纯原语，不查场景（由调用方保证
    在 MAP）。拖拽用 ``ctx.ic`` 直接调用（mouseDown→moveBy→mouseUp 连续动作，
    不走 g 桥避免每步开销）。
    """

    def __init__(self, ctx: "GameContext", g: "HighLevelApi"):
        self.ctx = ctx
        self.g = g

    # ── 缩放 ──

    def measure_zoom_level(self, frame=None) -> float | None:
        """测量当前地图缩放等级（1=最大放大，6=最大缩小）。

        匹配缩放滑块旋钮（MapScaleButton）Y 位置 → zoom_level。
        对照 BGI GetBigMapScale/GetBigMapZoomLevel。
        """
        rect = vu.find_template(
            self.ctx,
            "teleport/MapScaleButton.png",
            threshold=0.8,
            roi=_ZOOM_BTN_ROI,
            frame=frame,
        )
        if rect is None:
            return None
        knob_cy = rect.cy  # 旋钮中心 Y（1080p buffer）
        denom = _ZOOM_END_Y - _ZOOM_START_Y
        if denom == 0:
            return None
        scale = (_ZOOM_END_Y - knob_cy) / denom  # 1=顶部(缩最小), 0=底部(放最大)
        return -5.0 * scale + 6.0  # zoom_level: 1..6

    def set_zoom_level(self, target_zoom: float, frame=None) -> float | None:
        """滚轮缩放到目标 zoom_level（±容差）。返回最终 zoom 或 None（测不到）。"""
        cur = self.measure_zoom_level(frame)
        if cur is None:
            return None
        for _ in range(_MAX_ZOOM_ATTEMPTS):
            diff = target_zoom - cur
            if abs(diff) <= _ZOOM_TOLERANCE:
                return cur
            # 槽数 = |diff| / 每槽步长；方向 = sign(diff) × _ZOOM_WHEEL_SIGN
            notches = min(
                _MAX_ZOOM_NOTCHES, max(1, int(round(abs(diff) / _ZOOM_PER_NOTCH)))
            )
            dy = _ZOOM_WHEEL_SIGN * (1 if diff > 0 else -1) * notches
            self.ctx.ic.scroll(0, dy)
            utils.sleep(0.15)
            frame = self.ctx.capture()
            cur = self.measure_zoom_level(frame)
            if cur is None:
                return None
        return cur

    # ── 拖拽 ──

    def drag_map(self, dx_game: float, dy_game: float, zoom_level: float) -> None:
        """拖拽地图：把视口中心朝 (dx_game, dy_game) 游戏偏移方向移动。

        对照 BGI GetMoveMapState + MouseMoveMap：
        mouse_px = MapScaleFactor * |game_dist| / zoom_level，方向 sign(offset)；
        分 5-60 步（每步 ≤48px），快速段(前42%步走85%距)+慢速段曲线。

        ⚠ avc moveBy 在 mouseDown 状态下不触发游戏拖拽响应（游戏不认相对移动），
        改用 moveTo 绝对坐标：每步算出目标屏幕坐标，moveTo 移过去。
        moveTo 用逻辑屏幕像素，buffer→screen 缩放比 = screen_w/buf_w。
        """
        zoom_level = zoom_level if zoom_level > 0 else 1.0
        total_x = _MAP_SCALE_FACTOR * abs(dx_game) / zoom_level
        total_y = _MAP_SCALE_FACTOR * abs(dy_game) / zoom_level
        dist = math.hypot(total_x, total_y)
        if dist < 1.0:
            return
        sign_x = _DRAG_X_SIGN * (1 if dx_game >= 0 else -1)
        sign_y = _DRAG_Y_SIGN * (1 if dy_game >= 0 else -1)

        steps = max(5, min(60, int(math.ceil(dist / _MAP_DRAG_PIXELS_PER_STEP))))
        ic = self.ctx.ic
        btn = self.ctx._MouseButton["left"]

        # buffer→screen 缩放比：moveTo 用逻辑屏幕像素
        # to_screen 转换起点绝对坐标，但偏移量需要按 sc 客户区/buffer 比例缩放
        # sc.width/height 是窗口客户区逻辑尺寸，buffer 是 1920×1080
        buf_w = 1920  # buffer 宽（1080p）
        buf_to_scr = self.ctx.sc.width() / buf_w if buf_w > 0 else 1.0

        # 起点屏幕坐标
        sx, sy = _MAP_DRAG_START
        scr_x, scr_y = self.ctx.to_screen(sx, sy)
        ic.moveTo(int(scr_x), int(scr_y))
        ic.mouseDown(btn)
        prev_frac = 0.0
        # 累计偏移（屏幕像素）
        cum_mx = 0.0
        cum_my = 0.0
        for i in range(1, steps + 1):
            frac = self._drag_curve(i, steps)
            inc = frac - prev_frac
            prev_frac = frac
            cum_mx += sign_x * total_x * inc * buf_to_scr
            cum_my += sign_y * total_y * inc * buf_to_scr
            # 用 moveTo 绝对坐标（而非 moveBy 相对偏移）
            target_x = int(round(scr_x + cum_mx))
            target_y = int(round(scr_y + cum_my))
            ic.moveTo(target_x, target_y)
            utils.sleep(_MAP_DRAG_STEP_MS / 1000.0)
        ic.mouseUp(btn)

    @staticmethod
    def _drag_curve(i: int, steps: int) -> float:
        """第 i 步（1..steps）结束时累计移动比例（0→1）。

        快速段：前 ``_MAP_DRAG_FAST_STEP_RATIO`` 比例的步走 ``_MAP_DRAG_FAST_DISTANCE_RATIO``
        比例的距离；慢速段：剩余步走剩余距离（减速收尾）。对照 BGI MapDragFast*。
        """
        p = i / steps
        fast_s = _MAP_DRAG_FAST_STEP_RATIO
        fast_d = _MAP_DRAG_FAST_DISTANCE_RATIO
        if fast_s <= 0 or fast_s >= 1:
            return p
        if p <= fast_s:
            return fast_d * (p / fast_s)
        rem = (p - fast_s) / (1.0 - fast_s)
        return fast_d + (1.0 - fast_d) * rem

    # ── 层切换 ──

    def switch_to_ground_layer(self, frame=None) -> bool:
        """若当前在地下层，切回地面层（传送入口默认走地面）。

        对照 BGI SwitchToGroundMapLayerIfNeeded（简化：仅检测 MapUndergroundToGroundButton）。
        """
        rect = vu.find_template(
            self.ctx,
            "teleport/MapUndergroundToGroundButton.png",
            threshold=0.8,
            frame=frame,
        )
        if rect is None:
            return False
        self.ctx.click_at(rect.cx, rect.cy)
        utils.sleep(0.4)
        return True

    # ── 国家切换 ──

    def switch_country(self, country: str, frame=None) -> bool:
        """切换到目标国家 tab（Teyvat 7 国）。OCR 右侧列表点国家名。

        对照 BGI TrySwitchArea（点右下区域选择 → OCR 右 1/3 → 点国家名）。
        """
        if country not in _COUNTRY_CENTERS:
            return False
        self.ctx.click_at(*_COUNTRY_BTN_POS)
        utils.sleep(0.3)
        for _ in range(_SWITCH_AREA_RETRIES):
            rect = vu.find_text(self.ctx, country, roi=_COUNTRY_OCR_ROI)
            if rect is not None:
                self.ctx.click_at(rect.cx, rect.cy)
                utils.sleep(0.3)
                return True
            utils.sleep(0.2)
        return False

    @staticmethod
    def country_center(country: str) -> tuple[float, float] | None:
        """国家中心坐标（avc x,y 系），用作拖拽起点预测/异常回退。"""
        return _COUNTRY_CENTERS.get(country)

    # ── 传送点图标 ──

    def find_tp_icon(self, target_type: str, frame=None) -> "Rect | None":
        """按 target.type 匹配传送点图标，返回最接近视口中心的命中。

        对照 BGI ClickTeleportTargetMapPoint（简化：取最近型匹配图标，
        不做 BGI 匈牙利算法相对模式 —— 留 v2）。
        """
        paths = self._icon_paths_for(target_type)
        if not paths:
            return None
        found = vu.find_all_templates(
            self.ctx, paths, threshold=_TP_ICON_THRESHOLD, frame=frame
        )
        best = None
        best_d = float("inf")
        for rects in found.values():
            for r in rects:
                d = math.hypot(r.cx - _MAP_CENTER_X, r.cy - _MAP_CENTER_Y)
                if d < best_d:
                    best_d = d
                    best = r
        return best

    @staticmethod
    def _icon_paths_for(target_type: str) -> list[str]:
        """该 type 的候选图标模板（秘境类补 Domain2）。"""
        primary = _TP_ICON_BY_TYPE.get(target_type)
        paths: list[str] = []
        if primary:
            paths.append(primary)
        if target_type in _DOMAIN_TYPES and "teleport/Domain2.png" not in paths:
            paths.append("teleport/Domain2.png")
        return paths


# 模块级常量导出（供 tp.py 复用）
DISPLAY_TP_ZOOM = _DISPLAY_TP_ZOOM
MAP_CENTER_X = _MAP_CENTER_X
MAP_CENTER_Y = _MAP_CENTER_Y
