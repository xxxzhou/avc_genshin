"""Phase C 测试：fighter（血条检测 / 角色槽 / 连招派发 / 退出逻辑 / 释放）。

可 ``python -m pytest tests/test_phase_c.py -v`` 或 ``python tests/test_phase_c.py``。
不依赖游戏；纯 CV/逻辑测试始终跑，真实按键序列测试在 avc 缺失时自动 skip。
"""

from __future__ import annotations

import numpy as np
import pytest
from unittest.mock import MagicMock


# ── avc 可用性（按键序列测试用）──


def _has_avc() -> bool:
    try:
        from avc._core import KeyCode, MouseButton  # noqa: F401

        return True
    except Exception:
        return False


# ── 合成 frame（真 avc IImageBuffer，BGRA8；喂 avc 检测器）──


def _frame(bars=None, *, blocks=None, h=1080, w=1920):
    """构造 avc IImageBuffer 合成帧。

    bars: [(x, y, bw, bh)] 放红色血条（RGB(255,90,90) = BGRA(90,90,255,255)）。
    blocks: {slot_idx: "white"|"active"} 设置 AvatarIndexRectList 编号块状态。
    """
    from avc import Image
    from avc._core import ImageType

    arr = np.zeros((h, w, 4), dtype=np.uint8)
    for (x, y, bw, bh) in (bars or []):
        arr[y : y + bh, x : x + bw] = (90, 90, 255, 255)
    if blocks:
        from abilities.fighter import _AVATAR_INDEX_ROIS

        for slot, kind in blocks.items():
            x, y, bw, bh = _AVATAR_INDEX_ROIS[slot]
            if kind == "white":
                arr[y : y + bh, x : x + bw] = (255, 255, 255, 255)
            else:  # active：彩色（灰度 ~124，远低于白阈值 251）
                arr[y : y + bh, x : x + bw] = (50, 100, 200, 255)
    buf = Image.createImageBuffer()
    buf.setFormat(w, h, ImageType.bgra8)
    buf.from_bytes(arr.tobytes())
    return buf


class MockContext:
    """模拟 GameContext（无 avc 依赖）。"""

    def __init__(self, frame=None):
        self.cfg = MagicMock()
        self.ic = MagicMock()
        self._frame = frame

    def capture(self):
        return self._frame

    def release_all_keys(self):
        pass


# ── 血条检测（纯 CV）──


class TestBloodBarDetection:
    def test_no_bar(self):
        from abilities.fighter import detect_blood_bars

        assert detect_blood_bars(_frame(bars=[])) == []

    def test_one_bar(self):
        from abilities.fighter import detect_blood_bars

        bars = detect_blood_bars(_frame(bars=[(800, 400, 50, 8)]))
        assert len(bars) == 1
        assert bars[0].x == 800 and bars[0].w == 50

    def test_bar_center(self):
        from abilities.fighter import detect_blood_bars

        bar = detect_blood_bars(_frame(bars=[(800, 400, 50, 8)]))[0]
        assert bar.cx == 825  # 800 + 50/2

    def test_exclude_left_ui(self):
        """x<=200 的红色块应排除（队伍头像红边）。"""
        from abilities.fighter import detect_blood_bars

        assert detect_blood_bars(_frame(bars=[(100, 400, 50, 8)])) == []

    def test_exclude_tiny_noise(self):
        """面积 < 8 的连通块过滤。"""
        from abilities.fighter import detect_blood_bars

        assert detect_blood_bars(_frame(bars=[(800, 400, 2, 2)])) == []

    def test_outside_roi_excluded(self):
        """x=1600 在 ROI(0..1500) 外，检测不到。"""
        from abilities.fighter import detect_blood_bars

        assert detect_blood_bars(_frame(bars=[(1600, 400, 50, 8)])) == []

    def test_multiple_bars(self):
        from abilities.fighter import detect_blood_bars

        bars = detect_blood_bars(_frame(bars=[(300, 200, 40, 8), (900, 470, 40, 8)]))
        assert len(bars) == 2


