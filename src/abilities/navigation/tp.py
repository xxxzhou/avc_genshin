"""传送能力 —— 通过地图 UI 操作传送到指定位置（Phase B）。

对照 BetterGI TpTask.cs（~1400 行）的简化实现：
- BGI 使用复杂的缩放/拖拽/图标匹配来精确定位传送点
- 我们的 v1 使用简化的地图 UI 操作：打开地图 → 搜索/拖拽 → 点击 → 确认
- quick_teleport 守护（Phase A）已处理自动确认传送对话框

tp.json 格式（BGI AutoTrackPath/Assets/tp.json）：
{
  "language": "CHS",
  "version": "...",
  "data": [{
    "sceneId": 3,
    "mapName": "Teyvat",
    "description": "提瓦特大陆",
    "points": [{
      "id": 1,
      "type": "OneTimeDomain|Goddess|TeleportWaypoint|...",
      "name": "北风之狼的庙宇",
      "country": "蒙德",
      "areas": ["坠星山谷"],
      "position": [x, y, z],
      "tranPosition": [x, y, z]
    }]
  }]
}

position[0] = x 坐标, position[2] = y 坐标（原神地图坐标系，z 轴忽略）。
tranPosition 为传送后实际到达位置。
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from framework import utils
from framework.resources import res

if TYPE_CHECKING:
    from avc._core import KeyCode
    from avc.image import IImageBuffer

    from abilities.vision_utils import Rect
    from framework.context import GameContext
    from framework.high_level_api import HighLevelApi


# ── 数据模型 ──


@dataclass(frozen=True)
class TpPosition:
    """传送点（对应 tp.json 中的单个 point）。"""

    id: int
    type: str  # "TeleportWaypoint" | "Goddess" | "OneTimeDomain" | "BlessDomain" | ...
    name: str
    country: str | None
    areas: tuple[str, ...]
    x: float  # 原神地图坐标 X（position[0]）
    y: float  # 原神地图坐标 Y（position[2]）
    tran_x: float  # 传送后实际到达 X（tranPosition[0]）
    tran_y: float  # 传送后实际到达 Y（tranPosition[2])


# ── 传送点数据库 ──


class TpDatabase:
    """加载并查询 tp.json 传送点数据库。"""

    def __init__(self, tp_json_path: Path | str | None = None):
        self._scenes: dict[str, list[TpPosition]] = {}
        if tp_json_path is not None:
            self._load(Path(tp_json_path))
        else:
            default = res.map("tp.json")
            if default.exists():
                self._load(default)

    def _load(self, path: Path) -> None:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        for scene in data.get("data", []):
            map_name = scene.get("mapName", "Teyvat")
            positions: list[TpPosition] = []
            for pt in scene.get("points", []):
                pos = pt.get("position", [0, 0, 0])
                tran = pt.get("tranPosition", pos)
                positions.append(
                    TpPosition(
                        id=pt.get("id", 0),
                        type=pt.get("type", ""),
                        name=pt.get("name", ""),
                        country=pt.get("country"),
                        areas=tuple(pt.get("areas", [])),
                        x=float(pos[0]) if len(pos) > 0 else 0.0,
                        y=float(pos[2]) if len(pos) > 2 else 0.0,
                        tran_x=float(tran[0]) if len(tran) > 0 else 0.0,
                        tran_y=float(tran[2]) if len(tran) > 2 else 0.0,
                    )
                )
            self._scenes[map_name] = positions

    @property
    def scenes(self) -> dict[str, list[TpPosition]]:
        return dict(self._scenes)

    def find_nearest(
        self,
        x: float,
        y: float,
        map_name: str = "Teyvat",
        n: int = 2,
    ) -> list[TpPosition]:
        """找距离 (x, y) 最近的 N 个传送点（欧氏距离）。"""
        positions = self._scenes.get(map_name, [])
        if not positions:
            return []
        sorted_pos = sorted(
            positions,
            key=lambda p: math.hypot(p.x - x, p.y - y),
        )
        return sorted_pos[:n]

    def find_by_name(
        self,
        name: str,
        map_name: str = "Teyvat",
    ) -> TpPosition | None:
        """按名称查找传送点（精确匹配优先，其次包含匹配）。"""
        positions = self._scenes.get(map_name, [])
        # 精确匹配
        for p in positions:
            if p.name == name:
                return p
        # 包含匹配
        name_lower = name.lower()
        for p in positions:
            if name_lower in p.name.lower():
                return p
        return None

    def find_by_type(
        self,
        tp_type: str,
        map_name: str = "Teyvat",
    ) -> list[TpPosition]:
        """按类型查找传送点。"""
        return [p for p in self._scenes.get(map_name, []) if p.type == tp_type]


# ── 传送执行器 ──


# 1080p 常量（对照 BGI TpTask.cs）
_MAP_CENTER_X = 960
_MAP_CENTER_Y = 540
_CLICK_SAFE_MARGIN = 35  # 距屏幕边缘安全距离
_MAP_UI_OVERLAY_W = 360  # 左上角 UI 覆盖区域宽
_MAP_UI_OVERLAY_H = 400  # 左上角 UI 覆盖区域高
# ⚠ 2026-08-22 实机（r_20260822_023332）：目标锚点被玩家自定义标记 pin 覆盖
# （panel=MARKER ×3 连 pin_blocking 放弃）→ 扩到 6 个候选图标，跳过被 pin
# 盖住的锚点换稍远的干净锚点。
_TELEPORT_RETRY_COUNT = 6
_TELEPORT_WAIT_MAIN_UI_TIMEOUT = 60.0  # 传送完成后等待主界面的超时(秒)

# 导航循环（对照 BGI MoveMapToCore）
_MAX_NAV_ITER = 30  # 拖拽循环最大迭代数（BGI MaxIterations）
_MAX_NAV_FAILS = 3  # 连续 SIFT 定位失败上限 → 触发地图复位
_MAP_RESET_LIMIT = 3  # 地图复位（M 关/开图）上限，超过仍失败 → 中止
_NAV_NO_PROGRESS_LIMIT = 5  # 拖拽后 dist 连续 N 轮不降 → 复位地图视图（卡坏状态）
_MOVE_TOLERANCE = 200.0  # 目标进入容差即停止拖拽（game 单位，BGI Tolerance）
_MAP_ZOOM_OUT_DISTANCE = 1500.0  # 远距先缩小阈值（game 单位，≈BGI MapZoomOutDistance 1000px@zoom4）
_TELEPORT_MAX_ZOOM = 6.0  # 最大缩小档（BGI TeleportMaxZoomLevel）
_TRAVEL_ZOOM_CAP = 5.5  # 导航缩放上限：≥5.7 为全图概览档（无平移余量，勿进）
_ZOOM_TOL = 0.3  # 缩放达标容差（与 map_ops._ZOOM_TOLERANCE 一致）
_CONFIRM_WAIT_TIMEOUT = 5.0  # 等待传送按钮出现的兜底超时（秒）


class Teleporter:
    """通过地图 UI 操作执行传送。

    Flow (简化版，对照 BGI TpTask.TpOnce):
    1. 解析目标: 名称 → TpPosition, 或 (x,y) → 最近 TpPosition
    2. 打开地图: 按 M, 等待 MAP 场景
    3. 拖拽地图到目标区域
    4. 点击目标点
    5. 等待传送面板 → 点击确认
    6. 等待 MAIN_UI (传送完成)
    """

    def __init__(self, ctx: GameContext, g: HighLevelApi):
        self.ctx = ctx
        self.g = g
        self._db = TpDatabase()
        self._pg = None  # PositionGetter（大图 SIFT 定位 + prev 锚定，懒加载）
        self._map_open_seed: tuple[float, float] | None = None  # 开图前玩家位置（SIFT seed）

    def teleport_to(
        self,
        name_or_pos: str | tuple[float, float],
        map_name: str = "Teyvat",
    ) -> tuple[float, float]:
        """传送到指定位置。返回实际到达位置 (tran_x, tran_y)。

        Args:
            name_or_pos: 传送点名称（如"蒙德城"）或坐标 (x, y)
            map_name: 地图名称（默认"Teyvat"）
        """
        # 1. 解析目标
        target = self._resolve_target(name_or_pos, map_name)
        if target is None:
            raise ValueError(f"未找到传送点: {name_or_pos}")

        # 0. 确保原神窗口在前台：后续大量鼠标/键盘操作，焦点不在游戏则全部落空
        try:
            self.ctx.sc.activateWindow("原神")
        except Exception:
            pass
        utils.sleep(0.3)

        # 2. 打开地图
        self._open_map()

        # 3. 拖拽地图到目标区域，返回候选点击点（按距视口中心排序）
        candidates = self._navigate_map_to_target(target)

        # 4. 依次点击候选图标并确认传送面板（quick_teleport 守护也会自动确认）
        #    点击命中自定义标记 pin → 打开标记面板 → Esc 关闭后换下一候选重试
        self._click_and_confirm_teleport(candidates)

        # 5. 等待传送完成（超时 = 确认点击未生效/截图冻结 → 归因报错，
        #    不再盲继续。2026-08-15 实机：确认"成功"后地图仍开着，走路段盲走假定位）
        # ⚠ 2026-08-15 实机（r_20260815_074025）：传送成功落地主界面，但传送期间
        # SourcePlayer 帧冻结触发 capture.stale → scene_estimator 守护用 IScreenCapture
        # 回退帧在过渡瞬间判 MAP/UNKNOWN → wait_main_ui 等 60s 仍 MAP → 误判失败。
        # 修复：wait_main_ui 前强制 capture + classify_scene 一次，把 shared.scene 刷到
        # 实时画面（主界面→MAIN_UI），scene_estimator 守护高频更新有滞后。
        self._force_scene_refresh()
        if not self.g.wait_main_ui(timeout=_TELEPORT_WAIT_MAIN_UI_TIMEOUT):
            self.ctx.observe.event(
                "tp.confirm", ability="tp", phase="act",
                ok=False, reason="wait_main_ui_timeout",
            )
            from framework.errors import TaskError

            raise TaskError(
                f"传送后 {_TELEPORT_WAIT_MAIN_UI_TIMEOUT}s 未回到主界面"
                f"（确认点击未生效或画面冻结）: {name_or_pos}"
            )

        # 6. 传送后锚定定位（解决无 prev 时 6 层小地图定位选错层）
        self._set_prev_position(target)

        return (target.tran_x, target.tran_y)

    def _resolve_target(
        self,
        name_or_pos: str | tuple[float, float],
        map_name: str,
    ) -> TpPosition | None:
        """解析传送目标。

        可观测性：发 ``tp.resolve``（ability=tp, phase=decide, target, candidates, selected, ok）。
        痛点①「传送点选远」：坐标输入时展示 DB 最近 N 候选(name/type/距离)+选中谁，
        AI 据此判定是否选了过远的点（如地脉花最近传送点应为最近候选）。
        """
        if isinstance(name_or_pos, str):
            target = self._db.find_by_name(name_or_pos, map_name)
            self.ctx.observe.event(
                "tp.resolve", ability="tp", phase="decide",
                target=str(name_or_pos), map=map_name,
                selected=target.name if target else None,
                ok=target is not None,
                reason=None if target else "name_not_found",
            )
            return target
        x, y = name_or_pos
        candidates = self._db.find_nearest(x, y, map_name, n=3)
        target = candidates[0] if candidates else None
        self.ctx.observe.event(
            "tp.resolve", ability="tp", phase="decide",
            target=f"({x:.0f},{y:.0f})", map=map_name,
            candidates=[
                {"name": c.name, "type": c.type,
                 "dist": round(math.hypot(c.x - x, c.y - y))}
                for c in candidates
            ],
            selected=target.name if target else None,
            ok=target is not None,
            reason=None if target else "no_nearest",
        )
        return target

    def _open_map(self) -> None:
        """打开地图界面（按 M 键）。

        开图前先用小地图定位记录玩家位置——开图后地图以玩家为中心，把该位置
        作为大图 SIFT 的期望中心 seed（避免全图匹配在相似地形上假阳性返回错位
        置：2026-08-15 实机，玩家 (7,263) 被定位成 (-56,1224)，后续拖拽方向全错）。
        """
        from avc._core import KeyCode

        from framework.scene import Scene

        # 如果已在地图界面，无需操作
        if self.g.scene is not None and self.g.scene.scene is Scene.MAP:
            return
        # 开图前小地图定位（失败容忍——None 则首轮走全图匹配）
        self._map_open_seed = None
        try:
            if self._pg is None:
                from abilities.navigation.position import PositionGetter

                self._pg = PositionGetter(self.ctx)
            self._map_open_seed = self._pg.get_position()
        except Exception:
            self._map_open_seed = None
        # 释放所有按键
        self.ctx.release_all_keys()
        # 按 M 打开地图
        self.ctx.press(KeyCode.m)
        # 等待 MAP 场景
        self.g.wait_scene(Scene.MAP, timeout=10.0)
        # 开图动画 settle（2026-08-22 实机 r_20260822_031847：wait_scene 过早返回，
        # SIFT 跑在半开的地图上 → 全图匹配假阳性 dist 6459（目标距玩家仅 60）→
        # 拖拽马拉松。实证：settle 1.5s 后 SIFT 精准）
        utils.sleep(1.0)

    def _reset_map_view(self) -> None:
        """大地图视口复位：SIFT 连续定位失败（视口漂到海洋/未开放区）时，
        按 M 关图再开图，地图会以玩家当前位置为中心重新定位。

        用户建议：定位不到时"先 M 回去，再 M 打开重新定位"。
        复位后 zoom 保持，但视口回到玩家所在陆地。
        """
        from avc._core import KeyCode

        from framework.scene import Scene

        self.ctx.release_all_keys()
        utils.sleep(0.3)
        self.ctx.press(KeyCode.m)  # 关图
        utils.sleep(0.6)
        self.ctx.press(KeyCode.m)  # 重开，以玩家为中心
        self.g.wait_scene(Scene.MAP, timeout=10.0)
        utils.sleep(0.5)

    def _navigate_map_to_target(self, target: TpPosition) -> "list[Rect]":
        """在地图上拖拽到目标位置，返回候选传送点图标点击位置。

        对照 BGI TpOnce 阶段 3-5（MoveMapToCore + ClickTeleportTargetMapPoint）：
        切地面层 → 切国家 tab → SIFT 视口重定位循环（定位→算偏移→拖拽→重定位，
        直到目标进入容差）→ 放大让图标显示 → 匹配目标 type 图标 → 返回候选。

        返回按距视口中心升序的候选图标（点击留到 _click_and_confirm_teleport，
        因为玩家自定义标记可能覆盖部分图标，需逐个点击+OCR 确认换点重试）；
        无命中时兜底返回视口中心。

        v1 简化：异常检测只做"连续 N 次定位失败中止"；图标匹配取最近型候选
        （不做 BGI 匈牙利算法相对模式，留 v2）。
        """
        from abilities.navigation.map_ops import DISPLAY_TP_ZOOM, MapController
        from framework.scene import Scene

        ob = self.ctx.observe
        if self.g.scene is None or self.g.scene.scene is not Scene.MAP:
            ob.event("tp.navigate", ability="tp", phase="decide", ok=False,
                     reason="not_in_map", scene=str(self.g.scene))
            return

        mc = MapController(self.ctx, self.g)
        frame = self.ctx.capture()
        z0 = mc.measure_zoom_level(frame)

        # 1. 切地面层（传送入口默认走地面）
        grounded = mc.switch_to_ground_layer(frame)
        ob.event("tp.navigate", ability="tp", phase="observe",
                 zoom=z0, grounded=grounded, throttle_key="tp.navigate:init")
        frame = self.ctx.capture()

        # 1.5 概览档退出：z≥5.7 时地图进入"整块大陆小于视口"的全图概览模式
        # （四周黑边，**无平移余量**，任何拖拽无效——地图还会记忆上次视图，任务间
        # 状态残留）。2026-08-15 实机定论：dist 卡死循环主因。先缩回工作档。
        if z0 is not None and z0 >= 5.7:
            mc.set_zoom_level(4.4, frame)
            frame = self.ctx.capture()

        # 2. 切国家 tab（v1 禁用：实机确认 (1760,1020) 按钮本版本点不开国家列表，
        #    且点击会误动地图/可能弹面板拦截后续拖拽。SIFT 定位+拖拽可自行跨区收敛。
        #    留 v2：换按钮位置 + OCR ReplaceDictionary（蒙德→蒙徳）。）
        # if target.country:
        #     ok = mc.switch_country(target.country, frame)
        #     frame = self.ctx.capture()

        # 3. MoveMapToCore 循环：SIFT 定位视口 → 算偏移 → 拖拽 → 重定位
        # 首轮无 expected（全图 Match）；后续传上轮 center（切块局部，BGI KnnMatchLocal）。
        # 定位连续失败（典型：视口漂到海洋/未开放区）→ M 关/开图复位到玩家位置再继续。
        fail_streak = 0
        reset_count = 0
        best_dist: float | None = None  # dist 无进展检测基线
        no_progress_iters = 0
        # 首轮期望中心 = 开图前小地图定位的玩家位置（地图以玩家为中心打开；
        # 无 seed 时 None 走全图匹配）
        last_center: tuple[float, float] | None = getattr(
            self, "_map_open_seed", None
        )
        for i in range(_MAX_NAV_ITER):
            self.ctx.check_cancel()  # GuardRail/F9 取消即退（同步循环须自查）
            center = self._big_map_position(last_center)
            if center is None:
                fail_streak += 1
                ob.event("tp.navigate", ability="tp", phase="observe", iter=i,
                         ok=False, reason="sift_fail", fail_streak=fail_streak,
                         throttle_key="tp.navigate:sift")
                if fail_streak >= _MAX_NAV_FAILS:
                    if reset_count >= _MAP_RESET_LIMIT:
                        ob.event("tp.navigate", ability="tp", phase="decide",
                                 ok=False, reason="nav_abort",
                                 reset_count=reset_count, target=target.name)
                        raise RuntimeError(
                            f"大图视口连续 {fail_streak} 次 SIFT 定位失败"
                            f"且复位 {reset_count} 次仍无法导航到 {target.name}"
                        )
                    reset_count += 1
                    fail_streak = 0
                    last_center = None
                    ob.event("tp.navigate", ability="tp", phase="act",
                             reason="map_reset", reset_count=reset_count,
                             throttle_key="tp.navigate:reset")
                    self._reset_map_view()
                    frame = self.ctx.capture()
                    continue
                utils.sleep(0.3)
                frame = self.ctx.capture()
                continue
            fail_streak = 0
            last_center = center

            dx = target.x - center[0]
            dy = target.y - center[1]
            dist = math.hypot(dx, dy)
            ob.event("tp.navigate", ability="tp", phase="observe", iter=i,
                     center=(round(center[0]), round(center[1])),
                     target=(round(target.x), round(target.y)),
                     dist=round(dist), ok=dist < _MOVE_TOLERANCE,
                     throttle_key="tp.navigate:iter")
            if dist < _MOVE_TOLERANCE:
                break  # 目标进入容差 → 去点图标

            # dist 无进展检测：拖拽连续发出但 dist 不降 = 视口卡坏状态
            # （拖拽被吞/概览档残留/特殊视角），SIFT 却"正常"→ 原复位逻辑
            # （仅 SIFT 失败触发）永不命中。2026-08-15 实机：视口卡 (-68,1224)
            # 多轮任务拖拽全无效。→ 主动 M 复位回到玩家中心。
            if best_dist is None or dist < best_dist - 5.0:
                best_dist = dist
                no_progress_iters = 0
            else:
                no_progress_iters += 1
                if no_progress_iters >= _NAV_NO_PROGRESS_LIMIT:
                    if reset_count >= _MAP_RESET_LIMIT:
                        ob.event("tp.navigate", ability="tp", phase="decide",
                                 ok=False, reason="nav_abort_no_progress",
                                 dist=round(dist), reset_count=reset_count,
                                 target=target.name)
                        raise RuntimeError(
                            f"大图拖拽 {no_progress_iters} 轮无进展且复位 "
                            f"{reset_count} 次仍无法导航到 {target.name}"
                        )
                    reset_count += 1
                    no_progress_iters = 0
                    best_dist = None
                    # 复位后地图回到玩家中心：seed 复用（玩家在图中不动）
                    last_center = getattr(self, "_map_open_seed", None)
                    ob.event("tp.navigate", ability="tp", phase="act",
                             reason="map_reset_no_progress", reset_count=reset_count,
                             dist=round(dist), throttle_key="tp.navigate:reset")
                    self._reset_map_view()
                    frame = self.ctx.capture()
                    continue

            zoom = mc.measure_zoom_level(frame) or 4.0
            # 远距按比例缩小（BGI MoveMapToCore：target = cur * dist / 阈值）。
            # ⚠ 上限 5.5：z≥5.7 进入全图概览模式（大陆小于视口、无平移余量），
            # 拖拽全部无效（2026-08-15 实机定论）。
            if dist > _MAP_ZOOM_OUT_DISTANCE and zoom < _TRAVEL_ZOOM_CAP - _ZOOM_TOL:
                target_zoom = min(_TRAVEL_ZOOM_CAP, zoom * dist / _MAP_ZOOM_OUT_DISTANCE)
                mc.set_zoom_level(target_zoom, frame)
                frame = self.ctx.capture()
                zoom = mc.measure_zoom_level(frame) or zoom

            mc.drag_map(dx, dy, zoom)
            utils.sleep(0.3)
            frame = self.ctx.capture()

        # 4. 放大到 DisplayTpPointZoomLevel 让传送点图标显示
        mc.set_zoom_level(DISPLAY_TP_ZOOM, frame)
        frame = self.ctx.capture()

        # 5. 匹配目标 type 图标，返回候选点击点；未命中兜底视口中心（最后一次定位处）
        icons = mc.find_tp_icons(target.type, frame)
        if icons:
            ob.event("tp.navigate", ability="tp", phase="decide", icon_type=target.type,
                     icons=len(icons), ok=True, fallback=False,
                     hit_pos=[(round(ic.cx), round(ic.cy)) for ic in icons[:4]])
            return icons
        # 痛点①直接证据：未命中任何传送点图标 → 兜底点视口中心（可能选远/选错）
        ob.event("tp.navigate", ability="tp", phase="decide", icon_type=target.type,
                 icons=0, ok=False, reason="icon_miss_fallback_center", fallback=True)
        return [self._center_rect()]

    @staticmethod
    def _center_rect() -> "Rect":
        """视口中心兜底点击点。"""
        from abilities.vision_utils import Rect

        return Rect(_MAP_CENTER_X, _MAP_CENTER_Y, 0, 0, 0.0)

    def _click_and_confirm_teleport(self, candidates: "list[Rect]") -> None:
        """依次点击候选传送点图标并 OCR 确认传送面板。

        对照 BGI ClickTpPoint + HandleTeleportPanel（2417-2442）：
        - 点击后检测到传送面板（OCR '传送'）→ 点击按钮/按 F 确认，完成
        - 点击命中自定义标记 → 打开标记面板（OCR '追踪'/'总标记' 等）→ Esc 关闭，
          换下一个候选图标重试（避 pin：优先点无标记覆盖的锚点）
        - 无面板 → 短等后换下一候选

        可观测性：每候选发 ``tp.confirm``（ability=tp, phase=act, click, panel, pin_covered, ok）。
        痛点①：panel=MARKER/pin_covered=True 说明被自定义标记拦截（避 pin 是否生效）。
        """
        ob = self.ctx.observe
        if not candidates:
            candidates = [self._center_rect()]
        for icon in candidates[:_TELEPORT_RETRY_COUNT]:
            self.g.click(icon.cx, icon.cy)
            try:
                self.ctx.save_debug("debug/after_icon_click.png")  # 存帧看点击后画面
            except Exception:
                pass
            confirmed, kind_name, pin_covered = self._wait_and_confirm_teleport()
            ob.event("tp.confirm", ability="tp", phase="act",
                     click=(round(icon.cx), round(icon.cy)),
                     panel=kind_name, pin_covered=pin_covered, ok=confirmed,
                     reason="pin_blocking" if pin_covered and not confirmed else None)
            if confirmed:
                return
        ob.event("tp.confirm", ability="tp", phase="decide", ok=False,
                 reason="no_panel_all_candidates")

    def _wait_and_confirm_teleport(self) -> tuple[bool, str, bool]:
        """等待传送确认面板出现并确认（OCR 面板检测，兜底）。

        quick_teleport 守护（Scene.MAP 下活跃）是主确认路径；本方法在守护未挂载时兜底。
        对照 BGI TpTask.HandleTeleportPanel（2417-2442）：检测到传送按钮后点击/按 F 确认。

        返回 ``(是否已确认传送, 面板类型名, 是否被 pin 覆盖)``；未确认时调用方换下一候选重试。
        """
        import time

        from avc._core import KeyCode

        from abilities.tp_panel import (
            TeleportPanelKind,
            close_marker_panel,
            detect_tp_panel,
            find_teleport_button,
        )

        deadline = time.time() + _CONFIRM_WAIT_TIMEOUT
        last_kind = "NONE"
        while time.time() < deadline:
            frame = self.ctx.capture()
            kind = detect_tp_panel(self.ctx, frame)
            last_kind = kind.name
            if kind is TeleportPanelKind.TELEPORT:
                try:
                    self.ctx.save_debug("debug/go_teleport_detected.png")  # 存帧诊断面板实际状态
                except Exception:
                    pass
                btn = find_teleport_button(self.ctx, frame)
                if btn is not None:
                    self.g.click(btn.cx, btn.cy)
                else:
                    # OCR 未定位到按钮文字，按 F 兜底（BGI HandleTeleportPanel 按 F）
                    self.ctx.press(KeyCode.f)
                return True, kind.name, False
            if kind is TeleportPanelKind.MARKER:
                # 命中标记面板（自定义标记覆盖传送点），Esc 关闭后换下一候选重试
                close_marker_panel(self.ctx)
                return False, kind.name, True
            utils.sleep(0.3)
        return False, last_kind, False

    def _big_map_position(
        self, expected: tuple[float, float] | None = None
    ) -> tuple[float, float] | None:
        """SIFT 大图视口重定位（委托 PositionGetter.get_position_from_big_map）。

        expected: 上轮定位的游戏坐标，传给切块局部搜索（BGI KnnMatchLocal）；
        None 走全图兜底（BGI Match，首轮或丢失时用）。
        """
        if self._pg is None:
            from abilities.navigation.position import PositionGetter

            self._pg = PositionGetter(self.ctx)
        return self._pg.get_position_from_big_map(expected_center=expected)

    def _set_prev_position(self, target: TpPosition) -> None:
        """传送后锚定小地图定位 prev（避免无 prev 时 6 层定位选错层）。"""
        if self._pg is None:
            from abilities.navigation.position import PositionGetter

            self._pg = PositionGetter(self.ctx)
        self._pg.set_prev_position(target.tran_x, target.tran_y)

    def _force_scene_refresh(self) -> None:
        """传送确认点击后强制刷一次 scene 到 shared.scene。

        解决 2026-08-15 实机 r_074025：传送成功但 SourcePlayer 冻结期间
        scene_estimator 守护高频判定滞后（IScreenCapture 回退帧在过渡瞬间判
        MAP/UNKNOWN）→ wait_main_ui 等 60s 仍 MAP → 误判失败。
        直接 capture + classify_scene 一次写入 shared.scene，让 wait_main_ui
        拿到实时 scene。失败静默（不阻断：wait_main_ui 仍兜底超时归因）。
        """
        try:
            frame = self.ctx.capture()
            if frame is None:
                return
            from framework.scene import classify_scene
            from dataclasses import replace
            import time as _time

            state = classify_scene(frame)
            prev = self.g.runtime.shared.scene if self.g.runtime and self.g.runtime.shared else None
            since = prev.since if (prev and prev.scene == state.scene) else _time.monotonic()
            self.g.runtime.shared.scene = replace(state, since=since)
            self.ctx.observe.event(
                "tp.scene_refresh", ability="tp", phase="act",
                ok=True, scene=str(state.scene),
                throttle_key="tp.scene_refresh",
            )
        except Exception as e:
            self.ctx.observe.event(
                "tp.scene_refresh", ability="tp", phase="act",
                ok=False, reason="exception", detail=repr(e),
                throttle_key="tp.scene_refresh",
            )

    @staticmethod
    def _is_clickable(x: float, y: float) -> bool:
        """检查点击位置是否在可点击区域（对照 BGI IsGameRegionPointInClickableArea）。"""
        # 避开左上角 UI 覆盖区域
        if x < _MAP_UI_OVERLAY_W and y < _MAP_UI_OVERLAY_H:
            return False
        # 避开屏幕边缘
        if x < _CLICK_SAFE_MARGIN or y < _CLICK_SAFE_MARGIN:
            return False
        if x > 1920 - _CLICK_SAFE_MARGIN or y > 1080 - _CLICK_SAFE_MARGIN:
            return False
        return True
