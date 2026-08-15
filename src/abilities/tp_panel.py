"""传送/标记面板 OCR 检测与关闭（2026-08-08 实机探查修复）。

背景：玩家地图有大量自定义标记（实机 113/300）覆盖传送点，点击传送点图标时
游戏优先命中 pin → 打开的是**标记面板**而非**传送面板**，导致旧版
``GoTeleport.png`` 模板确认（实机仅 0.588 < 0.8）恒不命中。

实机 OCR 证据（1080p，diag_ocrpos.txt / diag_click*.txt）：
- 标记面板：'确认'@(1689,1007)、'总标记113/300'@(1532,934)、'F'@(1450,1028)、
  '追踪'@(1810,1007)、'删除'@(1569,1008)、'标记（点击更改标记名称）'@(1634,75)
- ⚠ 危险：标记面板的 '确认' 按钮带 F 快捷键，**按 F 会确认标记编辑而非传送**，
  因此检测到标记面板时绝不能按 F，只能按 Esc 关闭后换点重试。
- 传送面板：右下角有 '传送' 按钮（BGI TeleportButton），点击或按 F 确认传送
  （BGI TpTask.HandleTeleportPanel 检测到传送按钮后按 F / 点击）。

用法：
    kind = detect_tp_panel(ctx, frame)          # TELEPORT / MARKER / NONE
    if kind is TeleportPanelKind.TELEPORT: ...  # 点 '传送' 或按 F
    elif kind is TeleportPanelKind.MARKER: close_marker_panel(ctx)  # Esc 关闭
"""

from __future__ import annotations

from enum import Enum
from typing import TYPE_CHECKING

from framework import utils

if TYPE_CHECKING:
    from avc.image import IImageBuffer

    from framework.context import GameContext


class TeleportPanelKind(Enum):
    """地图上当前打开的面板类型。"""

    NONE = 0  # 纯地图，无面板
    TELEPORT = 1  # 传送面板（右下角 '传送' 按钮）
    MARKER = 2  # 自定义标记面板（'追踪'/'删除'/'总标记'/'标记（点击更改标记名称）'）


# 面板 OCR 区域：地图右半（标记面板按钮/标题与传送按钮均位于右侧）。
# 覆盖实机证据坐标：顶部 '标记（点击更改标记名称）'@(1634,75) 与右下
# '确认'@(1689,1007)/'追踪'@(1810,1007)/'删除'@(1569,1008)/'总标记'@(1532,934)/F@(1450,1028)。
_TP_PANEL_OCR_ROI = (1152, 0, 768, 1080)
# ⚠ 传送按钮 OCR 区域：右下角，仅下半屏 + 偏右，避开顶部「传送锚点」标题里的
# 「传送」二字（实机 2026-08-15：find_teleport_button 误匹配右上 (1384,745)「传送锚点」→
# 点了标题而非右下传送按钮 → 传送未触发，wait_main_ui_timeout）。
# 传送按钮在右下约 (1620, 670) 区域；留余量用 y∈[500,780], x∈[1300,1920]。
_TELEPORT_BUTTON_ROI = (1300, 500, 620, 280)

# 标记面板关键词（按特异性排序；'总标记' 为实机最独特的标记面板计数器文本）。
_MARKER_KEYWORDS = ("总标记", "追踪", "删除", "更改标记名称", "标记")
# 传送面板关键词。
_TELEPORT_KEYWORDS = ("传送",)

# 关闭标记面板最多尝试次数（Esc 一次可能未生效/动画中）。
_CLOSE_MARKER_MAX_ATTEMPTS = 3


def detect_tp_panel(
    ctx: "GameContext",
    frame: "IImageBuffer | None" = None,
) -> TeleportPanelKind:
    """OCR 检测地图上当前打开的面板类型（传送 / 标记 / 无）。

    先判标记面板再判传送面板：标记面板按钮 '追踪'/'删除' 与 '总标记' 计数器
    特异性高，且标记面板中绝无 '传送' 按钮，可安全先行短路。
    """
    from abilities import vision_utils as vu

    buf = frame if frame is not None else ctx.capture()
    if buf is None:
        return TeleportPanelKind.NONE
    # _quiet=True：detect_tp_panel 在确认/关闭循环里高频轮询，面板结果由 tp.confirm 携带，
    # 内部 OCR 是噪声（同 region 首条后折叠，避免 detect.ocr 爆）
    texts = vu.ocr_region(ctx, *_TP_PANEL_OCR_ROI, frame=buf, _quiet=True)
    joined = "".join(t for t, _ in texts)
    for kw in _MARKER_KEYWORDS:
        if kw in joined:
            return TeleportPanelKind.MARKER
    for kw in _TELEPORT_KEYWORDS:
        if kw in joined:
            return TeleportPanelKind.TELEPORT
    return TeleportPanelKind.NONE


def find_teleport_button(
    ctx: "GameContext",
    frame: "IImageBuffer | None" = None,
):
    """定位传送面板 '传送' 按钮（OCR），返回 Rect 或 None。

    ⚠ 2026-08-15 实机：原 ROI=全右半 (1152,0,768,1080) 会匹配到右上角
    「传送锚点」标题里的「传送」(1384,745) → 点击标题而非右下真正传送按钮
    → 传送未触发。改用右下专用 ROI _TELEPORT_BUTTON_ROI 避开标题误匹配。
    找不到时返回 None（调用方会按 F 兜底）。
    """
    from abilities import vision_utils as vu

    return vu.find_text(ctx, "传送", roi=_TELEPORT_BUTTON_ROI, frame=frame)


def close_marker_panel(ctx: "GameContext", max_attempts: int = _CLOSE_MARKER_MAX_ATTEMPTS) -> None:
    """关闭标记面板（按 Esc 关闭最上层 UI，回到大地图）。

    按 Esc 而非 F：标记面板的 '确认' 按钮带 F 快捷键，按 F 会确认标记编辑（危险）。
    最多尝试 ``max_attempts`` 次，直到 OCR 确认已非标记面板。
    """
    from avc._core import KeyCode

    for _ in range(max_attempts):
        frame = ctx.capture()
        if detect_tp_panel(ctx, frame) is not TeleportPanelKind.MARKER:
            return
        ctx.press(KeyCode.esc)
        utils.sleep(0.3)
