"""截图冻结守卫 / 传送验证 / 定位场景守卫 测试（2026-08-15 实机 r_20260815_064010）。

背景：传送加载切换后 SourcePlayer 冻结在旧地图帧 80s+（sc 是真实画面）→
wait_main_ui 空等 60s、小地图定位在旧帧上读出假位置 [3201,-967]、导航盲走。
三个修复的离线单测：
1. ``GameContext._player_frame_stale``：帧指纹不变 >6s 判冻结（含恢复事件）；
   ``capture()`` 冻结时回退 IScreenCapture。
2. ``Teleporter.teleport_to``：wait_main_ui 超时 → TaskError 归因（不再盲继续）。
3. ``PositionGetter.get_position``：scene=map 时小地图定位直接判失败。
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock

import pytest


# ── 1. 帧冻结守卫 ──


class _FakeCrop:
    def __init__(self, data: bytes):
        self._d = data

    def to_bytes(self) -> bytes:
        return self._d


class _FakeBuf:
    """伪 IImageBuffer：width/height + 帧内容标记（供 crop→to_bytes 指纹）。"""

    def __init__(self, content: int, width: int = 1920, height: int = 1080):
        self.content = content
        self.width = width
        self.height = height


class _FakeImage:
    """crop(buf, ...) → FakeCrop(to_bytes=bytes([buf.content]))。"""

    def crop(self, buf, x, y, w, h):
        return _FakeCrop(bytes([buf.content]))


def _mk_ctx(frame_content: int = 1):
    from framework.context import GameContext

    class _TestCtx(GameContext):
        """observe property 的测试替身（原 property 只读不可 setattr）。"""

        def __init__(self):
            self._mock_ob = MagicMock()
            self._Image = _FakeImage()
            self._shot_buf = _FakeBuf(frame_content)
            self._frame_sig = None
            self._frame_change_t = time.monotonic()
            self._stale_reported = False
            self._player = None

        @property
        def observe(self):
            return self._mock_ob

    ctx = _TestCtx()
    return ctx


class TestPlayerFrameStale:
    def test_fresh_frame_not_stale(self):
        ctx = _mk_ctx()
        assert ctx._player_frame_stale() is False  # 首帧：记录指纹
        assert ctx._player_frame_stale() is False  # 6s 内重复：不判冻结

    def test_frozen_frame_goes_stale(self):
        ctx = _mk_ctx()
        ctx._player_frame_stale()
        ctx._frame_change_t -= 10.0  # 模拟 10s 无变化
        assert ctx._player_frame_stale() is True
        # 冻结事件：进入冻结态只发一条
        evs = [c for c in ctx.observe.event.call_args_list
               if c.args[0] == "capture.stale"]
        assert len(evs) == 1 and evs[0].kwargs.get("reason") == "player_frame_frozen"

    def test_frame_change_recovers(self):
        ctx = _mk_ctx(1)
        ctx._player_frame_stale()
        ctx._frame_change_t -= 10.0
        assert ctx._player_frame_stale() is True
        ctx._stale_reported = True  # 上一轮已上报
        ctx._shot_buf = _FakeBuf(2)  # 帧恢复变化
        assert ctx._player_frame_stale() is False
        evs = [c for c in ctx.observe.event.call_args_list
               if c.args[0] == "capture.stale"]
        assert evs[-1].kwargs.get("ok") is True and evs[-1].kwargs.get("reason") == "recovered"


class TestCaptureFallback:
    def test_capture_uses_sc_when_stale(self):
        from framework.context import GameContext

        ctx = _mk_ctx()
        # SourcePlayer 链路 mock（screenShot 成功返回 _shot_buf）
        sr = MagicMock()
        sr.screenShot.return_value = True
        player = MagicMock()
        player.getSurfaceRender.return_value = sr
        ctx._player = player
        sentinel = object()
        ctx._capture_sc = lambda raw=False: sentinel
        ctx._player_frame_stale = lambda: True  # 判定冻结
        assert GameContext.capture(ctx) is sentinel

    def test_capture_uses_player_when_fresh(self):
        from framework.context import GameContext

        ctx = _mk_ctx()
        sr = MagicMock()
        sr.screenShot.return_value = True
        player = MagicMock()
        player.getSurfaceRender.return_value = sr
        ctx._player = player
        ctx._capture_sc = lambda raw=False: pytest.fail("不应走 sc 回退")
        ctx._player_frame_stale = lambda: False
        assert GameContext.capture(ctx) is ctx._shot_buf


# ── 2. teleport wait_main_ui 超时归因 ──


class TestTeleportVerify:
    def test_wait_main_ui_timeout_raises(self):
        from abilities.navigation.tp import Teleporter
        from framework.errors import TaskError

        ctx = MagicMock()
        g = MagicMock()
        g.wait_main_ui.return_value = False
        tp = Teleporter.__new__(Teleporter)
        tp.ctx = ctx
        tp.g = g
        tp._db = MagicMock()
        tp._pg = None
        tp._map_open_seed = None
        # 前置步骤全部 mock 成成功
        tp._resolve_target = lambda *a, **k: MagicMock(tran_x=1.0, tran_y=2.0)
        tp._open_map = lambda: None
        tp._navigate_map_to_target = lambda t: []
        tp._click_and_confirm_teleport = lambda c: None
        with pytest.raises(TaskError, match="未回到主界面"):
            tp.teleport_to((-653, 267))
        reasons = [c.kwargs.get("reason") for c in ctx.observe.event.call_args_list
                   if c.args and c.args[0] == "tp.confirm"]
        assert "wait_main_ui_timeout" in reasons

    def test_wait_main_ui_ok_no_raise(self):
        from abilities.navigation.tp import Teleporter

        ctx = MagicMock()
        g = MagicMock()
        g.wait_main_ui.return_value = True
        tp = Teleporter.__new__(Teleporter)
        tp.ctx = ctx
        tp.g = g
        tp._db = MagicMock()
        tp._pg = None
        tp._map_open_seed = None
        tp._resolve_target = lambda *a, **k: MagicMock(tran_x=1.0, tran_y=2.0)
        tp._open_map = lambda: None
        tp._navigate_map_to_target = lambda t: []
        tp._click_and_confirm_teleport = lambda c: None
        assert tp.teleport_to((-653, 267)) == (1.0, 2.0)


# ── 3. 定位场景守卫 ──


class TestPositionSceneGuard:
    def test_map_scene_rejects_minimap(self, monkeypatch):
        from framework.scene import Scene, SceneState

        monkeypatch.setattr(
            "framework.scene.classify_scene",
            lambda f: SceneState(scene=Scene.MAP),
        )
        from abilities.navigation.position import PositionGetter

        pg = PositionGetter(MagicMock())
        assert pg.get_position(frame=object()) is None
        reasons = [c.kwargs.get("reason") for c in
                   pg.ctx.observe.event.call_args_list]
        assert "scene_map_minimap_invalid" in reasons

    def test_main_ui_scene_not_blocked(self, monkeypatch):
        from framework.scene import Scene, SceneState

        monkeypatch.setattr(
            "framework.scene.classify_scene",
            lambda f: SceneState(scene=Scene.MAIN_UI),
        )
        from abilities.navigation.position import PositionGetter

        pg = PositionGetter(MagicMock())
        # 不被场景守卫拦截：继续走 _extract_minimap（mock 后返回 None 哨兵）
        pg._extract_minimap = lambda f: None
        assert pg.get_position(frame=object()) is None
        reasons = [c.kwargs.get("reason") for c in
                   pg.ctx.observe.event.call_args_list]
        assert "scene_map_minimap_invalid" not in reasons
