"""大地图操作原语 —— MapController（Phase B，对照 BGI TpTask.cs 的地图 UI 操作）。

封装地图打开后的视口操作：缩放测量/控制、拖拽平移、地面层切换、国家切换、
传送点图标匹配。供 ``tp.py`` 的 ``_navigate_map_to_target``（MoveMapToCore 循环）调用。

所有方法假设调用时已处于 ``Scene.MAP``（由调用方保证）。坐标均为 1080p buffer 坐标。

已 2026-08-08 实机标定（verify do_map_calib）：滚轮方向/每槽步长、拖拽方向/scale_factor。
保留常量的 BGI 来源注释；差异点（avc 语义）已就地标注。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
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
# avc scroll(0, dy) 中 dy 是滚轮槽数（C++ 内部 dy*WHEEL_DELTA），不是 WHEEL_DELTA 单位。
# ⚠ 大槽数单次无效：实机 scroll(0,±3)×3 → zoom delta=0（游戏把一次大滚动吞掉）；
# 必须逐槽发送（set_zoom_level 已逐槽 for 循环）。
_ZOOM_PER_NOTCH = 0.083  # 每滚轮槽 zoom 步长（2026-08-08 实机标定：scroll(0,+1)×5 → delta=-0.417）
_ZOOM_WHEEL_SIGN = -1  # scroll dy 与 zoom 增减关系（实机确认：+dy 缩小→zoom 减→sign=-1）
_MAX_ZOOM_NOTCHES = 16  # 单次缩放最大滚轮槽（BGI MaxMapZoomWheelBatchNotches）
_MAX_ZOOM_ATTEMPTS = 3  # 缩放重试轮数
_ZOOM_TOLERANCE = 0.3  # zoom 达标容差

# 拖拽平移（BGI GetMoveMapState / MouseMoveMap）
# ⚠ 2026-08-08 实机标定：SIFT 轴修复后重测（verify do_map_calib）：
# drag(+200)@zoom3.85 → 视口西轴移动 168 单位（84%），scale 偏低。
# 校准：_MAP_SCALE_FACTOR = 3.0 * (200/168) ≈ 3.57（buf_to_scr = sc.width()/1920 = 1293/1920 = 0.673）。
# 数学：total(物理px) = S*U/z*buf_to_scr；实测 px/unit@z ≈ 2.4/z → S ≈ 2.4/buf_to_scr ≈ 3.57。
_MAP_SCALE_FACTOR = 3.57  # 游戏单位→屏幕像素（zoom=1）；avc moveTo 语义，已含 dpi 补偿
_MAP_DRAG_PIXELS_PER_STEP = 48  # 每步最大像素（BGI MapDragPixelsPerStep）
_MAP_DRAG_FAST_STEP_RATIO = 0.42  # 快速段步数占比（BGI MapDragFastStepRatio）
_MAP_DRAG_FAST_DISTANCE_RATIO = 0.85  # 快速段距离占比（BGI MapDragFastDistanceRatio）
_MAP_DRAG_STEP_MS = 20  # 每步间隔（ms）
# 单次手势最大拖拽像素。⚠ 2026-08-14 实机诊断（diag_drag_far）：dist=8743 时
# 一次手势期望拖 9764px，光标顶死屏幕角 (0,0)，视口甩进海洋 → SIFT 连续失败 →
# tp.navigate 复位后重拖死循环（auto_boss dist=2739 卡点同源）。故按 400px/手势
# 分段：光标始终在 (960,650)±400 安全区内，多手势累计走完期望位移。
_MAP_DRAG_MAX_GESTURE_PX = 400
_DRAG_X_SIGN = 1  # 屏幕水平符号（西向增量→拖右：+西→+x→1）
_DRAG_Y_SIGN = 1  # 屏幕垂直符号（北向增量→拖下：+北→+y→1）
_DRAG_DPI_MULT = 0.0  # 已弃用：moveTo 绝对坐标模式下不需要 DPI 乘子（buf_to_scr 由 sc.width/1920 计算）

# 图标 / 区域
_DISPLAY_TP_ZOOM = 4.4  # 传送点图标显示所需 zoom（BGI DisplayTpPointZoomLevel）
_TP_ICON_THRESHOLD = 0.65  # 传送点图标匹配阈值（BGI NearbyMapIconTemplateThreshold）
_MAP_CENTER_X = 960
_MAP_CENTER_Y = 540
# 拖拽起点。⚠ 2026-08-08 标定 (960,650)；**2026-08-15 实机失效**：该点被玩家
# 位置标记 hitbox（中心 ~120px 宽、向下延伸到 ~650）拦截——mouseDown 落在标记上
# 整个手势被吞（像素差 0.2% vs 正常 80%+；当时只有 z6 缩最小档能拖）。改为
# **按拖拽方向自适应起点**：起点 = 中心往拖拽方向反向偏移 _DRAG_START_BACKOFF，
# 既避开中心标记 hitbox，又给 ≤400px 手势留足屏幕余量。实测可用起点：
# (960,780)/(960,450)/(1100,540)/(820,540)/(1500,800) 均 80%+ 像素差。
_MAP_DRAG_START_BACKOFF = 480  # 起点反向偏移（px；> 手势上限 400 留余量；已弃用，见 _DRAG_START_SAFE）
# 固定安全拖拽起点（buffer 坐标）。⚠ 2026-08-15 实机（calib_drag）确认：
# 原按方向自适应起点会撞上大地图右侧 UI（mouseDown 被吞）→ 改用固定安全点。
# 该点位于视口中心右侧 140px，避开中心玩家标记 hitbox，且任意方向 ≤400px 手势不出屏。
_DRAG_START_SAFE = (1100, 540)
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

# ── 地脉花图标模板 ──
# ⚠ 2026-08-15 实机标定（r_20260815_092822/094720）：官方 32×32 模板在 zoom=3.0
# 实机渲染下真位置仅 ~0.58-0.62，且最高分常落在**错误位置**（假阳性 ≥0.6 →
# 假花坐标 → 错误传送目标）。已移除，只留实机裁剪标定模板。
# Blossom_of_Wealth_live.png：zoom 3.0 档实机裁剪，真位置 1.0。
# revelation（启示）live 标定待实机画面出现启示之花后补。
_BLOSSOM_TEMPLATES: dict[str, str] = {
    "wealth": "map/Blossom_of_Wealth_live.png",
}
_BLOSSOM_THRESHOLD = 0.6  # 地脉花图标匹配阈值（比传送点低，花图标较小/变化多）


@dataclass(frozen=True, slots=True)
class BlossomCandidate:
    """大地图上检测到的地脉花候选。"""

    screen_x: float  # 花中心在 1080p buffer 上的 X
    screen_y: float  # 花中心在 1080p buffer 上的 Y
    blossom_type: str  # "revelation" | "wealth"
    score: float  # 模板匹配分数


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
        ⚠ 2026-08-22 实机（05:15）：概览档/过渡态渲染下模板分从 0.9 掉到 0.42
        → 阈值 0.8 全漏、zoom 永远测不到（地图卡概览档全链失效）。降 0.55 +
        ROI 扩到整条滑轨（极档位旋钮超出原窄条）；窄条内假阳性风险可忽略。
        """
        rect = vu.find_template(
            self.ctx,
            "teleport/MapScaleButton.png",
            threshold=0.55,
            roi=(20, 360, 80, 440),
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
        # ⚠ 滚轮作用于光标位置（2026-08-22 实机 05:14）：光标停在滑轨/面板/UI 元素
        # 上时滚轮全被吞（盲滚 30 槽 zoom 不动）。发轮前一律把光标归到地图中心。
        try:
            sx, sy = self.ctx.to_screen(_MAP_CENTER_X, _MAP_CENTER_Y)
            self.ctx.ic.moveTo(int(sx), int(sy))
        except Exception:
            pass
        cur = self.measure_zoom_level(frame)
        if cur is None:
            # 盲缩兜底（2026-08-22 实机 05:00）：概览档（z≥5.7 全大陆视图）下滑块
            # 旋钮超出标定 Y 范围/模板分骤降 → measure 返回 None → 原实现直接放弃，
            # 地图永远卡概览档（find_blossom 全链失效）。朝放大方向盲滚 3×10 槽，
            # 每轮重测。
            for _ in range(3):
                for _n in range(10):
                    # 放大方向（实测标定：scroll(0,-1) 使 measured zoom 增大=放大；
                    # 概览档滑块在顶部，唯一出路是放大）
                    self.ctx.ic.scroll(0, _ZOOM_WHEEL_SIGN)
                    utils.sleep(0.05)
                utils.sleep(0.4)
                frame = self.ctx.capture()
                cur = self.measure_zoom_level(frame)
                if cur is not None:
                    break
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
            # 逐槽发送（avc scroll(0,N) 一次发 N×WHEEL_DELTA，大 N 时游戏截断）
            dy_sign = _ZOOM_WHEEL_SIGN * (1 if diff > 0 else -1)
            for _ in range(notches):
                self.ctx.ic.scroll(0, dy_sign)
                utils.sleep(0.05)
            utils.sleep(0.15)
            frame = self.ctx.capture()
            cur = self.measure_zoom_level(frame)
            if cur is None:
                return None
        return cur

    # ── 拖拽 ──

    def drag_map(self, north_delta: float, west_delta: float, zoom_level: float) -> None:
        """拖拽地图：把视口中心沿游戏坐标移动 (north_delta, west_delta)。

        坐标约定与 tp.py TpPosition 一致（x=position[0]=北轴, y=position[2]=西轴）：
        - north_delta: 北向增量（+北 / -南）
        - west_delta:  西向增量（+西 / -东）

        屏幕映射（地图拖动物理：地图往下拖 → 看到北方；往右拖 → 看到西方）：
        - 屏幕水平偏移 ← +west_delta（往西 → 往右拖）
        - 屏幕垂直偏移 ← +north_delta（往北 → 往下拖）
        ⚠ 2026-08-08 实机确认（calib_drag）：原实现 (dx=北, dy=西) 错位成
        屏幕X←dx、屏幕Y←dy，导致拖拽方向偏 90°（视口漂到海洋/未开放区）。
        已改为上述正确映射。

        对照 BGI GetMoveMapState + MouseMoveMap：
        mouse_px = MapScaleFactor * |game_dist| / zoom_level，方向 sign(offset)；
        分 5-60 步（每步 ≤48px），快速段(前42%步走85%距)+慢速段曲线。

        ⚠ avc moveBy 在 mouseDown 状态下不触发游戏拖拽响应（游戏不认相对移动），
        改用 moveTo 绝对坐标：每步算出目标屏幕坐标，moveTo 移过去。
        moveTo 用逻辑屏幕像素，buffer→screen 缩放比 = screen_w/buf_w。
        """
        self.ctx.ensure_foreground()  # 拖拽全程 ic 直调，开头保证前台
        # 防御：若此前进程中断残留"左键按住"态（手势中途被杀），本手势会被整个吞掉。
        # 开头无条件 mouseUp 一次清态（无按键时是 no-op）。
        try:
            self.ctx.ic.mouseUp(self.ctx._MouseButton["left"])
        except Exception:
            pass
        zoom_level = zoom_level if zoom_level > 0 else 1.0
        # 剩余位移（游戏单位），按手势分段消耗
        rem_west = float(west_delta)
        rem_north = float(north_delta)
        gestures = 0
        while True:
            gx = _MAP_SCALE_FACTOR * abs(rem_west) / zoom_level  # 本段西向 → 屏幕水平 px
            gy = _MAP_SCALE_FACTOR * abs(rem_north) / zoom_level  # 本段北向 → 屏幕垂直 px
            gdist = math.hypot(gx, gy)
            if gdist < 1.0:
                break
            # 单手势超长 → 只走一部分（cap 在 _MAP_DRAG_MAX_GESTURE_PX），下一手势继续
            frac = 1.0 if gdist <= _MAP_DRAG_MAX_GESTURE_PX else _MAP_DRAG_MAX_GESTURE_PX / gdist
            self._drag_gesture(rem_west, rem_north, frac, zoom_level)
            rem_west *= 1.0 - frac
            rem_north *= 1.0 - frac
            gestures += 1
            if gestures > 60:  # 兜底（60×400px@zoom6 ≈ 4 万游戏单位，跨全图足够）
                break
        self.ctx.observe.event(
            "map.drag", ability="tp", phase="act",
            gestures=gestures,
            px=round(_MAP_SCALE_FACTOR * abs(west_delta) / zoom_level),
            ok=True,
            throttle_key="map.drag",
        )

    def _drag_gesture(
        self, west_delta: float, north_delta: float, frac: float, zoom_level: float
    ) -> None:
        """单次拖拽手势：把视口中心移动 frac 比例的 (north_delta, west_delta)。

        mouseDown → 分步 moveTo（快慢曲线）→ mouseUp。一次手势的屏幕位移有
        上限（光标不能出屏），长位移由 drag_map 分多手势完成。
        """
        total_x = _MAP_SCALE_FACTOR * abs(west_delta) * frac / zoom_level
        total_y = _MAP_SCALE_FACTOR * abs(north_delta) * frac / zoom_level
        dist = math.hypot(total_x, total_y)
        if dist < 1.0:
            return
        sign_x = _DRAG_X_SIGN * (1 if west_delta >= 0 else -1)  # 往西 → 往右拖（+x）
        sign_y = _DRAG_Y_SIGN * (1 if north_delta >= 0 else -1)  # 往北 → 往下拖（+y）

        steps = max(5, min(60, int(math.ceil(dist / _MAP_DRAG_PIXELS_PER_STEP))))
        ic = self.ctx.ic
        btn = self.ctx._MouseButton["left"]

        # buffer→screen 缩放比：moveTo 用逻辑屏幕像素
        # to_screen 转换起点绝对坐标，但偏移量需要按 sc 客户区/buffer 比例缩放
        # sc.width/height 是窗口客户区逻辑尺寸，buffer 是 1920×1080
        buf_w = 1920  # buffer 宽（1080p）
        buf_to_scr = self.ctx.sc.width() / buf_w if buf_w > 0 else 1.0

        # 起点屏幕坐标：固定安全起点（避开中心玩家标记 hitbox、顶部/右侧地图 UI）。
        # ⚠ 2026-08-15 实机（calib_drag）修正：原"按拖拽方向自适应起点" = 中心反向偏移
        # _DRAG_START_BACKOFF，会产生 (1440,540)/(960,60)/(480,540)/(960,1020) 等边缘起点。
        # 其中 (1440,540)（纯西向反向）落在大地图右侧 UI 上 → mouseDown 被吞 → 拖拽手势
        # 失效（Δwest=0），是 tp.navigate 视口卡死（nav_abort_no_progress）的一个来源。
        # 实测安全起点（像素差 80%+）：(960,780)/(960,450)/(1100,540)/(820,540)/(1500,800)。
        # 拖拽方向/位移由下方 moveTo 目标坐标决定，起点固定不影响方向。
        sx, sy = _DRAG_START_SAFE  # buffer 坐标
        scr_x, scr_y = self.ctx.to_screen(sx, sy)
        ic.moveTo(int(scr_x), int(scr_y))
        ic.mouseDown(btn)
        prev_frac = 0.0
        # 累计偏移（屏幕像素）
        cum_mx = 0.0
        cum_my = 0.0
        for i in range(1, steps + 1):
            frac_i = self._drag_curve(i, steps)
            inc = frac_i - prev_frac
            prev_frac = frac_i
            cum_mx += sign_x * total_x * inc * buf_to_scr
            cum_my += sign_y * total_y * inc * buf_to_scr
            # 用 moveTo 绝对坐标（而非 moveBy 相对偏移）
            target_x = int(round(scr_x + cum_mx))
            target_y = int(round(scr_y + cum_my))
            ic.moveTo(target_x, target_y)
            utils.sleep(_MAP_DRAG_STEP_MS / 1000.0)
        ic.mouseUp(btn)
        utils.sleep(0.12)  # 手势间短暂停顿，让地图惯性/渲染跟上

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

    def find_tp_icons(self, target_type: str, frame=None) -> "list[Rect]":
        """按 target.type 匹配所有传送点图标，按距视口中心距离升序返回。

        返回候选列表（而非单个）供传送链逐个点击：玩家地图可能有自定义标记
        覆盖部分图标，点击命中标记 pin 时需换下一个候选重试（见 tp.py 传送确认）。

        对照 BGI ClickTeleportTargetMapPoint（简化：候选按最近排序，
        不做 BGI 匈牙利算法相对模式 —— 留 v2）。
        """
        paths = self._icon_paths_for(target_type)
        if not paths:
            return []
        found = vu.find_all_templates(
            self.ctx, paths, threshold=_TP_ICON_THRESHOLD, frame=frame
        )
        icons: "list[Rect]" = []
        for rects in found.values():
            icons.extend(rects)
        icons.sort(key=lambda r: math.hypot(r.cx - _MAP_CENTER_X, r.cy - _MAP_CENTER_Y))
        return icons

    def find_tp_icon(self, target_type: str, frame=None) -> "Rect | None":
        """按 target.type 匹配传送点图标，返回最接近视口中心的命中（首个候选）。"""
        icons = self.find_tp_icons(target_type, frame=frame)
        return icons[0] if icons else None

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

    # ── 地脉花检测 ──

    def find_blossom_on_map(self, frame=None) -> list[BlossomCandidate]:
        """在大地图上检测地脉花图标，返回候选列表（按距视口中心距离升序）。

        同时匹配启示之花和藏金之花模板。需在 MAP 场景下调用，
        缩放等级需让花图标可见（zoom ≤ 3 左右，花图标在缩小级别显示）。
        """
        paths = list(_BLOSSOM_TEMPLATES.values())
        # 反向映射：模板文件名 → blossom_type
        name_to_type: dict[str, str] = {
            _BLOSSOM_TEMPLATES[k].split("/")[-1]: k for k in _BLOSSOM_TEMPLATES
        }
        found = vu.find_all_templates(
            self.ctx, paths, threshold=_BLOSSOM_THRESHOLD, frame=frame
        )
        candidates: list[BlossomCandidate] = []
        for tpl_name, rects in found.items():
            btype = name_to_type.get(tpl_name, "revelation")
            for r in rects:
                candidates.append(
                    BlossomCandidate(
                        screen_x=r.cx,
                        screen_y=r.cy,
                        blossom_type=btype,
                        score=r.score,
                    )
                )
        candidates.sort(
            key=lambda c: math.hypot(c.screen_x - _MAP_CENTER_X, c.screen_y - _MAP_CENTER_Y)
        )
        return candidates

    # ── 坐标转换 ──

    @staticmethod
    def screen_to_game(
        screen_x: float,
        screen_y: float,
        viewport_center: tuple[float, float],
        zoom_level: float,
    ) -> tuple[float, float]:
        """大地图屏幕坐标（1080p buffer）→ 游戏坐标。

        利用视口中心游戏坐标 + 屏幕偏移反算。
        原神大地图固定北朝上，屏幕方向=标准地图方向：
        - 屏幕右=东 → 西轴减小（东=-西）
        - 屏幕下=南 → 北轴减小（南=-北）

        从 drag_map() 的公式 screen_px = _MAP_SCALE_FACTOR * game_delta / zoom 反推：
        - 北向偏移 = (_MAP_CENTER_Y - screen_y) * zoom / _MAP_SCALE_FACTOR  (屏幕上=北)
        - 西向偏移 = (_MAP_CENTER_X - screen_x) * zoom / _MAP_SCALE_FACTOR  (屏幕左=西)

        Args:
            screen_x: 屏幕 X（1080p buffer 坐标）
            screen_y: 屏幕 Y（1080p buffer 坐标）
            viewport_center: SIFT 定位的视口中心游戏坐标 (北, 西)
            zoom_level: 当前缩放等级

        Returns:
            游戏坐标 (北, 西)，与 TpPosition.x/y 一致
        """
        if zoom_level <= 0:
            zoom_level = 1.0
        north_delta = (_MAP_CENTER_Y - screen_y) * zoom_level / _MAP_SCALE_FACTOR
        west_delta = (_MAP_CENTER_X - screen_x) * zoom_level / _MAP_SCALE_FACTOR
        return (viewport_center[0] + north_delta, viewport_center[1] + west_delta)


# 模块级常量导出（供 tp.py / high_level_api.py 复用）
DISPLAY_TP_ZOOM = _DISPLAY_TP_ZOOM
MAP_SCALE_FACTOR = _MAP_SCALE_FACTOR
MAP_CENTER_X = _MAP_CENTER_X
MAP_CENTER_Y = _MAP_CENTER_Y
