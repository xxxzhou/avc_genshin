"""任务体系测试（@task / Registry / 沙箱 / run_task / ctx.run，docs/design/04）。

无需游戏/avc（MockCtx）。``python -m pytest tests/test_tasks.py`` 或 ``python tests/test_tasks.py``。
"""

from __future__ import annotations

import tempfile
import time
from pathlib import Path

from framework import task
from framework.config import Config
from framework.errors import NormalEnd, Retry, TaskError
from framework.registry import TaskNotFound, TaskRegistry
from framework.runtime import Runtime
from framework.sandbox import CodeValidator, exec_sandboxed
from framework.task import TaskDescriptor


# ── MockCtx（精简，满足 g.*/Runtime）──


class _MockInput:
    def moveTo(self, *a, **k): pass
    def scroll(self, *a, **k): pass


class MockCtx:
    def __init__(self, cfg):
        self.cfg = cfg
        self.runtime = None
        self.ic = _MockInput()
        self.keys_released = 0

    def capture(self): return None
    def press(self, key, hold=0.0): pass
    def click_at(self, x, y, button="left"): pass
    def hotkey(self, *k): pass
    def type_text(self, t): pass
    def release_all_keys(self): self.keys_released += 1

    # 镜像 GameContext 的运行时控制（委托 runtime）
    def run(self, name, **params):
        return self.runtime._run_inline(name, **params)

    def mount(self, name, **opts):
        self.runtime.mount(name, **opts)

    def unmount(self, name):
        self.runtime.unmount(name)


def _rt(tmp=None):
    cfg = Config()
    if tmp:
        cfg.logs_dir = Path(tmp) / "logs"
        cfg.debug_dir = Path(tmp) / "debug"
    return Runtime(ctx=MockCtx(cfg), cfg=cfg)


# ── 沙箱 CodeValidator ──


def test_validator_rejects_dangerous():
    for bad in [
        "import os",
        "from os import path",
        "open('x')",
        "eval('1')",
        "exec('1')",
        "__import__('os')",
        "def m(ctx,g):\n  return ctx.__class__",  # dunder 属性
        "def m(ctx,g):\n  return g._secret",      # 下划线属性
    ]:
        ok, err = CodeValidator.validate(bad)
        assert not ok, f"应拒绝：{bad!r}（{err}）"


def test_validator_accepts_clean():
    ok, err = CodeValidator.validate("def main(ctx, g):\n    g.set_flag('a', 1)\n    return 1")
    assert ok, err


def test_exec_sandboxed_defines_main():
    code = "def main(ctx, g):\n    g.set_flag('eph', 1)\n    return 42"
    env = exec_sandboxed(code)
    assert callable(env["main"])


# ── Registry discover/get/list ──


def test_registry_discovers_examples():
    r = TaskRegistry()
    r.discover()
    names = r.names()
    assert "ping" in names and "compose_ping" in names
    d = r.get("ping")
    assert d.name == "ping" and "test" in d.tags
    assert d.params["echo"]["default"] == "ok"
    views = r.list(tags={"test"})
    assert any(v["name"] == "ping" for v in views)


def test_registry_task_not_found():
    r = TaskRegistry()
    try:
        r.get("nope")
        assert False
    except TaskNotFound:
        pass


def test_registry_reload(tmp_path=None):
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "dyn.py"
        p.write_text("from framework import task\n@task(name='dyn', desc='v1')\ndef main(ctx,g):\n    return 1\n", encoding="utf-8")
        r = TaskRegistry()
        r.discover(roots=(d,))
        assert r.get("dyn").desc == "v1"
        p.write_text("from framework import task\n@task(name='dyn', desc='v2')\ndef main(ctx,g):\n    return 2\n", encoding="utf-8")
        r.reload("dyn")
        assert r.get("dyn").desc == "v2"


# ── run_task / params / ctx.run ──


def test_run_task_basic_and_defaults():
    with tempfile.TemporaryDirectory() as d:
        rt = _rt(d)
        try:
            res = rt.run_task("ping", timeout=5)
            assert res["pong"] is True and res["echo"] == "ok"  # 默认注入
            assert rt.shared.get_flag("ping") == "ok"
        finally:
            rt.shutdown()


