"""GuardRail 护栏测试：same_reason / same_fail 触发、窗口剔除、一次性触发。

可 ``python -m pytest tests/test_guardrail.py -v``。
"""

from __future__ import annotations

from unittest.mock import MagicMock


def _make_guard(*, same_reason_n=5, same_fail_n=4, window_sec=10.0):
    """造一个 GuardRail（token/observe/ctx 全 mock）。返回 (guard, token, observe)。"""
    from framework.guardrail import GuardRail

    token = MagicMock()
    token.cancelled = False
    observe = MagicMock()
    observe.save_evidence.return_value = "debug/x/auto_kill.png"
    ctx = MagicMock()
    guard = GuardRail(
        token, observe, ctx,
        same_reason_n=same_reason_n,
        same_fail_n=same_fail_n,
        window_sec=window_sec,
    )
    return guard, token, observe


def _ev(*, ability="nav", event="nav.step", reason=None, scene="MAIN_UI", ok=None):
    e = {"event": event, "ability": ability, "scene": scene}
    if reason is not None:
        e["reason"] = reason
    if ok is not None:
        e["ok"] = ok
    return e


# ── 触发规则 ──


class TestSameReason:
    def test_triggers_cancel_at_threshold(self):
        """同 (ability,event,reason) 达到 same_reason_n 即触发 cancel + save_evidence + auto_kill 事件。"""
        guard, token, observe = _make_guard(same_reason_n=5)
        for _ in range(4):
            guard.on_event(_ev(reason="too_far"))
        assert not token.cancel.called  # 阈值前不触发
        observe.save_evidence.assert_not_called()
        guard.on_event(_ev(reason="too_far"))  # 第 5 次 → 触发
        assert token.cancel.called
        observe.save_evidence.assert_called_once()
        observe.event.assert_called_once()
        # observe.event("auto_kill", level=..., rule=..., ...) —— event 名是位置参数
        ev_args = observe.event.call_args.args
        ev_kwargs = observe.event.call_args.kwargs
        assert ev_args[0] == "auto_kill"
        assert ev_kwargs["rule"] == "same_reason"
        assert ev_kwargs["count"] == 5
        assert ev_kwargs["threshold"] == 5

    def test_different_reasons_count_separately(self):
        """不同 reason 各自计数（reason=None 和 'too_far' 不混算）。"""
        guard, token, _ = _make_guard(same_reason_n=5)
        for _ in range(4):
            guard.on_event(_ev(reason="too_far"))
        guard.on_event(_ev(reason="stuck") | {})  # 不同 reason
        # 5 次但分两组（4 too_far + 1 stuck），都不达阈值
        assert not token.cancel.called


class TestSameFail:
    def test_triggers_cancel_at_fail_threshold(self):
        """同 (ability,event,scene,ok=False) 达 same_fail_n 即触发（即使 reason 变化）。"""
        guard, token, observe = _make_guard(same_reason_n=100, same_fail_n=3)
        for r in ("a", "b", "c"):  # 不同 reason，但同 scene + ok=False
            guard.on_event(_ev(reason=r, ok=False, scene="map"))
        assert token.cancel.called
        ev_kwargs = observe.event.call_args.kwargs
        assert ev_kwargs["rule"] == "same_fail"
        assert ev_kwargs["count"] == 3
        assert observe.event.call_args.args[0] == "auto_kill"

    def test_success_does_not_count_as_fail(self):
        """ok=True 的事件不计入 same_fail（即使同 scene/ability）。"""
        guard, token, _ = _make_guard(same_reason_n=100, same_fail_n=3)
        guard.on_event(_ev(ok=True, scene="map"))
        guard.on_event(_ev(ok=True, scene="map"))
        guard.on_event(_ev(ok=True, scene="map"))
        assert not token.cancel.called

    def test_different_scene_count_separately(self):
        """不同 scene 的失败各自计数（map vs main_ui 不混算）。"""
        guard, token, _ = _make_guard(same_reason_n=100, same_fail_n=3)
        guard.on_event(_ev(ok=False, scene="map"))
        guard.on_event(_ev(ok=False, scene="map"))
        guard.on_event(_ev(ok=False, scene="main_ui"))  # 不同 scene
        assert not token.cancel.called


# ── 窗口 & 一次性 ──


class TestWindowAndFireOnce:
    def test_window_prunes_old_events(self, monkeypatch):
        """窗口外的旧事件被剔除，不参与计数。"""
        import framework.guardrail as gr

        t = [0.0]
        monkeypatch.setattr(gr.time, "monotonic", lambda: t[0])
        guard, token, _ = _make_guard(same_reason_n=3, window_sec=10.0)
        # t=0,1 各一次（窗口内累计 2，未达阈值 3）
        for dt in (0, 1):
            t[0] = float(dt)
            guard.on_event(_ev(reason="too_far"))
        assert not token.cancel.called
        # 时间前进到窗口外，旧 2 次剔除
        t[0] = 20.0
        guard.on_event(_ev(reason="too_far"))  # 窗口内只剩 1（当前）
        assert not token.cancel.called
        # 再 1 次达阈值（窗口内累计 2，未触发；再多 1 次到 3 才触发）
        t[0] = 21.0
        guard.on_event(_ev(reason="too_far"))
        assert not token.cancel.called  # 窗口内 2
        t[0] = 22.0
        guard.on_event(_ev(reason="too_far"))
        assert token.cancel.called  # 窗口内 3

    def test_fires_only_once(self):
        """触发后 _fired=True，后续事件直通 return（不重复 cancel）。"""
        guard, token, observe = _make_guard(same_reason_n=2)
        guard.on_event(_ev(reason="too_far"))
        guard.on_event(_ev(reason="too_far"))  # 触发
        assert token.cancel.call_count == 1
        observe.event.assert_called_once()
        # 后续 100 次不再触发
        for _ in range(100):
            guard.on_event(_ev(reason="too_far"))
        assert token.cancel.call_count == 1
        assert observe.event.call_count == 1  # auto_kill 事件只 emit 1 次

    def test_no_fire_when_token_already_cancelled(self):
        """token 已 cancelled 时不再触发（避免 teardown 期间重复 cancel）。"""
        guard, token, observe = _make_guard(same_reason_n=2)
        token.cancelled = True
        guard.on_event(_ev(reason="too_far"))
        guard.on_event(_ev(reason="too_far"))
        assert not token.cancel.called
        observe.save_evidence.assert_not_called()


# ── 不触发场景 ──


class TestNoFalsePositive:
    def test_below_threshold_no_fire(self):
        """次数低于阈值时不触发。"""
        guard, token, _ = _make_guard(same_reason_n=10, same_fail_n=10)
        for _ in range(9):
            guard.on_event(_ev(reason="too_far"))
        assert not token.cancel.called

    def test_missing_ability_falls_back_to_framework(self):
        """事件无 ability 字段时归类为 (framework)，仍正常计数。"""
        guard, token, _ = _make_guard(same_reason_n=2)
        guard.on_event({"event": "run_start"})  # 无 ability
        guard.on_event({"event": "run_start"})
        assert token.cancel.called  # 归类 (framework) 也算
