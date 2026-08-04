"""视觉工具（docs/design/07 §5）。

封装 avc ``ITemplateMatcher`` / ``ITextRecognizer`` / ``Image``，提供任务友好的
高层查询。``g.find_template`` / ``g.find_text`` / ``vision.*``（05 §2.4 / §4）是对它的再封装。

模式取自 avc skill ``flows/loops.py``（已验证可用）：addTemplatePath → match(buf) →
getMatch(i) → MatchResult(.x/.y/.w/.h/.score)；OCR 为 recognize → getMatch(i) → (text, OcrResult)。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from framework.errors import AvcsError
from framework.resources import res

if TYPE_CHECKING:
    from avc.image import IImageBuffer

    from framework.context import GameContext


@dataclass(frozen=True, slots=True)
class Rect:
    """矩形命中（截图缓冲坐标系，左上角 + 宽高 + 分数）。"""

    x: int
    y: int
    w: int
    h: int
    score: float = 0.0

    @property
    def cx(self) -> float:
        return self.x + self.w / 2

    @property
    def cy(self) -> float:
        return self.y + self.h / 2

    def center(self) -> tuple[float, float]:
        return self.cx, self.cy


# ── 内部 ──


def _resolve_template_path(path: str | Path) -> str:
    """模板路径解析：绝对/已存在直用；否则经 ``res.template()``（含 BGI 回退）。"""
    p = Path(path)
    if p.is_absolute() and p.exists():
        return str(p)
    return str(res.template(path))


def _frame(ctx: "GameContext", frame: "IImageBuffer | None") -> "IImageBuffer | None":
    return frame if frame is not None else ctx.capture()


def _need(ctx: "GameContext", which: str):
    obj = ctx.tm if which == "tm" else ctx.ocr
    if obj is None:
        raise AvcsError(
            f"avc 未启用 {'opencv' if which == 'tm' else 'ocr'} 插件，{which} 不可用"
        )
    return obj


# ── 模板匹配 ──


def find_template(
    ctx: "GameContext",
    path: str | Path,
    threshold: float = 0.8,
    roi: tuple[int, int, int, int] | None = None,
    frame: "IImageBuffer | None" = None,
) -> Rect | None:
    """即时查模板，返回首个命中（按 orderBy）或 None。

    threshold 推荐 0.7–0.8；roi=(x,y,w,h) 限定区域；frame=None 时自动截图。
    """
    tm = _need(ctx, "tm")
    buf = _frame(ctx, frame)
    if buf is None:
        return None
    tm.clearTemplates()
    if roi:
        tm.setRoi(*roi)
    else:
        tm.clearRoi()
    if tm.addTemplatePath(_resolve_template_path(path), threshold) < 0:
        return None
    if tm.match(buf) <= 0:
        return None
    r = tm.getMatch(0)
    if r is None:
        return None
    return Rect(r.x, r.y, r.w, r.h, r.score)


def find_all_templates(
    ctx: "GameContext",
    paths: list[str | Path],
    threshold: float = 0.8,
    roi: tuple[int, int, int, int] | None = None,
    frame: "IImageBuffer | None" = None,
) -> dict[str, list[Rect]]:
    """一次匹配多模板，返回 {模板名: [Rect, ...]}（仅命中的）。"""
    tm = _need(ctx, "tm")
    buf = _frame(ctx, frame)
    if buf is None:
        return {}
    tm.clearTemplates()
    if roi:
        tm.setRoi(*roi)
    else:
        tm.clearRoi()
    name_by_idx: dict[int, str] = {}
    for p in paths:
        idx = tm.addTemplatePath(_resolve_template_path(p), threshold)
        if idx >= 0:
            name_by_idx[idx] = Path(p).name
    n = tm.match(buf)
    out: dict[str, list[Rect]] = {}
    for i in range(n):
        r = tm.getMatch(i)
        if r is None:
            continue
        name = name_by_idx.get(r.templateIndex, f"tmpl_{r.templateIndex}")
        out.setdefault(name, []).append(Rect(r.x, r.y, r.w, r.h, r.score))
    return out


# ── OCR 文字 ──


def find_text(
    ctx: "GameContext",
    kw: str,
    roi: tuple[int, int, int, int] | None = None,
    frame: "IImageBuffer | None" = None,
) -> Rect | None:
    """OCR 子串匹配：返回含 ``kw`` 的最短文本框（避免长行误命中），或 None。"""
    ocr = _need(ctx, "ocr")
    buf = _frame(ctx, frame)
    if buf is None:
        return None
    if roi:
        ocr.setRoi(*roi)
    else:
        ocr.clearRoi()
    ocr.recognize(buf)
    target = kw.strip()
    best: tuple[str, Rect] | None = None
    for i in range(ocr.getMatchCount()):
        t, r = ocr.getMatch(i)
        if t is None or r is None:
            continue
        if target and target in t:
            rect = Rect(r.x, r.y, r.w, r.h, r.score)
            if best is None or len(t) < len(best[0]):
                best = (t, rect)
    return best[1] if best else None


def ocr_region(
    ctx: "GameContext",
    x: int,
    y: int,
    w: int,
    h: int,
    frame: "IImageBuffer | None" = None,
) -> list[tuple[str, float]]:
    """OCR 指定区域，返回 [(text, score), ...]。"""
    ocr = _need(ctx, "ocr")
    buf = _frame(ctx, frame)
    if buf is None:
        return []
    ocr.setRoi(x, y, w, h)
    ocr.recognize(buf)
    out = []
    for i in range(ocr.getMatchCount()):
        t, r = ocr.getMatch(i)
        if t is not None and r is not None:
            out.append((t, r.score))
    ocr.clearRoi()
    return out


# ── 图像 ──


def crop(frame: "IImageBuffer", x: int, y: int, w: int, h: int):
    """裁剪 frame，返回新 IImageBuffer（avc ``Image.crop``）。"""
    from avc import Image

    return Image.crop(frame, x, y, w, h)
