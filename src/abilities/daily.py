"""每日奖励领取领域能力（Phase D 新增）。

对照 BetterGI 两路领取：
1. **F1 冒险之证直领**（ClaimEncounterPointsRewardsTask）—— 打开冒险之证 →
   切到「委托」页 → 点击「领取相遇之缘奖励」按钮
2. **凯瑟琳对话领**（GoToAdventurersGuildTask）—— 走冒险家协会路径 →
   找 F「凯瑟琳」→ 对话「每日委托」→ 黑色确认 → 逐项领取 → 派遣一键领取/重新探索

BGI **不做** 4 个每日委托（代码中 disabled placeholder），只领奖励。
原神 5.0+ 用「相遇之缘」机制替代旧 4 委托领原石。

验证：OCR「今日奖励已领取」= 已领。

模板依赖（resources/templates/）：
- ui/btn_claim_encounter_points_rewards.png  F1 直领按钮
- ui/btn_black_confirm.png                   凯瑟琳黑色确认
- dialog/icon_daily_reward.png               每日奖励图标
- dialog/icon_explore.png                    探索派遣图标
- dialog/collect.png                         派遣领取
- dialog/re.png                              派遣重新探索
- ui/index_1.png ~ index_4.png              标签页索引
- pick/F.png                                 F 交互键
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import TYPE_CHECKING

from framework.resources import res

if TYPE_CHECKING:
    from framework.context import GameContext
    from framework.high_level_api import HighLevelApi

from abilities import vision_utils as vu


# ── OCR 文案常量（BGI zh-Hans 本地化）──

_DAILY_CLAIMED = "今日奖励已领取"
_COMMISSION = "委托"
_DAILY_QUEST = "每日委托"
_EXPLORE = "探索"
_KATHERINE = "凯瑟琳"

# ── ROI 常量（1080p 坐标，对照 BGI）──

# F1 冒险之证：左侧导航栏（BGI ROI: left 380px, 或 10%/10%/30%/70%）
_HANDBOOK_NAV_ROI = (0, 0, 576, 756)  # 左 30% 上 70%

# F1 冒险之证：领取按钮（BGI cutRightBottom(0.3, 0.5) = 右 30% 下 50%）
_CLAIM_BTN_ROI = (1344, 540, 576, 540)

# 凯瑟琳 F 交互搜索区域（中下偏右，NPC 交互提示通常位置）
_NPC_F_ROI = (640, 400, 640, 400)

# ── 间隔/超时常量 ──

_CLICK_INTERVAL_S = 0.5
_WAIT_SCENE_S = 5.0
_DIALOG_TIMEOUT_S = 15.0
_CLAIM_ATTEMPTS = 3  # F1 直领重试次数


# ══════════════════════════════════════════════════════════════════
# 1. F1 冒险之证直领（ClaimEncounterPointsRewardsTask）
# ══════════════════════════════════════════════════════════════════


def claim_encounter_points(ctx: "GameContext", g: "HighLevelApi") -> bool:
    """F1 冒险之证直领每日奖励。

    流程（对照 BGI ClaimEncounterPointsRewardsTask）：
    1. 按 F1 打开冒险之证
    2. OCR 找「委托」导航按钮 → 点击切到委托页
    3. 模板匹配 btn_claim_encounter_points_rewards（右下 ROI）→ 点击领取
    4. OCR 检查「今日奖励已领取」= 成功
    5. ESC 关闭冒险之证

    返回 True = 成功领取 / 已领取；False = 领取失败。
    """
    from avc._core import KeyCode
    from framework.scene import Scene

    # 1. 打开冒险之证
    g.press(KeyCode.f1)
    time.sleep(1.0)  # 等界面展开

    # 2. 找「委托」导航 → 点击
    for attempt in range(_CLAIM_ATTEMPTS):
        # 先检查是否已领取
        if _check_daily_claimed(ctx, g):
            _close_ui(ctx, g)
            return True

        # 找「委托」文字并点击（左侧导航栏 ROI）
        rect = g.find_text(_COMMISSION)
        if rect is not None:
            g.click(rect.cx, rect.cy)
            time.sleep(0.8)
            break

        # 找不到「委托」→ 可能已在委托页，直接尝试领
        time.sleep(0.5)

    # 3. 找领取按钮 → 点击
    for attempt in range(_CLAIM_ATTEMPTS):
        # 再次检查已领取
        if _check_daily_claimed(ctx, g):
            _close_ui(ctx, g)
            return True

        # 模板匹配领取按钮（右下 ROI）
        claim_rect = vu.find_template(
            ctx,
            res.template_ui("btn_claim_encounter_points_rewards.png"),
            threshold=0.7,
            roi=_CLAIM_BTN_ROI,
        )
        if claim_rect is not None:
            g.click(claim_rect.cx, claim_rect.cy)
            time.sleep(1.0)

            # 检查是否领成功
            if _check_daily_claimed(ctx, g):
                _close_ui(ctx, g)
                return True
        else:
            time.sleep(0.5)

    # 领取失败，关闭 UI
    _close_ui(ctx, g)
    return False


# ══════════════════════════════════════════════════════════════════
# 2. 凯瑟琳对话领（GoToAdventurersGuildTask）
# ══════════════════════════════════════════════════════════════════


def claim_daily_at_guild(
    ctx: "GameContext",
    g: "HighLevelApi",
    country: str = "蒙德",
) -> bool:
    """凯瑟琳对话领取每日奖励（F1 失败后的回退路径）。

    流程（对照 BGI GoToAdventurersGuildTask）：
    1. PathExecutor 走冒险家协会路径
    2. 找 F「凯瑟琳」→ 按 F 交互
    3. 对话选「每日委托」（橙色选项）
    4. 点击黑色确认按钮
    5. 逐项领取（SelectLastOptionUntilEnd 模式）
    6. ESC 退出对话 → 切到「探索派遣」标签
    7. 一键领取/重新探索派遣
    8. ESC 退出

    Args:
        country: 国家名（蒙德/璃月/稻妻/须弥/枫丹/挪德卡莱），决定走哪条路径

    返回 True = 成功；False = 失败。
    """
    from avc._core import KeyCode
    from abilities.navigation.path_executor import PathExecutor, load_path_task

    # 1. 走冒险家协会路径
    path_name = _guild_path_name(country)
    path_dir = res.path_json("guild")
    path_file = path_dir / path_name
    if not path_file.exists():
        return False

    task = load_path_task(path_file)
    executor = PathExecutor(ctx, g)
    executor.execute(task)

    # 2. 找 F「凯瑟琳」→ 按 F 交互
    if not _find_and_interact_npc(ctx, g, _KATHERINE):
        return False

    # 3. 对话选「每日委托」
    time.sleep(0.5)
    g.talk(_DAILY_QUEST)

    # 4. 点击黑色确认按钮
    time.sleep(0.5)
    _click_black_confirm(ctx, g)

    # 5. 逐项领取（BGI SelectLastOptionUntilEnd：反复选最后一个选项直到无选项）
    _select_last_until_end(ctx, g)

    # 6. ESC 退出当前对话 → 切到「探索派遣」
    g.press(KeyCode.esc)
    time.sleep(0.5)

    # 7. 一键领取/重新探索派遣
    one_key_expedition(ctx, g)

    # 8. ESC 退出
    g.press(KeyCode.esc)
    time.sleep(0.3)
    g.press(KeyCode.esc)

    return True


# ══════════════════════════════════════════════════════════════════
# 3. 派遣一键领取/重新探索
# ══════════════════════════════════════════════════════════════════


def one_key_expedition(ctx: "GameContext", g: "HighLevelApi") -> bool:
    """一键领取探索派遣奖励 + 重新探索。

    流程（对照 BGI OneKeyExpedition）：
    1. 点击「探索派遣」图标/文字
    2. 找「领取全部」按钮（collect 模板）→ 点击
    3. 确认领取
    4. 找「重新探索」按钮（re 模板）→ 点击
    5. 选择角色 → 确认

    返回 True = 至少执行了一步操作；False = 未找到派遣界面。
    """
    from avc._core import KeyCode

    # 1. 找探索派遣入口
    explore_rect = vu.find_template(
        ctx, res.template_dialog("icon_explore.png"), threshold=0.7
    )
    if explore_rect is None:
        # 尝试 OCR 找「探索」
        explore_rect = g.find_text(_EXPLORE)
    if explore_rect is None:
        return False

    g.click(explore_rect.cx, explore_rect.cy)
    time.sleep(1.0)

    did_action = False

    # 2. 领取全部（collect 模板）
    for _ in range(5):  # 最多 5 次领取（多个派遣）
        collect_rect = vu.find_template(
            ctx, res.template_dialog("collect.png"), threshold=0.7
        )
        if collect_rect is None:
            # 也尝试 ui/collect.png
            collect_rect = vu.find_template(
                ctx, res.template_ui("collect.png"), threshold=0.7
            )
        if collect_rect is None:
            break
        g.click(collect_rect.cx, collect_rect.cy)
        time.sleep(0.8)
        # 确认领取
        _click_black_confirm(ctx, g)
        time.sleep(0.5)
        did_action = True

    # 3. 重新探索（re 模板）
    for _ in range(5):  # 最多 5 次重派
        re_rect = vu.find_template(
            ctx, res.template_dialog("re.png"), threshold=0.7
        )
        if re_rect is None:
            break
        g.click(re_rect.cx, re_rect.cy)
        time.sleep(0.5)
        # 选择角色 → 确认（BGI: 选第一个可用角色 → 点击确认）
        # 简化：点击画面中央偏下区域（角色列表位置），然后确认
        g.click(960, 700)  # 角色列表大致位置
        time.sleep(0.3)
        _click_black_confirm(ctx, g)
        time.sleep(0.5)
        did_action = True

    return did_action


# ══════════════════════════════════════════════════════════════════
# 4. 验证 + 总控
# ══════════════════════════════════════════════════════════════════


def check_daily_claimed(ctx: "GameContext", g: "HighLevelApi") -> bool:
    """检查今日奖励是否已领取（OCR「今日奖励已领取」）。

    可在任意界面调用（不改变 UI 状态）。
    """
    return _check_daily_claimed(ctx, g)


def claim_daily_reward(
    ctx: "GameContext",
    g: "HighLevelApi",
    country: str = "蒙德",
) -> bool:
    """领取每日奖励总控：F1 直领 → 凯瑟琳回退 → 验证。

    对照 BGI OneDragonFlow 中「领取每日奖励」步骤：
    优先 F1 冒险之证直领（快速），失败则走凯瑟琳对话（可靠）。

    Args:
        country: 凯瑟琳回退路径的国家（蒙德/璃月/稻妻/须弥/枫丹/挪德卡莱）

    返回 True = 成功领取或已领取；False = 两路均失败。
    """
    # 先检查是否已领
    if check_daily_claimed(ctx, g):
        return True

    # 路径 1：F1 直领
    if claim_encounter_points(ctx, g):
        return True

    # 路径 2：凯瑟琳对话
    if claim_daily_at_guild(ctx, g, country):
        # 验证
        time.sleep(1.0)
        if check_daily_claimed(ctx, g):
            return True

    return False


# ══════════════════════════════════════════════════════════════════
# 内部辅助
# ══════════════════════════════════════════════════════════════════


def _check_daily_claimed(ctx: "GameContext", g: "HighLevelApi") -> bool:
    """OCR 检查「今日奖励已领取」。"""
    return g.find_text(_DAILY_CLAIMED) is not None


def _close_ui(ctx: "GameContext", g: "HighLevelApi") -> None:
    """ESC 关闭当前 UI → 等回主界面。"""
    from avc._core import KeyCode

    for _ in range(3):
        g.press(KeyCode.esc)
        if g.wait_main_ui(timeout=_WAIT_SCENE_S):
            return


def _click_black_confirm(ctx: "GameContext", g: "HighLevelApi") -> bool:
    """点击黑色确认按钮（btn_black_confirm 模板）。返回是否找到并点击。"""
    rect = vu.find_template(
        ctx, res.template_ui("btn_black_confirm.png"), threshold=0.7
    )
    if rect is not None:
        g.click(rect.cx, rect.cy)
        time.sleep(0.5)
        return True
    return False


def _find_and_interact_npc(
    ctx: "GameContext",
    g: "HighLevelApi",
    npc_name: str,
    timeout: float = 10.0,
) -> bool:
    """找 NPC 的 F 交互提示 → 按 F → 等对话场景。

    在 NPC_F_ROI 区域搜索 F 键图标 + OCR npc_name。
    """
    from avc._core import KeyCode
    from framework.scene import Scene

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        # 找 F 键图标
        f_rect = vu.find_template(
            ctx, res.template_pick("F.png"), threshold=0.7, roi=_NPC_F_ROI
        )
        if f_rect is not None:
            # 可选：OCR 确认 NPC 名字（F 图标附近文字）
            # 简化：找到 F 就按（凯瑟琳是唯一交互目标）
            g.press(KeyCode.f)
            time.sleep(0.8)
            # 等对话场景
            if g.wait_scene(Scene.DIALOG, timeout=3.0):
                return True
        time.sleep(0.3)

    return False


def _select_last_until_end(
    ctx: "GameContext",
    g: "HighLevelApi",
    max_rounds: int = 10,
) -> None:
    """反复选最后一个对话选项直到无选项（BGI SelectLastOptionUntilEnd）。

    用于凯瑟琳对话中逐项领取奖励。
    """
    from abilities.dialog import visible_options

    for _ in range(max_rounds):
        opts = visible_options(ctx)
        if not opts:
            break
        # 选最后一个（BGI: 最上面的选项在列表 index 0，对应画面最下面；
        # visible_options 按 Y 降序，所以 opts[0] = 画面最下面 = 最后一个选项）
        last = opts[0]
        g.click(last.rect.cx, last.rect.cy)
        time.sleep(0.5)


def _guild_path_name(country: str) -> str:
    """国家名 → 冒险家协会路径文件名。"""
    _COUNTRY_MAP = {
        "蒙德": "冒险家协会_蒙德.json",
        "璃月": "冒险家协会_璃月.json",
        "稻妻": "冒险家协会_稻妻.json",
        "须弥": "冒险家协会_须弥.json",
        "枫丹": "冒险家协会_枫丹.json",
        "挪德卡莱": "冒险家协会_挪德卡莱.json",
    }
    return _COUNTRY_MAP.get(country, f"冒险家协会_{country}.json")
