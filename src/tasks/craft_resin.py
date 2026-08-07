"""craft_resin —— 合成浓缩树脂（Phase D）。

对照 BGI GoToCraftingBenchTask：走合成台路径→按 F 进入→选浓缩树脂→确认合成→退出。
v1 不做精确数量控制（默认最大量）。
"""

from __future__ import annotations

from framework import task
from framework.errors import TaskError


@task(
    name="craft_resin",
    desc="合成浓缩树脂：走合成台路径→进入合成→选浓缩树脂→确认合成。country 指定合成台所在国家。",
    daemons=["frame", "scene_estimator", "auto_eat"],
    requires=["navigation"],
    params={
        "country": {
            "type": "str",
            "default": "蒙德",
            "desc": "合成台所在国家（蒙德/璃月/稻妻/枫丹）",
        },
    },
    tags=["p1", "daily"],
)
def main(ctx, g, country: str = "蒙德") -> dict:
    """浓缩树脂合成主流程。返回 ``{crafted, country}``。"""
    from abilities.craft import craft_condensed_resin

    ok = craft_condensed_resin(ctx, g, country=country)
    if not ok:
        raise TaskError(f"合成浓缩树脂失败（路径或进入失败）: {country}")
    return {"crafted": True, "country": country}
