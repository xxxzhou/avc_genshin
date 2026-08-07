"""尘歌壶领域能力（Phase D 新增）。

对照 BetterGI ``GoToSereniteaPotTask``，v1 大幅简化：

**v1 流程（地图传送模式）**：
1. ``g.teleport_to`` 切换到尘歌壶区域 → 找 ``sereniteapot_home`` 图标 → 传送
2. 等主界面 → 找 F「阿圆」→ 按 F
3. talk("信任") → ``sereniteapot_love`` 领好感 → ``sereniteapot_money`` 领宝钱 → ``page_close_white``
4. talk("再见") → 退出

**v1 不做**：背包进入模式、商店购买、硬编码移动找阿圆、洞天名称 OCR。
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from framework.resources import res

if TYPE_CHECKING:
    from framework.context import GameContext
    from framework.high_level_api import HighLevelApi

from abilities import vision_utils as vu

# 文案（BGI zh-Hans）
_TRUST = "信任"
_BYE = "再见"
_TUBBY = "阿圆"

# 模板 ROI（1080p，对照 BGI Recognition.json 各 pot 模板的 roi 区域）
# @potLove=rect(cw-cw/8,ch/2,cw/8,ch/4)  @potMoney=rect(cw/2,ch-ch/4,cw/4,ch/4)
# @potPageClose=rect(cw/2,ch/5,cw/4,ch/8)  @pageCloseWhite=rect(cw-cw/8,0,cw/8,ch/8)
_POT_ROI: dict[str, tuple[int, int, int, int]] = {
    "sereniteapot_love.png": (1680, 540, 240, 270),
    "sereniteapot_money.png": (960, 810, 480, 270),
    "sereniteapot_page_close.png": (960, 216, 480, 135),
    "page_close_white.png": (1680, 0, 240, 135),
}
# 模板阈值：对齐 BGI RecognitionObject 默认 0.8
_MATCH_THRESHOLD = 0.8


def enter_serenitea_pot(ctx: "GameContext", g: "HighLevelApi") -> bool:
    """进入尘歌壶。

    流程：传送尘歌壶区域 → 找住宅图标 → 传送 → 等主界面。

    ⚠ v1 简化：假设 ``g.teleport_to`` 能直接传送到尘歌壶（依赖传送链；
    实际 BGI 是切区域 + 找图标 + 点传送按钮的复杂流程，待实机补全）。

    返回 True = 成功进入。
    """
    # 传送到尘歌壶（v1：直接传住宅名，传送链实现具体逻辑）
    try:
        g.teleport_to("尘歌壶", map_name="SereniteaPot")
    except Exception:
        return False

    if not g.wait_main_ui(timeout=30.0):
        return False
    return True


def claim_pot_rewards(ctx: "GameContext", g: "HighLevelApi") -> bool:
    """领取尘歌壶奖励（好感 + 宝钱）。

    流程：
    1. 找 F「阿圆」→ 按 F
    2. talk("信任")
    3. sereniteapot_love 领好感
    4. sereniteapot_money 领宝钱
    5. page_close_white 关闭

    返回 True = 至少领了一项；False = 未找到壶灵或无奖励。
    """
    from avc._core import KeyCode

    # 1. 找 F「阿圆」→ 按 F
    if not _find_and_press_f(ctx, g):
        return False

    # 2. talk("信任")
    time.sleep(0.5)
    g.talk(_TRUST)
    time.sleep(1.0)

    did_claim = False

    # 3. 领好感（sereniteapot_love）
    if _click_template(ctx, g, "sereniteapot_love.png"):
        did_claim = True
        time.sleep(0.5)
        # 关闭可能弹出的提示
        _click_template(ctx, g, "sereniteapot_page_close.png")

    # 4. 领宝钱（sereniteapot_money）
    if _click_template(ctx, g, "sereniteapot_money.png"):
        did_claim = True
        time.sleep(0.5)
        _click_template(ctx, g, "sereniteapot_page_close.png")

    # 5. page_close_white 关闭
    _click_template(ctx, g, "page_close_white.png", subdir="ui")

    return did_claim


def exit_serenitea_pot(ctx: "GameContext", g: "HighLevelApi") -> bool:
    """退出尘歌壶：先关面板 → talk("再见") → 传送回提瓦特。

    返回 True = 成功退出。

    对照 BGI Finished：先点 PageCloseWhite（若面板开着），再选「再见」，
    最后 Tp(4508.97, 3630.56) 回枫丹。
    """
    # 1. 关掉可能开着的面板（BGI Finished 先点 page_close_white）
    _click_template(ctx, g, "page_close_white.png", subdir="ui")

    # 2. talk("再见") 与壶灵告别
    g.talk(_BYE)
    g.wait_main_ui(timeout=10.0)

    # 3. 传送回提瓦特（v1：传送回枫丹凯瑟琳附近，对照 BGI TpTask.Tp(4508, 3630)）
    try:
        g.teleport_to((4508.97, 3630.56))
    except Exception:
        return False

    return g.wait_main_ui(timeout=30.0)


# ── 内部辅助 ──


def _find_and_press_f(ctx: "GameContext", g: "HighLevelApi", timeout: float = 15.0) -> bool:
    """找 F「阿圆」→ 按 F → 等对话场景。"""
    from avc._core import KeyCode
    from framework.scene import Scene

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        # OCR 验证 F 附近是壶灵
        rect = g.find_text(_TUBBY)
        if rect is not None:
            g.press(KeyCode.f)
            time.sleep(0.8)
            if g.wait_scene(Scene.DIALOG, timeout=3.0):
                return True
        time.sleep(0.5)
    return False


def _click_template(
    ctx: "GameContext",
    g: "HighLevelApi",
    name: str,
    subdir: str = "ui",
    threshold: float = _MATCH_THRESHOLD,
) -> bool:
    """找模板 → 点击，返回是否找到。

    模板若有 BGI ROI（_POT_ROI）则限 ROI 匹配（提速 + 防误匹）。
    """
    resolver = getattr(res, f"template_{subdir}")
    rect = vu.find_template(
        ctx, resolver(name), threshold=threshold, roi=_POT_ROI.get(name)
    )
    if rect is not None:
        g.click(rect.cx, rect.cy)
        time.sleep(0.3)
        return True
    return False
