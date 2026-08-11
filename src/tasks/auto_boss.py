"""auto_boss —— 自动讨伐世界首领（40 原粹树脂，Phase D 骨架）。

对照 BGI ``AutoBossTask``（**世界首领**，非周本；消耗 40 原粹树脂）的简化版：

1. 解析首领路径 JSON（``resources/paths/boss/{boss}前往.json``）
2. 循环 count 次：
   a. ``PathExecutor`` 到首领附近（**传送可用**；走最后一段待位置追踪）
   b. 等敌人进入战斗 → ``fight_until_clear`` 战斗到清场
   c. 等宝箱/花交互提示 → 按 F 开奖励对话框
   d. ``claim_resin_reward`` 领取；树脂耗尽 → ``NormalEnd``

⚠️ 骨架边界：位置追踪（``PositionGetter``）未就绪时 ``PathExecutor`` 只能"传送到附近"，
  走不到首领就触发不了战斗——编排逻辑先就位，实机后补导航（Phase D 真跑通依赖它）。
  未做：树脂预检/脆弱树脂补充、回归七天神像、掉物拾取（auto_pick 与 auto_eat 同为
  INTERACT 通道会 InputConflict，暂选 auto_eat 保命）。
"""

from __future__ import annotations

from framework import task
from framework.errors import NormalEnd, TaskError
from framework.resources import res


@task(
    name="auto_boss",
    desc="自动讨伐世界首领（40 原粹树脂）：传送到首领→战斗→领奖→循环。boss_name 须有对应路径 JSON。",
    daemons=["frame", "scene_estimator", "auto_eat"],
    requires=["navigation", "fighter"],
    params={
        "boss_name": {
            "type": "str",
            "required": True,
            "desc": "首领名（须有 resources/paths/boss/{名}前往.json）",
        },
        "count": {
            "type": "int",
            "default": 5,
            "desc": "讨伐次数（0=直到树脂耗尽）",
        },
    },
    tags=["p1", "combat"],
)
def main(ctx, g, boss_name: str, count: int = 5) -> dict:
    """世界首领讨伐主流程。返回 ``{boss, count}``。"""
    from abilities.game_state import has_chest_f_icon, has_flower_f_icon
    from abilities.navigation.path_executor import PathExecutor, load_path_task
    from abilities.reward import claim_resin_reward
    from avc._core import KeyCode

    ob = ctx.observe
    # 1. 首领路径 JSON
    path_file = res.path_json(f"boss/{boss_name}前往.json")
    if not path_file.exists():
        raise TaskError(
            f"首领路径缺失: {path_file}（需 resources/paths/boss/{boss_name}前往.json）"
        )
    pt = load_path_task(path_file)
    pe = PathExecutor(ctx, g)

    done = 0
    while count == 0 or done < count:
        itr = done + 1
        # 2. 传送 + 接近（走最后一段待位置追踪）
        pe.execute(pt)
        ob.event("auto_boss.step", ability="auto_boss", phase="act",
                 step="navigate", iter=itr, ok=True)
        # 3. 战斗到清场
        if not g.wait_until(lambda: g.has_enemy(), timeout=60):
            ob.event("auto_boss.step", ability="auto_boss", phase="observe",
                     step="enemy_wait", iter=itr, ok=False, reason="no_enemy")
            raise TaskError(f"到达首领附近但未进入战斗: {boss_name}")
        if not g.fight_until_clear(timeout=180):
            ob.event("auto_boss.step", ability="auto_boss", phase="act",
                     step="fight", iter=itr, ok=False, reason="timeout")
            raise TaskError(f"战斗超时未清场: {boss_name}")
        ob.event("auto_boss.step", ability="auto_boss", phase="act",
                 step="fight", iter=itr, ok=True)
        # 4. 领奖：等交互提示 → 按 F → 树脂领取
        g.wait_main_ui(timeout=20)
        if not g.wait_until(
            lambda: has_chest_f_icon(ctx) or has_flower_f_icon(ctx), timeout=20
        ):
            ob.event("auto_boss.step", ability="auto_boss", phase="observe",
                     step="reward_icon_wait", iter=itr, ok=False, reason="no_reward_icon")
            raise TaskError(f"未检测到首领奖励交互提示: {boss_name}")
        g.press(KeyCode.f)
        if not g.wait_until(lambda: g.find_text("原粹树脂") is not None, timeout=15):
            ob.event("auto_boss.step", ability="auto_boss", phase="observe",
                     step="reward_dialog_wait", iter=itr, ok=False, reason="no_reward_dialog")
            raise TaskError("未出现树脂奖励对话框")
        ok = claim_resin_reward(ctx, g)
        ob.event("auto_boss.step", ability="auto_boss", phase="act",
                 step="claim", iter=itr, ok=ok, exhausted=not ok)
        done += 1
        if not ok:
            raise NormalEnd(f"树脂耗尽，已完成 {done} 次讨伐")
        g.wait_main_ui(timeout=30)

    return {"boss": boss_name, "count": done}