def test_run_task_param_override_and_coercion():
    with tempfile.TemporaryDirectory() as d:
        rt = _rt(d)
        try:
            res = rt.run_task("ping", echo="custom", timeout=5)
            assert res["echo"] == "custom"
            # 注册一个带类型校验的任务
            @task(name="typed", desc="d", params={"n": {"type": "int", "default": 1}})
            def m(ctx, g, n=1):
                return {"n": n}
            rt.registry.register(TaskDescriptor(name="typed", desc="d", main=m, params={"n": {"type": "int", "default": 1}}))
            assert rt.run_task("typed", n="5", timeout=5)["n"] == 5  # str→int 强制
        finally:
            rt.shutdown()


def test_ctx_run_composition():
    with tempfile.TemporaryDirectory() as d:
        rt = _rt(d)
        try:
            res = rt.run_task("compose_ping", timeout=5)
            assert res["nested"]["pong"] is True
            assert rt.shared.get_flag("composed") is True
            assert rt.shared.get_flag("ping") == "from_parent"  # 子任务收到父参数
        finally:
            rt.shutdown()


def test_normal_end_returns_none():
    with tempfile.TemporaryDirectory() as d:
        rt = _rt(d)
        @task(name="ne", desc="d")
        def m(ctx, g):
            raise NormalEnd("今日已领")
        rt.registry.register(TaskDescriptor(name="ne", desc="d", main=m))
        try:
            assert rt.run_task("ne", timeout=5) is None  # NormalEnd 非异常
        finally:
            rt.shutdown()


def test_retry_then_succeed():
    with tempfile.TemporaryDirectory() as d:
        rt = _rt(d)
        calls = [0]
        @task(name="rt", desc="d")
        def m(ctx, g):
            calls[0] += 1
            if calls[0] < 3:
                raise Retry("再来", attempts=3)
            return {"ok": calls[0]}
        rt.registry.register(TaskDescriptor(name="rt", desc="d", main=m))
        try:
            res = rt.run_task("rt", timeout=5)
            assert res["ok"] == 3
        finally:
            rt.shutdown()


def test_ephemeral_task_runs():
    """即时任务：沙箱 exec → 注册 → run_task。"""
    with tempfile.TemporaryDirectory() as d:
        rt = _rt(d)
        code = "def main(ctx, g):\n    g.set_flag('eph_ok', True)\n    return {'eph': True}"
        env = exec_sandboxed(code)
        rt.registry.register(TaskDescriptor(name="eph", desc="即时", main=env["main"], kind="ephemeral"))
        try:
            res = rt.run_task("eph", timeout=5)
            assert res["eph"] is True and rt.shared.get_flag("eph_ok") is True
        finally:
            rt.shutdown()


def test_missing_required_param_raises():
    with tempfile.TemporaryDirectory() as d:
        rt = _rt(d)
        @task(name="req", desc="d", params={"x": {"type": "int", "required": True}})
        def m(ctx, g, x):
            return {"x": x}
        rt.registry.register(TaskDescriptor(name="req", desc="d", main=m, params={"x": {"type": "int", "required": True}}))
        try:
            raised = False
            try:
                rt.run_task("req", timeout=5)  # 缺 x
            except (TaskError, Exception):
                raised = True
            assert raised
        finally:
            rt.shutdown()


# ── runner ──

def test_notify_pub_sub():
    """notify 发布订阅：注册 handler 收到 (event, fields)。"""
    from framework import notify as N

    received = []
    N.register(lambda event, fields: received.append((event, fields.get("task"))))
    N.notify("task_start", task="auto_boss")
    N.notify("task_end", task="auto_boss", normal_end=True)
    assert ("task_start", "auto_boss") in received
    assert ("task_end", "auto_boss") in received


def test_notify_error_fields():
    """task_error 带 error 字段。"""
    from framework import notify as N

    received = []
    N.register(lambda event, fields: received.append(fields.get("error")))
    N.notify("task_error", task="x", error="Timeout")
    assert "Timeout" in received


_TESTS = [v for k, v in sorted(globals().items()) if k.startswith("test_")]


def main():
    for fn in _TESTS:
        fn()
        print(f"  ✓ {fn.__name__}")
    print(f"ALL {len(_TESTS)} TASK TESTS PASSED")


if __name__ == "__main__":
    main()
