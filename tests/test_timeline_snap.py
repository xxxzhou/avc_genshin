"""TimelineSnap 守护的离线测试。

mock dctx + monkeypatch time.monotonic，验证存图触发条件 / 帧回退 / seq / _quiet。
不依赖实机（buf 是 MagicMock，验证 save 调用参数而非真存盘）。
可 ``python -m pytest tests/test_timeline_snap.py -v``。
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

from framework.scene import Scene


def _make_dctx(tmp_path, *, scene=Scene.MAIN_UI, frame="mock", capture_buf=None):
    """造 mock DaemonCtx。frame="mock"→truthy 共享帧；frame=None→无共享帧走 capture。"""
    dctx = MagicMock()
    if scene is None:
        dctx.shared.scene = None
    else:
        dctx.shared.scene = MagicMock(scene=scene)  # .scene = Scene enum（有 .value）
    dctx.shared.frame = MagicMock() if frame == "mock" else frame
    dctx.ctx.capture.return_value = capture_buf
    dctx.observe.debug_dir = tmp_path / "debug" / "r_test"
    dctx.observe.logger.ts.return_value = 12.5
    return dctx


def _run(inst, dctx):
    asyncio.run(inst.step(dctx))


class TestTimelineSnap:
    def test_scene_change_snaps(self, tmp_path, monkeypatch):
        import framework.daemons.timeline_snap as mod
        monkeypatch.setattr(mod.time, "monotonic", lambda: 100.0)
        from framework.daemons.timeline_snap import TimelineSnapDaemon

        buf = MagicMock()
        dctx = _make_dctx(tmp_path, scene=Scene.MAIN_UI, frame=buf)
        inst = TimelineSnapDaemon()
        _run(inst, dctx)

        # 存图：save 被调，路径含 timeline/0000_..._main_ui.png
        buf.save.assert_called_once()
        path = buf.save.call_args[0][0]
        assert "timeline" in path and "0000" in path and "main_ui" in path
        # mkdir 建了 timeline 子目录
        assert (tmp_path / "debug" / "r_test" / "timeline").is_dir()
        # event 带 _quiet + trigger=scene_change（首次 last_scene=None→main_ui）
        dctx.observe.event.assert_called_once()
        kw = dctx.observe.event.call_args.kwargs
        assert kw["_quiet"] is True
        assert kw["trigger"] == "scene_change"
        assert kw["seq"] == 0

    def test_periodic_snap_when_scene_same(self, tmp_path, monkeypatch):
        import framework.daemons.timeline_snap as mod
        t = [100.0]
        monkeypatch.setattr(mod.time, "monotonic", lambda: t[0])
        from framework.daemons.timeline_snap import TimelineSnapDaemon

        buf = MagicMock()
        dctx = _make_dctx(tmp_path, scene=Scene.MAIN_UI, frame=buf)
        inst = TimelineSnapDaemon()
        _run(inst, dctx)  # 首次：scene 变 → 存（_last_snap_ts=100）
        buf.save.reset_mock()
        dctx.observe.event.reset_mock()

        t[0] = 120.0  # 推进 20s < snap_interval(30) → 不存
        _run(inst, dctx)
        buf.save.assert_not_called()

        t[0] = 135.0  # 推进 ≥30s → 周期存
        _run(inst, dctx)
        buf.save.assert_called_once()
        assert dctx.observe.event.call_args.kwargs["trigger"] == "periodic"

    def test_frame_none_falls_back_to_capture(self, tmp_path, monkeypatch):
        import framework.daemons.timeline_snap as mod
        monkeypatch.setattr(mod.time, "monotonic", lambda: 100.0)
        from framework.daemons.timeline_snap import TimelineSnapDaemon

        cap_buf = MagicMock()
        dctx = _make_dctx(tmp_path, scene=Scene.MAIN_UI, frame=None, capture_buf=cap_buf)
        inst = TimelineSnapDaemon()
        _run(inst, dctx)

        dctx.ctx.capture.assert_called_once()  # 共享帧 None → fallback 自抓
        cap_buf.save.assert_called_once()  # 存的是 capture 返回的帧

    def test_both_frame_and_capture_none_skips(self, tmp_path, monkeypatch):
        import framework.daemons.timeline_snap as mod
        monkeypatch.setattr(mod.time, "monotonic", lambda: 100.0)
        from framework.daemons.timeline_snap import TimelineSnapDaemon

        dctx = _make_dctx(tmp_path, scene=Scene.MAIN_UI, frame=None, capture_buf=None)
        inst = TimelineSnapDaemon()
        _run(inst, dctx)

        dctx.observe.event.assert_not_called()  # 没帧可存 → 不发事件、不存

    def test_seq_increments_across_snaps(self, tmp_path, monkeypatch):
        import framework.daemons.timeline_snap as mod
        t = [100.0]
        monkeypatch.setattr(mod.time, "monotonic", lambda: t[0])
        from framework.daemons.timeline_snap import TimelineSnapDaemon

        buf = MagicMock()
        dctx = _make_dctx(tmp_path, scene=Scene.MAIN_UI, frame=buf)
        inst = TimelineSnapDaemon()
        _run(inst, dctx)  # seq 0
        t[0] = 140.0
        _run(inst, dctx)  # seq 1（周期）

        paths = [c[0][0] for c in buf.save.call_args_list]
        assert "0000" in paths[0]
        assert "0001" in paths[1]

    def test_no_scene_field_still_snaps(self, tmp_path, monkeypatch):
        """shared.scene=None（分类器还没出结果）→ scene=None，仍存图（文件名 unknown）。"""
        import framework.daemons.timeline_snap as mod
        monkeypatch.setattr(mod.time, "monotonic", lambda: 100.0)
        from framework.daemons.timeline_snap import TimelineSnapDaemon

        buf = MagicMock()
        dctx = _make_dctx(tmp_path, scene=None, frame=buf)
        inst = TimelineSnapDaemon()
        _run(inst, dctx)

        path = buf.save.call_args[0][0]
        assert "unknown" in path  # scene None → 文件名 unknown
