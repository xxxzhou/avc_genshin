"""auto_ley_line —— 自动刷地脉之花（启示之花 / 藏金之花）。

**v2 流程（动态找花）**：

1. ``press M`` 开地图 → ``find_blossom_and_nearest_tp`` 在大图上识别地脉花图标
   并返回花位置 + 最近传送点
2. ``teleport_to(nearest_tp.name)`` 传送到花附近（tp 内部自动开关地图）
3. ``go_to(blossom_pos)`` 走到花
4. 循环 count 次（每次重新找花，花被领奖后消失）：
   a. 等花交互提示 → 按 F 激活地脉花
   b. ``fight_until_clear`` 战斗到清场
   c. 再等花交互提示 → 按 F → ``claim_resin_reward``；耗尽 → ``NormalEnd``

**对照 BGI ``AutoLeyLineOutcropTask``**：v1 走固定路径文件（``蒙德1-风起地-1.json``
等）不适合地脉花每日位置变化；v2 用 ``find_blossom_and_nearest_tp`` 动态识别花
位置 + 选最近传送点，等价于 BGI 的 ``LocateLeyLineOutcrop`` + ``GetNearestGpp``
组合（简化版）。

⚠️ v2 实机边界：① ``flower_type`` 仅记录不参与筛选（find_blossom 内部会过滤）；
② 找不到花立即 ``NormalEnd``（count=0 模式）或 ``TaskError``（指定次数）。
"""

from __future__ import annotations

from framework import task
from framework.errors import NormalEnd, TaskError
from framework.resources import res


