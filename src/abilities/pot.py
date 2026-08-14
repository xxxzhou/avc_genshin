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
    """进入尘歌壶（背包小道具流程，对照 BGI QuickSereniteaPotTask.Done）。

    2026-08-15 重写（v1 的 teleport_to("尘歌壶") 是设计 bug：壶不在 tp.json）：
    1. 确保主界面 → 按 B 开背包（MapCloseButton 模板确认已开）
    2. 点「小道具」页签 (1050,50)（BGI 同款固定坐标）
    3. 找 SereniteaPotIcon → 点选 → 点白色确认按钮（放置）
    4. 等主界面 → OCR F 交互「进入」「尘歌壶」→ 按 F → 点 (1010,760) 进入
    5. 等加载 → 主界面（已在壶内）

    可观测性：每步发 ``pot.step``（ability=pot, step, ok, reason）。
    """
    from avc._core import KeyCode

    ob = ctx.observe
    ctx.ensure_foreground()

    def _step(name: str, ok: bool, reason: str | None = None, **facts):
        ob.event("pot.step", ability="pot", phase="act",
                 step=name, ok=ok, reason=reason, **facts)

    # 0. 回主界面（B 只在主界面有效）
    if not g.wait_main_ui(timeout=5.0):
        from avc._core import KeyCode as _KC
        ctx.press(_KC.esc)
        time.sleep(0.5)
        if not g.wait_main_ui(timeout=5.0):
            _step("back_main_ui", False, "not_main_ui")
            return False
    time.sleep(0.5)

    # 1. 开背包
    ctx.press(KeyCode.b)
    if not _wait_template(ctx, g, "ui/MapCloseButton.png", timeout=5.0):
        _step("open_bag", False, "bag_not_open")
        return False
    _step("open_bag", True)
    time.sleep(0.5)

    # 2. 小道具页签（BGI 固定坐标 1080p）
    g.click(1050, 50)
    time.sleep(0.4)

    # 3. 找壶图标 → 选中 → 放置
    rect = vu.find_template(
        ctx, "ui/SereniteaPotIcon.png", threshold=0.7,
        roi=(100, 100, 1190, 860),
    )
    if rect is None:
        _step("find_pot_icon", False, "pot_icon_not_found")
        _close_bag(ctx, g)
        return False
    g.click(rect.cx, rect.cy)
    time.sleep(0.4)
    # 点「放置」按钮：固定右下坐标（2026-08-15 实机标定：右下两按钮
    # 左=放置(1423,1017) 右=详情(1683,1018)；BGI 的 size-(225,60) 落在详情上）。
    # 放置后进入放置预览模式（背包关闭 + 世界中出现壶 + F「进入尘歌壶」提示）。
    g.click(1423, 1017)
    time.sleep(1.2)
    # 背包还开着（首次点击未生效）→ 重试一次；已关则等 F 提示
    if vu.find_template(ctx, "ui/MapCloseButton.png", threshold=0.8) is not None:
        g.click(1423, 1017)
        time.sleep(1.2)
    _step("place_pot", True)

    # 4. 等放置预览完成：F「进入尘歌壶」提示出现（放置模式残 UI 不再干扰）
    enter = _find_enter_pot_f(ctx, g, timeout=12.0)
    if not enter:
        # 可能已在壶内（重复进入）——主界面即算成功
        if g.wait_main_ui(timeout=2.0):
            _step("entered", True, note="already_inside")
            return True
        # 壶已放置在别处：放置按钮禁用（点击无效）+ 无 F 提示。
        # TODO: 旋转视角扫描 F / 传送到上次放置点。2026-08-15 实机场景。
        _step("find_enter_f", False, "pot_placed_elsewhere_or_f_not_found")
        return False
    ctx.press(KeyCode.f)
    time.sleep(0.8)
    # 进入确认面板（BGI：非联机时点击不影响）
    g.click(1010, 760)

    # 5. 等加载 → 主界面
    time.sleep(2.0)
    if not g.wait_main_ui(timeout=20.0):
        _step("enter_load", False, "main_ui_timeout")
        return False
    _step("entered", True)
    return True


def _wait_template(
    ctx: "GameContext", g: "HighLevelApi", path: str, timeout: float
) -> bool:
    """轮询等模板出现。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if vu.find_template(ctx, path, threshold=0.8) is not None:
            return True
        time.sleep(0.3)
    return False


def _close_bag(ctx: "GameContext", g: "HighLevelApi") -> None:
    """失败出口：关背包回主界面（按 B / Esc）。"""
    from avc._core import KeyCode

    ctx.press(KeyCode.b)
    time.sleep(0.4)
    g.wait_main_ui(timeout=3.0)


def _find_enter_pot_f(
    ctx: "GameContext", g: "HighLevelApi", timeout: float
) -> bool:
    """OCR 找 F 交互「进入…尘歌壶」提示。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        r1 = g.find_text("进入")
        r2 = g.find_text("尘歌壶")
        if r1 is not None and r2 is not None:
            return True
        time.sleep(0.4)
    return False


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
