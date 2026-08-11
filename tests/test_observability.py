"""可观测性测试：Observe 节流门 / _NullObserve 签名一致 / report 分组 /
咽喉事件带 ability / scene-classifier 10Hz 抑制。

可 ``python -m pytest tests/test_observability.py -v``。
"""

from __future__ import annotations

import inspect
from unittest.mock import MagicMock


# ── Observe 构造辅助 ──


def _make_observe():
    """造一个真 Observe（logger/shared 全 mock），用于节流/report 测试。"""
    from framework.observe import Observe

    logger = MagicMock()
    logger.run_id = "test"
    shared = MagicMock()
    shared.scene.scene.value = "MAIN_UI"
    return Observe(logger, shared, debug_dir="debug")


# ── 节流门（最关键：防热轮询爆 JSONL）──


class TestThrottle:
    def test_time_window_folds_elided(self, monkeypatch):
        """同 throttle_key 成功事件 1s 窗口内折叠；窗口过期后下条 emit 带 elided + sampled。"""
        import framework.observe as obs

        t = [0.0]
        monkeypatch.setattr(obs.time, "monotonic", lambda: t[0])
        ob = _make_observe()
        ob.event("nav.step", ability="nav", pos=(1, 2), throttle_key="nav.step")  # t=0 发
        for _ in range(4):
            t[0] = 0.1  # 同窗口内 → 折叠（_elided 累加到 4）
            ob.event("nav.step", ability="nav", pos=(1, 2), throttle_key="nav.step")
        t[0] = 1.5  # 窗口过期 → 下条 emit 带 elided=4
        ob.event("nav.step", ability="nav", pos=(1, 2), throttle_key="nav.step")
        nav = [e for e in ob.timeline() if e["event"] == "nav.step"]
        assert len(nav) == 2
        assert nav[1]["elided"] == 4
        assert nav[1]["sampled"] is True

    def test_ok_false_bypasses_time_window(self):
        """time-window 模式下 ok=False 每次都发（失败不被 1s 窗口吃掉）。"""
        ob = _make_observe()
        for _ in range(5):
            ob.event("nav.step", ability="nav", ok=False, reason="stuck",
                     throttle_key="nav.step")
        nav = [e for e in ob.timeline() if e["event"] == "nav.step"]
        assert len(nav) == 5

    def test_quiet_first_only(self):
        """_quiet：同 key 整 run 只发首条成功（场景分类器「找到一次」足够）。"""
        ob = _make_observe()
        for _ in range(5):
            ob.event("detect.ui", ability="gs", name="paimon", ok=True,
                     _quiet=True)
        ev = [e for e in ob.timeline() if e["event"] == "detect.ui"]
        assert len(ev) == 1

    def test_quiet_suppresses_failures_too(self):
        """_quiet 优先于「ok=False 永不节流」：显式模式即便失败也折叠（防 10Hz 未找到爆）。"""
        ob = _make_observe()
        for _ in range(5):
            ob.event("detect.ui", ability="gs", name="paimon", ok=False,
                     _quiet=True)
        ev = [e for e in ob.timeline() if e["event"] == "detect.ui"]
        assert len(ev) == 1

    def test_transition_emits_on_change(self):
        """_transition：ok 跳变才发；同值重复折叠（survival.low_hp False↔True）。"""
        ob = _make_observe()
        ob.event("survival.low_hp", ability="fighter", ok=False, _transition=True)
        ob.event("survival.low_hp", ability="fighter", ok=False, _transition=True)
        ob.event("survival.low_hp", ability="fighter", ok=True, _transition=True)
        ob.event("survival.low_hp", ability="fighter", ok=True, _transition=True)
        ev = [e for e in ob.timeline() if e["event"] == "survival.low_hp"]
        assert len(ev) == 2  # False→(折叠)→True→(折叠)

    def test_no_throttle_key_always_emits(self):
        """无 throttle_key/_quiet/_transition → 全量落地（保留既有 runtime/g.* 行为）。"""
        ob = _make_observe()
        for _ in range(5):
            ob.event("tp.confirm", ability="tp", ok=True)
        ev = [e for e in ob.timeline() if e["event"] == "tp.confirm"]
        assert len(ev) == 5

    def test_scene_classifier_10hz_does_not_explode(self):
        """场景分类器 10Hz × 10s（100 次 has_* 全 _quiet）→ 只 1 条事件。"""
        ob = _make_observe()
        for _ in range(100):  # 模拟 10Hz × 10s
            ob.event("detect.ui", ability="game_state", name="paimon",
                     ok=True, _quiet=True)
        ev = [e for e in ob.timeline() if e["event"] == "detect.ui"]
        assert len(ev) == 1  # 远低于 1 事件/秒


# ── _NullObserve 签名一致 + no-op ──


class TestNullObserve:
    @staticmethod
    def _params(method):
        return list(inspect.signature(method).parameters)

    def test_signatures_match(self):
        """_NullObserve 与 Observe 方法签名钉死一致（调用方永不判空的前提）。"""
        from framework.observe import Observe, _NullObserve

        for name in ("event", "failure", "save_evidence", "subscribe", "timeline"):
            assert self._params(getattr(Observe, name)) == self._params(
                getattr(_NullObserve, name)
            ), f"签名漂移: {name}"

    def test_null_is_noop(self):
        """_NULL 调用全 no-op、不抛、timeline() 返空 list。"""
        from framework.observe import _NULL

        _NULL.event("anything", ability="t", ok=False)
        _NULL.failure("TaskError")
        _NULL.subscribe(lambda e: None)
        assert _NULL.save_evidence(MagicMock()) is None
        assert _NULL.timeline() == []


