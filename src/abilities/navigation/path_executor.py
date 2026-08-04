"""路径执行器 —— 解析路径 JSON + 分段执行（Phase B）。

对照 BetterGI PathExecutor.cs（~1400 行）的简化实现：
- BGI 路径格式：{info:{name,type,map_name}, positions:[{id,type,x,y,move_mode,action}]}
- 核心逻辑：按传送点分割路径 → 逐段执行（传送 + 行走）
- v1 简化：不实现 action 处理器（fight/pick_up 等留给 Phase C/D）

路径 JSON 格式（BGI AutoPathing）：
{
  "info": {
    "name": "急冻树前往",
    "type": "collect",
    "map_name": "Teyvat"
  },
  "positions": [
    {"id": 1, "type": "teleport", "move_mode": "walk", "x": -1638.5, "y": 2153.9, "action": "", "action_params": ""},
    {"id": 2, "type": "path", "move_mode": "walk", "x": -1641.5, "y": 2151.0, "action": "", "action_params": ""},
    {"id": 3, "type": "path", "move_mode": "fly", "x": -1651.7, "y": 2107.5, "action": "stop_flying", "action_params": ""}
  ]
}

注意：BGI 路径的 x/y 是原神地图坐标（position[0], position[2]），Y 轴方向与 tp.json 一致。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from framework.resources import res

if TYPE_CHECKING:
    from framework.context import GameContext
    from framework.high_level_api import HighLevelApi


# ── 数据模型 ──


@dataclass(frozen=True)
class Waypoint:
    """路径点（对应 BGI Waypoint.cs）。"""

    x: float
    y: float
    type: str = "path"  # "teleport" | "path" | "target" | "orientation"
    move_mode: str = "walk"  # "walk" | "fly" | "climb" | "swim"
    action: str = ""  # "" | "fight" | "pick_up" | "stop_flying" | etc.
    action_params: str = ""


@dataclass(frozen=True)
class PathTaskInfo:
    """路径任务元信息（对应 BGI PathingTaskInfo.cs）。"""

    name: str = ""
    task_type: str = ""  # "collect" | "combat" | etc.
    map_name: str = "Teyvat"


@dataclass(frozen=True)
class PathTask:
    """路径任务（对应 BGI PathingTask.cs）。"""

    info: PathTaskInfo
    waypoints: tuple[Waypoint, ...] = ()


# ── 路径加载 ──


def load_path_task(path: Path | str) -> PathTask:
    """从 BGI 路径 JSON 文件加载 PathTask。"""
    with open(path, encoding="utf-8") as f:
        data = json.load(f)

    info_data = data.get("info", {})
    info = PathTaskInfo(
        name=info_data.get("name", ""),
        task_type=info_data.get("type", ""),
        map_name=info_data.get("map_name", "Teyvat"),
    )

    waypoints: list[Waypoint] = []
    for pos in data.get("positions", []):
        waypoints.append(
            Waypoint(
                x=float(pos.get("x", 0)),
                y=float(pos.get("y", 0)),
                type=pos.get("type", "path"),
                move_mode=pos.get("move_mode", "walk"),
                action=pos.get("action", ""),
                action_params=pos.get("action_params", ""),
            )
        )

    return PathTask(info=info, waypoints=tuple(waypoints))


# ── 路径执行器 ──


class PathExecutor:
    """执行路径任务：按传送点分段 → 逐段执行。

    对照 BGI PathExecutor.Pathing():
    - 按传送点分割路径
    - 每段首点为传送 → TpTask
    - 剩余点 → Navigation.MoveTo
    - v1 不实现: 切队/战斗/action 处理器/HP 恢复
    """

    def __init__(self, ctx: GameContext, g: HighLevelApi):
        self.ctx = ctx
        self.g = g

    def execute(self, task: PathTask) -> None:
        """执行路径任务。按传送点分段，逐段传送+行走。"""
        from abilities.navigation.navigator import Navigator
        from abilities.navigation.tp import Teleporter

        segments = self._split_by_teleport(task.waypoints)
        teleporter = Teleporter(self.ctx, self.g)
        navigator = Navigator(self.ctx, self.g)

        for segment in segments:
            if not segment:
                continue
            # 首点为传送
            if segment[0].type == "teleport":
                teleporter.teleport_to((segment[0].x, segment[0].y))
            # 行走剩余路径点
            for wp in segment[1:]:
                navigator.go_to(wp)

    @staticmethod
    def _split_by_teleport(waypoints: tuple[Waypoint, ...]) -> list[list[Waypoint]]:
        """按传送点分割路径（对照 BGI ConvertWaypointsForTrack）。

        每遇到一个 teleport 类型的路径点，就开始一个新段。
        返回的每段首点要么是 teleport，要么是第一个路径点（无传送的段）。
        """
        if not waypoints:
            return []

        segments: list[list[Waypoint]] = []
        current: list[Waypoint] = []

        for wp in waypoints:
            if wp.type == "teleport" and current:
                segments.append(current)
                current = [wp]
            else:
                current.append(wp)

        if current:
            segments.append(current)

        return segments
