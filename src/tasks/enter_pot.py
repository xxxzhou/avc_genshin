"""enter_pot —— 进入尘歌壶并领取奖励（Phase D）。

对照 BGI GoToSereniteaPotTask：进入尘歌壶→找阿圆→领好感+宝钱→退出。
v1 简化：不做商店购买、硬编码移动找阿圆。
"""

from __future__ import annotations

from framework import task
from framework.errors import TaskError


@task(
    name="enter_pot",
    desc="进入尘歌壶并领取奖励：传送进入→找阿圆→领好感+宝钱→退出。v1 简化版。",
    daemons=["frame", "scene_estimator", "auto_skip", "auto_talk"],
    requires=["navigation"],
    tags=["p1", "daily"],
)
def main(ctx, g) -> dict:
    """尘歌壶奖励领取主流程。返回 ``{entered, claimed, exited}``。"""
    from abilities.pot import claim_pot_rewards, enter_serenitea_pot, exit_serenitea_pot

    # 1. 进入尘歌壶
    if not enter_serenitea_pot(ctx, g):
        raise TaskError("进入尘歌壶失败")

    # 2. 领取奖励（失败不致命，记录即可）
    claimed = claim_pot_rewards(ctx, g)

    # 3. 退出尘歌壶
    if not exit_serenitea_pot(ctx, g):
        raise TaskError("退出尘歌壶失败")

    return {"entered": True, "claimed": claimed, "exited": True}
