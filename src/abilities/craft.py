"""合成浓缩树脂领域能力（Phase D 新增）。

对照 BetterGI ``GoToCraftingBenchTask.GoCraftResin``：
1. PathExecutor 走合成台路径 → 找 F「合成」→ 按 F
2. 选最后一个对话选项进入合成界面
3. 找 craft_condensed_resin 模板 → 点击
4. btn_white_confirm 确认合成 → btn_black_confirm 确认结果
5. ESC 退出

v1 简化：不做精确数量控制（MinResinToKeep / 增减按钮调数量），直接默认最大量。
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from framework.resources import res

if TYPE_CHECKING:
    from framework.context import GameContext
    from framework.high_level_api import HighLevelApi

from abilities import vision_utils as vu

# ROI（1080p，对照 BGI Recognition.json）
# craftCondensedResin: rect(cw/2, 0, cw/2, ch/3*2) = 右半上 2/3
_CRAFT_RESIN_ROI = (960, 0, 960, 720)
# 模板阈值：对齐 BGI RecognitionObject 默认 0.8
_MATCH_THRESHOLD = 0.8


def craft_condensed_resin(
    ctx: "GameContext",
    g: "HighLevelApi",
    country: str = "蒙德",
) -> bool:
    """合成浓缩树脂。

    返回 True = 成功合成或无可合成；False = 失败。

    Args:
        country: 国家名（蒙德/璃月/稻妻/枫丹），决定走哪条合成台路径
    """
    from abilities.navigation.path_executor import PathExecutor, load_path_task
    from avc._core import KeyCode

    # 1. 走合成台路径 + 找 F「合成」→ 按 F
    path_name = _bench_path_name(country)
    path_dir = res.path_json("craft")
    path_file = path_dir / path_name
    if not path_file.exists():
        return False

    task = load_path_task(path_file)
    PathExecutor(ctx, g).execute(task)

    # 按 F 进入合成（BGI: FindFAndPress + retry）
    if not _press_f_to_enter(ctx, g):
        return False

    # 2. 选最后一个对话选项进入合成界面（BGI: SelectLastOptionUntilEnd）
    _select_last_option(ctx, g)
    time.sleep(0.8)

    # 3. 找浓缩树脂图标 → 点击
    resin_rect = vu.find_template(
        ctx,
        res.template_ui("craft_condensed_resin.png"),
        threshold=_MATCH_THRESHOLD,
        roi=_CRAFT_RESIN_ROI,
    )
    if resin_rect is None:
        # 无可合成 → ESC 退出
        g.press(KeyCode.esc)
        _return_main_ui(ctx, g)
        return True

    g.click(resin_rect.cx, resin_rect.cy)
    time.sleep(0.5)

    # 4. 白色确认 → 黑色确认（BGI: ClickWhiteConfirmButton + ClickBlackConfirmButton）
    _click_template(ctx, g, "btn_white_confirm.png", subdir="ui")
    time.sleep(0.5)
    _click_template(ctx, g, "btn_black_confirm.png", subdir="ui")
    time.sleep(1.3)  # BGI: 1300ms

    # 5. ESC 退出合成
    g.press(KeyCode.esc)
    _return_main_ui(ctx, g)
    return True


def _press_f_to_enter(ctx: "GameContext", g: "HighLevelApi", retries: int = 3) -> bool:
    """按 F 直到进入对话场景。"""
    from avc._core import KeyCode
    from framework.scene import Scene

    for _ in range(retries):
        g.press(KeyCode.f)
        time.sleep(0.5)
        if g.wait_scene(Scene.DIALOG, timeout=3.0):
            return True
    return False


def _select_last_option(ctx: "GameContext", g: "HighLevelApi", max_rounds: int = 10) -> None:
    """反复选最后一个对话选项直到无选项（BGI SelectLastOptionUntilEnd）。"""
    from abilities.dialog import visible_options

    for _ in range(max_rounds):
        opts = visible_options(ctx)
        if not opts:
            break
        last = opts[0]
        g.click(last.rect.cx, last.rect.cy)
        time.sleep(0.5)


def _click_template(
    ctx: "GameContext",
    g: "HighLevelApi",
    name: str,
    subdir: str = "ui",
    threshold: float = _MATCH_THRESHOLD,
) -> bool:
    """找模板 → 点击，返回是否找到。"""
    resolver = getattr(res, f"template_{subdir}")
    rect = vu.find_template(ctx, resolver(name), threshold=threshold)
    if rect is not None:
        g.click(rect.cx, rect.cy)
        time.sleep(0.3)
        return True
    return False


def _return_main_ui(ctx: "GameContext", g: "HighLevelApi") -> None:
    """ESC → 等回主界面。"""
    from avc._core import KeyCode

    for _ in range(3):
        g.press(KeyCode.esc)
        if g.wait_main_ui(timeout=3.0):
            return


def _bench_path_name(country: str) -> str:
    return f"合成台_{country}.json"
