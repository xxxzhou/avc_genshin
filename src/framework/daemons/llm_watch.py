"""LlmWatch —— 每 60s 截图提交视觉 LLM 判读（CLAUDE.md §9 实机验证强制要求）。

「实机易卡流程中段不自知」：本守护周期截图 → 视觉 LLM 判读「当前在哪个界面 /
卡在哪步 / 下一步做什么」→ 结果落盘 ``debug/<run_id>/llm/<seq>_<ts>.txt`` +
observe 事件（ability=llm_watch）。事后 diagnose / AI 回放即知每分钟画面状态。

- API 调用走 ``asyncio.to_thread``（网络 IO 不占单线程 loop）。
- 判读失败（无 key/网络断）记事件不重试轰炸；下一周期自然再试。
- opt-in：``@task(daemons=[..., "llm_watch"])`` 或 ``ctx.mount("llm_watch")``。
- prompt 固定用 ``vision_llm.WATCH_PROMPT``；间隔可类属性覆盖。
"""

from __future__ import annotations

import asyncio
import time

from framework.daemons.base import Daemon, DaemonCtx, daemon
from framework.vision_llm import WATCH_PROMPT, describe_image


@daemon(name="llm_watch", interval=5.0)  # step 5s 轻量检查；实际判读由 watch_interval 节流
class LlmWatchDaemon(Daemon):
    """周期视觉判读。文件名 ``<seq>_<ts>.txt``（seq 单调递增，与 timeline_snap 对齐）。"""

    watch_interval: float = 60.0  # 判读间隔（秒）
    _seq: int = 0
    _last_ts: float = -1e9  # 初值极负 → 首次 step 必判读一张初始画面

    async def step(self, dctx: DaemonCtx) -> None:
        now = time.monotonic()
        if (now - self._last_ts) < self.watch_interval:
            return
        self._last_ts = now  # 先占位：判读慢也不并发叠加

        buf = dctx.shared.frame or dctx.ctx.capture()
        if buf is None:
            dctx.observe.event("llm.watch", ability="llm_watch", ok=False, reason="no_frame", _quiet=True)
            return

        seq = self._seq
        text = await asyncio.to_thread(describe_image, buf, WATCH_PROMPT)
        ok = not text.startswith("ERR")
        if ok:
            try:
                llm_dir = dctx.observe.debug_dir / "llm"
                llm_dir.mkdir(parents=True, exist_ok=True)
                (llm_dir / f"{seq:04d}_{dctx.observe.logger.ts():.1f}.txt").write_text(
                    text, encoding="utf-8")
            except Exception:  # noqa: BLE001 — 落盘失败不拖垮守护链
                pass
        self._seq = seq + 1
        dctx.observe.event(
            "llm.watch", ability="llm_watch", phase="observe", seq=seq, ok=ok,
            reason=None if ok else text[:120],
            _quiet=True,  # 整 run 只发首条成败；细节看 llm/ 目录 txt
        )
