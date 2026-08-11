"""树脂奖励领取（auto_boss / auto_ley_line / auto_domain 共用）。

对照 BGI ``TryUseOriginalResinOnRewardPrompt`` / ``PressUseResin``：调用方已把奖励
对话框交互出来（按 F 到位）→ 本模块负责"点『使用原粹树脂』直到消失 / 识别
『补充原粹树脂』= 树脂耗尽" + 关闭奖励页回主界面。

用 OCR（``g.find_text``）判文案，不依赖树脂模板（``resources/templates`` 未收录，
且文字比图标稳定）。boss 领征讨之花、地脉领地脉之花、秘境领石化古树，走同一领取逻辑。

返回语义：``True`` = 成功领取；``False`` = 树脂耗尽（调用方应停止循环）。
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from framework.context import GameContext
    from framework.high_level_api import HighLevelApi

# 对话框文案（OCR，BGI 默认 zh-Hans 本地化）
_USE_RESIN = "使用原粹树脂"
_EXHAUSTED = "补充原粹树脂"  # 树脂耗尽提示
_CLICK_INTERVAL_S = 0.4
_CLOSE_ESC_ATTEMPTS = 3
_WAIT_MAIN_UI_S = 5.0


def claim_resin_reward(
    ctx: "GameContext", g: "HighLevelApi", *, timeout: float = 25.0
) -> bool:
    """领取当前已打开的树脂奖励页。返回 False = 树脂耗尽。

    流程：
    1. 反复点『使用原粹树脂』直到它消失（每次点击后等 ``_CLICK_INTERVAL_S`` 再查）
    2. 期间出现『补充原粹树脂』→ 树脂耗尽 → 关页返回 False
    3. 点完 → ESC 关奖励页 → 等回主界面

    可观测性：发 ``reward.claim``（ability=reward, use_resin_found, click_count,
    exhausted, ok, reason=exhausted|claimed|timeout）。boss/ley_line/domain 共用。
    """
    ob = ctx.observe
    click_count = 0
    use_resin_found = False
    exhausted = False
    ended = "timeout"  # 默认：循环耗尽 deadline 仍未消失（可疑成功）
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if g.find_text(_EXHAUSTED) is not None:
            exhausted = True
            ended = "exhausted"
            break
        rect = g.find_text(_USE_RESIN)
        if rect is None:
            ended = "claimed"  # 『使用原粹树脂』消失 = 已消耗完
            break
        use_resin_found = True
        g.click(rect.cx, rect.cy)
        click_count += 1
        time.sleep(_CLICK_INTERVAL_S)
    _close_reward_page(ctx, g)
    ok = not exhausted
    ob.event("reward.claim", ability="reward", phase="act",
             use_resin_found=use_resin_found, click_count=click_count,
             exhausted=exhausted, ok=ok, reason=ended)
    return ok


def _close_reward_page(ctx: "GameContext", g: "HighLevelApi") -> None:
    """ESC 关闭奖励页 → 等回主界面（最多 ``_CLOSE_ESC_ATTEMPTS`` 次）。"""
    from framework.scene import Scene
    from avc._core import KeyCode

    for _ in range(_CLOSE_ESC_ATTEMPTS):
        g.press(KeyCode.esc)
        if g.wait_scene(Scene.MAIN_UI, timeout=_WAIT_MAIN_UI_S):
            return
