"""GuardRail —— 健康度护栏（自动 kill 异常状态）。

订阅 Observe 事件流，**触发阈值即调 ``token.cancel()``** + 存证 + emit ``auto_kill`` 事件。
解决"任务出问题但停不下来"的痛点：不用等用户按 F9，也不用等到 600s 超时。

触发规则（任一即触发，**一次性**——一发即停）：

1. ``same_reason``：同 ``(ability, event, reason)`` 在 ``window_sec`` 秒内出现 ``same_reason_n`` 次以上。
   覆盖能力级死循环（如 ``nav.step too_far`` 反复出现 30 次）—— navigator 内部 ``too_far_count > 50``
   是 ability 自救，护栏是系统级兜底（ability 自救失效时仍能止损）。
2. ``same_fail``：同 ``(ability, event, scene, ok=False)`` 在 ``window_sec`` 秒内出现 ``same_fail_n`` 次以上。
   覆盖错位循环（如 scene 卡 map 时 pos.match 在错的 scene 反复"匹配成功"但实际错位）。

触发后：``observe.save_evidence`` 存截图 → emit ``auto_kill`` 事件（带 rule/ability/count/evidence）
→ ``token.cancel(reason)`` → runtime 走正常 teardown（卸载守护 + 释放按键 + run_summary 包含 auto_kill）。

调参：``Runtime(guardrail_cfg=...)`` 或环境变量 ``AVCGS_GUARD_*``（见 ``Config``）。
"""

from __future__ import annotations

import time
from collections import deque
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from framework.cancellation import CancellationToken
    from framework.context import GameContext
    from framework.observe import Observe


# 默认阈值（02 §4 护栏；实机调参后写回此处）
DEFAULTS = {
    "same_reason_n": 30,   # 同 (ability,event,reason) 窗口内 N 次（含 ok=True/None/False）
    "same_fail_n": 30,     # 同 (ability,event,scene,ok=False) 窗口内 N 次
    "window_sec": 60.0,
}

# 扫描否定白名单：这些 reason 表示「合理轮询中的预期否定」（如 wait_until 等 UI 出现），
# 不计入 same_fail 死循环检测。否则 wait_until 60s 轮询 120 次会误触发。
# 真死循环的 reason（如 nav.step too_far / tp.navigate dist_too_far）不在白名单。
IGNORE_REASONS = frozenset({
    "template_not_matched",  # 模板未匹配（场景分类器扫非当前场景模板 / wait_until 等 UI）
    "no_blood_bar",          # 血条色块未检测到（MAIN_UI 无怪 / 视角无怪）
})


class GuardRail:
    """健康度护栏：监控 Observe 事件流，触发阈值自动 cancel。

    线程安全：``on_event`` 在 Observe.event 同步调用（loop 或 pool 线程），deque.append 是
    GIL 保护的单操作，无需加锁。一次性触发后 ``_fired=True``，后续事件直通 return。
    """

    def __init__(
        self,
        token: "CancellationToken",
        observe: "Observe",
        ctx: "GameContext",
        *,
        same_reason_n: int = DEFAULTS["same_reason_n"],
        same_fail_n: int = DEFAULTS["same_fail_n"],
        window_sec: float = DEFAULTS["window_sec"],
    ):
        self.token = token
        self.observe = observe
        self.ctx = ctx
        self.same_reason_n = max(1, same_reason_n)
        self.same_fail_n = max(1, same_fail_n)
        self.window_sec = max(1.0, window_sec)
        # 滑窗：每个 key 维护一个 deque[monotonic_ts]
        self._reason_hits: dict[tuple, deque] = {}
        self._fail_hits: dict[tuple, deque] = {}
        self._fired = False

    # ── 订阅回调 ──

    def on_event(self, event: dict[str, Any]) -> None:
        """Observe.subscribe 的回调。异常自吞（Observe 已包 try/except，但双保险）。"""
        try:
            self._check(event)
        except Exception:
            pass  # 护栏失败绝不淹没原始事件流

    # ── 内部 ──

    def _check(self, event: dict[str, Any]) -> None:
        if self._fired or self.token.cancelled:
            return
        ability = event.get("ability") or event.get("task") or "(framework)"
        kind = event.get("event", "")
        if not kind:
            return
        reason = event.get("reason")
        scene = event.get("scene")
        ok = event.get("ok")
        now = time.monotonic()

        # 扫描否定白名单（template_not_matched/no_blood_bar = 合理轮询否定）
        # 跳过 same_reason + same_fail 双规则——避免 wait_until 等 UI 反复扫描误杀
        if reason in IGNORE_REASONS:
            return

        # reason=None 的事件（成功/未填）不算 same_reason 死循环：
        # 反复成功的 pos.match / nav.step（reason=None 普通走步）是正常热轮询。
        # 死循环信号必须有明确 reason（如 nav.step too_far / tp.navigate dist_too_far）。
        if reason is not None:
            # 规则 1：同 (ability, event, reason) 窗口内 N 次（覆盖热轮询死循环）
            k1 = (ability, kind, reason)
            d1 = self._reason_hits.get(k1)
            if d1 is None:
                d1 = deque()
                self._reason_hits[k1] = d1
            d1.append(now)
            self._trim(d1, now)
            if len(d1) >= self.same_reason_n:
                self._fire(rule="same_reason", key=k1, count=len(d1), scene=scene)
                return

        # 规则 2：同 (ability, event, scene, ok=False) 窗口内 N 次（覆盖错位循环）
        if ok is False:
            k2 = (ability, kind, scene)
            d2 = self._fail_hits.get(k2)
            if d2 is None:
                d2 = deque()
                self._fail_hits[k2] = d2
            d2.append(now)
            self._trim(d2, now)
            if len(d2) >= self.same_fail_n:
                self._fire(rule="same_fail", key=k2, count=len(d2), scene=scene)
                return

    def _trim(self, d: deque, now: float) -> None:
        cutoff = now - self.window_sec
        while d and d[0] < cutoff:
            d.popleft()

    def _fire(self, *, rule: str, key: tuple, count: int, scene: Any) -> None:
        """触发取消。一次性（_fired 守卫），save_evidence 失败不阻断。"""
        if self._fired:
            return
        self._fired = True
        ability = key[0]
        kind = key[1]
        reason = key[2] if rule == "same_reason" else None
        threshold = self.same_reason_n if rule == "same_reason" else self.same_fail_n
        summary = (
            f"guardrail:{rule} ability={ability} event={kind} "
            f"reason={reason!r} count={count}/{self.window_sec:.0f}s (threshold={threshold})"
        )
        # 1. 存证截图（失败容忍——可能 ctx 已坏）
        try:
            evidence = self.observe.save_evidence(self.ctx, "auto_kill")
        except Exception:
            evidence = None
        # 2. emit auto_kill 事件（run_summary 会包含它）
        self.observe.event(
            "auto_kill",
            level="error",
            rule=rule,
            ability=ability,
            event_name=kind,
            reason=reason,
            scene=scene,
            count=count,
            threshold=threshold,
            window_sec=self.window_sec,
            evidence=evidence,
            summary=summary,
        )
        # 3. 触发取消（runtime 走正常 teardown：卸载守护 + 释放按键 + run_summary）
        self.token.cancel(summary)
