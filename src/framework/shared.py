"""SharedState —— 运行内共享事实源（docs/design/02 §1、03 §6）。

SceneEstimator / FrameDaemon 在 loop 线程**写入**；g.* 经同步桥在 loop 线程**读取**。
单线程 loop 天然序列化；跨线程的字段（scene/frame/detections）采用**整体替换（swap）
而非原地改写**的纪律——读取方拿到的永远是完整一致的快照（GIL 下引用读/写原子）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from avc.image import IImageBuffer

    from framework.scene import SceneState


@dataclass
class SharedState:
    """运行期共享事实：当前场景、最新帧、检测结果、玩家位置、世界模型标志。"""

    scene: "SceneState | None" = None
    frame: "IImageBuffer | None" = None
    detections: dict[str, Any] = field(default_factory=dict)  # FrameDaemon 每次整体替换
    player_pos: tuple[float, float] | None = None

    # ── 世界模型（跨任务持久状态，06）──
    _flags: dict[str, Any] = field(default_factory=dict)

    def set_flag(self, key: str, val: Any) -> None:
        self._flags[key] = val

    def has_flag(self, key: str) -> bool:
        return key in self._flags

    def get_flag(self, key: str, default: Any = None) -> Any:
        return self._flags.get(key, default)

    def snapshot(self) -> dict[str, Any]:
        """只读快照（供日志/Observe 引用，不被后续写入影响）。"""
        return {
            "scene": self.scene.scene.value if self.scene else None,
            "has_frame": self.frame is not None,
            "det_classes": list(self.detections.keys()),
            "player_pos": self.player_pos,
        }
