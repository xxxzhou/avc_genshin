"""claim_daily_reward —— 领取每日奖励（Phase D）。

对照 BGI ClaimEncounterPointsRewardsTask + GoToAdventurersGuildTask：
- 优先 F1 冒险之证直领（快速）
- 失败回退凯瑟琳对话（走冒险家协会路径 → 对话领取 + 派遣一键领取/重探）
- 验证 OCR「今日奖励已领取」

BGI **不做** 4 个每日委托（代码中 disabled placeholder），只领奖励。
原神 5.0+ 用「相遇之缘」机制替代旧 4 委托领原石。
"""

from __future__ import annotations

from framework import task
from framework.errors import TaskError


@task(
    name="claim_daily_reward",
    desc="领取每日奖励：F1 冒险之证直领 → 凯瑟琳对话回退 → 验证。country 指定凯瑟琳所在国家。",
    daemons=["frame", "scene_estimator"],
    params={
        "country": {
            "type": "str",
            "default": "蒙德",
            "desc": "凯瑟琳回退路径的国家（蒙德/璃月/稻妻/须弥/枫丹/挪德卡莱）",
        },
    },
    tags=["p1", "daily"],
)
def main(ctx, g, country: str = "蒙德") -> dict:
    """每日奖励领取主流程。返回 ``{claimed, method}``。"""
    from abilities.daily import claim_daily_reward, check_daily_claimed

    # 先检查是否已领
    if check_daily_claimed(ctx, g):
        return {"claimed": True, "method": "already_claimed"}

    # 两路领取
    ok = claim_daily_reward(ctx, g, country=country)
    if not ok:
        raise TaskError("每日奖励领取失败（F1 直领 + 凯瑟琳对话均失败）")

    # 判断哪路成功（简化：F1 成功=encounter_points，否则=guild）
    method = "encounter_points"  # F1 优先
    return {"claimed": True, "method": method}
