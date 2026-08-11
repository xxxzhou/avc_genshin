"""auto_ley_line —— 自动刷地脉之花（启示之花 / 藏金之花，Phase D 骨架）。

对照 BGI ``AutoLeyLineOutcropTask`` 的**大幅简化**（不做大图图标/冒险之证定位，
直接按 region 挑一条既有路径走）：

1. 按 region 挑地脉路径（``resources/paths/ley_line/{region}*.json`` 第一条）
2. 循环 count 次：
   a. ``PathExecutor`` 到地脉花附近
   b. 等花交互提示 → 按 F 激活地脉花
   c. ``fight_until_clear`` 战斗到清场
   d. 再等花交互提示 → 按 F → ``claim_resin_reward``；耗尽 → ``NormalEnd``

⚠️ 骨架边界：① 位置追踪未就绪，PathExecutor 只能传送到附近（同 auto_boss）；②
  按 region 挑**第一条**路径 = 固定刷一个点位，BGI 的"大图找花+就近分支"未做；
  ③ ``flower_type`` 目前仅记录，不参与路径筛选。实机后按需补。
"""

from __future__ import annotations

from framework import task
from framework.errors import NormalEnd, TaskError
from framework.resources import res


@task(
    name="auto_ley_line",
    desc="自动刷地脉之花（启示/藏金）：走路径→激活花→战斗→领奖→循环。region 指定地区前缀。",
    daemons=["frame", "scene_estimator", "auto_eat"],
    requires=["navigation", "fighter"],
    params={
        "region": {
            "type": "str",
            "default": "蒙德",
            "desc": "地区前缀（匹配 resources/paths/ley_line/{region}*.json）",
        },
        "flower_type": {
            "type": "str",
            "default": "启示之花",
            "desc": "地脉花类型（启示之花/藏金之花；暂仅记录不参与筛选）",
        },
        "count": {
            "type": "int",
            "default": 4,
            "desc": "刷取次数（0=直到树脂耗尽）",
        },
    },
    tags=["p1", "combat"],
)
def main(ctx, g, region: str = "蒙德", flower_type: str = "启示之花", count: int = 4) -> dict:
    """地脉之花刷取主流程。返回 ``{region, flower_type, count}``。"""
    from abilities.game_state import has_flower_f_icon
    from abilities.navigation.path_executor import PathExecutor, load_path_task
    from abilities.reward import claim_resin_reward
    from avc._core import KeyCode

    ob = ctx.observe
    # 1. 按 region 挑路径
    path_dir = res.path_json("ley_line")
    candidates = sorted(path_dir.glob(f"{region}*.json")) if path_dir.exists() else []
    if not candidates:
        raise TaskError(
            f"未找到 {region} 的地脉路径（需 resources/paths/ley_line/{region}*.json）"
        )
    pt = load_path_task(candidates[0])
    pe = PathExecutor(ctx, g)

    done = 0
    while count == 0 or done < count:
        itr = done + 1
        # 2. 到地脉花附近
        pe.execute(pt)
        ob.event("auto_ley_line.step", ability="auto_ley_line", phase="act",
                 step="navigate", iter=itr, path=candidates[0].name, ok=True)
        # 3. 激活地脉花
        if not g.wait_until(lambda: has_flower_f_icon(ctx), timeout=60):
            ob.event("auto_ley_line.step", ability="auto_ley_line", phase="observe",
                     step="flower_wait", iter=itr, ok=False, reason="no_flower_icon")
            raise TaskError(f"未检测到地脉花交互提示: {candidates[0].name}")
        g.press(KeyCode.f)
        ob.event("auto_ley_line.step", ability="auto_ley_line", phase="act",
                 step="activate", iter=itr, ok=True)
        # 4. 战斗到清场
        if not g.fight_until_clear(timeout=180):
            ob.event("auto_ley_line.step", ability="auto_ley_line", phase="act",
                     step="fight", iter=itr, ok=False, reason="timeout")
            raise TaskError("地脉战斗超时未清场")
        ob.event("auto_ley_line.step", ability="auto_ley_line", phase="act",
                 step="fight", iter=itr, ok=True)
        # 5. 领奖：等花交互提示 → 按 F → 树脂领取
        if not g.wait_until(lambda: has_flower_f_icon(ctx), timeout=20):
            ob.event("auto_ley_line.step", ability="auto_ley_line", phase="observe",
                     step="reward_icon_wait", iter=itr, ok=False, reason="no_reward_icon")
            raise TaskError("未检测到地脉花领奖交互提示")
        g.press(KeyCode.f)
        if not g.wait_until(lambda: g.find_text("原粹树脂") is not None, timeout=15):
            ob.event("auto_ley_line.step", ability="auto_ley_line", phase="observe",
                     step="reward_dialog_wait", iter=itr, ok=False, reason="no_reward_dialog")
            raise TaskError("未出现树脂奖励对话框")
        ok = claim_resin_reward(ctx, g)
        ob.event("auto_ley_line.step", ability="auto_ley_line", phase="act",
                 step="claim", iter=itr, ok=ok, exhausted=not ok)
        done += 1
        if not ok:
            raise NormalEnd(f"树脂耗尽，已完成 {done} 次地脉")
        g.wait_main_ui(timeout=30)

    return {"region": region, "flower_type": flower_type, "count": done}