class TestHasEnemyInFrame:
    def test_true_when_bar(self):
        from abilities.fighter import has_enemy_in_frame

        assert has_enemy_in_frame(_frame(bars=[(800, 400, 50, 8)])) is True

    def test_false_when_no_bar(self):
        from abilities.fighter import has_enemy_in_frame

        assert has_enemy_in_frame(_frame(bars=[])) is False

    def test_false_when_only_left_ui(self):
        from abilities.fighter import has_enemy_in_frame

        assert has_enemy_in_frame(_frame(bars=[(100, 400, 50, 8)])) is False


class TestFindNearestEnemy:
    def test_picks_closest_to_center(self):
        from abilities.fighter import SimpleFighter

        # 两个血条：一个远离中心、一个接近 (960,480)
        f = SimpleFighter(
            MockContext(_frame(bars=[(300, 200, 40, 8), (920, 460, 40, 8)])), g=None
        )
        nearest = f.find_nearest_enemy()
        assert nearest is not None
        assert nearest.x == 920  # 离 (960,480) 更近

    def test_none_when_no_bar(self):
        from abilities.fighter import SimpleFighter

        f = SimpleFighter(MockContext(_frame(bars=[])), g=None)
        assert f.find_nearest_enemy() is None


# ── 角色出战槽（cv2）──


class TestActiveSlot:
    def test_returns_active_slot(self):
        from abilities.fighter import SimpleFighter

        f = SimpleFighter(
            MockContext(
                _frame(blocks={0: "white", 1: "active", 2: "white", 3: "white"})
            ),
            g=None,
        )
        assert f._active_slot_index() == 1

    def test_first_active_wins(self):
        from abilities.fighter import SimpleFighter

        f = SimpleFighter(
            MockContext(
                _frame(blocks={0: "active", 1: "active", 2: "white", 3: "white"})
            ),
            g=None,
        )
        assert f._active_slot_index() == 0  # 取第一个非白

    def test_none_when_all_white(self):
        from abilities.fighter import SimpleFighter

        f = SimpleFighter(
            MockContext(
                _frame(blocks={0: "white", 1: "white", 2: "white", 3: "white"})
            ),
            g=None,
        )
        assert f._active_slot_index() is None


# ── 连招派发路由（mock 子动作，不触发 avc）──


class TestRotationDispatch:
    def test_attack_routes(self):
        from abilities.fighter import SimpleFighter

        f = SimpleFighter(MockContext(), g=None)
        called = []
        f._attack = lambda d: called.append(("attack", d))
        f._exec_step(("attack", 1.5))
        assert called == [("attack", 1.5)]

    def test_skill_routes_hold(self):
        from abilities.fighter import SimpleFighter

        f = SimpleFighter(MockContext(), g=None)
        called = []
        f._use_skill = lambda hold: called.append(hold)
        f._exec_step(("skill", True))
        assert called == [True]

    def test_burst_routes(self):
        from abilities.fighter import SimpleFighter

        f = SimpleFighter(MockContext(), g=None)
        called = []
        f._use_burst = lambda: called.append(True)
        f._exec_step(("burst",))
        assert called == [True]

    def test_switch_routes(self):
        from abilities.fighter import SimpleFighter

        f = SimpleFighter(MockContext(), g=None)
        called = []
        f.switch_character = lambda s: called.append(s)
        f._exec_step(("switch", 3))
        assert called == [3]

    def test_wait_routes(self):
        from abilities.fighter import SimpleFighter

        f = SimpleFighter(MockContext(), g=None)
        # wait 不应抛错（实际 sleep 可被 monkeypatch，这里只验不崩）
        f._exec_step(("wait", 0.01))

    def test_unknown_action_raises(self):
        from abilities.fighter import SimpleFighter

        f = SimpleFighter(MockContext(), g=None)
        with pytest.raises(ValueError):
            f._exec_step(("nope",))


# ── fight_until_clear 退出逻辑（mock 依赖）──


