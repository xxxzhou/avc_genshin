"""Runtime 集成测试（无需游戏/avc，用 MockCtx）。

验证执行引擎机制：任务串行执行、守护框架驱动循环（挂载/卸载/场景门控/取消）、
同步桥、超时→卸载守护+释放按键、结构化日志落盘。
``python -m pytest tests/test_runtime.py`` 或 ``python tests/test_runtime.py``。
"""

from __future__ import annotations

import asyncio
import tempfile
import time
from pathlib import Path

from framework.config import Config
from framework.daemons.base import Daemon, DaemonCtx, daemon
from framework.runtime import Runtime
from framework.scene import Scene, SceneState


# ── 测试守护（计数器：每步把 ticks+1 写进 SharedState）──


@daemon(name="test_tick", interval=0.04)
class _TickDaemon(Daemon):
    async def step(self, dctx: DaemonCtx) -> None:
        dctx.shared.set_flag("ticks", dctx.shared.get_flag("ticks", 0) + 1)


@daemon(name="test_gated", scenes={Scene.MAIN_UI}, interval=0.04)
class _GatedDaemon(Daemon):
    async def step(self, dctx: DaemonCtx) -> None:
        dctx.shared.set_flag("gated_ticks", dctx.shared.get_flag("gated_ticks", 0) + 1)


# ── MockCtx（满足 Runtime/g.*/守护 需要，不碰 avc）──


class _MockInput:
    def moveTo(self, *a, **k): pass
    def scroll(self, *a, **k): pass


class MockCtx:
    def __init__(self, cfg):
        self.cfg = cfg
        self.runtime = None
        self.ic = _MockInput()
        self.keys_released = 0
        self.presses = []

    def capture(self):
        return None  # FrameDaemon 无帧 → no-op

    def press(self, key, hold=0.0):
        self.presses.append(key)

    def click_at(self, x, y, button="left"): pass
    def hotkey(self, *keys): pass
    def type_text(self, text): pass
    def release_all_keys(self):
        self.keys_released += 1


def _cfg(tmp: str) -> Config:
    c = Config()
    c.logs_dir = Path(tmp) / "logs"
    c.debug_dir = Path(tmp) / "debug"
    return c


# ── 测试 ──


def test_task_runs_daemon_mounts_and_logs():
    with tempfile.TemporaryDirectory() as tmp:
        cfg = _cfg(tmp)
        rt = Runtime(ctx=MockCtx(cfg), cfg=cfg)
        try:
            def fn(ctx, g):
                g.mount("test_tick")
                time.sleep(0.3)
                ticks = g.get_flag("ticks", 0)
                g.unmount("test_tick")
                g.set_flag("done", True)
                return {"ticks": ticks}

            res = rt.run_callable(fn, task_name="t1", timeout=5)
            assert res["ticks"] >= 2, f"守护应跑多次，got {res['ticks']}"
            assert rt.shared.has_flag("done")
            # 日志落盘
            logs = list(Path(cfg.logs_dir).glob("*.jsonl"))
            assert len(logs) == 1
            events = [l.strip() for l in logs[0].read_text(encoding="utf-8").splitlines() if l.strip()]
            kinds = [_ev(e, "event") for e in events]
            assert "run_start" in kinds and "task_return" in kinds and "run_end" in kinds
            assert "mount" in kinds and "unmount" in kinds
        finally:
            rt.shutdown()


def test_scene_gating_suppresses_daemon_outside_scene():
    """守护声明 scenes={MAIN_UI}，但 SharedState.scene=None → 被 gating，step 不执行。"""
    with tempfile.TemporaryDirectory() as tmp:
        cfg = _cfg(tmp)
        rt = Runtime(ctx=MockCtx(cfg), cfg=cfg)
        try:
            def fn(ctx, g):
                g.mount("test_gated")
                time.sleep(0.3)
                g.unmount("test_gated")
                return g.get_flag("gated_ticks", 0)

            ticks = rt.run_callable(fn, task_name="gated", timeout=5)
            assert ticks == 0, f"场景不符应被 gating，got {ticks}"
        finally:
            rt.shutdown()


def test_scene_gating_runs_when_scene_matches():
    with tempfile.TemporaryDirectory() as tmp:
        cfg = _cfg(tmp)
        rt = Runtime(ctx=MockCtx(cfg), cfg=cfg)
        rt.shared.scene = SceneState(scene=Scene.MAIN_UI)  # 预置场景
        try:
            def fn(ctx, g):
                g.mount("test_gated")
                time.sleep(0.3)
                g.unmount("test_gated")
                return g.get_flag("gated_ticks", 0)

            ticks = rt.run_callable(fn, task_name="gated2", timeout=5)
            assert ticks >= 2, f"场景匹配应运行，got {ticks}"
        finally:
            rt.shutdown()


def test_timeout_tears_down_and_releases_keys():
    with tempfile.TemporaryDirectory() as tmp:
        cfg = _cfg(tmp)
        ctx = MockCtx(cfg)
        rt = Runtime(ctx=ctx, cfg=cfg)
        try:
            def fn(ctx, g):
                # 短超时下故意耗时（有界，避免孤儿线程长留）
                for _ in range(40):
                    time.sleep(0.02)

            raised = False
            try:
                rt.run_callable(fn, task_name="slow", timeout=0.2)
            except Exception:
                raised = True
            assert raised, "超时应抛异常"
            assert ctx.keys_released >= 1, "teardown 应释放按键"
        finally:
            rt.shutdown()


def test_unknown_daemon_raises():
    with tempfile.TemporaryDirectory() as tmp:
        cfg = _cfg(tmp)
        rt = Runtime(ctx=MockCtx(cfg), cfg=cfg)
        try:
            def fn(ctx, g):
                g.mount("does_not_exist")

            raised = False
            try:
                rt.run_callable(fn, task_name="bad", timeout=3)
            except Exception:
                raised = True
            assert raised, "未知守护应报错"
        finally:
            rt.shutdown()


# ── 辅助 / runner ──


def _ev(line: str, key: str):
    import json
    try:
        return json.loads(line).get(key)
    except Exception:
        return None


_TESTS = [v for k, v in sorted(globals().items()) if k.startswith("test_")]


def main():
    for fn in _TESTS:
        fn()
        print(f"  ✓ {fn.__name__}")
    print(f"ALL {len(_TESTS)} RUNTIME TESTS PASSED")


if __name__ == "__main__":
    main()
