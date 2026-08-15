"""临时诊断任务：实机验证 fighter（打怪 + 血条检测 + 血量/死亡检测）链路。

玩家当前在蒙德城 [1982,-866] 内部，被房子挡，开阔地无怪。
本任务**不依赖走路/传送**，直接在当前位置验证：
1. has_enemy（血条检测，IColorDetector）
2. find_nearest_enemy（最近血条）
3. seek_enemy（转视角索敌）
4. find_enemies（世界敌人，bgi_world YOLO）
5. is_low_hp / has_resurrection_icon / has_recovery_icon（血量/死亡检测）
6. is_q_ready（Q 就绪）
7. current_avatar（出战角色识别）
8. 如有怪，fight_until_clear 短打一场

实机回答用户：能打怪吗？能自动检测血量或回复吗？
"""
from __future__ import annotations

from framework import task
from framework.errors import TaskError


@task(
    name="diag_fight",
    desc="实机诊断 fighter 链路（打怪+血量+死亡+回复）",
    daemons=["frame", "scene_estimator", "auto_eat"],
    requires=["navigation", "fighter"],
    params={},
    tags=["diag"],
)
def main(ctx, g) -> dict:
    from abilities.fighter import SimpleFighter

    ob = ctx.observe
    f = SimpleFighter(ctx, g)

    # 0. 基线
    baseline = {
        "scene": g.scene.scene.name if g.scene else None,
    }
    ob.event("diag_fight.step", ability="diag_fight", phase="observe",
             step="baseline", ok=True, **baseline)

    # 1. 战斗态血条检测（has_enemy）
    has_enemy = f.has_enemy()

    # 2. 最近血条
    nearest = f.find_nearest_enemy()
    nearest_xy = (int(nearest.cx), int(nearest.cy)) if nearest else None

    # 3. 转视角索敌（最多 8 次）
    sought = f.seek_enemy(max_turns=8)

    # 4. 世界敌人（YOLO，含发呆态）
    world = f.find_enemies()

    # 5. 血量/死亡/恢复检测
    from abilities.game_state import (
        has_resurrection_icon, is_low_hp, has_recovery_icon,
    )
    frame = ctx.capture()
    dead = has_resurrection_icon(ctx, frame) if frame else False
    low = is_low_hp(ctx, frame) if frame else False
    recovery = has_recovery_icon(ctx, frame) if frame else False

    # 6. Q 就绪 + 出战角色
    q_ready = f.is_q_ready()
    avatar = f.current_avatar()

    # 7. fight_until_clear（如有怪）
    fight_ok = None
    if nearest is not None or world:
        try:
            fight_ok = g.fight_until_clear(timeout=45)
        except Exception as e:
            ob.event("diag_fight.step", ability="diag_fight", phase="act",
                     step="fight_exception", ok=False, detail=repr(e))
    else:
        ob.event("diag_fight.step", ability="diag_fight", phase="observe",
                 step="fight_skipped", ok=False, reason="no_enemy_in_frame")

    ob.event("diag_fight.step", ability="diag_fight", phase="act",
             step="done", ok=True,
             fight_cleared=fight_ok)

    return {
        "scene": baseline["scene"],
        "has_enemy": has_enemy,
        "nearest_blood_bar": nearest_xy,
        "sought_blood_bar": sought is not None,
        "world_enemies_count": len(world),
        "dead_detected": dead,
        "low_hp_detected": low,
        "recovery_icon": recovery,
        "q_ready": q_ready,
        "current_avatar": avatar,
        "fight_cleared": fight_ok,
    }