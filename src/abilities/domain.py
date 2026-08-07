"""自动秘境领域能力（Phase D 新增）。

对照 BetterGI ``AutoDomainTask``，v1 简化：

**v1 流程**：
1. ``g.teleport_to`` 秘境坐标 → 向前走 → 等 F 图标 → 按 F
2. OCR「单人挑战」→ 点击确认（btn_white_confirm）
3. 等 ``party_btn_choose_view``（队伍选择）→ 点击开始挑战
4. ``g.fight_until_clear`` 战斗到清场
5. OCR「石化古树」→ 按 F → ``claim_resin_reward``
6. confirm 继续 / exit 退出 → ESC + btn_black_confirm
7. 循环 count 次

**v1 不做**：YOLO 树检测 + 摄像机旋转、队伍切换、圣遗物分解、树脂 20/40 切换、复活重试。
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
_SINGLE_CHALLENGE = "单人挑战"
_START_CHALLENGE = "开始挑战"
_PETRIFIED_TREE = "石化古树"
_RESIN_EXHAUSTED = "补充原粹树脂"

# 模板 ROI（1080p，对照 BGI Recognition.json）
# AutoFight Confirm=rect(cw/2,ch/2,cw/2,ch/2)（右半）  Exit=rect(0,ch/2,cw/2,ch/2)（左半）
# partyChooseView=rect(0,ch-120*s,cw/7,120*s)
_CONFIRM_ROI = (960, 540, 960, 540)
_EXIT_ROI = (0, 540, 960, 540)
_PARTY_VIEW_ROI = (0, 960, 274, 120)
# 模板阈值：对齐 BGI RecognitionObject 默认 0.8
_MATCH_THRESHOLD = 0.8

# 秘境名：以 BGI tp.json 地点名为准（与 BGI MapLazyAssets.DomainPositionMap 同源，
# 由 Type in (BlessDomain, ForgeryDomain, MasteryDomain) 过滤，含 35 个可刷秘境）。
# 玩家常用「圣遗物套装名」作简称（tp.json 用地点名），别名表在此映射。
_DOMAIN_ALIASES: dict[str, str] = {
    "绝缘之境": "椛染之庭",  # 绝缘之旗印/追忆之注连 套
    "绝缘之旗印": "椛染之庭",
    "追忆之注连": "椛染之庭",
}

_db: "TpDatabase | None" = None


def _database() -> "TpDatabase":
    """tp.json 传送点数据库（懒加载单例，与 teleport_to 同源）。"""
    global _db
    if _db is None:
        from abilities.navigation.tp import TpDatabase

        _db = TpDatabase()
    return _db


def get_domain_coords(domain_name: str) -> tuple[float, float] | None:
    """获取秘境坐标（游戏地图坐标，与 teleport_to 同系）。

    查询顺序：别名 → tp.json 标准名 → None。坐标缺失返回 None。
    """
    canonical = _DOMAIN_ALIASES.get(domain_name, domain_name)
    tp = _database().find_by_name(canonical)
    if tp is None:
        return None
    return (tp.x, tp.y)


def enter_domain(ctx: "GameContext", g: "HighLevelApi", domain_name: str) -> bool:
    """传送到秘境并进入。

    流程：
    1. 传送到秘境坐标
    2. 向前走 + 等 F 图标
    3. 按 F → OCR「单人挑战」→ 点击确认
    4. 等队伍选择界面 → 点击开始挑战

    返回 True = 成功进入秘境。
    """
    from avc._core import KeyCode

    # 1. 传送到秘境坐标
    coords = get_domain_coords(domain_name)
    if coords is None:
        return False
    g.teleport_to(coords)
    g.wait_main_ui(timeout=30.0)

    # 2. 向前走 + 等 F 图标（BGI: 各秘境特定移动 + WaitForElementAppear）
    if not _walk_and_press_f(ctx, g, timeout=30.0):
        return False

    # 3. OCR「单人挑战」→ 点击确认（BGI: AutoFight Confirm=confirm.png 右半）
    if not g.wait_until(lambda: g.find_text(_SINGLE_CHALLENGE) is not None, timeout=15.0):
        return False
    _click_template(ctx, g, "confirm.png", roi=_CONFIRM_ROI)
    time.sleep(1.0)

    # 4. 等队伍选择界面 → 点击开始挑战（BGI: 等 PartyBtnChooseView）
    if not g.wait_until(
        lambda: _has_template(ctx, g, "party_btn_choose_view.png", roi=_PARTY_VIEW_ROI),
        timeout=15.0,
    ):
        return False
    if not g.wait_until(lambda: g.find_text(_START_CHALLENGE) is not None, timeout=10.0):
        return False
    _click_template(ctx, g, "confirm.png", roi=_CONFIRM_ROI)

    # 等待进入秘境（加载）
    g.wait_main_ui(timeout=30.0)  # 秘境内也是 MAIN_UI
    return True


def fight_domain(ctx: "GameContext", g: "HighLevelApi", timeout: float = 300.0) -> bool:
    """在秘境中战斗直到清场。

    返回 True = 战斗完成。
    """
    return g.fight_until_clear(timeout=timeout)


def claim_domain_reward(ctx: "GameContext", g: "HighLevelApi") -> bool:
    """领取秘境奖励（石化古树）。

    流程：
    1. OCR「石化古树」→ 按 F
    2. claim_resin_reward 领取（复用 reward.py）

    返回 True = 成功领取；False = 树脂耗尽。
    """
    from abilities.reward import claim_resin_reward
    from avc._core import KeyCode

    # 1. 等石化古树出现 → 按 F
    if not g.wait_until(lambda: g.find_text(_PETRIFIED_TREE) is not None, timeout=30.0):
        return False
    g.press(KeyCode.f)

    # 检查树脂耗尽
    if g.wait_until(lambda: g.find_text(_RESIN_EXHAUSTED) is not None, timeout=3.0):
        return False

    # 2. 领取（复用 reward.py 的树脂领取逻辑）
    if not g.wait_until(lambda: g.find_text("原粹树脂") is not None, timeout=15.0):
        return False
    return claim_resin_reward(ctx, g)


def exit_domain(ctx: "GameContext", g: "HighLevelApi") -> bool:
    """退出秘境：ESC → btn_black_confirm → 等主界面。

    返回 True = 成功退出。
    """
    from avc._core import KeyCode

    g.press(KeyCode.esc)
    time.sleep(0.3)
    g.press(KeyCode.esc)
    time.sleep(0.5)

    # 点击黑色确认（退出秘境）
    _click_template(ctx, g, "btn_black_confirm.png")

    return g.wait_main_ui(timeout=15.0)


# ── 内部辅助 ──


def _walk_and_press_f(
    ctx: "GameContext",
    g: "HighLevelApi",
    timeout: float = 30.0,
) -> bool:
    """向前走 + 等 F 图标出现 → 按 F。BGI WalkToPressF 简化版。"""
    from abilities.game_state import has_pick_f
    from avc._core import KeyCode

    # v1 简化：直接等 F 图标（不做 W 键移动，假设传送后就在入口附近）
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if has_pick_f(ctx):
            g.press(KeyCode.f)
            time.sleep(0.5)
            return True
        time.sleep(0.3)
    return False


def _has_template(
    ctx: "GameContext",
    g: "HighLevelApi",
    name: str,
    subdir: str = "ui",
    threshold: float = _MATCH_THRESHOLD,
    roi: tuple[int, int, int, int] | None = None,
) -> bool:
    """模板是否存在。"""
    resolver = getattr(res, f"template_{subdir}")
    return (
        vu.find_template(ctx, resolver(name), threshold=threshold, roi=roi) is not None
    )


def _click_template(
    ctx: "GameContext",
    g: "HighLevelApi",
    name: str,
    subdir: str = "ui",
    threshold: float = _MATCH_THRESHOLD,
    roi: tuple[int, int, int, int] | None = None,
) -> bool:
    """找模板 → 点击，返回是否找到。"""
    resolver = getattr(res, f"template_{subdir}")
    rect = vu.find_template(ctx, resolver(name), threshold=threshold, roi=roi)
    if rect is not None:
        g.click(rect.cx, rect.cy)
        time.sleep(0.3)
        return True
    return False
