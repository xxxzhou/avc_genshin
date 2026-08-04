"""Scene 枚举 + SceneState + 场景分类（docs/design/02 §1）。

SceneEstimator 守护（见 daemons/）以固定频率调用 ``classify_scene`` 判定当前场景，
写入 SharedState。所有读取方（g.wait_main_ui / 守护场景门控 / Observe）问 SharedState.scene，
不再各自匹配——单一事实源，避免"主任务认为在主界面、守护认为在对话"的不一致。

分类规则起步用**可组合的特征函数**（abilities/game_state.py：小地图/血条/对话框/加载特征），
UNKNOWN 比例高时由 Observe 回流驱动补充（02 §1.5）。阶段三先提供接口 + 简单规则。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from avc.image import IImageBuffer


class Scene(str, Enum):
    """游戏场景（02 §1.2）。值即 JSONL 日志里的 scene 字段。"""

    MAIN_UI = "main_ui"  # 大世界主界面（可移动、可见小地图）
    COMBAT = "combat"  # 战斗中（血条/技能 UI）
    DIALOG = "dialog"  # 对话中
    MAP = "map"  # 地图打开
    MENU = "menu"  # 背包/角色/设置/派蒙菜单等
    LOADING = "loading"  # 加载界面
    DOMAIN = "domain"  # 秘境内
    UNKNOWN = "unknown"  # 未识别（触发 on_uncertain / 回流）


@dataclass(frozen=True, slots=True)
class SceneState:
    scene: Scene
    confidence: float = 1.0  # [0,1]
    since: float = 0.0  # 进入该场景的时刻（稳定判定用，monotonic）
    sub: str | None = None  # 子类（如 MENU 下的 "inventory"/"settings"）


# 分类器类型：frame → SceneState。可被注册/替换（热加载规则，02 §1.5）。
SceneClassifier = Callable[["IImageBuffer"], "SceneState"]


# ── 默认分类器（阶段三占位：返回 UNKNOWN，待 abilities/game_state 规则接入）──


def _default_classifier(frame: "IImageBuffer") -> "SceneState":
    return SceneState(scene=Scene.UNKNOWN, confidence=0.0, since=0.0)


# 当前生效的分类器（Runtime/SceneEstimator 启动时可 set_classifier 替换）。
_classifier: SceneClassifier = _default_classifier


def set_classifier(fn: SceneClassifier) -> None:
    """注册场景分类器（如基于 abilities/game_state 特征的组合规则）。"""
    global _classifier
    _classifier = fn


def classify_scene(frame: "IImageBuffer") -> "SceneState":
    """判一次场景。由 SceneEstimator 守护调用，结果写入 SharedState。"""
    return _classifier(frame)
