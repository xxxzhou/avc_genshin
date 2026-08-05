"""Phase D 测试：auto_boss / auto_ley_line 任务骨架 + 共享树脂领取 helper。

纯控制流 mock 测试（不依赖游戏；任务内 lazy import avc，测试环境有 avc 即可）。
覆盖：树脂领取三种情形、任务主流程（happy/耗尽/缺资源）、Registry 可发现。
运行: python -m pytest tests/test_phase_d.py -v
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from abilities.reward import claim_resin_reward
from abilities.vision_utils import Rect
from framework.errors import NormalEnd, TaskError
from tasks.auto_boss import main as auto_boss_main
from tasks.auto_ley_line import main as auto_ley_line_main

# ── 打桩 ──


def _g(**overrides) -> MagicMock:
    """HighLevelApi mock：wait_until/fight 恒 True，find_text 恒 None（可覆盖）。"""
    g = MagicMock()
    g.wait_until.return_value = True
    g.fight_until_clear.return_value = True
    g.find_text.return_value = None
    for k, v in overrides.items():
        setattr(g, k, v)
    return g


def _claim_true(ctx, g, *, timeout=25.0) -> bool:
    return True


def _claim_false(ctx, g, *, timeout=25.0) -> bool:
    return False


def _noop_close(ctx, g) -> None:
    pass


# ── 树脂领取 helper ──


class TestRewardClaim:
    def test_already_consumed_returns_true(self, monkeypatch):
        """无『使用原粹树脂』按钮（find_text 恒 None）= 已消耗 → True，不点击。"""
        monkeypatch.setattr("abilities.reward._close_reward_page", _noop_close)
        g = _g()
        assert claim_resin_reward(MagicMock(), g) is True
        g.click.assert_not_called()

    def test_clicks_use_resin_until_gone(self, monkeypatch):
        """出现『使用原粹树脂』→ 点击中心 → 消失 → True。"""
        monkeypatch.setattr("abilities.reward._close_reward_page", _noop_close)
        state = {"use": 0}

        def fake_find(kw):
            if kw == "补充原粹树脂":
                return None
            state["use"] += 1
            return Rect(100, 200, 50, 30) if state["use"] == 1 else None

        g = _g(find_text=MagicMock(side_effect=fake_find))
        assert claim_resin_reward(MagicMock(), g) is True
        g.click.assert_called_once_with(125, 215)  # Rect(100,200,50,30) 的中心

    def test_exhausted_returns_false(self, monkeypatch):
        """出现『补充原粹树脂』= 耗尽 → False，不点『使用』。"""
        monkeypatch.setattr("abilities.reward._close_reward_page", _noop_close)
        g = _g(find_text=MagicMock(return_value=Rect(0, 0, 10, 10)))
        assert claim_resin_reward(MagicMock(), g) is False
        g.click.assert_not_called()


# ── auto_boss ──


class TestAutoBoss:
    def test_happy_path(self, monkeypatch):
        """完整一轮：execute→战斗→按 F→领奖，count=1 正常返回。"""
        pe = MagicMock()
        monkeypatch.setattr(
            "abilities.navigation.path_executor.PathExecutor", lambda ctx, g: pe
        )
        monkeypatch.setattr("abilities.reward.claim_resin_reward", _claim_true)
        g = _g()

        result = auto_boss_main(MagicMock(), g, boss_name="急冻树", count=1)

        assert result == {"boss": "急冻树", "count": 1}
        pe.execute.assert_called_once()
        g.fight_until_clear.assert_called_once()
        g.press.assert_called_once()  # 按 F 开奖励对话框
        g.wait_main_ui.assert_called()

    def test_resin_exhausted_normal_end(self, monkeypatch):
        """树脂耗尽（claim 返回 False）→ NormalEnd。"""
        pe = MagicMock()
        monkeypatch.setattr(
            "abilities.navigation.path_executor.PathExecutor", lambda ctx, g: pe
        )
        monkeypatch.setattr("abilities.reward.claim_resin_reward", _claim_false)
        g = _g()

        with pytest.raises(NormalEnd) as exc:
            auto_boss_main(MagicMock(), g, boss_name="急冻树", count=5)
        assert "树脂耗尽" in str(exc.value)

    def test_missing_path_raises_task_error(self, monkeypatch):
        """首领路径 JSON 缺失 → TaskError。"""
        g = _g()
        with pytest.raises(TaskError):
            auto_boss_main(MagicMock(), g, boss_name="不存在的首领", count=1)


# ── auto_ley_line ──


class TestAutoLeyLine:
    def test_happy_path(self, monkeypatch):
        """按 region 挑到路径，完整一轮正常返回。"""
        pe = MagicMock()
        monkeypatch.setattr(
            "abilities.navigation.path_executor.PathExecutor", lambda ctx, g: pe
        )
        monkeypatch.setattr("abilities.reward.claim_resin_reward", _claim_true)
        g = _g()

        result = auto_ley_line_main(MagicMock(), g, region="蒙德", count=1)

        assert result["region"] == "蒙德"
        assert result["count"] == 1
        pe.execute.assert_called_once()
        g.fight_until_clear.assert_called_once()

    def test_missing_region_raises_task_error(self, monkeypatch):
        """region 无匹配路径 → TaskError。"""
        g = _g()
        with pytest.raises(TaskError):
            auto_ley_line_main(MagicMock(), g, region="不存在地区", count=1)


# ── 注册表可发现（L3 插件契约：AI 按名调用）──


class TestRegistryDiscover:
    def test_phase_d_tasks_discoverable(self):
        from framework.registry import TaskRegistry

        roots = (str(Path(__file__).parent.parent / "src" / "tasks"),)
        reg = TaskRegistry()
        reg.discover(roots=roots)
        assert reg.get("auto_boss") is not None
        assert reg.get("auto_ley_line") is not None
        assert reg.get("verify") is not None


# ── verify 诊断任务 ──


class _FakeG:
    """HighLevelApi 探针替身：各探测返回固定值，记录 teleport 调用。"""

    scene = None  # 由测试注入 SceneState

    def __init__(self):
        self.teleported = None

    def is_loading(self):
        return False

    def wait_main_ui(self, timeout=30):
        return True

    def teleport_to(self, name):
        self.teleported = name
        return (123.0, 456.0)

    def has_enemy(self):
        return True

    def find_nearest_enemy(self):
        return None

    def is_q_ready(self):
        return True

    def find_text(self, kw):
        return None


def _mk_ctx(ocr_value=None):
    ctx = MagicMock()
    ctx.capture.return_value = None
    ctx.ocr = ocr_value
    return ctx


def _run_verify(ctx, g, **kw):
    from tasks.verify import main as verify_main

    return verify_main(ctx, g, **kw)


class TestVerify:
    """游戏内诊断任务：各探测记录 OK/ERR，异常不中断，do_teleport 开关生效。"""

    def test_readonly_probes(self, monkeypatch):
        from framework.scene import Scene, SceneState

        class MockPos:
            def get_position(self):
                return (100.0, 200.0)

        class MockCam:
            def get_orientation(self):
                return 90.0

        monkeypatch.setattr("abilities.navigation.position.PositionGetter", lambda ctx: MockPos())
        monkeypatch.setattr("abilities.navigation.camera.CameraControl", lambda ctx: MockCam())

        g = _FakeG()
        g.scene = SceneState(scene=Scene.MAIN_UI, confidence=0.9)
        result = _run_verify(_mk_ctx(), g, do_teleport=False)
        r = result["results"]

        # 只读探测全跑、全 OK
        assert "OK" in r["scene"] and "MAIN_UI" in r["scene"]
        assert "OK" in r["wait_main_ui(10s)"]
        assert "OK" in r["tp_lookup"]  # tp.json 已纳入 manifest，按名可查
        assert "OK" in r["position"] and "100.0" in r["position"]
        assert "OK" in r["orientation"] and "90.0" in r["orientation"]
        assert "OK" in r["has_enemy"]
        assert "OK" in r["nearest_enemy"]
        assert "OK" in r["is_q_ready"]
        assert "OK" in r["ocr_boxes"]  # ctx.ocr=None → "no avc_ocr"
        # do_teleport=False → 不真传送
        assert "teleport_to" not in r
        assert g.teleported is None

    def test_do_teleport_calls_teleport(self, monkeypatch):
        monkeypatch.setattr("abilities.navigation.position.PositionGetter", lambda ctx: MagicMock(get_position=lambda: None))
        monkeypatch.setattr("abilities.navigation.camera.CameraControl", lambda ctx: MagicMock(get_orientation=lambda: None))

        g = _FakeG()
        g.scene = None
        result = _run_verify(_mk_ctx(), g, do_teleport=True, waypoint="北风之狼的庙宇")
        r = result["results"]
        assert g.teleported == "北风之狼的庙宇"  # 真传送被调用
        assert "teleport_to" in r and "OK" in r["teleport_to"]

    def test_probe_error_does_not_stop_others(self, monkeypatch):
        from framework.scene import Scene, SceneState

        class BadPos:
            def get_position(self):
                raise RuntimeError("boom")

        monkeypatch.setattr("abilities.navigation.position.PositionGetter", lambda ctx: BadPos())
        monkeypatch.setattr("abilities.navigation.camera.CameraControl", lambda ctx: MagicMock(get_orientation=lambda: 0.0))

        g = _FakeG()
        g.scene = SceneState(scene=Scene.MAIN_UI, confidence=0.9)
        result = _run_verify(_mk_ctx(), g)
        r = result["results"]
        # 失败的探测记 ERR，其余仍 OK
        assert r["position"].startswith("ERR") and "RuntimeError" in r["position"]
        assert "OK" in r["scene"]
        assert "OK" in r["orientation"]
