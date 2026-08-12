"""TimelineSnap —— 周期 + scene 跳变时存当前帧到 ``debug/<run_id>/timeline/``。

**实机诊断的「中段画面」素材**：``observe.save_evidence`` 只在失败时存图，任务卡在中段
（没触发 failure）就是画面盲区——这正是用户痛点「卡在流程中段不自知」。本守护周期性
+ 场景跳变时存图，让 AI 事后回放「第几分钟画面长啥样」，发现「第 N 秒开始卡在某界面」。

- **不调任何 API**（纯存图）；判读由 claude code 本体完成（``Read`` 这些 PNG）。
- 复用 ``frame`` 守护的 ``SharedState.frame``（避免重复 capture）；无共享帧时 fallback 自抓。
- opt-in：``ctx.mount("timeline_snap")`` 或 ``@task(daemons=[..., "timeline_snap"])``。
- diagnose 工具会自动列出 ``debug/<run_id>/timeline/*.png``（见 ``framework/diagnose.py``）。
"""

from __future__ import annotations

import time

from framework.daemons.base import Daemon, DaemonCtx, daemon


@daemon(name="timeline_snap", interval=5.0)  # step 5s：检测 scene 跳变粒度
class TimelineSnapDaemon(Daemon):
    """周期 + scene 跳变存图。文件名 ``<seq>_<ts>_<scene>.png``（seq 单调递增，便于回放排序）。"""

    snap_interval: float = 30.0  # 周期存图间隔（秒）；scene 跳变则无视间隔立即存
    _seq: int = 0
    _last_scene: str | None = None
    _last_snap_ts: float = -1e9  # 初值极负 → 首次 step 必触发（存一张初始画面）

    async def step(self, dctx: DaemonCtx) -> None:
        now = time.monotonic()
        ss = dctx.shared.scene
        scene = ss.scene.value if (ss and ss.scene) else None

        scene_changed = scene != self._last_scene
        periodic = (now - self._last_snap_ts) >= self.snap_interval
        if not (scene_changed or periodic):
            return

        # 帧优先复用 frame 守护的共享帧（free）；无则自抓一次
        buf = dctx.shared.frame or dctx.ctx.capture()
        if buf is None:
            return

        seq = self._seq
        try:
            tl_dir = dctx.observe.debug_dir / "timeline"
            tl_dir.mkdir(parents=True, exist_ok=True)
            ts = dctx.observe.logger.ts()
            name = f"{seq:04d}_{ts:.1f}_{scene or 'unknown'}.png"
            buf.save(str(tl_dir / name))
        except Exception:
            return  # 存图失败（磁盘满/权限）不拖垮守护链；诊断时 timeline/ 文件数=0 即知
        self._seq = seq + 1
        self._last_scene = scene
        self._last_snap_ts = now
        dctx.observe.event(
            "timeline.snap", ability="timeline", phase="observe",
            seq=seq, scene=scene,
            trigger="scene_change" if scene_changed else "periodic",
            _quiet=True,  # 整 run 只发首条（防爆 jsonl）；细节看 timeline/ 目录文件清单
        )