@task(
    name="auto_ley_line",
    desc="自动刷地脉之花（启示/藏金）：动态找花→传送→战斗→领奖→循环。region 保留兼容旧参数（v2 用 find_blossom 自动定位）。",
    daemons=["frame", "scene_estimator", "auto_eat", "timeline_snap", "llm_watch"],
    requires=["navigation", "fighter"],
    params={
        "region": {
            "type": "str",
            "default": "蒙德",
            "desc": "保留兼容旧参数（v2 动态找花不再依赖路径文件）。地图找花失败时回退用 region 路径",
        },
        "flower_type": {
            "type": "str",
            "default": "",
            "desc": "地脉花类型筛选：''=不限，'revelation'=启示之花，'wealth'=藏金之花",
        },
        "count": {
            "type": "int",
            "default": 4,
            "desc": "刷取次数（0=直到树脂耗尽或地图无花）",
        },
    },
    tags=["p1", "combat"],
)
def main(ctx, g, region: str = "蒙德", flower_type: str = "", count: int = 4) -> dict:
    """地脉之花刷取主流程（v2：动态找花）。

    返回 ``{flower_type, count, last_nearest_tp}``。
    """
    from abilities.game_state import has_flower_f_icon
    from abilities.reward import claim_resin_reward
    from avc._core import KeyCode
    from framework.scene import Scene

    ob = ctx.observe
    done = 0
    last_info: dict | None = None
    # 失败花黑名单：山地/悬崖花 go_to 不收敛时换花重试，不死磕一朵
    # （2026-08-22 实机：奥藏山花 cam.rotate 30s×3 失败 dist 卡 230-290 不收敛）
    blacklist: list[tuple[float, float]] = []
    MAX_BLOSSOM_ATTEMPTS = 3  # 每轮最多换 3 朵花

    class _BlossomRetry(Exception):
        """换花重试信号（黑名单当前花）。"""

    while count == 0 or done < count:
        itr = done + 1
        for attempt in range(1, MAX_BLOSSOM_ATTEMPTS + 1):
            # 1. 动态找花（每次循环都重新找，花被领奖后消失；失败花进黑名单跳过）
            info = _find_blossom(
                ctx, g, flower_type=flower_type, iter=itr, exclude=blacklist
            )
            if info is None:
                if done == 0 and not blacklist:
                    raise TaskError(
                        f"地图上未检测到地脉花（flower_type={flower_type!r}）"
                    )
                if not blacklist:
                    raise NormalEnd(f"地图上无更多地脉花，已完成 {done} 次")
                raise TaskError(
                    f"黑名单内外的花均不可用（已拉黑 {len(blacklist)} 朵，完成 {done} 次）"
                )
            last_info = info
            ob.event(
                "auto_ley_line.step", ability="auto_ley_line", phase="observe",
                step="find_blossom", iter=itr, attempt=attempt, ok=True,
                blossom_type=info["blossom_type"],
                blossom_pos=tuple(round(v) for v in info["blossom_pos"]),
                nearest_tp=info["nearest_tp"].name,
                evidence=ob.save_evidence(ctx, f"find_blossom_iter{itr}_try{attempt}"),
            )

            try:
                # 2. 传送到最近点（teleport_to 内部自动开关地图）
                # ⚠ 2026-08-15 实机：同名「传送锚点」大量存在，按名解析歧义（曾落到
                # 群玉阁）→ 改传坐标（TpPosition.tran_x/tran_y）。
                tp_pt = info["nearest_tp"]
                g.teleport_to((tp_pt.tran_x, tp_pt.tran_y))
                ob.event(
                    "auto_ley_line.step", ability="auto_ley_line", phase="act",
                    step="teleport", iter=itr, attempt=attempt, ok=True,
                    target=f"{tp_pt.name}@({tp_pt.tran_x:.0f},{tp_pt.tran_y:.0f})",
                    evidence=ob.save_evidence(
                        ctx, f"teleport_landed_iter{itr}_try{attempt}"
                    ),
                )

                # 3. 走到花（容差 25：r_20260822_025353 实机 dist 52-137 绕花打转——
                # 容差 8 过紧，到达 F 交互圈边缘即停比穿过花心再折返稳）
                if not g.go_to(info["blossom_pos"], tolerance=25.0, timeout=150.0):
                    raise _BlossomRetry("go_to_timeout")
                ob.event(
                    "auto_ley_line.step", ability="auto_ley_line", phase="act",
                    step="go_to_blossom", iter=itr, attempt=attempt, ok=True,
                    evidence=ob.save_evidence(
                        ctx, f"arrived_blossom_iter{itr}_try{attempt}"
                    ),
                )

                # 4. 激活地脉花
                if not g.wait_until(lambda: has_flower_f_icon(ctx), timeout=60):
                    ob.event(
                        "auto_ley_line.step", ability="auto_ley_line",
                        phase="observe", step="flower_wait", iter=itr,
                        attempt=attempt, ok=False, reason="no_flower_icon",
                    )
                    raise _BlossomRetry("no_flower_icon")
                break  # 到位且有交互提示 → 出重试循环去激活
            except (_BlossomRetry, TaskError) as e:
                # 换花重试：go_to 不收敛 / 无交互提示 / 传送失败 / 走路阵亡（复活后
                # 换下一朵，连败 3 朵再整体失败归因）。NormalEnd 不在此列。
                blacklist.append(info["blossom_pos"])
                ob.event(
                    "auto_ley_line.step", ability="auto_ley_line", phase="decide",
                    step="blacklist_blossom", iter=itr, attempt=attempt,
                    ok=False, reason=f"{type(e).__name__}:{e}",
                    blossom_pos=tuple(round(v) for v in info["blossom_pos"]),
                    blacklisted=len(blacklist),
                )
        else:
            raise TaskError(
                f"连续 {MAX_BLOSSOM_ATTEMPTS} 朵花均失败（黑名单 {len(blacklist)} 朵）"
            )

        g.press(KeyCode.f)
        ob.event(
            "auto_ley_line.step", ability="auto_ley_line", phase="act",
            step="activate", iter=itr, ok=True,
        )

        # 5. 战斗到清场
        if not g.fight_until_clear(timeout=180):
            ob.event(
                "auto_ley_line.step", ability="auto_ley_line", phase="act",
                step="fight", iter=itr, ok=False, reason="timeout",
            )
            raise TaskError(f"地脉战斗超时未清场（iter={itr}）")
        ob.event(
            "auto_ley_line.step", ability="auto_ley_line", phase="act",
            step="fight", iter=itr, ok=True,
            evidence=ob.save_evidence(ctx, f"fight_cleared_iter{itr}"),
        )

        # 6. 领奖：等花交互提示 → 按 F → 树脂领取
        if not g.wait_until(lambda: has_flower_f_icon(ctx), timeout=20):
            ob.event(
                "auto_ley_line.step", ability="auto_ley_line", phase="observe",
                step="reward_icon_wait", iter=itr, ok=False, reason="no_reward_icon",
            )
            raise TaskError(f"未检测到地脉花领奖交互提示（iter={itr}）")
        g.press(KeyCode.f)
        if not g.wait_until(lambda: g.find_text("原粹树脂") is not None, timeout=15):
            ob.event(
                "auto_ley_line.step", ability="auto_ley_line", phase="observe",
                step="reward_dialog_wait", iter=itr, ok=False, reason="no_reward_dialog",
            )
            raise TaskError(f"未出现树脂奖励对话框（iter={itr}）")
        ob.save_evidence(ctx, f"reward_dialog_iter{itr}")  # 领奖对话框快照（无事件，纯存档）
        ok = claim_resin_reward(ctx, g)
        ob.event(
            "auto_ley_line.step", ability="auto_ley_line", phase="act",
            step="claim", iter=itr, ok=ok, exhausted=not ok,
        )
        done += 1
        if not ok:
            raise NormalEnd(f"树脂耗尽，已完成 {done} 次地脉")
        g.wait_main_ui(timeout=30)

    return {
        "flower_type": last_info["blossom_type"] if last_info else flower_type,
        "count": done,
        "last_nearest_tp": last_info["nearest_tp"].name if last_info else None,
    }


# ── 内部：找花（开图 → find_blossom → 关图）──


def _find_blossom(
    ctx, g, *, flower_type: str = "", iter: int = 0,
    exclude: list | None = None,
) -> dict | None:
    """开地图找花，返回 ``find_blossom_and_nearest_tp`` 结果或 None。负责开/关地图。

    ``find_blossom_and_nearest_tp`` 要求在 MAP scene 调用（high_level_api.py:273 docstring）。
    调完关闭地图回到 MAIN_UI，让后续 ``teleport_to``（内部自己开图）从干净状态开始。
    """
    from avc._core import KeyCode
    from framework.scene import Scene

    # 开地图（若已在 MAP 则跳过）
    if g.scene is None or g.scene.scene is not Scene.MAP:
        ctx.release_all_keys()
        g.press(KeyCode.m)
        if not g.wait_scene(Scene.MAP, timeout=8.0):
            return None  # 开图失败，让上层判断

    try:
        info = g.find_blossom_and_nearest_tp(flower_type=flower_type, exclude=exclude)
    finally:
        # 不管找没找到，都关地图回 MAIN_UI（teleport_to 自己会再开图）
        ctx.release_all_keys()
        g.press(KeyCode.m)
        g.wait_main_ui(timeout=5.0)
    return info
