"""邮件奖励领取领域能力（Phase D 新增）。

对照 BetterGI ``ClaimMailRewardsTask``：
1. ESC 打开派蒙菜单
2. 找邮件图标（esc_mail_reward）→ 点击进入邮件页
3. 找「全部领取」按钮（collect 模板）→ 点击
4. ESC 关闭回主界面

无邮件时优雅跳过（BGI：记录"没有邮件奖励"）。
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from framework.resources import res

if TYPE_CHECKING:
    from framework.context import GameContext
    from framework.high_level_api import HighLevelApi

from abilities import vision_utils as vu

# ROI（1080p，对照 BGI ClaimMailRewardsTask）
# 邮件图标：cutLeftBottom(0.1, 0.5) = 左 10% 宽 × 下 50% 高
_MAIL_ICON_ROI = (0, 540, 192, 540)
# 全部领取按钮：rect(0, ch-ch/3, cw/4, ch/3) = 左下 1/4
_COLLECT_ROI = (0, 720, 480, 360)


# 模板匹配阈值：对齐 BGI RecognitionObject 默认 0.8（比 0.7 更稳——
# 离线筛查曾见错误场景最高 0.71，0.7 会误收）
_MATCH_THRESHOLD = 0.8


def claim_all_mail(ctx: "GameContext", g: "HighLevelApi") -> bool:
    """领取所有邮件奖励。

    返回 True = 成功领取或无邮件可领；False = 领取失败。

    流程（对照 BGI ClaimMailRewardsTask）：
    0. 确保主界面（BGI ReturnMainUiTask 前置）
    1. 按 ESC 打开派蒙菜单
    2. 找邮件图标 → 点击
    3. 找全部领取按钮 → 点击
    4. ESC 关闭
    """
    from avc._core import KeyCode

    # 0. 确保在主界面（BGI 开头 ReturnMainUiTask.Start）
    _ensure_main_ui(ctx, g)

    # 1. 打开派蒙菜单
    g.press(KeyCode.esc)
    time.sleep(1.3)  # BGI: 1300ms 等菜单展开

    # 2. 找邮件图标
    mail_rect = vu.find_template(
        ctx,
        res.template_ui("esc_mail_reward.png"),
        threshold=_MATCH_THRESHOLD,
        roi=_MAIL_ICON_ROI,
    )
    if mail_rect is None:
        # 无邮件 → 关闭菜单返回 True（BGI：没有邮件奖励）
        _close_menu(ctx, g)
        return True

    # 点击邮件图标
    g.click(mail_rect.cx, mail_rect.cy)
    time.sleep(1.0)

    # 3. 找全部领取按钮
    collect_rect = vu.find_template(
        ctx,
        res.template_dialog("collect.png"),
        threshold=_MATCH_THRESHOLD,
        roi=_COLLECT_ROI,
    )
    if collect_rect is not None:
        g.click(collect_rect.cx, collect_rect.cy)
        time.sleep(0.3)  # BGI: 200ms
        # 按 ESC 关闭邮件页
        g.press(KeyCode.esc)
        time.sleep(0.3)

    # 4. 关闭菜单回主界面
    _close_menu(ctx, g)
    return True


def _close_menu(ctx: "GameContext", g: "HighLevelApi") -> None:
    """ESC 关闭菜单 → 等回主界面（BGI ReturnMainUiTask 简化版）。

    v1 不做 BtnExitDoor 特判（邮件/派蒙菜单无退出门按钮）。
    """
    from avc._core import KeyCode

    for _ in range(3):
        g.press(KeyCode.esc)
        if g.wait_main_ui(timeout=3.0):
            return


def _ensure_main_ui(ctx: "GameContext", g: "HighLevelApi") -> None:
    """确保在主界面（对照 BGI ClaimMailRewardsTask 开头 ReturnMainUiTask.Start）。

    场景估计守护已算出当前场景则直接判断；未知时回退 _close_menu（ESC 到主界面）。
    """
    from framework.scene import Scene

    s = getattr(g, "scene", None)
    if s is not None and s.scene is Scene.MAIN_UI:
        return
    _close_menu(ctx, g)
