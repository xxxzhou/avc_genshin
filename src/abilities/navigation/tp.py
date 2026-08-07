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
_TELEPORT_RETRY_COUNT = 3
_TELEPORT_WAIT_MAIN_UI_TIMEOUT = 60.0  # 传送完成后等待主界面的超时(秒)

# 导航循环（对照 BGI MoveMapToCore）
_MAX_NAV_ITER = 30  # 拖拽循环最大迭代数（BGI MaxIterations）
_MAX_NAV_FAILS = 3  # 连续 SIFT 定位失败上限 → 中止
_MOVE_TOLERANCE = 200.0  # 目标进入容差即停止拖拽（game 单位，BGI Tolerance）
_MAP_ZOOM_OUT_DISTANCE = 1500.0  # 远距先缩小阈值（game 单位，≈BGI MapZoomOutDistance 1000px@zoom4）
_TELEPORT_MAX_ZOOM = 6.0  # 最大缩小档（BGI TeleportMaxZoomLevel）
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

        # 2. 打开地图
        self._open_map()

        # 3. 拖拽地图到目标区域并点击
        self._navigate_map_to_target(target)

        # 4. 等待传送面板确认（quick_teleport 守护会自动确认）
        #    如果守护未激活，手动检测并确认
        self._wait_and_confirm_teleport()

        # 5. 等待传送完成
        self.g.wait_main_ui(timeout=_TELEPORT_WAIT_MAIN_UI_TIMEOUT)

        # 6. 传送后锚定定位（解决无 prev 时 6 层小地图定位选错层）
        self._set_prev_position(target)

        return (target.tran_x, target.tran_y)

    def _resolve_target(
        self,
        name_or_pos: str | tuple[float, float],
        map_name: str,
    ) -> TpPosition | None:
        """解析传送目标。"""
        if isinstance(name_or_pos, str):
            return self._db.find_by_name(name_or_pos, map_name)
        x, y = name_or_pos
        nearest = self._db.find_nearest(x, y, map_name, n=1)
        return nearest[0] if nearest else None

    def _open_map(self) -> None:
        """打开地图界面（按 M 键）。"""
        from avc._core import KeyCode

        from framework.scene import Scene

        # 如果已在地图界面，无需操作
        if self.g.scene is not None and self.g.scene.scene is Scene.MAP:
            return
        # 释放所有按键
        self.ctx.release_all_keys()
        # 按 M 打开地图
        self.ctx.press(KeyCode.m)
        # 等待 MAP 场景
        self.g.wait_scene(Scene.MAP, timeout=10.0)

    def _navigate_map_to_target(self, target: TpPosition) -> None:
        """在地图上拖拽到目标位置并点击传送点图标。

        对照 BGI TpOnce 阶段 3-5（MoveMapToCore + ClickTeleportTargetMapPoint）：
        切地面层 → 切国家 tab → SIFT 视口重定位循环（定位→算偏移→拖拽→重定位，
        直到目标进入容差）→ 放大让图标显示 → 匹配目标 type 图标 → 点击。

        v1 简化：异常检测只做"连续 N 次定位失败中止"；图标匹配取最近型命中
        （不做 BGI 匈牙利算法相对模式，留 v2）。
        """
        from abilities.navigation.map_ops import DISPLAY_TP_ZOOM, MapController
        from framework.scene import Scene

        if self.g.scene is None or self.g.scene.scene is not Scene.MAP:
            return

        mc = MapController(self.ctx, self.g)
        frame = self.ctx.capture()

        # 1. 切地面层（传送入口默认走地面）
        mc.switch_to_ground_layer(frame)
        frame = self.ctx.capture()

        # 2. 切国家 tab（按 target.country，Teyvat 7 国）
        if target.country:
            mc.switch_country(target.country, frame)
            frame = self.ctx.capture()

        # 3. MoveMapToCore 循环：SIFT 定位视口 → 算偏移 → 拖拽 → 重定位
        # 首轮无 expected（全图 Match）；后续传上轮 center（切块局部，BGI KnnMatchLocal）
        fail_streak = 0
        last_center: tuple[float, float] | None = None
        for _ in range(_MAX_NAV_ITER):
            center = self._big_map_position(last_center)
            if center is None:
                fail_streak += 1
                if fail_streak >= _MAX_NAV_FAILS:
                    raise RuntimeError(
                        f"大图视口连续 {fail_streak} 次 SIFT 定位失败，无法导航到 {target.name}"
                    )
                utils.sleep(0.3)
                frame = self.ctx.capture()
                continue
            fail_streak = 0
            last_center = center

            dx = target.x - center[0]
            dy = target.y - center[1]
            dist = math.hypot(dx, dy)
            if dist < _MOVE_TOLERANCE:
                break  # 目标进入容差 → 去点图标

            zoom = mc.measure_zoom_level(frame) or 4.0
            # 远距先缩小（拖拽更快），上限 _TELEPORT_MAX_ZOOM
            if dist > _MAP_ZOOM_OUT_DISTANCE and zoom < _TELEPORT_MAX_ZOOM:
                mc.set_zoom_level(min(_TELEPORT_MAX_ZOOM, zoom + 1.5), frame)
                frame = self.ctx.capture()
                zoom = mc.measure_zoom_level(frame) or zoom

            mc.drag_map(dx, dy, zoom)
            utils.sleep(0.3)
            frame = self.ctx.capture()

        # 4. 放大到 DisplayTpPointZoomLevel 让传送点图标显示
        mc.set_zoom_level(DISPLAY_TP_ZOOM, frame)
        frame = self.ctx.capture()

        # 5. 匹配目标 type 图标并点击；未命中兜底点视口中心（最后一次定位处）
        icon = mc.find_tp_icon(target.type, frame)
        if icon is not None:
            self.g.click(icon.cx, icon.cy)
        else:
            self.g.click(_MAP_CENTER_X, _MAP_CENTER_Y)

    def _wait_and_confirm_teleport(self) -> None:
        """等待传送确认面板出现并点击传送按钮（兜底）。

        quick_teleport 守护（Scene.MAP 下活跃）是主确认路径；本方法在守护未挂载时兜底。
        用模板匹配 GoTeleport 按钮真实位置（替代硬编码坐标）。
        """
        import time

        from abilities import vision_utils as vu
        from abilities.game_state import has_go_teleport

        deadline = time.time() + _CONFIRM_WAIT_TIMEOUT
        while time.time() < deadline:
            frame = self.ctx.capture()
            if frame is not None and has_go_teleport(self.ctx, frame):
                rect = vu.find_template(self.ctx, "teleport/GoTeleport.png", frame=frame)
                if rect is not None:
                    self.g.click(rect.cx, rect.cy)
                    return
            utils.sleep(0.3)

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
