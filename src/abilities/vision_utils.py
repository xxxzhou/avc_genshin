"""视觉工具（docs/design/07 §5）。

封装 avc ``ITemplateMatcher`` / ``ITextRecognizer`` / ``Image``，提供任务友好的
高层查询。``g.find_template`` / ``g.find_text`` / ``vision.*``（05 §2.4 / §4）是对它的再封装。

模式取自 avc skill ``flows/loops.py``（已验证可用）：addTemplatePath → match(buf) →
getMatch(i) → MatchResult(.x/.y/.w/.h/.score)；OCR 为 recognize → getMatch(i) → (text, OcrResult)。
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
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
    """模板路径解析：绝对/已存在直用；否则经 ``res.template()``（含 BGI 回退）。

    兼容两种调用形式：
    - ``"ui/x.png"``（文件名+子目录）→ ``res.template()`` 解析
    - ``res.template_ui("x.png")`` 返回的相对路径 ``resources/templates/ui/x.png``
      → 相对 cwd 解析后已存在则直接用（避免二次拼接）
    """
    p = Path(path)
    if p.is_absolute() and p.exists():
        return str(p)
    abs_p = p.resolve()
    if abs_p.exists():
        return str(abs_p)
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


@lru_cache(maxsize=256)
def _tpl_size(path: str) -> tuple[int, int] | None:
    """读模板尺寸 (w, h)（cv2，离线无 avc 崩溃风险）。失败返回 None。"""
    try:
        import cv2

        img = cv2.imread(path)
        if img is None:
            return None
        h, w = img.shape[:2]
        return (w, h)
    except Exception:
        return None


def _search_region_size(frame, roi: tuple[int, int, int, int] | None) -> tuple[int, int]:
    """计算搜索区域尺寸 (w, h)：roi=(x,y,w,h) 或整个 frame。"""
    if roi:
        return roi[2], roi[3]
    w = getattr(frame, "width", 0) or 0
    h = getattr(frame, "height", 0) or 0
    return w, h


def _template_fits(
    tmpl_path: str,
    frame,
    roi: tuple[int, int, int, int] | None,
) -> bool:
    """模板是否严格小于搜索区域（每维至少小 1px）。

    ⚠ 防 avc 内部 OpenCV ``cv::crossCorr`` 断言崩溃（2026-08-15 实机 P0）：
    模板 ≥ 搜索区域时 ``matchTemplate`` 触发 ``corr.rows <= img.rows + templ.rows - 1``
    断言失败，走 OpenCV ``terminate`` 绕过 Python 异常路径 → 无 failure 事件/无存证。
    读不到模板尺寸时保守放行（不拦截，避免误伤正常小模板）。
    """
    size = _tpl_size(tmpl_path)
    if size is None:
        return True  # 读不到模板尺寸时保守放行
    tw, th = size
    rw, rh = _search_region_size(frame, roi)
    # 容错：mock/异常 frame 的 .width/.height 不是数字（MagicMock），避免崩溃；放行
    if not (isinstance(rw, (int, float)) and isinstance(rh, (int, float))) or rw <= 0 or rh <= 0:
        return True  # 无搜索区域信息/异常时不拦截
    return tw < rw and th < rh


def find_template(
    ctx: "GameContext",
    path: str | Path,
    threshold: float = 0.8,
    roi: tuple[int, int, int, int] | None = None,
    frame: "IImageBuffer | None" = None,
    _quiet: bool = False,
) -> Rect | None:
    """即时查模板，返回首个命中（按 orderBy）或 None。

    threshold 推荐 0.7–0.8；roi=(x,y,w,h) 限定区域；frame=None 时自动截图。

    可观测性：发 ``detect.template``（ability=vision_utils, name, threshold, ok, score,
    match_pos）。``_quiet=True`` 时同 name 整 run 只发首条（热轮询 wait_template 用）。
    """
    tm = _need(ctx, "tm")
    buf = _frame(ctx, frame)
    if buf is None:
        ctx.observe.event("detect.template", ability="vision_utils",
                          name=Path(path).name, threshold=threshold, ok=False,
                          reason="no_frame", _quiet=_quiet)
        return None
    tm.clearTemplates()
    if roi:
        tm.setRoi(*roi)
    else:
        tm.clearRoi()
    resolved = _resolve_template_path(path)
    if tm.addTemplatePath(resolved, threshold) < 0:
        ctx.observe.event("detect.template", ability="vision_utils",
                          name=Path(path).name, threshold=threshold, ok=False,
                          reason="template_missing", _quiet=_quiet)
        return None
    # ⚠ 模板 ≥ 搜索区域会触发 avc 内部 OpenCV matchTemplate 断言崩溃（P0）→ 跳过
    if not _template_fits(resolved, buf, roi):
        ctx.observe.event("detect.template", ability="vision_utils",
                          name=Path(path).name, threshold=threshold, ok=False,
                          reason="template_larger_than_region", _quiet=_quiet)
        return None
    n = tm.match(buf)
    if n <= 0:
        ctx.observe.event("detect.template", ability="vision_utils",
                          name=Path(path).name, threshold=threshold, ok=False,
                          reason="template_not_matched",
                          _quiet=_quiet)
        return None
    r = tm.getMatch(0)
    if r is None:
        ctx.observe.event("detect.template", ability="vision_utils",
                          name=Path(path).name, threshold=threshold, ok=False,
                          reason="getMatch_none",
                          _quiet=_quiet)
        return None
    rect = Rect(r.x, r.y, r.w, r.h, r.score)
    ctx.observe.event("detect.template", ability="vision_utils",
                      name=Path(path).name, threshold=threshold, ok=True,
                      score=r.score, match_pos=(rect.cx, rect.cy), _quiet=_quiet)
    return rect


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
        resolved = _resolve_template_path(p)
        # ⚠ 模板 ≥ 搜索区域会触发 avc 内部 OpenCV matchTemplate 崩溃（P0）→ 跳过
        if not _template_fits(resolved, buf, roi):
            continue
        idx = tm.addTemplatePath(resolved, threshold)
        if idx >= 0:
            name_by_idx[idx] = Path(p).name
    if not name_by_idx:
        return {}
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
    _quiet: bool = False,
) -> Rect | None:
    """OCR 子串匹配：返回含 ``kw`` 的最短文本框（避免长行误命中），或 None。

    可观测性：发 ``detect.ocr``（ability=vision_utils, keyword, ok, score, match_pos）。
    ``_quiet=True`` 时同 keyword 整 run 只发首条（热轮询 wait_text 用）。
    """
    ocr = _need(ctx, "ocr")
    buf = _frame(ctx, frame)
    if buf is None:
        ctx.observe.event("detect.ocr", ability="vision_utils", keyword=kw,
                          ok=False, reason="no_frame", _quiet=_quiet)
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
    if best is None:
        ctx.observe.event("detect.ocr", ability="vision_utils", keyword=kw,
                          ok=False, _quiet=_quiet)
        return None
    ctx.observe.event("detect.ocr", ability="vision_utils", keyword=kw, ok=True,
                      score=best[1].score, match_pos=(best[1].cx, best[1].cy),
                      _quiet=_quiet)
    return best[1]


def ocr_region(
    ctx: "GameContext",
    x: int,
    y: int,
    w: int,
    h: int,
    frame: "IImageBuffer | None" = None,
    _quiet: bool = False,
) -> list[tuple[str, float]]:
    """OCR 指定区域，返回 [(text, score), ...]。

    可观测性：发 ``detect.ocr``（ability=vision_utils, region, count, ok 仅在无帧时 False）。
    纯观测（命中数量本身无成败），``_quiet=True`` 时同 region 整 run 只发首条。
    """
    ocr = _need(ctx, "ocr")
    buf = _frame(ctx, frame)
    if buf is None:
        ctx.observe.event("detect.ocr", ability="vision_utils",
                          region=(x, y, w, h), count=0, ok=False, reason="no_frame",
                          _quiet=_quiet)
        return []
    ocr.setRoi(x, y, w, h)
    ocr.recognize(buf)
    out = []
    for i in range(ocr.getMatchCount()):
        t, r = ocr.getMatch(i)
        if t is not None and r is not None:
            out.append((t, r.score))
    ocr.clearRoi()
    ctx.observe.event("detect.ocr", ability="vision_utils", region=(x, y, w, h),
                      count=len(out), sample=out[0][0] if out else None, _quiet=_quiet)
    return out


# ── 图像 ──


def crop(frame: "IImageBuffer", x: int, y: int, w: int, h: int):
    """裁剪 frame，返回新 IImageBuffer（avc ``Image.crop``）。"""
    from avc import Image

    return Image.crop(frame, x, y, w, h)
