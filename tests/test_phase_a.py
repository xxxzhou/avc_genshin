"""Phase A 测试：game_state / dialog / 新守护。

可 ``python -m pytest tests/test_phase_a.py -v`` 或直接 ``python tests/test_phase_a.py``。
所有测试不依赖 avc/游戏环境，使用 mock 对象。
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import Any
from unittest.mock import MagicMock, PropertyMock, patch

import pytest

from framework.authority import InputAuthority, InputChannel as IC
from framework.cancellation import CancellationToken
from framework.config import Config
from framework.daemons.base import DaemonCtx, get_daemon_class, list_daemons
from framework.observe import Observe
from framework.scene import Scene, SceneState, classify_scene, set_classifier
from framework.shared import SharedState


# ── Mock 辅助 ──


class MockImageBuffer:
    """模拟 IImageBuffer，用 numpy 风格的 BGRA8 字节数组。"""

    def __init__(self, width: int = 1920, height: int = 1080, fill: tuple = (40, 40, 40, 255)):
        # 默认灰色填充（非黑色，避免被误判为加载界面）
        self._width = width
        self._height = height
        self._fill = fill
        self._data = bytearray(fill) * (width * height)

    @property
    def width(self):
        return self._width

    @property
    def height(self):
        return self._height

    def to_bytes(self):
        return bytes(self._data)

    def set_pixel(self, x: int, y: int, b: int, g: int, r: int, a: int = 255):
        offset = (y * self._width + x) * 4
        struct.pack_into("BBBB", self._data, offset, b, g, r, a)


class MockTemplateMatcher:
    """模拟 ITemplateMatcher。"""

    def __init__(self, matches: dict[str, bool] | None = None):
        self._matches = matches or {}
        self._templates: dict[int, str] = {}
        self._next_idx = 0
        self._roi = None

    def clearTemplates(self):
        self._templates.clear()
        self._next_idx = 0

    def addTemplatePath(self, path: str, threshold: float) -> int:
        # 从路径中提取文件名来检查匹配
        name = path.replace("\\", "/").rsplit("/", 1)[-1].lower()
        idx = self._next_idx
        self._templates[idx] = name
        self._next_idx += 1
        return idx

    def match(self, buf) -> int:
        # 检查是否有任何模板匹配
        for idx, name in self._templates.items():
            if self._matches.get(name, False):
                return 1
        return 0

    def getMatch(self, i):
        return None

    def setRoi(self, *args):
        self._roi = args

    def clearRoi(self):
        self._roi = None


class MockContext:
    """模拟 GameContext（无 avc 依赖）。"""

    def __init__(self, tm_matches: dict[str, bool] | None = None):
        self.tm = MockTemplateMatcher(tm_matches) if tm_matches else None
        self.ocr = None
        self.cfg = Config.load()
        self._press_log: list = []

    def capture(self):
        return MockImageBuffer()

    def press(self, key, hold=0.0):
        self._press_log.append((key, hold))

    def click_at(self, x, y, button="left"):
        pass


def _make_dctx(ctx=None, scene=None, frame=None, detections=None):
    """创建测试用 DaemonCtx。"""
    ctx = ctx or MockContext()
    shared = SharedState()
    if scene:
        shared.scene = scene
    if frame:
        shared.frame = frame
    if detections:
        shared.detections = detections
    token = CancellationToken()
    observe = Observe(MagicMock(), shared)
    return DaemonCtx(
        ctx=ctx, shared=shared, authority=InputAuthority(),
        observe=observe, token=token, cfg=Config.load(),
    )


# ── game_state 测试 ──


class TestPixelChecks:
    """像素级检测（不依赖 avc）。"""

    def test_low_hp_red_pixel(self):
        """红血像素 (808, 1010) 为红色时检测到。"""
        frame = MockImageBuffer(1920, 1080)
        frame.set_pixel(808, 1010, b=90, g=90, r=255, a=255)
        ctx = MockContext()
        from abilities.game_state import is_low_hp

        assert is_low_hp(ctx, frame) is True

    def test_low_hp_normal_pixel(self):
        """正常血量像素不触发。"""
        frame = MockImageBuffer(1920, 1080)
        frame.set_pixel(808, 1010, b=0, g=200, r=0, a=255)  # 绿色
        ctx = MockContext()
        from abilities.game_state import is_low_hp

        assert is_low_hp(ctx, frame) is False

    def test_low_hp_small_frame(self):
        """帧太小时不崩溃。"""
        frame = MockImageBuffer(100, 100)
        ctx = MockContext()
        from abilities.game_state import is_low_hp

        assert is_low_hp(ctx, frame) is False


class TestOrangeOption:
    """橙色选项检测。"""

    def test_orange_text_detected(self):
        """BGR (48,195,243) 区域为橙色。"""
        frame = MockImageBuffer(100, 50)
        # 填充橙色区域
        for y in range(10, 30):
            for x in range(10, 80):
                frame.set_pixel(x, y, b=50, g=200, r=250, a=255)
        from abilities.game_state import is_orange_option

        assert is_orange_option(frame, 10, 10, 70, 20) is True

    def test_non_orange_text_not_detected(self):
        """白色区域不是橙色。"""
        frame = MockImageBuffer(100, 50)
        for y in range(10, 30):
            for x in range(10, 80):
                frame.set_pixel(x, y, b=255, g=255, r=255, a=255)
        from abilities.game_state import is_orange_option

        assert is_orange_option(frame, 10, 10, 70, 20) is False


class TestSceneClassifier:
    """场景分类器。"""

    def test_default_classifier_returns_unknown(self):
        """默认分类器返回 UNKNOWN。"""
        # 重置为默认
        from framework.scene import _default_classifier

        set_classifier(_default_classifier)
        frame = MockImageBuffer()
        result = classify_scene(frame)
        assert result.scene is Scene.UNKNOWN
        assert result.confidence == 0.0

    def test_make_classifier_returns_callable(self):
        """make_classifier 返回可调用分类器。"""
        from abilities.game_state import make_classifier

        ctx = MockContext()
        classifier = make_classifier(ctx)
        assert callable(classifier)

    def test_make_classifier_no_templates_returns_unknown(self):
        """无模板匹配时返回 UNKNOWN。"""
        from abilities.game_state import make_classifier

        ctx = MockContext()
        classifier = make_classifier(ctx)
        frame = MockImageBuffer()
        result = classifier(frame)
        assert result.scene is Scene.UNKNOWN


# ── 守护注册测试 ──


class TestDaemonRegistration:
    """Phase A 新守护已注册。"""

    def test_auto_eat_registered(self):
        assert get_daemon_class("auto_eat") is not None

    def test_quick_teleport_registered(self):
        assert get_daemon_class("quick_teleport") is not None

    def test_auto_open_chest_registered(self):
        assert get_daemon_class("auto_open_chest") is not None

    def test_auto_talk_registered(self):
        assert get_daemon_class("auto_talk") is not None

    def test_all_daemons_count(self):
        # 旧 5 + 新 4 = 9（auto_eat/quick_teleport/auto_open_chest/auto_talk）
        assert len(list_daemons()) >= 9

    def test_auto_eat_scenes(self):
        cls = get_daemon_class("auto_eat")
        assert Scene.MAIN_UI in cls.scenes
        assert Scene.COMBAT in cls.scenes
        assert Scene.DOMAIN in cls.scenes

    def test_quick_teleport_scenes(self):
        cls = get_daemon_class("quick_teleport")
        assert Scene.MAP in cls.scenes

    def test_auto_open_chest_scenes(self):
        cls = get_daemon_class("auto_open_chest")
        assert Scene.MAIN_UI in cls.scenes
        assert Scene.DOMAIN in cls.scenes


# ── 守护 step 逻辑测试 ──


class TestAutoEatDaemon:
    """auto_eat 守护逻辑。"""

    def test_step_no_frame(self):
        """无帧时不操作。"""
        cls = get_daemon_class("auto_eat")
        inst = cls()
        dctx = _make_dctx()
        # 不应抛异常
        import asyncio

        asyncio.get_event_loop().run_until_complete(inst.step(dctx))

    def test_step_normal_frame(self):
        """正常帧（无红血）不操作。"""
        cls = get_daemon_class("auto_eat")
        inst = cls()
        frame = MockImageBuffer()
        ctx = MockContext()
        dctx = _make_dctx(ctx=ctx, frame=frame)
        import asyncio

        asyncio.get_event_loop().run_until_complete(inst.step(dctx))
        # 不应按任何键
        assert len(ctx._press_log) == 0


class TestAutoOpenChestDaemon:
    """auto_open_chest 守护逻辑。"""

    def test_step_no_frame(self):
        """无帧时不操作。"""
        cls = get_daemon_class("auto_open_chest")
        inst = cls()
        dctx = _make_dctx()
        import asyncio

        asyncio.get_event_loop().run_until_complete(inst.step(dctx))


class TestAutoTalkDaemon:
    """auto_talk NPC 交互守护逻辑。"""

    def test_step_no_frame(self):
        """无帧时不操作。"""
        cls = get_daemon_class("auto_talk")
        inst = cls()
        dctx = _make_dctx()
        import asyncio

        asyncio.get_event_loop().run_until_complete(inst.step(dctx))

    def test_step_normal_frame_no_press(self):
        """有帧但无 F 图标 → 不按键。"""
        cls = get_daemon_class("auto_talk")
        inst = cls()
        frame = MockImageBuffer()
        ctx = MockContext()
        dctx = _make_dctx(ctx=ctx, frame=frame)
        import asyncio

        with patch("abilities.game_state.has_pick_f", return_value=False):
            asyncio.get_event_loop().run_until_complete(inst.step(dctx))
        assert len(ctx._press_log) == 0

    def test_step_f_icon_presses_f(self):
        """检测到 F 图标 → 按 F。"""
        cls = get_daemon_class("auto_talk")
        inst = cls()
        frame = MockImageBuffer()
        ctx = MockContext()
        dctx = _make_dctx(ctx=ctx, frame=frame)
        import asyncio

        with patch("abilities.game_state.has_pick_f", return_value=True):
            asyncio.get_event_loop().run_until_complete(inst.step(dctx))
        assert "f" in str(ctx._press_log).lower()

    def test_priority_higher_than_auto_pick(self):
        """auto_talk priority=5 > auto_pick priority=0。"""
        auto_talk_cls = get_daemon_class("auto_talk")
        auto_pick_cls = get_daemon_class("auto_pick")
        assert auto_talk_cls.priority > auto_pick_cls.priority

    def test_scenes_main_ui_only(self):
        """auto_talk 仅在 MAIN_UI 活跃。"""
        cls = get_daemon_class("auto_talk")
        assert Scene.MAIN_UI in cls.scenes
        assert Scene.DIALOG not in cls.scenes


class TestQuickTeleportDaemon:
    """quick_teleport 守护逻辑。"""

    def test_step_no_frame(self):
        """无帧时不操作。"""
        cls = get_daemon_class("quick_teleport")
        inst = cls()
        dctx = _make_dctx()
        import asyncio

        asyncio.get_event_loop().run_until_complete(inst.step(dctx))


# ── 资源定位器测试 ──


class TestResources:
    """资源定位器新快捷方式。"""

    def test_template_ui(self):
        from framework.resources import Resources

        r = Resources(root="resources")
        p = r.template_ui("paimon_menu.png")
        assert "ui" in str(p) and "paimon_menu" in str(p)

    def test_template_dialog(self):
        from framework.resources import Resources

        r = Resources(root="resources")
        p = r.template_dialog("disabled_ui.png")
        assert "dialog" in str(p) and "disabled_ui" in str(p)

    def test_template_eat(self):
        from framework.resources import Resources

        r = Resources(root="resources")
        p = r.template_eat("Recovery.png")
        assert "eat" in str(p) and "Recovery" in str(p)

    def test_template_chest(self):
        from framework.resources import Resources

        r = Resources(root="resources")
        p = r.template_chest("chest_F_icon.png")
        assert "chest" in str(p)

    def test_template_teleport(self):
        from framework.resources import Resources

        r = Resources(root="resources")
        p = r.template_teleport("MapScaleButton.png")
        assert "teleport" in str(p)

    def test_template_loading(self):
        from framework.resources import Resources

        r = Resources(root="resources")
        p = r.template_loading("enter_game.png")
        assert "loading" in str(p)

    def test_map_tp_json(self):
        from framework.resources import Resources

        r = Resources(root="resources")
        p = r.map("tp.json")
        assert p.exists()


# ── 高层 API 测试 ──


class TestHighLevelApiTalk:
    """g.talk / g.talk_skip 不再 NotImplementedError。"""

    def test_talk_signature_exists(self):
        from framework.high_level_api import HighLevelApi

        assert hasattr(HighLevelApi, "talk")

    def test_talk_skip_signature_exists(self):
        from framework.high_level_api import HighLevelApi

        assert hasattr(HighLevelApi, "talk_skip")


# ── Dialog 模块导入测试 ──


class TestDialogImport:
    """dialog.py 可导入。"""

    def test_import(self):
        from abilities.dialog import talk, talk_skip, visible_options, is_orange_option

        assert callable(talk)
        assert callable(talk_skip)
        assert callable(visible_options)
        assert callable(is_orange_option)


# ── game_state 导入测试 ──


class TestGameSateImport:
    """game_state.py 可导入。"""

    def test_import(self):
        from abilities.game_state import (
            has_paimon_menu,
            has_disabled_ui_btn,
            has_map_scale_btn,
            has_in_domain,
            is_loading_screen,
            is_low_hp,
            has_recovery_icon,
            has_resurrection_icon,
            has_chest_f_icon,
            has_flower_f_icon,
            make_classifier,
        )

        assert callable(make_classifier)
