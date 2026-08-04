"""可靠性地基单测（cancellation / shared / scene / logging / observe / authority / policy）。

可 ``python -m pytest tests/test_reliability.py`` 或直接 ``python tests/test_reliability.py``。
重点覆盖 InputAuthority 的并发仲裁（冲突 / 抢占 / 共存 / 原子性）——这是并发安全核心。
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from framework.authority import InputAuthority, InputChannel as IC
from framework.cancellation import CancellationToken, RunContext
from framework.errors import CancelledError, InputConflict, PolicyViolation
from framework.logging import JsonlLogger, new_run_id
from framework.observe import Observe
from framework.policy import Policy
from framework.scene import Scene, SceneState, classify_scene
from framework.shared import SharedState


# ── cancellation ──


def test_token_check_and_callbacks():
    t = CancellationToken()
    assert not t.cancelled
    t.check()  # 未取消不抛
    fired = []
    t.on_cancel(lambda: fired.append(1))
    t.cancel("用户中断")
    assert t.cancelled and t.reason == "用户中断"
    assert fired == [1]
    try:
        t.check()
        assert False, "应抛 CancelledError"
    except CancelledError:
        pass
    # 已取消后注册的回调立即触发
    t.on_cancel(lambda: fired.append(2))
    assert fired == [1, 2]


def test_runcontext_child_shares_token():
    t = CancellationToken()
    parent = RunContext(token=t, run_id="r_x", task="parent")
    child = parent.child("sub")
    assert child.token is t and child.run_id == "r_x"
    assert child.task == "sub" and child.depth == 1 and child.mounted == []


# ── shared ──


def test_shared_flags_and_snapshot():
    s = SharedState()
    assert not s.has_flag("k")
    s.set_flag("daily_claimed", True)
    assert s.has_flag("daily_claimed") and s.get_flag("daily_claimed") is True
    snap = s.snapshot()
    assert snap["scene"] is None and snap["det_classes"] == []


# ── scene ──


def test_scene_default_classifier_unknown():
    st = classify_scene(None)
    assert st.scene is Scene.UNKNOWN
    assert isinstance(Scene.MAIN_UI.value, str)


def test_scenestate_frozen():
    s = SceneState(scene=Scene.MAIN_UI, confidence=0.9)
    try:
        s.scene = Scene.DIALOG  # type: ignore[misc]
        assert False, "应不可变"
    except AttributeError:
        pass


# ── logging ──


def test_jsonl_logger_writes_lines():
    with tempfile.TemporaryDirectory() as d:
        rid = new_run_id()
        log = JsonlLogger(rid, logs_dir=d)
        log.log({"event": "task_start", "task": "t", "params": {"n": 4}})
        log.log({"event": "action", "action": "press", "key": "f", "result": "ok"})
        log.close()
        lines = Path(log.path).read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 2
        e = json.loads(lines[0])
        assert e["run_id"] == rid and e["ts"] >= 0 and e["event"] == "task_start"
        assert json.loads(lines[1])["key"] == "f"


# ── observe ──


def test_observe_event_and_failure():
    with tempfile.TemporaryDirectory() as d:
        log = JsonlLogger("r_o", logs_dir=d)
        shared = SharedState()
        obs = Observe(log, shared, debug_dir=d)
        obs.event("action", action="press", key="f")
        shared.scene = SceneState(scene=Scene.MAIN_UI)
        obs.event("detect", cls="interact", items=[{"x": 1, "y": 2}])
        assert len(obs.timeline()) == 2
        obs.failure("TemplateNotFound", path="t.png")
        last = obs.timeline()[-1]
        assert last["event"] == "failure" and last["failure_type"] == "TemplateNotFound"
        assert last["scene"] == "main_ui"  # 来自 shared
        assert "timeline_tail" in last
        log.close()


class _FakeBuf:
    def save(self, path):
        Path(path).write_bytes(b"PNG")


class _FakeCtx:
    def capture(self):
        return _FakeBuf()


def test_observe_save_evidence():
    with tempfile.TemporaryDirectory() as d:
        log = JsonlLogger("r_e", logs_dir=d)
        obs = Observe(log, SharedState(), debug_dir=d)
        p = obs.save_evidence(_FakeCtx(), "notfound")
        assert p and Path(p).exists()
        assert "r_e" in p  # 落在 debug/<run_id>/
        log.close()


# ── authority（并发核心）──


def test_authority_acquire_release():
    a = InputAuthority()
    lease = a.acquire({IC.MOVE, IC.MOUSE_MOVE}, "go_to", priority=0)
    assert lease.active
    assert a.holder_of(IC.MOVE) == "go_to"
    lease.release()
    assert not lease.active
    assert a.holder_of(IC.MOVE) is None


def test_authority_non_overlapping_coexist():
    """边走边拾取：MOVE 与 INTERACT 不重叠 → 共存。"""
    a = InputAuthority()
    move = a.acquire({IC.MOVE, IC.MOUSE_MOVE}, "go_to", priority=0)
    pick = a.acquire({IC.INTERACT}, "auto_pick", priority=0)
    assert move.active and pick.active  # 共存合法


def test_authority_same_channel_same_priority_conflict():
    a = InputAuthority()
    a.acquire({IC.INTERACT}, "auto_pick", priority=0)
    try:
        a.acquire({IC.INTERACT}, "other", priority=0)
        assert False, "应抛 InputConflict"
    except InputConflict as e:
        assert "interact" in e.channel
        assert "auto_pick" in e.holders and "other" in e.holders


def test_authority_same_holder_refresh():
    a = InputAuthority()
    l1 = a.acquire({IC.INTERACT}, "auto_pick", priority=0)
    l2 = a.acquire({IC.INTERACT}, "auto_pick", priority=0)  # 同 holder 刷新
    assert l2.active
    l1.release()  # 旧租约释放（不影响新租约）
    assert l2.active


def test_authority_high_priority_preempts_low():
    a = InputAuthority()
    low = a.acquire({IC.INTERACT}, "auto_pick", priority=0)
    assert low.active
    high = a.acquire({IC.INTERACT}, "dialog_handler", priority=5)  # 抢占
    assert high.active
    assert not low.active  # 被抢占 → 自动失效，守护应挂起
    high.release()
    # 高优先释放后，低优先可重新 acquire（恢复）
    low2 = a.acquire({IC.INTERACT}, "auto_pick", priority=0)
    assert low2.active


def test_authority_conflict_is_atomic():
    """任一通道冲突 → 整个 acquire 失败，不部分授予。"""
    a = InputAuthority()
    a.acquire({IC.INTERACT}, "auto_pick", priority=0)
    try:
        a.acquire({IC.INTERACT, IC.MOUSE_MOVE}, "other", priority=0)
        assert False
    except InputConflict:
        pass
    assert a.holder_of(IC.MOUSE_MOVE) is None  # 未被部分授予


def test_authority_empty_channels_always_active():
    """owns_keys 空（如 loading_wait）→ 永远 active。"""
    a = InputAuthority()
    lease = a.acquire(set(), "loading_wait")
    assert lease.active


# ── policy ──


def test_policy_spend_and_time():
    p = Policy(never_spend=["primogem"], time_budget_s=10)
    try:
        p.check_spend("primogem")
        assert False
    except PolicyViolation:
        pass
    p.check_spend("mora")  # 未禁
    try:
        p.check_time(11)
        assert False
    except PolicyViolation:
        pass


def test_policy_needs_confirm_threshold():
    assert Policy(confirm_threshold="high").needs_confirm("high")
    assert not Policy(confirm_threshold="high").needs_confirm("medium")
    assert Policy(confirm_threshold="low").needs_confirm("medium")


# ── runner ──

_TESTS = [v for k, v in sorted(globals().items()) if k.startswith("test_")]


def main():
    for fn in _TESTS:
        fn()
        print(f"  ✓ {fn.__name__}")
    print(f"ALL {len(_TESTS)} RELIABILITY TESTS PASSED")


if __name__ == "__main__":
    main()
