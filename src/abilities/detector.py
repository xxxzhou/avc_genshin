"""GenshinDetector —— YOLO 推理 + 解码，全量走 avc IYoloDetector（docs/design/07 §4）。

avc 的 ``IYoloDetector``（``plugins/avc_cv/yolo/``）忠实移植自本文件历史 Python 实现：
标准 Ultralytics 解码 —— detect=letterbox / classify=exact resize / 类名从元数据透传 /
不做 sigmoid（已 bake 进图）/ 输出布局 ``[1,4+nc,N]``。本层只做 ``Box→Detection`` 包装。

⚠ **avc 是硬依赖**：``avc_cv`` 插件未编译/未加载 → 构造直接抛错，**不回退** ort/cv2
（不维护两套；avc 不在 = 启动不了）。
⚠ 模型路径必须**绝对正斜杠**：``res.model()`` 返回相对路径，会被 avc 拼成 ``models/<rel>``
找不到（见 任务进度「avc 待办」）。本类构造时 ``path.resolve().as_posix()`` 已处理。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from framework.resources import res

if TYPE_CHECKING:
    from avc.image import IImageBuffer


@dataclass(frozen=True, slots=True)
class Detection:
    """一个检测结果（**原图**坐标系，左上 + 右下 + 分数 + 类名）。"""

    x1: int
    y1: int
    x2: int
    y2: int
    score: float
    name: str

    @property
    def cx(self) -> float:
        return (self.x1 + self.x2) / 2

    @property
    def cy(self) -> float:
        return (self.y1 + self.y2) / 2

    @property
    def w(self) -> int:
        return self.x2 - self.x1

    @property
    def h(self) -> int:
        return self.y2 - self.y1

    def center(self) -> tuple[float, float]:
        return self.cx, self.cy


class GenshinDetector:
    """YOLO 检测/分类器（复用 BetterGI ONNX 模型，推理走 avc ``IYoloDetector``）。

    用法：
        det = GenshinDetector(res.model("bgi_world.onnx"))
        dets = det.detect(ctx.capture())           # {类名: [Detection, ...]}
        cls, score = det.classify(ctx.capture())   # 分类模型（avatar/q）
    """

    def __init__(self, model_path, *, conf: float = 0.3, iou: float = 0.45):
        path = Path(model_path)
        if not path.is_file():
            raise FileNotFoundError(f"模型不存在: {path}（经 res.model 解析）")

        from avc import Vision

        # avc 要绝对路径正斜杠；相对路径会被拼成 models/<rel> 找不到
        self._det = Vision.createYoloDetector(path.resolve().as_posix(), conf, iou)
        if self._det is None:
            raise RuntimeError(
                "avc IYoloDetector 不可用（avc_cv 插件未编译/未加载）；"
                "GenshinDetector 要求 avc 含 avc_cv，不回退 ort/cv2。"
            )

        self.conf = conf
        self.iou = iou
        # 元数据从 avc 读（getTask/getClassCount 触发模型懒加载）
        self.task: str = self._det.getTask() or "detect"
        self.names: dict[int, str] = {
            i: self._det.getClassName(i) for i in range(self._det.getClassCount())
        }

    def name(self, cls_id: int) -> str:
        return self.names.get(cls_id, str(cls_id))

    def detect(
        self, frame, *, conf: float | None = None, iou: float | None = None
    ) -> dict[str, list[Detection]]:
        """检测：frame（avc IImageBuffer）→ {类名: [Detection, ...]}。

        conf/iou 传 None 用构造时默认值。``Box``(float) → ``Detection``(int，截断)。
        """
        if self.task not in ("detect", ""):
            raise ValueError(f"模型 task={self.task!r}，不是 detect；请用 classify()")

        out: dict[str, list[Detection]] = {}
        for name, boxes in self._det.detect(frame, conf=conf, iou=iou).items():
            out[name] = [
                Detection(int(b.x1), int(b.y1), int(b.x2), int(b.y2), float(b.score), name)
                for b in boxes
            ]
        return out

    def classify(self, frame) -> tuple[str, float]:
        """分类（avatar_side / q_classify 等）：返回 (类名, 分数)。

        avc 用 exact resize（对齐 BGI YoloSharp ``Classify`` 训练分布）。
        失败（模型 task 不匹配/无类名）返回 ``("", 0.0)``。
        """
        r = self._det.classify(frame)
        return r if r else ("", 0.0)