class TestFightUntilClear:
    def test_returns_true_when_clear(self, monkeypatch):
        from abilities.fighter import SimpleFighter

        f = SimpleFighter(MockContext(), g=None)
        monkeypatch.setattr(f, "has_enemy", lambda: False)
        monkeypatch.setattr(f, "fight", lambda **k: None)
        monkeypatch.setattr(f, "_release_everything", lambda: None)
        assert f.fight_until_clear(timeout=5, clear_stable_s=0.1) is True

    def test_returns_false_on_timeout(self, monkeypatch):
        from abilities.fighter import SimpleFighter

        f = SimpleFighter(MockContext(), g=None)
        monkeypatch.setattr(f, "has_enemy", lambda: True)  # 一直有敌
        monkeypatch.setattr(f, "fight", lambda **k: None)
        monkeypatch.setattr(f, "_release_everything", lambda: None)
        assert f.fight_until_clear(timeout=0.3, clear_stable_s=10) is False

    def test_releases_on_exit(self, monkeypatch):
        from abilities.fighter import SimpleFighter

        f = SimpleFighter(MockContext(), g=None)
        monkeypatch.setattr(f, "has_enemy", lambda: False)
        monkeypatch.setattr(f, "fight", lambda **k: None)
        released = []
        monkeypatch.setattr(f, "_release_everything", lambda: released.append(True))
        f.fight_until_clear(timeout=1, clear_stable_s=0.05)
        assert released == [True]


# ── fight 的 finally 释放 ──


class TestFightFinally:
    def test_releases_on_exception(self, monkeypatch):
        from abilities.fighter import SimpleFighter

        # has_enemy=True（有血条）→ 进循环 → _exec_step 抛 → finally release
        f = SimpleFighter(MockContext(_frame(bars=[(800, 400, 50, 8)])), g=None)
        released = []
        monkeypatch.setattr(f, "_release_everything", lambda: released.append(True))

        def boom(step):
            raise RuntimeError("sim")

        monkeypatch.setattr(f, "_exec_step", boom)
        with pytest.raises(RuntimeError):
            f.fight(duration_s=5)
        assert released == [True]

    def test_releases_on_no_enemy_early_exit(self, monkeypatch):
        from abilities.fighter import SimpleFighter

        f = SimpleFighter(MockContext(_frame(bars=[])), g=None)  # has_enemy=False
        released = []
        monkeypatch.setattr(f, "_release_everything", lambda: released.append(True))
        f.fight(duration_s=5)
        assert released == [True]


# ── 真实按键序列（需 avc）──


@pytest.mark.skipif(not _has_avc(), reason="avc 未安装，跳过真实按键序列测试")
class TestKeySequence:
    def test_switch_presses_x_then_num(self, monkeypatch):
        from abilities.fighter import SimpleFighter
        from avc._core import KeyCode

        f = SimpleFighter(MockContext(), g=None)
        monkeypatch.setattr("abilities.fighter.utils.sleep", lambda s: None)
        f.switch_character(2)
        keys = [c.args[0] for c in f.ctx.ic.press.call_args_list]
        assert KeyCode.x in keys and KeyCode.num2 in keys

    def test_skill_tap_presses_e(self, monkeypatch):
        from abilities.fighter import SimpleFighter
        from avc._core import KeyCode

        f = SimpleFighter(MockContext(), g=None)
        monkeypatch.setattr("abilities.fighter.utils.sleep", lambda s: None)
        f._use_skill(hold=False)
        keys = [c.args[0] for c in f.ctx.ic.press.call_args_list]
        assert KeyCode.e in keys

    def test_attack_clicks_mouse(self, monkeypatch):
        from abilities.fighter import SimpleFighter

        f = SimpleFighter(MockContext(), g=None)
        monkeypatch.setattr("abilities.fighter.utils.sleep", lambda s: None)
        f._attack(0.45)  # ~2 个 tick
        assert f.ctx.ic.mouseDown.call_count >= 2
        assert f.ctx.ic.mouseUp.call_count >= 2

    def test_charge_holds_mouse(self, monkeypatch):
        from abilities.fighter import SimpleFighter

        f = SimpleFighter(MockContext(), g=None)
        monkeypatch.setattr("abilities.fighter.utils.sleep", lambda s: None)
        f._charge(2.0)
        f.ctx.ic.mouseDown.assert_called_once()
        f.ctx.ic.mouseUp.assert_called_once()


# ── 索敌（转视角找敌）──


