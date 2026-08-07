"""daily —— 每日总控（Phase D）。

对照 BGI OneDragonFlow 的每日部分：
1. 领邮件 → claim_mail
2. 合成浓缩树脂 → craft_resin
3. 自动秘境 → auto_domain
4. 自动首领讨伐 → auto_boss
5. 自动地脉之花 → auto_ley_line
6. 领取每日奖励 → claim_daily_reward
7. 尘歌壶 → enter_pot

当前实现：全流程覆盖。各步骤失败不阻断后续（记录错误）。
"""

from __future__ import annotations

from framework import task
from framework.errors import NormalEnd, TaskError


@task(
    name="daily",
    desc="每日总控：领邮件→合成树脂→自动秘境→首领讨伐→地脉之花→领取每日奖励→尘歌壶",
    daemons=["frame", "scene_estimator", "auto_eat", "auto_skip"],
    requires=["navigation", "fighter"],
    params={
        "domain_name": {
            "type": "str",
            "default": "绝缘之境",
            "desc": "秘境名（tp.json 地点名或别名，如 绝缘之境）",
        },
        "domain_count": {
            "type": "int",
            "default": 5,
            "desc": "秘境刷取次数（0=直到树脂耗尽）",
        },
        "boss_name": {
            "type": "str",
            "default": "急冻树",
            "desc": "首领名（须有 resources/paths/boss/{名}前往.json）",
        },
        "boss_count": {
            "type": "int",
            "default": 5,
            "desc": "首领讨伐次数（0=直到树脂耗尽）",
        },
        "ley_line_region": {
            "type": "str",
            "default": "蒙德",
            "desc": "地脉花地区前缀",
        },
        "ley_line_count": {
            "type": "int",
            "default": 4,
            "desc": "地脉花刷取次数（0=直到树脂耗尽）",
        },
        "country": {
            "type": "str",
            "default": "蒙德",
            "desc": "凯瑟琳所在国家（蒙德/璃月/稻妻/须弥/枫丹/挪德卡莱）",
        },
        "craft_country": {
            "type": "str",
            "default": "蒙德",
            "desc": "合成台所在国家（蒙德/璃月/稻妻/枫丹）",
        },
    },
    tags=["p1", "daily"],
)
def main(
    ctx,
    g,
    domain_name: str = "绝缘之境",
    domain_count: int = 5,
    boss_name: str = "急冻树",
    boss_count: int = 5,
    ley_line_region: str = "蒙德",
    ley_line_count: int = 4,
    country: str = "蒙德",
    craft_country: str = "蒙德",
) -> dict:
    """每日总控主流程。返回各步骤结果摘要。"""
    results: dict = {}

    # ── 1. 领邮件 ──
    try:
        mail_result = g.run("claim_mail")
        results["mail"] = mail_result
    except TaskError as e:
        results["mail"] = {"error": str(e)}

    # ── 2. 合成浓缩树脂 ──
    try:
        craft_result = g.run("craft_resin", country=craft_country)
        results["craft"] = craft_result
    except TaskError as e:
        results["craft"] = {"error": str(e)}

    # ── 3. 自动秘境 ──
    try:
        domain_result = g.run("auto_domain", domain_name=domain_name, count=domain_count)
        results["domain"] = domain_result
    except NormalEnd as e:
        results["domain"] = {"exhausted": True, "msg": str(e)}
    except TaskError as e:
        results["domain"] = {"error": str(e)}

    # ── 4. 自动首领讨伐 ──
    try:
        boss_result = g.run("auto_boss", boss_name=boss_name, count=boss_count)
        results["boss"] = boss_result
    except NormalEnd as e:
        results["boss"] = {"exhausted": True, "msg": str(e)}
    except TaskError as e:
        results["boss"] = {"error": str(e)}

    # ── 5. 自动地脉之花 ──
    try:
        ley_result = g.run(
            "auto_ley_line", region=ley_line_region, count=ley_line_count
        )
        results["ley_line"] = ley_result
    except NormalEnd as e:
        results["ley_line"] = {"exhausted": True, "msg": str(e)}
    except TaskError as e:
        results["ley_line"] = {"error": str(e)}

    # ── 6. 领取每日奖励 ──
    try:
        daily_result = g.run("claim_daily_reward", country=country)
        results["daily_reward"] = daily_result
    except TaskError as e:
        results["daily_reward"] = {"error": str(e)}

    # ── 7. 尘歌壶 ──
    try:
        pot_result = g.run("enter_pot")
        results["pot"] = pot_result
    except TaskError as e:
        results["pot"] = {"error": str(e)}

    return results
