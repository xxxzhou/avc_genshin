"""GenshinDetector —— YOLO 推理 + 解码（docs/design/07 §4、IMPLEMENTATION §6.1）。

复用 BetterGI 的 ONNX 模型，用 onnxruntime Python 重写推理 + 解码。

⚠️ 解码真相（已据 BetterGI 源码 + YoloSharp 源码核实，修正了 CLAUDE §8 /
IMPLEMENTATION §7 的旧说法）：
BetterGI 把 YOLO 全部委托给 NuGet 包 ``Compunet.YoloSharp``，而 YoloSharp 用的是
**标准 Ultralytics YOLOv8/YOLO11 导出格式**——BGI **没有任何自定义解码**。
真正的坑只有三个（均与"网上的 Python YOLOv8 示例"有关）：
  1. **不要再做 sigmoid**：box 的 DFL 与 cls 的 sigmoid 已 bake 进 ONNX 计算图，直接用原始输出。
  2. **布局是 ``[1, 4+nc, N]``**（转置过的），取 ``out[0].T`` 得 ``[N, 4+nc]``；box 前 4 = cx,cy,w,h。
  3. **letterbox**：min 比例等比缩放 + 居中零填充；输出框按 ``(x - pad) / scale`` 反变换。
默认阈值：conf 0.3、IoU 0.45（YoloSharp ``YoloConfiguration.Default``）。
NMS：按类独立、贪心、纯 IoU。

类别名**不**维护 label.json——直接从 ONNX 元数据 ``names`` 读（旧 Predictor 的 label.json 已废弃）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

import cv2
import numpy as np

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


_NAMES_RE = re.compile(r"(\d+)\s*:\s*'([^']*)'")


class GenshinDetector:
    """YOLO 检测/分类器（复用 BetterGI ONNX 模型）。

    用法：
        det = GenshinDetector(res.model("bgi_world.onnx"))
        dets = det.detect(ctx.capture())                 # {类名: [Detection, ...]}
        cls, score = det.classify(ctx.capture())         # 分类模型（avatar/q）
    """

    def __init__(
        self,
        model_path: str,
        *,
        conf: float = 0.3,
        iou: float = 0.45,
        interp: int = cv2.INTER_LINEAR,
    ):
        path = str(model_path)
        if not _path_exists(path):
            raise FileNotFoundError(f"模型不存在: {path}（经 res.model 解析）")

        import onnxruntime as ort  # 懒导入：capture/vision 不需要它，避免环境缺/坏时连累 import

        avail = ort.get_available_providers()
        providers = []
        if "CUDAExecutionProvider" in avail:
            providers.append("CUDAExecutionProvider")
        providers.append("CPUExecutionProvider")
        self.session = ort.InferenceSession(path, providers=providers)

        self.conf = conf
        self.iou = iou
        self.interp = interp

        meta = self.session.get_modelmeta().custom_metadata_map
        self.task: str = meta.get("task", "detect").lower()
        self.names: dict[int, str] = self._parse_names(meta.get("names", ""))
        self.in_name = self.session.get_inputs()[0].name
        self.out_name = self.session.get_outputs()[0].name
        self.imgsz = self._parse_imgsz(meta)

    # ── 元数据 ──

    @staticmethod
    def _parse_names(raw: str) -> dict[int, str]:
        """解析 ONNX ``names`` 元数据 ``"{0: 'drops', 1: 'ore'}"``。

        YoloSharp 会把类名中的 ``_`` 替换为空格；这里同样处理，以与 BGI 输出字符串一致。
        """
        out: dict[int, str] = {}
        for idx, name in _NAMES_RE.findall(raw):
            out[int(idx)] = name.replace("_", " ")
        return out

    def _parse_imgsz(self, meta: dict[str, str]) -> int:
        """模型输入边长。优先输入 shape（[1,3,H,W]），其次元数据 imgsz，默认 640。"""
        shape = self.session.get_inputs()[0].shape  # e.g. [1, 3, 640, 640] 或含动态串
        for v in shape[-2:]:
            if isinstance(v, int) and v > 0:
                return v
        raw = meta.get("imgsz", "")
        m = re.findall(r"\d+", raw)
        if m:
            return int(m[0])
        return 640

    def name(self, cls_id: int) -> str:
        return self.names.get(cls_id, str(cls_id))

    # ── 检测 ──

    def detect(self, frame, *, conf: float | None = None, iou: float | None = None) -> dict[str, list[Detection]]:
        """检测：frame（avc IImageBuffer 或 RGB ndarray）→ {类名: [Detection, ...]}。"""
        if self.task not in ("detect", ""):
            raise ValueError(f"模型 task={self.task!r}，不是 detect；请用 classify()")

        tensor, scale, pad = self._preprocess(frame)
        out = self.session.run([self.out_name], {self.in_name: tensor})[0]  # [1,4+nc,N]
        pred = out[0].T  # [N, 4+nc]

        conf_t = self.conf if conf is None else conf
        iou_t = self.iou if iou is None else iou

        boxes_xywh = pred[:, :4]
        scores = pred[:, 4:]
        cls_ids = scores.argmax(axis=1)
        confs = scores.max(axis=1)
        keep = confs > conf_t
        if not keep.any():
            return {}

        boxes_xywh = boxes_xywh[keep]
        cls_ids = cls_ids[keep]
        confs = confs[keep]

        # xywh → xyxy → 反 letterbox → int（截断，与 YoloSharp 一致）
        xyxy = self._xywh_to_xyxy(boxes_xywh)
        xyxy = self._unletterbox(xyxy, scale, pad)
        xyxy = np.floor(xyxy).astype(int)

        keep_idx = self._nms_per_class(xyxy, confs, cls_ids, iou_t)
        xyxy, confs, cls_ids = xyxy[keep_idx], confs[keep_idx], cls_ids[keep_idx]

        out_dict: dict[str, list[Detection]] = {}
        for (x1, y1, x2, y2), s, c in zip(xyxy, confs, cls_ids):
            out_dict.setdefault(self.name(int(c)), []).append(
                Detection(int(x1), int(y1), int(x2), int(y2), float(s), self.name(int(c)))
            )
        # 每类内按分数降序
        for v in out_dict.values():
            v.sort(key=lambda d: d.score, reverse=True)
        return out_dict

    # ── 分类 ──

    def classify(self, frame) -> tuple[str, float]:
        """分类（avatar_side / q_classify 等）：返回 (类名, 分数)。

        输出已含 sigmoid/softmax（在图里），直接 argmax，不再做 softmax。
        """
        tensor, _, _ = self._preprocess(frame)
        out = self.session.run([self.out_name], {self.in_name: tensor})[0].flatten()
        top = int(out.argmax())
        return self.name(top), float(out[top])

    # ── 预处理（letterbox + /255 + NCHW）──

    def _preprocess(self, frame) -> tuple[np.ndarray, float, tuple[int, int]]:
        rgb = _to_rgb_np(frame)  # HxWx3 uint8 RGB
        canvas, scale, pad = _letterbox(rgb, self.imgsz, self.interp)
        tensor = np.ascontiguousarray(canvas.astype(np.float32).transpose(2, 0, 1)[None] / 255.0)
        return tensor, scale, pad

    # ── 后处理几何 ──

    @staticmethod
    def _xywh_to_xyxy(b: np.ndarray) -> np.ndarray:
        xyxy = np.empty_like(b)
        xyxy[:, 0] = b[:, 0] - b[:, 2] / 2
        xyxy[:, 1] = b[:, 1] - b[:, 3] / 2
        xyxy[:, 2] = b[:, 0] + b[:, 2] / 2
        xyxy[:, 3] = b[:, 1] + b[:, 3] / 2
        return xyxy

    @staticmethod
    def _unletterbox(xyxy: np.ndarray, scale: float, pad: tuple[int, int]) -> np.ndarray:
        """模型坐标 → 原图坐标：x_orig = (x_model - pad) / scale。"""
        out = xyxy.astype(np.float32).copy()
        out[:, [0, 2]] = (out[:, [0, 2]] - pad[0]) / scale
        out[:, [1, 3]] = (out[:, [1, 3]] - pad[1]) / scale
        return out

    @staticmethod
    def _nms_per_class(
        xyxy: np.ndarray, confs: np.ndarray, cls_ids: np.ndarray, iou_t: float
    ) -> np.ndarray:
        """按类独立贪心 NMS（纯 IoU）；返回保留下标。"""
        order = np.argsort(-confs)
        kept: list[int] = []
        suppressed = np.zeros(len(xyxy), dtype=bool)
        for i in order:
            if suppressed[i]:
                continue
            kept.append(i)
            for j in order:
                if j == i or suppressed[j]:
                    continue
                if cls_ids[j] != cls_ids[i]:
                    continue  # 不同类不互相抑制
                if _iou(xyxy[i], xyxy[j]) > iou_t:
                    suppressed[j] = True
        return np.array(kept, dtype=int)


# ── 模块级自由函数（便于单测：avc 无关的纯几何/预处理）──


def _path_exists(p: str) -> bool:
    from os.path import isfile

    return isfile(p)


def _to_rgb_np(frame) -> np.ndarray:
    """frame → HxWx3 uint8 RGB。接受 avc IImageBuffer 或 ndarray（假定 RGB）。"""
    if isinstance(frame, np.ndarray):
        return frame
    raw = bytes(frame.to_bytes())
    h, w = frame.height, frame.width
    arr = np.frombuffer(raw, dtype=np.uint8).reshape(h, w, -1)
    return _convert_by_imagetype(arr, frame.imageType)


def _convert_by_imagetype(arr: np.ndarray, image_type) -> np.ndarray:
    """按 avc ImageType 把像素数组转成 RGB。默认按 BGRA 处理（avc 截图默认格式）。"""
    try:
        from avc._core import ImageType

        it = int(image_type)
        if it == int(ImageType.rgba8):
            return arr[:, :, :3]
        if it == int(ImageType.rgb8):
            return arr
        if it == int(ImageType.bgr8):
            return arr[:, :, ::-1]
        # bgra8 及其它 → 按 BGRA 取前 3 通道反转
    except Exception:
        pass
    return arr[:, :, :3][:, :, ::-1]


def _letterbox(img: np.ndarray, imgsz: int, interp: int):
    """等比缩放（min 比例）+ 居中零填充到 imgsz×imgsz。返回 (canvas, scale, (pad_w, pad_h))。"""
    h, w = img.shape[:2]
    scale = min(imgsz / w, imgsz / h)
    nw, nh = int(w * scale), int(h * scale)
    resized = cv2.resize(img, (nw, nh), interpolation=interp) if (nw, nh) != (w, h) else img
    canvas = np.zeros((imgsz, imgsz, 3), dtype=np.uint8)
    pad_w = (imgsz - nw) // 2
    pad_h = (imgsz - nh) // 2
    canvas[pad_h : pad_h + nh, pad_w : pad_w + nw] = resized
    return canvas, scale, (pad_w, pad_h)


def _iou(a: np.ndarray, b: np.ndarray) -> float:
    """两 xyxy 框的 IoU。"""
    ix1 = max(a[0], b[0])
    iy1 = max(a[1], b[1])
    ix2 = min(a[2], b[2])
    iy2 = min(a[3], b[3])
    iw = max(0.0, ix2 - ix1)
    ih = max(0.0, iy2 - iy1)
    inter = iw * ih
    area_a = max(0.0, a[2] - a[0]) * max(0.0, a[3] - a[1])
    area_b = max(0.0, b[2] - b[0]) * max(0.0, b[3] - b[1])
    union = area_a + area_b - inter
    return float(inter / union) if union > 0 else 0.0
