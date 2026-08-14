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
    daemons=["frame", "scene_estimator", "auto_skip", "auto_talk", "timeline_snap"],
    requires=["navigation"],
    tags=["p1", "daily"],
)
def main(ctx, g) -> dict:
    """尘歌壶奖励领取主流程。返回 ``{entered, claimed, exited}``。"""
    from abilities.pot import claim_pot_rewards, enter_serenitea_pot, exit_serenitea_pot

    ob = ctx.observe
    # 1. 进入尘歌壶
    if not enter_serenitea_pot(ctx, g):
        ob.event("pot.step", ability="enter_pot", phase="act",
                 step="enter", ok=False, reason="enter_failed")
        raise TaskError("进入尘歌壶失败")
    ob.event("pot.step", ability="enter_pot", phase="act", step="enter", ok=True)

    # 2. 领取奖励（失败不致命，记录即可）
    claimed = claim_pot_rewards(ctx, g)
    ob.event("pot.claim", ability="enter_pot", phase="act",
             step="claim", ok=claimed, claimed=claimed)

    # 3. 退出尘歌壶
    if not exit_serenitea_pot(ctx, g):
        ob.event("pot.step", ability="enter_pot", phase="act",
                 step="exit", ok=False, reason="exit_failed")
        raise TaskError("退出尘歌壶失败")
    ob.event("pot.step", ability="enter_pot", phase="act", step="exit", ok=True)

    return {"entered": True, "claimed": claimed, "exited": True}
