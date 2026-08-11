"""auto_domain —— 自动刷秘境（Phase D 骨架）。

对照 BGI AutoDomainTask 的简化版：

1. 传送到秘境 → 进入
2. 循环 count 次：
   a. ``fight_domain`` 战斗到清场
   b. ``claim_domain_reward`` 领取（树脂耗尽 → ``NormalEnd``）
   c. ``exit_domain`` 退出 → 再进入下一轮

⚠️ 骨架边界：① 位置追踪/传送链未实机验证；② 不做 YOLO 树检测+摄像机旋转
  （v1 用 OCR「石化古树」简化）；③ 不做队伍切换/圣遗物分解/树脂 20/40 切换/复活重试。
"""

from __future__ import annotations

from framework import task
from framework.errors import NormalEnd, TaskError


@task(
    name="auto_domain",
    desc="自动刷秘境：传送→进入→战斗→领奖→退出→循环。domain_name 须有坐标记录。v1 简化版。",
    daemons=["frame", "scene_estimator", "auto_eat", "auto_skip", "auto_pick"],
    requires=["navigation", "fighter"],
    params={
        "domain_name": {
            "type": "str",
            "required": True,
            "desc": "秘境名（tp.json 地点名或别名，如 绝缘之境）",
        },
        "count": {
            "type": "int",
            "default": 5,
            "desc": "刷取次数（0=直到树脂耗尽）",
        },
    },
    tags=["p1", "combat"],
)
def main(ctx, g, domain_name: str, count: int = 5) -> dict:
    """秘境刷取主流程。返回 ``{domain, count}``。"""
    from abilities.domain import (
        claim_domain_reward,
        enter_domain,
        exit_domain,
        get_domain_coords,
    )

    ob = ctx.observe
    # 检查秘境坐标是否存在
    if get_domain_coords(domain_name) is None:
        raise TaskError(f"未找到秘境坐标: {domain_name}（tp.json 无此秘境，且无别名）")

    done = 0
    while count == 0 or done < count:
        itr = done + 1
        # 1. 进入秘境
        if not enter_domain(ctx, g, domain_name):
            ob.event("auto_domain.step", ability="auto_domain", phase="act",
                     step="enter", iter=itr, ok=False, reason="enter_failed")
            raise TaskError(f"进入秘境失败: {domain_name}")
        ob.event("auto_domain.step", ability="auto_domain", phase="act",
                 step="enter", iter=itr, ok=True)

        # 2. 战斗到清场
        if not fight_domain_safe(ctx, g, timeout=300):
            ob.event("auto_domain.step", ability="auto_domain", phase="act",
                     step="fight", iter=itr, ok=False, reason="timeout")
            raise TaskError(f"秘境战斗超时未清场: {domain_name}")
        ob.event("auto_domain.step", ability="auto_domain", phase="act",
                 step="fight", iter=itr, ok=True)

        # 3. 领奖
        ok = claim_domain_reward(ctx, g)
        ob.event("auto_domain.step", ability="auto_domain", phase="act",
                 step="claim", iter=itr, ok=ok, exhausted=not ok)
        done += 1
        if not ok:
            # 树脂耗尽 → 退出后正常结束
            exit_domain(ctx, g)
            raise NormalEnd(f"树脂耗尽，已完成 {done} 次秘境")

        # 4. 退出秘境（下一轮会重新进入）
        if not exit_domain(ctx, g):
            ob.event("auto_domain.step", ability="auto_domain", phase="act",
                     step="exit", iter=itr, ok=False, reason="exit_failed")
            raise TaskError("退出秘境失败")
        ob.event("auto_domain.step", ability="auto_domain", phase="act",
                 step="exit", iter=itr, ok=True)

    return {"domain": domain_name, "count": done}


def fight_domain_safe(ctx, g, timeout: float = 300) -> bool:
    """战斗包装（避免 main 里 lazy import 报错）。"""
    from abilities.domain import fight_domain

    return fight_domain(ctx, g, timeout=timeout)