# ── report（读端：summarize/live_line）──


class TestReport:
    def test_summarize_groups_by_ability(self):
        """按 ability 分组；ok 计通过/失败，缺省 ok（纯观测）不计。"""
        from framework.report import summarize, summary_text

        timeline = [
            {"event": "tp.resolve", "ability": "tp", "ok": True},
            {"event": "tp.confirm", "ability": "tp", "ok": False, "reason": "pin_blocking"},
            {"event": "nav.step", "ability": "nav", "ok": True},
            {"event": "nav.step", "ability": "nav", "ok": False, "reason": "abort_stuck"},
            {"event": "detect.ocr", "ability": "vision_utils"},  # 纯观测
        ]
        s = summarize(timeline)
        assert s["total"] == 5
        assert s["fail_total"] == 2
        assert s["by_ability"]["tp"] == {"ok": 1, "fail": 1, "failures": [
            {"stage": "tp.confirm", "reason": "pin_blocking", "attempt": None, "evidence": None}
        ]}
        assert s["by_ability"]["nav"]["fail"] == 1
        assert s["by_ability"]["vision_utils"]["ok"] == 0  # 纯观测不计通过

        txt = summary_text(s)
        assert "[tp]" in txt and "[nav]" in txt
        assert "vision_utils" not in txt  # 无失败的 ability 不列

    def test_summarize_skips_run_summary(self):
        """run_summary 事件本身不计入（防自引用）。"""
        from framework.report import summarize

        s = summarize([{"event": "run_summary", "ability": "?", "summary": {}}])
        assert s["total"] == 0
        assert s["fail_total"] == 0

    def test_summarize_clean_text(self):
        """无失败时一行「全绿」。"""
        from framework.report import summarize, summary_text

        s = summarize([{"event": "nav.step", "ability": "nav", "ok": True}])
        txt = summary_text(s)
        assert "无失败" in txt

    def test_live_line_format(self):
        """live_line：[ability] kind 失败 reason 关键事实。"""
        from framework.report import live_line

        line = live_line({
            "ability": "tp", "event": "tp.confirm", "ok": False,
            "reason": "pin_blocking", "panel": "MARKER",
        })
        assert "[tp]" in line
        assert "失败" in line
        assert "pin_blocking" in line


# ── 咽喉事件必带 ability（Tier-1 集成）──


class _ObserveCtx:
    """最小 ctx 替身：observe 是真 Observe，tm/ocr/capture 可控。"""

    def __init__(self, observe):
        self.observe = observe
        self.tm = MagicMock()
        self.ocr = MagicMock()
        self._frame = MagicMock()

    def capture(self):
        return self._frame


class TestThroatsHaveAbility:
    """Tier-1 咽喉发出的 detect.* 事件必带 ability 字段（AI 按此分组点名）。"""

    def test_find_template_emits_ability(self, monkeypatch):
        from abilities import vision_utils as vu

        monkeypatch.setattr(vu, "_resolve_template_path", lambda p: "x.png")
        ctx = _ObserveCtx(_make_observe())
        ctx.tm.addTemplatePath.return_value = 0  # 模板加入成功
        ctx.tm.match.return_value = 0  # 无命中 → detect.template ok=False

        assert vu.find_template(ctx, "ui/x.png") is None
        ev = [e for e in ctx.observe.timeline() if e["event"] == "detect.template"]
        assert len(ev) == 1
        assert ev[0]["ability"] == "vision_utils"
        assert ev[0]["ok"] is False

    def test_find_template_hit_emits_ability(self, monkeypatch):
        from abilities import vision_utils as vu

        monkeypatch.setattr(vu, "_resolve_template_path", lambda p: "x.png")
        ctx = _ObserveCtx(_make_observe())
        ctx.tm.addTemplatePath.return_value = 0
        ctx.tm.match.return_value = 1
        m = MagicMock(x=10, y=20, w=30, h=40, score=0.92)
        ctx.tm.getMatch.return_value = m

        rect = vu.find_template(ctx, "ui/x.png")
        assert rect is not None
        ev = [e for e in ctx.observe.timeline() if e["event"] == "detect.template"]
        assert ev[0]["ability"] == "vision_utils"
        assert ev[0]["ok"] is True
        assert ev[0]["score"] == 0.92

    def test_find_text_emits_ability(self, monkeypatch):
        from abilities import vision_utils as vu

        ctx = _ObserveCtx(_make_observe())
        ctx.ocr.recognize.return_value = 0
        ctx.ocr.getMatchCount.return_value = 0  # 无文字 → detect.ocr ok=False

        assert vu.find_text(ctx, "传送") is None
        ev = [e for e in ctx.observe.timeline() if e["event"] == "detect.ocr"]
        assert len(ev) == 1
        assert ev[0]["ability"] == "vision_utils"


if __name__ == "__main__":
    import pytest as _pytest

    _pytest.main([__file__, "-v"])
