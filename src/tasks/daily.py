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
    ob = ctx.observe
    results: dict = {}

    def _run_step(name: str, thunk):
        """跑一个子任务，发 daily.step（ability=daily, step=name, ok/exhausted/error）。"""
        try:
            res = thunk()
            results[name] = res
            ob.event("daily.step", ability="daily", phase="act",
                     step=name, ok=True)
        except NormalEnd as e:
            results[name] = {"exhausted": True, "msg": str(e)}
            ob.event("daily.step", ability="daily", phase="act",
                     step=name, ok=True, exhausted=True)
        except TaskError as e:
            results[name] = {"error": str(e)}
            ob.event("daily.step", ability="daily", phase="act",
                     step=name, ok=False, reason="subtask_error", error=str(e))

    # ── 1. 领邮件 ──
    _run_step("mail", lambda: g.run("claim_mail"))
    # ── 2. 合成浓缩树脂 ──
    _run_step("craft", lambda: g.run("craft_resin", country=craft_country))
    # ── 3. 自动秘境 ──
    _run_step("domain", lambda: g.run("auto_domain", domain_name=domain_name, count=domain_count))
    # ── 4. 自动首领讨伐 ──
    _run_step("boss", lambda: g.run("auto_boss", boss_name=boss_name, count=boss_count))
    # ── 5. 自动地脉之花 ──
    _run_step("ley_line", lambda: g.run("auto_ley_line", region=ley_line_region, count=ley_line_count))
    # ── 6. 领取每日奖励 ──
    _run_step("daily_reward", lambda: g.run("claim_daily_reward", country=country))
    # ── 7. 尘歌壶 ──
    _run_step("pot", lambda: g.run("enter_pot"))

    return results