class TestFighterSeek:
    def test_seek_rotates_when_no_enemy(self, monkeypatch):
        from abilities.fighter import SimpleFighter

        ctx = MockContext()
        f = SimpleFighter(ctx, MagicMock())
        monkeypatch.setattr(f, "find_nearest_enemy", lambda: None)
        r = f.seek_enemy(max_turns=3)
        assert r is None
        assert ctx.ic.moveMouseBy.call_count == 3  # 每次盲转一档

    def test_seek_aligns_to_blood_bar(self, monkeypatch):
        from abilities.fighter import SimpleFighter
        from abilities.vision_utils import Rect

        ctx = MockContext()
        f = SimpleFighter(ctx, MagicMock())
        calls = iter([None, Rect(1400, 200, 60, 20)])  # 第二次找到, 血条偏右
        monkeypatch.setattr(f, "find_nearest_enemy", lambda: next(calls))
        r = f.seek_enemy(max_turns=3)
        assert r is not None
        assert ctx.ic.moveMouseBy.call_count == 2  # 盲转 1 次 + 对准 1 次

    def test_fight_until_clear_seeks_before_conclude(self, monkeypatch):
        from abilities.fighter import SimpleFighter

        ctx = MockContext()
        f = SimpleFighter(ctx, MagicMock())
        monkeypatch.setattr(f, "has_enemy", lambda: False)
        monkeypatch.setattr(f, "recover_on_death", lambda: False)
        monkeypatch.setattr(f, "seek_enemy", MagicMock(return_value=None))
        # clear_stable 巨大 → 不会提前判清场; timeout 短 → 超时 False
        ok = f.fight_until_clear(timeout=0.3, clear_stable_s=999)
        assert ok is False
        assert f.seek_enemy.call_count > 0  # 敌人消失未清场时先索敌


# ── 死亡恢复 ──


class TestFighterDeathRecovery:
    def test_death_triggers_retry_and_statue_tp(self, monkeypatch):
        from abilities.fighter import SimpleFighter
        from framework.errors import Retry

        ctx = MockContext(frame=_frame(bars=[]))
        g = MagicMock()
        f = SimpleFighter(ctx, g)
        monkeypatch.setattr(
            "abilities.game_state.has_resurrection_icon", lambda ctx, frame=None: True
        )
        with pytest.raises(Retry):
            f.recover_on_death()
        g.teleport_to.assert_called_once_with("七天神像-风")

    def test_no_death_returns_false(self, monkeypatch):
        from abilities.fighter import SimpleFighter

        ctx = MockContext(frame=_frame(bars=[]))
        f = SimpleFighter(ctx, MagicMock())
        monkeypatch.setattr(
            "abilities.game_state.has_resurrection_icon", lambda ctx, frame=None: False
        )
        assert f.recover_on_death() is False


# ── 掉物拾取 + 战斗结束判定 ──


class TestFighterPickup:
    def test_pick_drops_presses_f(self, monkeypatch):
        from avc._core import KeyCode

        from abilities.fighter import SimpleFighter

        ctx = MockContext(frame=_frame(bars=[]))
        f = SimpleFighter(ctx, MagicMock())
        monkeypatch.setattr(
            "abilities.game_state.has_chest_f_icon", lambda c, frame=None: True
        )
        monkeypatch.setattr(
            "abilities.game_state.has_flower_f_icon", lambda c, frame=None: False
        )
        picked = f.pick_drops(timeout=0.6)
        assert picked >= 1
        assert ctx.ic.press.call_args_list[0].args[0] == KeyCode.f

    def test_no_icon_no_pick(self, monkeypatch):
        from abilities.fighter import SimpleFighter

        ctx = MockContext(frame=_frame(bars=[]))
        f = SimpleFighter(ctx, MagicMock())
        monkeypatch.setattr(
            "abilities.game_state.has_chest_f_icon", lambda c, frame=None: False
        )
        monkeypatch.setattr(
            "abilities.game_state.has_flower_f_icon", lambda c, frame=None: False
        )
        assert f.pick_drops(timeout=0.2) == 0


class TestFightFinished:
    def test_default_blood_bar_absence(self, monkeypatch):
        from abilities.fighter import SimpleFighter

        f = SimpleFighter(MockContext(), MagicMock())
        monkeypatch.setattr(f, "has_enemy", lambda: False)
        assert f._fight_finished() is True
        monkeypatch.setattr(f, "has_enemy", lambda: True)
        assert f._fight_finished() is False


if __name__ == "__main__":
    import pytest as _pytest

    _pytest.main([__file__, "-v"])
