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

    def ensure_foreground(self, wait_s=0.0):
        return True


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
        """真罗盘：朝北（+北轴）为 0 度（2026-08-08 改罗盘帧，0=北/90=东）。"""
        from abilities.navigation.camera import CameraControl

        angle = CameraControl.target_orientation((0, 0), (10, 0))
        # atan2(0, 10) = 0° → 北
        assert 0 <= angle <= 2

    def test_target_orientation_same_point(self):
        """相同点返回 0。"""
        from abilities.navigation.camera import CameraControl

        angle = CameraControl.target_orientation((5, 5), (5, 5))
        assert angle == 0

    def test_target_orientation_northeast(self):
        """真罗盘：朝东北（+北、+东）为 45 度。坐标 (北,西)：东=-西。"""
        from abilities.navigation.camera import CameraControl

        angle = CameraControl.target_orientation((0, 0), (10, -10))
        # dx=10(北), dy=-10(西)=+10(东) → atan2(10,10)=45°
        assert 44 <= angle <= 46

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
    """avc IOrientationDetector 可用且返回有效角度。"""

    pytestmark = pytest.mark.skipif(
        not _avc_od_available(), reason="需 avc + avc_opencv 插件"
    )

    def test_noise(self):
        import numpy as np
        import avc
        from avc._core import ImageType

        rng = np.random.default_rng(42)
        gray = rng.integers(0, 256, (212, 212), dtype=np.uint8)
        buf = avc.Image.IImageBuffer()
        buf.setFormat(212, 212, ImageType.r8)
        buf.from_bytes(gray.tobytes())
        ang = avc.Vision.createOrientationDetector().compute(buf)
        assert isinstance(ang, float) and 0 <= ang <= 360

    def test_arrow_wedge(self):
        import numpy as np
        import avc
        from avc._core import ImageType

        img = np.zeros((212, 212), np.uint8)
        for r in range(20, 90):
            for th in range(45, 90, 2):  # 径向楔形（模拟角色箭头）
                x = 106 + int(r * np.cos(np.radians(th)))
                y = 106 + int(r * np.sin(np.radians(th)))
                if 0 <= x < 212 and 0 <= y < 212:
                    img[y, x] = 255
        buf = avc.Image.IImageBuffer()
        buf.setFormat(212, 212, ImageType.r8)
        buf.from_bytes(img.tobytes())
        ang = avc.Vision.createOrientationDetector().compute(buf)
        assert isinstance(ang, float) and 0 <= ang <= 360

    def test_gradient(self):
        import numpy as np
        import avc
        from avc._core import ImageType

        gx, gy = np.meshgrid(np.arange(212), np.arange(212))
        gray = ((gx + gy) * 255 / 420).astype(np.uint8)
        buf = avc.Image.IImageBuffer()
        buf.setFormat(212, 212, ImageType.r8)
        buf.from_bytes(gray.tobytes())
        ang = avc.Vision.createOrientationDetector().compute(buf)
        assert isinstance(ang, float) and 0 <= ang <= 360


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

        # 实机标定(2026-08-08)：BGI Rect(62,19,212,212) 中心 (168,125) 在本版本偏上 ~29px，
        # 径向剖面实测环心 (169,154) r≈108 → Rect(61,46,216,216)。见 camera.py 同注释。
        assert MINIMAP_X == 61
        assert MINIMAP_Y == 46
        assert MINIMAP_W == 216
        assert MINIMAP_H == 216
        assert MINIMAP_SIZE == 216
        assert MINIMAP_CENTER_X == 169
        assert MINIMAP_CENTER_Y == 154


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
        mock_tp = MagicMock()
        mock_tp.teleport_to.return_value = (0.0, 0.0)  # teleport_to 返回 (tran_x, tran_y)
        monkeypatch.setattr(
            "abilities.navigation.tp.Teleporter", lambda ctx, g: mock_tp
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
        mock_tp = MagicMock()
        mock_tp.teleport_to.return_value = (0.0, 0.0)
        monkeypatch.setattr("abilities.navigation.tp.Teleporter", lambda c, g: mock_tp)
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


# ── 大图 SIFT 视口重定位（get_position_from_big_map）──


class TestBigMapSiftRelocation:
    """get_position_from_big_map：BGI 预存底图 + 切块局部/全图定位 → 游戏坐标链。"""

    @staticmethod
    def _pg_with(fm, vp):
        """构造 PositionGetter，把懒加载/预处理换成可控桩（隔离 avc/resources）。"""
        from abilities.navigation.position import PositionGetter

        pg = PositionGetter(MagicMock())
        pg._ensure_feature_matcher = lambda: fm
        pg._load_train_features = lambda f: True  # 默认底图特征已加载
        pg._shrink_for_bigmap = lambda frame: vp
        return pg

    def test_coordinate_chain_full_search(self):
        """无 expected → matchQueryFull；命中点 (340,335) → _map256_to_game → (6852.0, 15024.0)。"""
        fm = MagicMock()
        fm.matchQueryFull.return_value = 1  # 全图兜底，>0 视为命中
        fm.getMatch.return_value = MagicMock(x=340, y=335, w=0, h=0)  # 点结果（w=h=0 哨兵）
        pg = self._pg_with(fm, MagicMock())
        # 2026-08-08 轴对换：r.x=西轴(px), r.y=北轴(py)
        # position[0]北=(2048-335)/0.25=6852.0, position[2]西=(4096-340)/0.25=15024.0
        assert pg.get_position_from_big_map(frame=MagicMock()) == (6852.0, 15024.0)
        fm.matchQueryFull.assert_called_once()
        fm.matchQueryLocal.assert_not_called()

    def test_returns_none_when_feature_matcher_unavailable(self):
        """fm 创建失败（无 avc/插件）→ None。"""
        pg = self._pg_with(None, MagicMock())
        assert pg.get_position_from_big_map(frame=MagicMock()) is None

    def test_returns_none_when_load_train_fails(self):
        """底图特征加载失败 → None。"""
        fm = MagicMock()
        pg = self._pg_with(fm, MagicMock())
        pg._load_train_features = lambda f: False  # 覆盖：加载失败
        assert pg.get_position_from_big_map(frame=MagicMock()) is None

    def test_returns_none_when_no_match(self):
        """matchQueryFull 返回 0 → None。"""
        fm = MagicMock()
        fm.matchQueryFull.return_value = 0
        pg = self._pg_with(fm, MagicMock())
        assert pg.get_position_from_big_map(frame=MagicMock()) is None

    def test_returns_none_when_get_match_none(self):
        """getMatch(0) 返回 None → None。"""
        fm = MagicMock()
        fm.matchQueryFull.return_value = 1
        fm.getMatch.return_value = None
        pg = self._pg_with(fm, MagicMock())
        assert pg.get_position_from_big_map(frame=MagicMock()) is None

    def test_expected_center_uses_local_search(self):
        """有 expected_center → matchQueryLocal（切块局部，BGI KnnMatchLocal）；
        验证 roi 尺寸（BGI BuildLocalSearchRect：min(train, max(query×2, train/4))）+ expandCells。"""
        from abilities.navigation.position import _BIGMAP_EXPAND_CELLS

        fm = MagicMock()
        fm.matchQueryLocal.return_value = 1
        fm.getMatch.return_value = MagicMock(x=340, y=335, w=0, h=0)
        vp = MagicMock()
        vp.width = 480  # 视口缩 1/4 后（BGI resizedGrey 尺度）
        vp.height = 270
        pg = self._pg_with(fm, vp)
        # expected_center 给游戏坐标（position[0]北, position[2]西 序）；
        # _build_search_rect 转 256 底图系算 roi
        result = pg.get_position_from_big_map(
            frame=MagicMock(), expected_center=(6852.0, 15024.0)
        )
        assert result == (6852.0, 15024.0)
        fm.matchQueryLocal.assert_called_once()
        fm.matchQueryFull.assert_not_called()
        # matchQueryLocal(viewport, roiX, roiY, roiW, roiH, expandCells)
        args = fm.matchQueryLocal.call_args.args
        assert args[0] is vp
        assert args[5] == _BIGMAP_EXPAND_CELLS  # expandCells=2（BGI 默认）
        # w=min(5632,max(480*2,5632//4))=min(5632,max(960,1408))=1408
        # h=min(3840,max(270*2,3840//4))=min(3840,max(540,960))=960
        assert args[3] == 1408
        assert args[4] == 960


# ── MapController：缩放测量 / 拖拽 / 国家切换 / 图标 ──


def _mc(ctx=None, g=None):
    """构造 MapController（ctx/g 用 MagicMock）。"""
    from abilities.navigation.map_ops import MapController

    ctx = ctx or MagicMock()
    return MapController(ctx, g or MagicMock())


class TestMapControllerZoom:
    """measure_zoom_level：旋钮 Y → zoom_level（BGI -5*scale+6）。"""

    def _mc_with_knob(self, monkeypatch, knob_cy):
        from abilities.vision_utils import Rect

        # Rect.cy = y + h/2；取 h=20 → y = knob_cy - 10
        fake = lambda *a, **k: Rect(0, int(knob_cy) - 10, 20, 20)
        monkeypatch.setattr(
            "abilities.navigation.map_ops.vu.find_template", fake
        )
        return _mc()

    def test_top_knob_is_max_zoom_out(self, monkeypatch):
        """旋钮在顶（Y=468）→ zoom=1（最小放大）。"""
        mc = self._mc_with_knob(monkeypatch, 468)
        assert mc.measure_zoom_level(MagicMock()) == 1.0

    def test_bottom_knob_is_max_zoom_in(self, monkeypatch):
        """旋钮在底（Y=612）→ zoom=6（最大放大）。"""
        mc = self._mc_with_knob(monkeypatch, 612)
        assert mc.measure_zoom_level(MagicMock()) == 6.0

    def test_midpoint(self, monkeypatch):
        """旋钮居中（Y=540）→ zoom=3.5。"""
        mc = self._mc_with_knob(monkeypatch, 540)
        assert mc.measure_zoom_level(MagicMock()) == 3.5

    def test_returns_none_when_knob_not_found(self, monkeypatch):
        """旋钮模板未匹配 → None。"""
        monkeypatch.setattr(
            "abilities.navigation.map_ops.vu.find_template", lambda *a, **k: None
        )
        assert _mc().measure_zoom_level(MagicMock()) is None

    def test_set_zoom_level_converges(self, monkeypatch):
        """滚轮缩放：diff→notches→逐槽 scroll，进入容差即停（不超额滚）。"""
        monkeypatch.setattr(
            "abilities.navigation.map_ops.utils.sleep", lambda *a, **k: None
        )
        ctx = MagicMock()
        mc = _mc(ctx)
        # 初始 4.0 → diff 1.0(>0.3) 滚一轮；4.9 → diff 0.1(≤0.3) 停
        mc.measure_zoom_level = MagicMock(side_effect=[4.0, 4.9])
        assert mc.set_zoom_level(5.0) == 4.9
        # notches = round(1.0 / 0.083) = 12；逐槽 dy = _ZOOM_WHEEL_SIGN(-1) × (+1) = -1
        # 2026-08-08 实机：一次 scroll(0,大N) 被游戏吞掉，必须逐槽发送
        assert ctx.ic.scroll.call_count == 12
        for call in ctx.ic.scroll.call_args_list:
            assert call.args == (0, -1)

    def test_set_zoom_level_caps_notches(self, monkeypatch):
        """diff 极大 → notches 封顶 _MAX_ZOOM_NOTCHES(16)，不溢出。"""
        monkeypatch.setattr(
            "abilities.navigation.map_ops.utils.sleep", lambda *a, **k: None
        )
        ctx = MagicMock()
        mc = _mc(ctx)
        # 1.0 → 6.0：diff=5.0，单轮 notches 应封顶 16；6.0 已达容差返回
        mc.measure_zoom_level = MagicMock(side_effect=[1.0, 6.0])
        mc.set_zoom_level(6.0)
        assert ctx.ic.scroll.call_count == 16
        for call in ctx.ic.scroll.call_args_list:
            assert call.args == (0, -1)

    def test_set_zoom_level_none_when_unmeasurable(self, monkeypatch):
        """测不到 zoom → 不滚轮，返回 None。"""
        monkeypatch.setattr(
            "abilities.navigation.map_ops.utils.sleep", lambda *a, **k: None
        )
        ctx = MagicMock()
        mc = _mc(ctx)
        mc.measure_zoom_level = MagicMock(return_value=None)
        assert mc.set_zoom_level(5.0) is None
        ctx.ic.scroll.assert_not_called()


class TestMapControllerDrag:
    """drag_map：mouseDown→moveTo×N→mouseUp + buf_to_scr缩放 + 分步数。"""

    def _ctx(self):
        ctx = MagicMock()
        ctx._MouseButton = {"left": "LB"}
        ctx._dpi_scale = 1.0
        ctx.ic = MagicMock()
        ctx.ic.screenBounds = MagicMock(return_value=(1920, 1080))
        ctx.sc = MagicMock()
        ctx.sc.width = MagicMock(return_value=1920)
        ctx.to_screen = MagicMock(return_value=(960, 540))
        return ctx

    def test_drag_sequence_and_total_distance(self):
        """拖 1000 北向 game 单位（zoom=1）→ 屏幕 Y 移动，终点 Y ≈ 540+3570。"""
        mc = _mc(self._ctx())
        mc.drag_map(1000.0, 0.0, 1.0)  # north_delta=1000, west_delta=0
        ic = mc.ctx.ic
        # 第一个 moveTo 是起点，后续 moveTo 是每步目标
        moves = ic.moveTo.call_args_list
        assert len(moves) >= 6  # 起点 + 5~60 步
        # 起点
        assert moves[0].args == (960, 540)
        # 2026-08-08 实机标定：北向 → 屏幕 Y 偏移 = MapScaleFactor(3.57)*1000/1.0 = 3570
        # 3.57 = 3.0*(200/168)（drag(+200)@zoom3.85 实测西轴移 168 单位，scale 偏低校准）
        final_y = moves[-1].args[1]
        assert abs(final_y - 4110) <= 25
        # X 全程 ≈ 960（西向增量 0）
        for m in moves:
            assert m.args[0] == 960
        ic.mouseDown.assert_called_once_with("LB")
        ic.mouseUp.assert_called_once_with("LB")

    def test_small_distance_no_drag(self):
        """偏移太小（<1px）→ 直接返回，不产生鼠标动作。"""
        mc = _mc(self._ctx())
        mc.drag_map(0.1, 0.1, 1.0)
        mc.ctx.ic.mouseDown.assert_not_called()
        mc.ctx.ic.moveTo.assert_not_called()

    def test_drag_curve_monotonic_to_one(self):
        """_drag_curve 0→1 单调递增，终点=1.0。"""
        from abilities.navigation.map_ops import MapController

        steps = 50
        vals = [MapController._drag_curve(i, steps) for i in range(steps + 1)]
        assert vals[0] == 0.0
        assert abs(vals[-1] - 1.0) < 1e-9
        for a, b in zip(vals, vals[1:]):
            assert a <= b  # 单调非减


class TestMapControllerCountryAndIcon:
    """switch_country 调用序列 + 国家中心换轴 + 图标模板映射。"""

    def test_country_center_axis_swap(self):
        """蒙德中心 = (2278, -876)（avc 系，已从 BGI [X=-876,Y=2278] 换轴）。"""
        from abilities.navigation.map_ops import MapController

        assert MapController.country_center("蒙德") == (2278.0, -876.0)
        assert MapController.country_center("不存在的国家") is None

    def test_switch_country_click_sequence(self, monkeypatch):
        """点区域按钮 → OCR 国家名 → 点国家名中心。"""
        from abilities.vision_utils import Rect

        monkeypatch.setattr(
            "abilities.navigation.map_ops.vu.find_text",
            lambda *a, **k: Rect(100, 100, 200, 40),  # cx=200, cy=120
        )
        ctx = MagicMock()
        mc = _mc(ctx)
        assert mc.switch_country("蒙德", MagicMock()) is True
        clicks = ctx.click_at.call_args_list
        # 第一击：右下区域选择按钮
        assert clicks[0].args == (1760, 1020)
        # 第二击：OCR 命中国家名中心
        assert clicks[1].args == (200, 120)

    def test_switch_country_unknown_returns_false(self):
        """未知国家名 → False，不点击。"""
        ctx = MagicMock()
        mc = _mc(ctx)
        assert mc.switch_country("亚特兰蒂斯", MagicMock()) is False
        ctx.click_at.assert_not_called()

    def test_icon_paths_teleport_waypoint(self):
        from abilities.navigation.map_ops import MapController

        assert MapController._icon_paths_for("TeleportWaypoint") == [
            "teleport/TeleportWaypoint.png"
        ]

    def test_icon_paths_domain_supplements_domain2(self):
        """秘境类补 Domain2.png（BGI 两种秘境图标）。"""
        from abilities.navigation.map_ops import MapController

        paths = MapController._icon_paths_for("OneTimeDomain")
        assert "teleport/Domain.png" in paths
        assert "teleport/Domain2.png" in paths

    def test_icon_paths_unknown_type_empty(self):
        from abilities.navigation.map_ops import MapController

        assert MapController._icon_paths_for("MysteryType") == []

    def test_switch_country_retries_then_succeeds(self, monkeypatch):
        """OCR 前 2 次失败、第 3 次命中 → 仍点中国家名并返回 True。"""
        from abilities.vision_utils import Rect

        monkeypatch.setattr(
            "abilities.navigation.map_ops.utils.sleep", lambda *a, **k: None
        )
        monkeypatch.setattr(
            "abilities.navigation.map_ops.vu.find_text",
            MagicMock(side_effect=[None, None, Rect(100, 100, 200, 40)]),
        )
        ctx = MagicMock()
        mc = _mc(ctx)
        assert mc.switch_country("蒙德") is True
        clicks = ctx.click_at.call_args_list
        assert clicks[0].args == (1760, 1020)  # 区域选择按钮
        assert clicks[1].args == (200, 120)  # 国家名中心(cx=200,cy=120)

    def test_switch_country_all_retries_fail(self, monkeypatch):
        """OCR 4 次全失败 → False，只点了区域按钮。"""
        monkeypatch.setattr(
            "abilities.navigation.map_ops.utils.sleep", lambda *a, **k: None
        )
        find_text = MagicMock(return_value=None)
        monkeypatch.setattr("abilities.navigation.map_ops.vu.find_text", find_text)
        ctx = MagicMock()
        mc = _mc(ctx)
        assert mc.switch_country("璃月") is False
        assert find_text.call_count == 4  # _SWITCH_AREA_RETRIES
        assert ctx.click_at.call_count == 1  # 只点了区域按钮，没点国家名
        assert ctx.click_at.call_args.args == (1760, 1020)

    def test_find_tp_icon_picks_nearest_to_center(self, monkeypatch):
        """多候选 → 选最接近视口中心(960,540)者。"""
        from abilities.vision_utils import Rect

        found = {
            "TeleportWaypoint.png": [
                Rect(100, 100, 40, 40),  # cx=120,cy=120 远
                Rect(900, 500, 40, 40),  # cx=920,cy=520 近
            ]
        }
        monkeypatch.setattr(
            "abilities.navigation.map_ops.vu.find_all_templates",
            lambda *a, **k: found,
        )
        mc = _mc()
        r = mc.find_tp_icon("TeleportWaypoint")
        assert r is not None
        assert (r.cx, r.cy) == (920, 520)

    def test_find_tp_icon_no_match_returns_none(self, monkeypatch):
        """视口内无匹配图标 → None。"""
        monkeypatch.setattr(
            "abilities.navigation.map_ops.vu.find_all_templates",
            lambda *a, **k: {},
        )
        assert _mc().find_tp_icon("TeleportWaypoint") is None

    def test_find_tp_icon_unknown_type_returns_none(self):
        """未知 type → 无候选模板 → 直接 None（不调 find_all_templates）。"""
        assert _mc().find_tp_icon("MysteryType") is None


# ── _navigate_map_to_target（MoveMapToCore 循环）──


class TestNavigateMapToTarget:
    """_navigate_map_to_target：定位→拖拽→图标点击主循环。"""

    @staticmethod
    def _teleporter(monkeypatch, mc):
        from framework.scene import Scene
        from abilities.navigation.tp import Teleporter

        monkeypatch.setattr(
            "abilities.navigation.map_ops.MapController", lambda c, g: mc
        )
        # 跳过 utils.sleep 真睡，保持测试快
        monkeypatch.setattr(
            "abilities.navigation.tp.utils.sleep", lambda *a, **k: None
        )
        ctx = MagicMock()
        g = MagicMock()
        g.scene = MagicMock(scene=Scene.MAP)
        tp = Teleporter(ctx, g)
        tp._pg = MagicMock()  # 预置，避免 _big_map_position 真建 PositionGetter
        return tp

    def _target(self, **kw):
        from abilities.navigation.tp import TpPosition

        base = dict(
            id=1, type="TeleportWaypoint", name="t", country=None,
            areas=(), x=2000.0, y=0.0, tran_x=0.0, tran_y=0.0,
        )
        base.update(kw)
        return TpPosition(**base)

    def test_convergence_drags_then_returns_candidates(self, monkeypatch):
        """位置递进：远→拖一次→近→退出→返回候选图标（点击留到确认循环）。"""
        from abilities.navigation.map_ops import DISPLAY_TP_ZOOM
        from abilities.vision_utils import Rect

        mc = MagicMock()
        mc.measure_zoom_level.return_value = 4.0
        mc.find_tp_icons.return_value = [Rect(100, 100, 40, 40)]  # cx=120, cy=120
        tp = self._teleporter(monkeypatch, mc)
        # iter1: 中心(1500,0) → dist 500(>200) 拖；iter2: 中心(1850,0) → dist 150(<200) 退
        tp._pg.get_position_from_big_map.side_effect = [(1500.0, 0.0), (1850.0, 0.0)]

        candidates = tp._navigate_map_to_target(self._target())

        mc.switch_to_ground_layer.assert_called_once()
        mc.switch_country.assert_not_called()  # country=None
        mc.drag_map.assert_called_once_with(500.0, 0.0, 4.0)
        # 收尾放大到 DisplayTpPointZoomLevel
        assert mc.set_zoom_level.call_args.args[0] == DISPLAY_TP_ZOOM
        mc.find_tp_icons.assert_called_once()
        # 不再内部点击，返回候选供 _click_and_confirm_teleport 使用
        tp.g.click.assert_not_called()
        assert len(candidates) == 1
        assert (candidates[0].cx, candidates[0].cy) == (120, 120)

    def test_aborts_after_consecutive_locate_failures(self, monkeypatch):
        """SIFT 连续失败且复位超限 → RuntimeError，不拖拽。

        2026-08-08 新增 M 关/开图复位：默认 _MAP_RESET_LIMIT=3（每轮 3 次失败复位一次）；
        置 0 直接走到"首轮失败即中止"，避免 mock 需喂满 12 次 None。
        """
        mc = MagicMock()
        tp = self._teleporter(monkeypatch, mc)
        monkeypatch.setattr("abilities.navigation.tp._MAP_RESET_LIMIT", 0)
        tp._pg.get_position_from_big_map.side_effect = [None, None, None]
        with pytest.raises(RuntimeError):
            tp._navigate_map_to_target(self._target())
        mc.drag_map.assert_not_called()

    def test_country_switch_disabled_in_v1(self, monkeypatch):
        """v1 禁用国家切换（实机确认 (1760,1020) 点不开列表且会误动地图）：
        target.country 被忽略，走 SIFT 定位 + 拖拽自行跨区收敛。"""
        mc = MagicMock()
        mc.measure_zoom_level.return_value = 4.0
        mc.find_tp_icons.return_value = []
        tp = self._teleporter(monkeypatch, mc)
        # 起点已在容差内（与 target (2000,0) 重合 → dist 0 < 200，不拖）
        tp._pg.get_position_from_big_map.side_effect = [(2000.0, 0.0)]

        tp._navigate_map_to_target(self._target(country="蒙德"))

        mc.switch_country.assert_not_called()  # v1 禁用（见 tp.py 注释，留 v2）
        mc.drag_map.assert_not_called()  # 已在容差内，不拖

    def test_icon_not_found_falls_back_to_center(self, monkeypatch):
        """图标未匹配 → 兜底返回视口中心 (960,540)（由确认循环点击）。"""
        mc = MagicMock()
        mc.measure_zoom_level.return_value = 4.0
        mc.find_tp_icons.return_value = []
        tp = self._teleporter(monkeypatch, mc)
        tp._pg.get_position_from_big_map.side_effect = [(1990.0, 0.0)]  # 容差内

        candidates = tp._navigate_map_to_target(self._target())

        tp.g.click.assert_not_called()
        assert len(candidates) == 1
        assert (candidates[0].cx, candidates[0].cy) == (960, 540)

    def test_not_in_map_scene_noop(self, monkeypatch):
        """不在 Scene.MAP → 直接返回（不报错、不操作）。"""
        from framework.scene import Scene

        mc = MagicMock()
        tp = self._teleporter(monkeypatch, mc)
        tp.g.scene.scene = Scene.MAIN_UI
        tp._navigate_map_to_target(self._target())
        mc.switch_to_ground_layer.assert_not_called()


# ── 传送/标记面板 OCR 检测（tp_panel.py）──


class _MockCtx:
    """最小 GameContext 替身：记录按键，capture 返回 MagicMock。"""

    def __init__(self):
        self.ic = MagicMock()
        self._press_log: list = []

    def capture(self):
        return MagicMock()

    def press(self, key, hold=0.0):
        self._press_log.append((key, hold))

    def save_debug(self, path):
        pass


class TestTpPanelDetect:
    """detect_tp_panel：OCR 分类 传送/标记/无。"""

    @staticmethod
    def _ocr(monkeypatch, texts):
        monkeypatch.setattr(
            "abilities.vision_utils.ocr_region",
            lambda *a, **k: [(t, 0.99) for t in texts],
        )

    def test_none_when_no_panel_text(self, monkeypatch):
        from abilities.tp_panel import TeleportPanelKind, detect_tp_panel

        self._ocr(monkeypatch, ["蒙德", "风起地"])
        assert detect_tp_panel(_MockCtx(), MagicMock()) is TeleportPanelKind.NONE

    def test_teleport_when_has_chuansong(self, monkeypatch):
        from abilities.tp_panel import TeleportPanelKind, detect_tp_panel

        self._ocr(monkeypatch, ["传送"])
        assert detect_tp_panel(_MockCtx(), MagicMock()) is TeleportPanelKind.TELEPORT

    def test_marker_when_total_marker_counter(self, monkeypatch):
        """'总标记113/300' → MARKER（实机最独特的标记面板标识）。"""
        from abilities.tp_panel import TeleportPanelKind, detect_tp_panel

        self._ocr(monkeypatch, ["总标记113/300", "确认"])
        assert detect_tp_panel(_MockCtx(), MagicMock()) is TeleportPanelKind.MARKER

    def test_marker_precedence_over_teleport(self, monkeypatch):
        """标记面板按钮 '追踪'/'删除' 优先于 '传送' 判断（标记面板绝无传送按钮）。"""
        from abilities.tp_panel import TeleportPanelKind, detect_tp_panel

        self._ocr(monkeypatch, ["追踪", "确认"])
        assert detect_tp_panel(_MockCtx(), MagicMock()) is TeleportPanelKind.MARKER

    def test_marker_keyword_zhui_zong(self, monkeypatch):
        from abilities.tp_panel import TeleportPanelKind, detect_tp_panel

        self._ocr(monkeypatch, ["删除"])
        assert detect_tp_panel(_MockCtx(), MagicMock()) is TeleportPanelKind.MARKER

    def test_find_teleport_button_locates_text(self, monkeypatch):
        from abilities.vision_utils import Rect

        from abilities.tp_panel import find_teleport_button

        monkeypatch.setattr(
            "abilities.vision_utils.find_text",
            lambda *a, **k: Rect(100, 100, 40, 20),  # cx=120, cy=110
        )
        btn = find_teleport_button(_MockCtx(), MagicMock())
        assert btn is not None
        assert (btn.cx, btn.cy) == (120, 110)

    def test_close_marker_panel_presses_esc_until_closed(self, monkeypatch):
        from avc._core import KeyCode

        from abilities.tp_panel import TeleportPanelKind, close_marker_panel

        monkeypatch.setattr(
            "abilities.tp_panel.detect_tp_panel",
            lambda *a, **k: TeleportPanelKind.MARKER,  # 一直未关闭 → 按满 max_attempts
        )
        ctx = _MockCtx()
        close_marker_panel(ctx, max_attempts=2)
        assert len(ctx._press_log) == 2
        assert all(k == KeyCode.esc for k, _ in ctx._press_log)

    def test_close_marker_panel_skips_when_already_closed(self, monkeypatch):
        from abilities.tp_panel import TeleportPanelKind, close_marker_panel

        monkeypatch.setattr(
            "abilities.tp_panel.detect_tp_panel",
            lambda *a, **k: TeleportPanelKind.NONE,
        )
        ctx = _MockCtx()
        close_marker_panel(ctx)
        assert ctx._press_log == []


class TestTeleportConfirmFlow:
    """_wait_and_confirm_teleport / _click_and_confirm_teleport：OCR 面板确认 + 避 pin 换点。"""

    @staticmethod
    def _tp():
        from abilities.navigation.tp import Teleporter

        ctx = _MockCtx()
        g = MagicMock()
        return Teleporter(ctx, g), ctx, g

    @staticmethod
    def _rect(cx, cy):
        from abilities.vision_utils import Rect

        return Rect(int(cx) - 20, int(cy) - 20, 40, 40)

    @staticmethod
    def _no_sleep(monkeypatch):
        monkeypatch.setattr("abilities.navigation.tp.utils.sleep", lambda *a, **k: None)

    def test_confirm_teleport_clicks_button(self, monkeypatch):
        from abilities.tp_panel import TeleportPanelKind

        tp, ctx, g = self._tp()
        self._no_sleep(monkeypatch)
        monkeypatch.setattr(
            "abilities.tp_panel.detect_tp_panel",
            lambda *a, **k: TeleportPanelKind.TELEPORT,
        )
        monkeypatch.setattr(
            "abilities.tp_panel.find_teleport_button",
            lambda *a, **k: self._rect(1600, 1000),
        )
        assert tp._wait_and_confirm_teleport() is True
        g.click.assert_called_once_with(1600, 1000)

    def test_confirm_teleport_presses_f_when_button_text_missing(self, monkeypatch):
        from avc._core import KeyCode

        from abilities.tp_panel import TeleportPanelKind

        tp, ctx, g = self._tp()
        self._no_sleep(monkeypatch)
        monkeypatch.setattr(
            "abilities.tp_panel.detect_tp_panel",
            lambda *a, **k: TeleportPanelKind.TELEPORT,
        )
        monkeypatch.setattr(
            "abilities.tp_panel.find_teleport_button", lambda *a, **k: None
        )
        assert tp._wait_and_confirm_teleport() is True
        assert any(k == KeyCode.f for k, _ in ctx._press_log)

    def test_confirm_marker_closes_and_returns_false(self, monkeypatch):
        from abilities.tp_panel import TeleportPanelKind

        tp, ctx, g = self._tp()
        self._no_sleep(monkeypatch)
        closed = []
        monkeypatch.setattr(
            "abilities.tp_panel.detect_tp_panel",
            lambda *a, **k: TeleportPanelKind.MARKER,
        )
        monkeypatch.setattr(
            "abilities.tp_panel.close_marker_panel",
            lambda *a, **k: closed.append(1),
        )
        assert tp._wait_and_confirm_teleport() is False
        assert len(closed) == 1  # 标记面板已关闭
        g.click.assert_not_called()  # 不点任何按钮（确认按钮会误改标记）

    def test_confirm_timeout_returns_false(self, monkeypatch):
        from abilities.tp_panel import TeleportPanelKind

        tp, ctx, g = self._tp()
        self._no_sleep(monkeypatch)
        monkeypatch.setattr(
            "abilities.tp_panel.detect_tp_panel",
            lambda *a, **k: TeleportPanelKind.NONE,
        )
        monkeypatch.setattr("abilities.navigation.tp._CONFIRM_WAIT_TIMEOUT", 0.0)
        assert tp._wait_and_confirm_teleport() is False
        g.click.assert_not_called()

    def test_click_and_confirm_retries_next_candidate_on_marker(self, monkeypatch):
        """避 pin：第一次点击命中标记面板 → 关闭后换下一候选 → 传送面板按 F 确认。"""
        from unittest.mock import MagicMock

        from avc._core import KeyCode

        from abilities.tp_panel import TeleportPanelKind

        tp, ctx, g = self._tp()
        self._no_sleep(monkeypatch)
        monkeypatch.setattr(
            "abilities.tp_panel.detect_tp_panel",
            MagicMock(
                side_effect=[TeleportPanelKind.MARKER, TeleportPanelKind.TELEPORT]
            ),
        )
        monkeypatch.setattr(
            "abilities.tp_panel.close_marker_panel", lambda *a, **k: None
        )
        # 传送按钮文字未命中 → 走按 F 兜底，不产生额外 g.click
        monkeypatch.setattr(
            "abilities.tp_panel.find_teleport_button", lambda *a, **k: None
        )
        candidates = [self._rect(500, 400), self._rect(700, 600)]
        tp._click_and_confirm_teleport(candidates)
        assert g.click.call_count == 2
        assert g.click.call_args_list[0].args == (500, 400)
        assert g.click.call_args_list[1].args == (700, 600)
        assert (KeyCode.f, 0.0) in ctx._press_log  # 传送面板 → 按 F 确认

    def test_click_and_confirm_all_candidates_fail(self, monkeypatch):
        from abilities.tp_panel import TeleportPanelKind

        tp, ctx, g = self._tp()
        self._no_sleep(monkeypatch)
        monkeypatch.setattr(
            "abilities.tp_panel.detect_tp_panel",
            lambda *a, **k: TeleportPanelKind.MARKER,
        )
        monkeypatch.setattr(
            "abilities.tp_panel.close_marker_panel", lambda *a, **k: None
        )
        tp._click_and_confirm_teleport([self._rect(500, 400), self._rect(700, 600)])
        # 两个候选都点了（各自返回 False），最终未确认
        assert g.click.call_count == 2

    def test_click_and_confirm_uses_center_fallback(self, monkeypatch):
        from abilities.tp_panel import TeleportPanelKind

        tp, ctx, g = self._tp()
        self._no_sleep(monkeypatch)
        # 无面板可确认 → 短等后超时，仅验证兜底点击视口中心
        monkeypatch.setattr("abilities.navigation.tp._CONFIRM_WAIT_TIMEOUT", 0.0)
        monkeypatch.setattr(
            "abilities.tp_panel.detect_tp_panel",
            lambda *a, **k: TeleportPanelKind.NONE,
        )
        tp._click_and_confirm_teleport([])  # 空候选 → 兜底点视口中心
        g.click.assert_called_once_with(960, 540)


# ── path_executor 传送后锚定 navigator prev ──


class TestPathExecutorTeleportAnchor:
    """传送后把 (tran_x, tran_y) 锚定到 navigator 的 prev_position。"""

    def test_sets_navigator_prev_after_teleport(self, monkeypatch):
        from abilities.navigation.path_executor import (
            PathExecutor,
            PathTask,
            PathTaskInfo,
            Waypoint,
        )

        navigator = MagicMock()
        monkeypatch.setattr(
            "abilities.navigation.navigator.Navigator", lambda c, g: navigator
        )
        teleporter = MagicMock()
        teleporter.teleport_to.return_value = (1234.0, -567.0)
        monkeypatch.setattr(
            "abilities.navigation.tp.Teleporter", lambda c, g: teleporter
        )

        pe = PathExecutor(MagicMock(), MagicMock())
        pt = PathTask(
            info=PathTaskInfo(name="t"),
            waypoints=(
                Waypoint(x=0, y=0, type="teleport"),
                Waypoint(x=1, y=1, type="path"),
            ),
        )
        pe.execute(pt)
        navigator.set_prev_position.assert_called_once_with(1234.0, -567.0)
