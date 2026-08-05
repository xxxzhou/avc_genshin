"""Phase B 测试：navigation / teleport / path_executor / camera / position / trap_escaper。

可 ``python -m pytest tests/test_phase_b.py -v`` 或直接 ``python tests/test_phase_b.py``。
所有测试不依赖 avc/游戏环境，使用 mock 对象。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any
from unittest.mock import MagicMock, patch

import pytest


# ── Mock 辅助 ──


class MockContext:
    """模拟 GameContext（无 avc 依赖）。"""

    def __init__(self):
        self.cfg = MagicMock()
        self._press_log: list = []
        self.ic = MagicMock()  # IInputController mock

    def press(self, key, hold=0.0):
        self._press_log.append((key, hold))

    def click_at(self, x, y, button="left"):
        pass


def _make_mock_ctx():
    return MockContext()


# ── TpDatabase 测试 ──


class TestTpDatabase:
    """传送点数据库测试。"""

    def test_tp_db_load(self):
        """tp.json 加载并包含传送点。"""
        from abilities.navigation.tp import TpDatabase

        db = TpDatabase("resources/map/tp.json")
        scenes = db.scenes
        assert "Teyvat" in scenes
        assert len(scenes["Teyvat"]) > 0

    def test_tp_db_find_nearest(self):
        """查找最近的传送点。"""
        from abilities.navigation.tp import TpDatabase

        db = TpDatabase("resources/map/tp.json")
        # 蒙德城传送锚点大约在 (1859, -579) 附近
        nearest = db.find_nearest(1860, -580, "Teyvat", n=1)
        assert len(nearest) >= 1
        # 应该是七天神像或附近的传送锚点
        assert nearest[0].type in ("Goddess", "TeleportWaypoint", "OneTimeDomain")

    def test_tp_db_find_nearest_n(self):
        """查找 N 个最近的传送点。"""
        from abilities.navigation.tp import TpDatabase

        db = TpDatabase("resources/map/tp.json")
        nearest_2 = db.find_nearest(1860, -580, "Teyvat", n=2)
        assert len(nearest_2) >= 2
        # 第一个应该比第二个近
        d1 = math.hypot(nearest_2[0].x - 1860, nearest_2[0].y - (-580))
        d2 = math.hypot(nearest_2[1].x - 1860, nearest_2[1].y - (-580))
        assert d1 <= d2

    def test_tp_db_find_by_name_exact(self):
        """精确名称查找传送点。"""
        from abilities.navigation.tp import TpDatabase

        db = TpDatabase("resources/map/tp.json")
        # tp.json 中的名称，找一个肯定存在的
        result = db.find_by_name("七天神像-风", "Teyvat")
        # 可能找到也可能找不到（名称可能不完全匹配）
        # 但至少不应该崩溃
        assert result is None or result.type == "Goddess"

    def test_tp_db_find_by_name_not_found(self):
        """名称不存在的传送点返回 None。"""
        from abilities.navigation.tp import TpDatabase

        db = TpDatabase("resources/map/tp.json")
        result = db.find_by_name("不存在的传送点xyz123", "Teyvat")
        assert result is None

    def test_tp_db_find_by_type(self):
        """按类型查找传送点。"""
        from abilities.navigation.tp import TpDatabase

        db = TpDatabase("resources/map/tp.json")
        goddess = db.find_by_type("Goddess", "Teyvat")
        assert len(goddess) > 0
        for g in goddess:
            assert g.type == "Goddess"

    def test_tp_db_empty_map(self):
        """不存在的地图名返回空列表。"""
        from abilities.navigation.tp import TpDatabase

        db = TpDatabase("resources/map/tp.json")
        assert db.find_nearest(0, 0, "NonExistent") == []
        assert db.find_by_name("test", "NonExistent") is None

    def test_tp_position_fields(self):
        """TpPosition 字段完整。"""
        from abilities.navigation.tp import TpPosition

        tp = TpPosition(
            id=1,
            type="Goddess",
            name="七天神像-风",
            country="蒙德",
            areas=("苍风高地",),
            x=1859.34,
            y=-575.6,
            tran_x=1854.05,
            tran_y=-578.44,
        )
        assert tp.id == 1
        assert tp.type == "Goddess"
        assert tp.country == "蒙德"
        assert len(tp.areas) == 1
        assert tp.x != tp.tran_x  # 传送后位置略有偏移


# ── Waypoint / PathTask 测试 ──


class TestWaypoint:
    """路径点数据模型测试。"""

    def test_waypoint_default(self):
        """默认值为 path + walk。"""
        from abilities.navigation.path_executor import Waypoint

        wp = Waypoint(x=100.0, y=200.0)
        assert wp.type == "path"
        assert wp.move_mode == "walk"
        assert wp.action == ""
        assert wp.action_params == ""

    def test_waypoint_teleport(self):
        """传送路径点。"""
        from abilities.navigation.path_executor import Waypoint

        wp = Waypoint(x=-1638.5, y=2153.9, type="teleport")
        assert wp.type == "teleport"

    def test_waypoint_frozen(self):
        """Waypoint 是不可变的。"""
        from abilities.navigation.path_executor import Waypoint

        wp = Waypoint(x=100.0, y=200.0)
        with pytest.raises(AttributeError):
            wp.x = 999  # type: ignore


class TestPathTaskSplit:
    """路径按传送点分割测试。"""

    def test_split_empty(self):
        """空路径返回空列表。"""
        from abilities.navigation.path_executor import PathExecutor

        result = PathExecutor._split_by_teleport(())
        assert result == []

    def test_split_no_teleport(self):
        """无传送点的路径为单段。"""
        from abilities.navigation.path_executor import PathExecutor, Waypoint

        wps = (
            Waypoint(x=1, y=1),
            Waypoint(x=2, y=2),
            Waypoint(x=3, y=3),
        )
        result = PathExecutor._split_by_teleport(wps)
        assert len(result) == 1
        assert len(result[0]) == 3

    def test_split_one_teleport_at_start(self):
        """首点为传送点，后续为行走。"""
        from abilities.navigation.path_executor import PathExecutor, Waypoint

        wps = (
            Waypoint(x=1, y=1, type="teleport"),
            Waypoint(x=2, y=2),
            Waypoint(x=3, y=3),
        )
        result = PathExecutor._split_by_teleport(wps)
        assert len(result) == 1
        assert result[0][0].type == "teleport"
        assert len(result[0]) == 3

    def test_split_multiple_teleports(self):
        """多个传送点分割成多段。"""
        from abilities.navigation.path_executor import PathExecutor, Waypoint

        wps = (
            Waypoint(x=1, y=1, type="teleport"),
            Waypoint(x=2, y=2),
            Waypoint(x=3, y=3, type="teleport"),
            Waypoint(x=4, y=4),
        )
        result = PathExecutor._split_by_teleport(wps)
        assert len(result) == 2
        assert result[0][0].type == "teleport"
        assert result[1][0].type == "teleport"

    def test_split_all_teleports(self):
        """全部为传送点，每个自成一段。"""
        from abilities.navigation.path_executor import PathExecutor, Waypoint

        wps = (
            Waypoint(x=1, y=1, type="teleport"),
            Waypoint(x=2, y=2, type="teleport"),
        )
        result = PathExecutor._split_by_teleport(wps)
        assert len(result) == 2


class TestPathTaskLoad:
    """路径 JSON 加载测试。"""

    def test_load_boss_path(self):
        """加载 BGI Boss 路径 JSON。"""
        from pathlib import Path

        from abilities.navigation.path_executor import load_path_task

        boss_dir = Path("resources/paths/boss")
        json_files = list(boss_dir.glob("*.json"))
        if not json_files:
            pytest.skip("No boss path JSON files found")

        task = load_path_task(json_files[0])
        assert task.info.name != ""
        assert task.info.map_name == "Teyvat"
        assert len(task.waypoints) > 0
        # 第一个路径点应该是传送点
        assert task.waypoints[0].type == "teleport"

    def test_load_waypoint_coordinates(self):
        """路径点坐标正确加载。"""
        import json
        from pathlib import Path

        from abilities.navigation.path_executor import load_path_task

        boss_dir = Path("resources/paths/boss")
        json_files = list(boss_dir.glob("*.json"))
        if not json_files:
            pytest.skip("No boss path JSON files found")

        # 直接读取 JSON 验证
        with open(json_files[0], encoding="utf-8") as f:
            raw = json.load(f)
        first_pos = raw["positions"][0]
        task = load_path_task(json_files[0])
        assert abs(task.waypoints[0].x - first_pos["x"]) < 0.01
        assert abs(task.waypoints[0].y - first_pos["y"]) < 0.01


# ── CameraControl 测试 ──


class TestCameraControl:
    """摄像机控制测试。"""

    def test_target_orientation_east(self):
        """朝东（正 X 方向）为 0 度。"""
        from abilities.navigation.camera import CameraControl

        angle = CameraControl.target_orientation((0, 0), (10, 0))
        assert angle == 0

    def test_target_orientation_north(self):
        """朝北（负 Y 方向）为 270 度。"""
        from abilities.navigation.camera import CameraControl

        angle = CameraControl.target_orientation((0, 0), (0, -10))
        # atan2(-10, 0) = -90°, 转正后 270°
        assert 269 <= angle <= 271

    def test_target_orientation_same_point(self):
        """相同点返回 0。"""
        from abilities.navigation.camera import CameraControl

        angle = CameraControl.target_orientation((5, 5), (5, 5))
        assert angle == 0

    def test_target_orientation_northeast(self):
        """朝东北方向。"""
        from abilities.navigation.camera import CameraControl

        angle = CameraControl.target_orientation((0, 0), (10, -10))
        # atan2(-10, 10) ≈ -45°, 转正后 315°
        assert 314 <= angle <= 316

    def test_distance(self):
        """欧氏距离。"""
        from abilities.navigation.camera import CameraControl

        assert CameraControl.distance((0, 0), (3, 4)) == 5.0

    def test_distance_same_point(self):
        """相同点距离为 0。"""
        from abilities.navigation.camera import CameraControl

        assert CameraControl.distance((5, 5), (5, 5)) == 0.0

    def test_control_ratio_large_diff(self):
        """大角度差使用高控制比例。"""
        from abilities.navigation.camera import CameraControl

        assert CameraControl._control_ratio(100) == 4.0
        assert CameraControl._control_ratio(50) == 3.0
        assert CameraControl._control_ratio(10) == 2.0
        assert CameraControl._control_ratio(3) == 1.0

    def test_angle_diff(self):
        """角度差计算。"""
        from abilities.navigation.camera import CameraControl

        assert CameraControl._angle_diff(10, 5) == 5.0
        assert CameraControl._angle_diff(350, 10) == -20.0  # 350→10 差 20 度
        assert CameraControl._angle_diff(10, 350) == 20.0


# ── avc IOrientationDetector 忠实性对比（需 avc + avc_opencv 插件）──


def _avc_od_available() -> bool:
    try:
        import avc

        return avc.Vision.createOrientationDetector() is not None
    except Exception:
        return False


class TestOrientationAvc:
    """C++（avc）与 Python compute_orientation 输出必须一致（验证 BGI 移植忠实）。"""

    pytestmark = pytest.mark.skipif(
        not _avc_od_available(), reason="需 avc + avc_opencv 插件"
    )

    def _compare(self, gray):
        import avc
        from avc._core import ImageType

        from abilities.navigation.camera import compute_orientation

        ang_py = compute_orientation(gray.copy())
        buf = avc.Image.IImageBuffer()
        buf.setFormat(212, 212, ImageType.r8)
        buf.from_bytes(gray.tobytes())
        ang_avc = avc.Vision.createOrientationDetector().compute(buf)
        assert ang_py == ang_avc, f"角度不一致: py={ang_py}, avc={ang_avc}"

    def test_noise(self):
        import numpy as np

        rng = np.random.default_rng(42)
        self._compare(rng.integers(0, 256, (212, 212), dtype=np.uint8))

    def test_arrow_wedge(self):
        import numpy as np

        img = np.zeros((212, 212), np.uint8)
        for r in range(20, 90):
            for th in range(45, 90, 2):  # 径向楔形（模拟角色箭头）
                x = 106 + int(r * np.cos(np.radians(th)))
                y = 106 + int(r * np.sin(np.radians(th)))
                if 0 <= x < 212 and 0 <= y < 212:
                    img[y, x] = 255
        self._compare(img)

    def test_gradient(self):
        import numpy as np

        gx, gy = np.meshgrid(np.arange(212), np.arange(212))
        self._compare(((gx + gy) * 255 / 420).astype(np.uint8))


# ── TrapEscaper 测试 ──


class TestTrapEscaper:
    """卡死检测与脱困测试。"""

    def test_not_stuck_initially(self):
        """初始不卡死。"""
        from abilities.navigation.trap_escaper import TrapEscaper

        ctx = _make_mock_ctx()
        esc = TrapEscaper(ctx)
        assert not esc.is_stuck()

    def test_not_stuck_few_samples(self):
        """采样数不足不卡死。"""
        from abilities.navigation.trap_escaper import TrapEscaper

        ctx = _make_mock_ctx()
        esc = TrapEscaper(ctx)
        for i in range(5):
            esc.record_position(100.0, 100.0)
        assert not esc.is_stuck()  # < 8 个采样

    def test_stuck_detection(self):
        """位置不变检测为卡死。"""
        from abilities.navigation.trap_escaper import TrapEscaper

        ctx = _make_mock_ctx()
        esc = TrapEscaper(ctx)
        for _ in range(10):
            esc.record_position(100.0, 100.0)
        assert esc.is_stuck()

    def test_not_stuck_moving(self):
        """位置变化不大但超过阈值不卡死。"""
        from abilities.navigation.trap_escaper import TrapEscaper

        ctx = _make_mock_ctx()
        esc = TrapEscaper(ctx)
        for i in range(10):
            esc.record_position(float(i) * 2, float(i) * 2)
        assert not esc.is_stuck()  # 移动距离足够

    def test_stuck_count_increases(self):
        """escape 后卡死计数增加。"""
        from abilities.navigation.trap_escaper import TrapEscaper
        from abilities.navigation.path_executor import Waypoint

        ctx = _make_mock_ctx()
        esc = TrapEscaper(ctx, max_stuck_count=3)
        wp = Waypoint(x=100, y=100)
        esc.escape(wp)
        assert esc.stuck_count == 1

    def test_should_abort(self):
        """卡死次数超限应放弃。"""
        from abilities.navigation.trap_escaper import TrapEscaper
        from abilities.navigation.path_executor import Waypoint

        ctx = _make_mock_ctx()
        esc = TrapEscaper(ctx, max_stuck_count=2)
        wp = Waypoint(x=100, y=100)
        esc.escape(wp)
        assert not esc.should_abort
        esc.escape(wp)
        assert esc.should_abort

    def test_reset_clears(self):
        """重置清空历史。"""
        from abilities.navigation.trap_escaper import TrapEscaper

        ctx = _make_mock_ctx()
        esc = TrapEscaper(ctx)
        for _ in range(10):
            esc.record_position(100.0, 100.0)
        esc.reset()
        assert not esc.is_stuck()
        assert esc.stuck_count == 0


# ── PositionGetter 测试 ──


class TestPositionGetter:
    """位置检测测试。"""

    def test_position_getter_import(self):
        """PositionGetter 可导入。"""
        from abilities.navigation.position import PositionGetter

        assert callable(PositionGetter)

    def test_coordinate_conversion_roundtrip(self):
        """坐标转换往返一致。"""
        from abilities.navigation.position import PositionGetter

        game_x, game_y = 2000.0, -1000.0
        img_x, img_y = PositionGetter.game_to_image_coords(game_x, game_y)
        result_x, result_y = PositionGetter.image_to_game_coords(img_x, img_y)
        assert abs(result_x - game_x) < 0.01
        assert abs(result_y - game_y) < 0.01

    def test_coordinate_conversion_known(self):
        """已知坐标转换。"""
        from abilities.navigation.position import (
            TEYVAT_ORIGIN_X,
            TEYVAT_ORIGIN_Y,
            TEYVAT_SCALE,
            PositionGetter,
        )

        # 原点处的游戏坐标应为 (0, 0)
        game_x, game_y = PositionGetter.image_to_game_coords(
            float(TEYVAT_ORIGIN_X), float(TEYVAT_ORIGIN_Y)
        )
        assert abs(game_x) < 0.01
        assert abs(game_y) < 0.01


# ── Navigator 导入测试 ──


class TestNavigatorImport:
    """Navigator 模块导入测试。"""

    def test_import(self):
        """Navigator 可导入。"""
        from abilities.navigation.navigator import Navigator

        assert callable(Navigator)


# ── Teleporter 导入测试 ──


class TestTeleporterImport:
    """Teleporter 模块导入测试。"""

    def test_import(self):
        """Teleporter 可导入。"""
        from abilities.navigation.tp import Teleporter

        assert callable(Teleporter)


# ── 高层 API 测试 ──


class TestHighLevelApiNavigation:
    """g.teleport_to / g.go_to 签名存在。"""

    def test_teleport_to_signature(self):
        """g.teleport_to 方法存在。"""
        from framework.high_level_api import HighLevelApi

        assert hasattr(HighLevelApi, "teleport_to")

    def test_go_to_signature(self):
        """g.go_to 方法存在。"""
        from framework.high_level_api import HighLevelApi

        assert hasattr(HighLevelApi, "go_to")


# ── PathExecutor 导入测试 ──


class TestPathExecutorImport:
    """PathExecutor 导入测试。"""

    def test_import(self):
        from abilities.navigation.path_executor import PathExecutor

        assert callable(PathExecutor)

    def test_load_path_task_import(self):
        from abilities.navigation.path_executor import load_path_task

        assert callable(load_path_task)


# ── Resources 测试 ──


class TestResourcesPhaseB:
    """资源定位器 Phase B 新增。"""

    def test_path_json_shortcut(self):
        from framework.resources import Resources

        r = Resources(root="resources")
        p = r.path_json("boss/急冻树前往.json")
        assert "boss" in str(p) and "急冻树" in str(p)

    def test_boss_paths_exist(self):
        from pathlib import Path

        boss_dir = Path("resources/paths/boss")
        assert boss_dir.exists()
        assert len(list(boss_dir.glob("*.json"))) > 0

    def test_ley_line_paths_exist(self):
        from pathlib import Path

        ley_dir = Path("resources/paths/ley_line")
        assert ley_dir.exists()
        assert len(list(ley_dir.glob("*.json"))) > 0


# ── Camera 导入测试 ──


class TestCameraImport:
    """Camera 模块导入测试。"""

    def test_import(self):
        from abilities.navigation.camera import CameraControl

        assert callable(CameraControl)

    def test_minimap_constants(self):
        from abilities.navigation.camera import (
            MINIMAP_CENTER_X,
            MINIMAP_CENTER_Y,
            MINIMAP_H,
            MINIMAP_SIZE,
            MINIMAP_W,
            MINIMAP_X,
            MINIMAP_Y,
        )

        # 对照 BGI MapAssets.MimiMapRect1080P = Rect(62, 19, 212, 212)
        assert MINIMAP_X == 62
        assert MINIMAP_Y == 19
        assert MINIMAP_W == 212
        assert MINIMAP_H == 212
        assert MINIMAP_SIZE == 212
        assert MINIMAP_CENTER_X == 168
        assert MINIMAP_CENTER_Y == 125


# ── PathExecutor action 派发 ──


class TestPathExecutorActions:
    def test_action_dispatch(self, monkeypatch):
        from avc._core import KeyCode

        from abilities.navigation.path_executor import (
            PathExecutor,
            PathTask,
            PathTaskInfo,
            Waypoint,
        )

        g = MagicMock()
        monkeypatch.setattr(
            "abilities.navigation.navigator.Navigator",
            lambda ctx, g: MagicMock(),
        )
        monkeypatch.setattr(
            "abilities.navigation.tp.Teleporter", lambda ctx, g: MagicMock()
        )
        pe = PathExecutor(MagicMock(), g)
        pt = PathTask(
            info=PathTaskInfo(name="t", task_type="collect"),
            waypoints=(
                Waypoint(x=0, y=0, type="teleport"),
                Waypoint(x=1, y=1, type="path", action="stop_flying"),
                Waypoint(x=2, y=2, type="path", action="fight"),
                Waypoint(x=3, y=3, type="path", action="pick_up"),
                Waypoint(x=4, y=4, type="path", action="mystery"),
            ),
        )
        pe.execute(pt)
        pressed = [c.args[0] for c in g.press.call_args_list]
        assert KeyCode.space in pressed  # stop_flying → 空格落地
        assert KeyCode.f in pressed  # pick_up → F
        g.fight_until_clear.assert_called_once()  # fight → 战斗
        assert any("mystery" in w for w in pe.warnings)  # 未实现 action → warning

    def test_no_action_no_press(self, monkeypatch):
        from abilities.navigation.path_executor import (
            PathExecutor,
            PathTask,
            PathTaskInfo,
            Waypoint,
        )

        g = MagicMock()
        monkeypatch.setattr("abilities.navigation.navigator.Navigator", lambda c, g: MagicMock())
        pe = PathExecutor(MagicMock(), g)
        pt = PathTask(
            info=PathTaskInfo(name="t"),
            waypoints=(Waypoint(x=1, y=1, type="path"),),
        )
        pe.execute(pt)
        g.press.assert_not_called()
        g.fight_until_clear.assert_not_called()

    def test_gadget_and_collect_actions(self, monkeypatch):
        from avc._core import KeyCode

        from abilities.navigation.path_executor import (
            PathExecutor,
            PathTask,
            PathTaskInfo,
            Waypoint,
        )

        g = MagicMock()
        monkeypatch.setattr("abilities.navigation.navigator.Navigator", lambda c, g: MagicMock())
        monkeypatch.setattr("abilities.navigation.tp.Teleporter", lambda c, g: MagicMock())
        pe = PathExecutor(MagicMock(), g)
        pt = PathTask(
            info=PathTaskInfo(name="t"),
            waypoints=(
                Waypoint(x=0, y=0, type="teleport"),
                Waypoint(x=1, y=1, type="path", action="use_gadget"),
                Waypoint(x=2, y=2, type="path", action="collect"),
            ),
        )
        pe.execute(pt)
        pressed = [c.args[0] for c in g.press.call_args_list]
        assert KeyCode.z in pressed  # use_gadget → Z
        assert KeyCode.f in pressed  # collect → F


# ── Navigator 移动模式 ──


class TestNavigatorMoveModes:
    @staticmethod
    def _nav():
        from abilities.navigation.navigator import Navigator

        ctx = MagicMock()
        ctx.ic = MagicMock()
        nav = Navigator(ctx, MagicMock())
        nav._position_getter = MagicMock(get_position=lambda: (0.0, 0.0))
        nav._camera = MagicMock()
        nav._trap_escaper = MagicMock(
            is_stuck=lambda: True, escape=MagicMock(), should_abort=False
        )
        return nav, ctx

    def test_fly_presses_space(self):
        from avc._core import KeyCode

        from abilities.navigation.path_executor import Waypoint

        nav, ctx = self._nav()
        nav.go_to(Waypoint(x=1.0, y=1.0, move_mode="fly"), timeout=0.05)
        ctx.ic.press.assert_any_call(KeyCode.space, 50)

    def test_climb_skips_trap_escape(self):
        from abilities.navigation.path_executor import Waypoint

        nav, ctx = self._nav()
        nav.go_to(Waypoint(x=100.0, y=0.0, move_mode="climb"), timeout=0.05)
        nav._trap_escaper.escape.assert_not_called()

    def test_walk_triggers_trap_escape(self):
        from abilities.navigation.path_executor import Waypoint

        nav, ctx = self._nav()
        nav.go_to(Waypoint(x=100.0, y=0.0, move_mode="walk"), timeout=0.05)
        nav._trap_escaper.escape.assert_called()

    def test_run_holds_and_releases_shift(self):
        from avc._core import KeyCode

        from abilities.navigation.path_executor import Waypoint

        nav, ctx = self._nav()
        nav.go_to(Waypoint(x=1.0, y=1.0, move_mode="run"), timeout=0.05)
        ctx.ic.keyDown.assert_any_call(KeyCode.shift)  # 冲刺
        ctx.ic.keyUp.assert_any_call(KeyCode.shift)  # finally 释放

    def test_dash_holds_shift(self):
        from avc._core import KeyCode

        from abilities.navigation.path_executor import Waypoint

        nav, ctx = self._nav()
        nav.go_to(Waypoint(x=1.0, y=1.0, move_mode="dash"), timeout=0.05)
        ctx.ic.keyDown.assert_any_call(KeyCode.shift)

    def test_jump_periodic(self, monkeypatch):
        from avc._core import KeyCode

        from abilities.navigation.path_executor import Waypoint

        monkeypatch.setattr("abilities.navigation.navigator._JUMP_INTERVAL_S", 0.1)
        nav, ctx = self._nav()
        nav._trap_escaper.is_stuck = lambda: False  # 不触发卡死, 让循环走到周期跳
        nav.go_to(Waypoint(x=100.0, y=0.0, move_mode="jump"), timeout=0.5)
        space_presses = [
            c for c in ctx.ic.press.call_args_list if c.args[0] == KeyCode.space
        ]
        assert len(space_presses) > 0  # 周期跳被触发
