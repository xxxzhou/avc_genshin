"""每日奖励领取测试（Phase D 新增）。

纯控制流 mock 测试（不依赖游戏）。
覆盖：
- claim_encounter_points（F1 直领：已领/成功/失败）
- claim_daily_at_guild（凯瑟琳对话：路径执行+对话+派遣）
- one_key_expedition（派遣一键领取/重探）
- claim_daily_reward 总控（F1 优先→凯瑟琳回退→验证）
- claim_daily_reward 任务插件
- daily 总控编排
- Registry 可发现

运行: python -m pytest tests/test_daily.py -v
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

from abilities.daily import (
    claim_daily_at_guild,
    claim_daily_reward,
    claim_encounter_points,
    check_daily_claimed,
    one_key_expedition,
    _check_daily_claimed,
    _click_black_confirm,
    _close_ui,
    _find_and_interact_npc,
    _guild_path_name,
    _select_last_until_end,
)
from abilities.vision_utils import Rect
from framework.errors import NormalEnd, TaskError


# ── 打桩 ──


def _g(**overrides) -> MagicMock:
    """HighLevelApi mock。"""
    g = MagicMock()
    g.find_text.return_value = None
    g.wait_main_ui.return_value = True
    g.wait_scene.return_value = True
    g.wait_until.return_value = True
    for k, v in overrides.items():
        setattr(g, k, v)
    return g


def _ctx() -> MagicMock:
    return MagicMock()


# ── _guild_path_name ──


class TestGuildPathName:
    def test_known_countries(self):
        assert _guild_path_name("蒙德") == "冒险家协会_蒙德.json"
        assert _guild_path_name("璃月") == "冒险家协会_璃月.json"
        assert _guild_path_name("稻妻") == "冒险家协会_稻妻.json"
        assert _guild_path_name("须弥") == "冒险家协会_须弥.json"
        assert _guild_path_name("枫丹") == "冒险家协会_枫丹.json"
        assert _guild_path_name("挪德卡莱") == "冒险家协会_挪德卡莱.json"

    def test_unknown_country_fallback(self):
        assert _guild_path_name("至冬") == "冒险家协会_至冬.json"


# ── _check_daily_claimed ──


class TestCheckDailyClaimed:
    def test_already_claimed(self):
        g = _g(find_text=MagicMock(return_value=Rect(100, 200, 50, 30)))
        assert _check_daily_claimed(_ctx(), g) is True

    def test_not_claimed(self):
        g = _g(find_text=MagicMock(return_value=None))
        assert _check_daily_claimed(_ctx(), g) is False


# ── _close_ui ──


class TestCloseUi:
    def test_esc_closes(self):
        g = _g(wait_main_ui=MagicMock(return_value=True))
        _close_ui(_ctx(), g)
        g.press.assert_called()  # ESC pressed
        g.wait_main_ui.assert_called()


# ── _click_black_confirm ──


class TestClickBlackConfirm:
    def test_found_and_clicked(self, monkeypatch):
        rect = Rect(500, 600, 80, 40, 0.95)
        monkeypatch.setattr(
            "abilities.daily.vu.find_template", lambda *a, **kw: rect
        )
        g = _g()
        assert _click_black_confirm(_ctx(), g) is True
        g.click.assert_called_once_with(rect.cx, rect.cy)

    def test_not_found(self, monkeypatch):
        monkeypatch.setattr("abilities.daily.vu.find_template", lambda *a, **kw: None)
        g = _g()
        assert _click_black_confirm(_ctx(), g) is False
        g.click.assert_not_called()


# ── _find_and_interact_npc ──


class TestFindAndInteractNpc:
    def test_finds_f_and_interacts(self, monkeypatch):
        f_rect = Rect(960, 540, 40, 40, 0.9)
        monkeypatch.setattr(
            "abilities.daily.vu.find_template", lambda *a, **kw: f_rect
        )
        g = _g(wait_scene=MagicMock(return_value=True))
        assert _find_and_interact_npc(_ctx(), g, "凯瑟琳") is True
        g.press.assert_called()  # F pressed

    def test_no_f_icon_timeout(self, monkeypatch):
        monkeypatch.setattr("abilities.daily.vu.find_template", lambda *a, **kw: None)
        g = _g(wait_scene=MagicMock(return_value=False))
        assert _find_and_interact_npc(_ctx(), g, "凯瑟琳", timeout=0.5) is False


# ── _select_last_until_end ──


class TestSelectLastUntilEnd:
    def test_selects_last_option(self, monkeypatch):
        from abilities.dialog import DialogOption

        opts = [
            DialogOption(text="选项A", rect=Rect(100, 500, 200, 30), is_orange=False),
            DialogOption(text="选项B", rect=Rect(100, 400, 200, 30), is_orange=True),
        ]
        monkeypatch.setattr("abilities.dialog.visible_options", lambda ctx: opts)
        g = _g()
        _select_last_until_end(_ctx(), g, max_rounds=1)
        # opts[0] = 画面最下面 = 最后一个选项
        g.click.assert_called_once_with(opts[0].rect.cx, opts[0].rect.cy)

    def test_no_options_exits(self, monkeypatch):
        monkeypatch.setattr("abilities.dialog.visible_options", lambda ctx: [])
        g = _g()
        _select_last_until_end(_ctx(), g)
        g.click.assert_not_called()


# ── claim_encounter_points（F1 直领）──


class TestClaimEncounterPoints:
    def test_already_claimed(self, monkeypatch):
        """OCR「今日奖励已领取」→ 直接返回 True，不点领取按钮。"""
        monkeypatch.setattr(
            "abilities.daily._check_daily_claimed", lambda ctx, g: True
        )
        monkeypatch.setattr("abilities.daily._close_ui", lambda ctx, g: None)
        g = _g()
        assert claim_encounter_points(_ctx(), g) is True
        g.click.assert_not_called()  # 不点领取按钮

    def test_claim_success(self, monkeypatch):
        """找到委托导航 + 领取按钮 → 点击 → 验证已领 → True。"""
        state = {"claimed": False}

        def fake_check(ctx, g):
            return state["claimed"]

        commission_rect = Rect(100, 300, 120, 40, 0.9)
        claim_rect = Rect(1500, 700, 100, 50, 0.95)

        def fake_find_text(kw):
            if kw == "委托":
                return commission_rect
            return None

        def fake_find_template(ctx, path, threshold=0.7, roi=None):
            if "btn_claim" in str(path):
                return claim_rect
            return None

        monkeypatch.setattr("abilities.daily._check_daily_claimed", fake_check)
        monkeypatch.setattr("abilities.daily._close_ui", lambda ctx, g: None)
        monkeypatch.setattr("abilities.daily.vu.find_template", fake_find_template)
        # 点击领取后标记已领
        g = _g(find_text=MagicMock(side_effect=fake_find_text))

        def fake_click(x, y):
            if x == claim_rect.cx and y == claim_rect.cy:
                state["claimed"] = True

        g.click.side_effect = fake_click
        assert claim_encounter_points(_ctx(), g) is True

    def test_claim_fail_no_button(self, monkeypatch):
        """找不到领取按钮 → False。"""
        monkeypatch.setattr(
            "abilities.daily._check_daily_claimed", lambda ctx, g: False
        )
        monkeypatch.setattr("abilities.daily._close_ui", lambda ctx, g: None)
        monkeypatch.setattr("abilities.daily.vu.find_template", lambda *a, **kw: None)
        g = _g(find_text=MagicMock(return_value=None))
        assert claim_encounter_points(_ctx(), g) is False


# ── one_key_expedition ──


class TestOneKeyExpedition:
    def test_collect_and_re(self, monkeypatch):
        """找到 collect + re 模板 → 领取 + 重探 → True。"""
        collect_rect = Rect(800, 600, 60, 30, 0.9)
        re_rect = Rect(800, 700, 60, 30, 0.9)
        call_count = {"collect": 0, "re": 0}

        def fake_find_template(ctx, path, threshold=0.7, roi=None):
            if "collect" in str(path) and call_count["collect"] < 1:
                call_count["collect"] += 1
                return collect_rect
            if "re" in str(path) and call_count["re"] < 1:
                call_count["re"] += 1
                return re_rect
            return None

        monkeypatch.setattr("abilities.daily.vu.find_template", fake_find_template)
        monkeypatch.setattr("abilities.daily._click_black_confirm", lambda ctx, g: True)
        g = _g()
        assert one_key_expedition(_ctx(), g) is True
        # 应点击 collect + re + 角色选择 + 确认
        assert g.click.call_count >= 2

    def test_no_expedition_ui(self, monkeypatch):
        """找不到探索派遣入口 → False。"""
        monkeypatch.setattr("abilities.daily.vu.find_template", lambda *a, **kw: None)
        g = _g(find_text=MagicMock(return_value=None))
        assert one_key_expedition(_ctx(), g) is False


# ── claim_daily_at_guild（凯瑟琳对话）──


class TestClaimDailyAtGuild:
    def test_happy_path(self, monkeypatch):
        """走路径 → 找凯瑟琳 → 对话 → 领取 → 派遣 → True。"""
        pe = MagicMock()
        monkeypatch.setattr(
            "abilities.navigation.path_executor.PathExecutor", lambda ctx, g: pe
        )
        monkeypatch.setattr(
            "abilities.navigation.path_executor.load_path_task", lambda p: MagicMock()
        )
        monkeypatch.setattr(
            "abilities.daily._find_and_interact_npc", lambda ctx, g, name: True
        )
        monkeypatch.setattr(
            "abilities.daily._click_black_confirm", lambda ctx, g: True
        )
        monkeypatch.setattr(
            "abilities.daily._select_last_until_end", lambda ctx, g, max_rounds=10: None
        )
        monkeypatch.setattr(
            "abilities.daily.one_key_expedition", lambda ctx, g: True
        )
        g = _g()
        assert claim_daily_at_guild(_ctx(), g, country="蒙德") is True
        pe.execute.assert_called_once()
        g.talk.assert_called_once_with("每日委托")

    def test_missing_path(self, monkeypatch):
        """路径文件不存在 → False。"""
        # res.path_json("guild") 返回不存在的路径
        g = _g()
        # 不 monkeypatch PathExecutor，让 res.path_json 自然解析
        # resources/paths/guild/ 存在但 "冒险家协会_不存在.json" 不存在
        result = claim_daily_at_guild(_ctx(), g, country="不存在国家")
        assert result is False


# ── claim_daily_reward 总控 ──


class TestClaimDailyReward:
    def test_already_claimed(self, monkeypatch):
        """已领取 → 直接 True。"""
        monkeypatch.setattr(
            "abilities.daily.check_daily_claimed", lambda ctx, g: True
        )
        g = _g()
        assert claim_daily_reward(_ctx(), g) is True

    def test_f1_success(self, monkeypatch):
        """F1 直领成功 → True（不走凯瑟琳）。"""
        state = {"claimed": False}

        def fake_check(ctx, g):
            return state["claimed"]

        monkeypatch.setattr("abilities.daily.check_daily_claimed", fake_check)
        monkeypatch.setattr(
            "abilities.daily.claim_encounter_points",
            lambda ctx, g: (state.__setitem__("claimed", True) or True),
        )
        g = _g()
        assert claim_daily_reward(_ctx(), g) is True

    def test_f1_fail_guild_success(self, monkeypatch):
        """F1 失败 → 凯瑟琳成功 → 验证通过 → True。"""
        monkeypatch.setattr(
            "abilities.daily.check_daily_claimed",
            lambda ctx, g: False,  # 初始未领
        )
        monkeypatch.setattr(
            "abilities.daily.claim_encounter_points", lambda ctx, g: False
        )
        monkeypatch.setattr(
            "abilities.daily.claim_daily_at_guild", lambda ctx, g, country: True
        )
        # 凯瑟琳领完后验证通过
        call_count = [0]

        def fake_check_after(ctx, g):
            call_count[0] += 1
            # 第二次调用（验证时）返回 True
            return call_count[0] >= 2

        monkeypatch.setattr("abilities.daily.check_daily_claimed", fake_check_after)
        g = _g()
        assert claim_daily_reward(_ctx(), g, country="蒙德") is True

    def test_both_fail(self, monkeypatch):
        """两路均失败 → False。"""
        monkeypatch.setattr(
            "abilities.daily.check_daily_claimed", lambda ctx, g: False
        )
        monkeypatch.setattr(
            "abilities.daily.claim_encounter_points", lambda ctx, g: False
        )
        monkeypatch.setattr(
            "abilities.daily.claim_daily_at_guild", lambda ctx, g, country: False
        )
        g = _g()
        assert claim_daily_reward(_ctx(), g) is False


# ── claim_daily_reward 任务插件 ──


class TestClaimDailyRewardTask:
    def test_already_claimed(self, monkeypatch):
        from tasks.claim_daily_reward import main as task_main

        monkeypatch.setattr(
            "abilities.daily.check_daily_claimed", lambda ctx, g: True
        )
        g = _g()
        result = task_main(_ctx(), g)
        assert result["claimed"] is True
        assert result["method"] == "already_claimed"

    def test_claim_success(self, monkeypatch):
        from tasks.claim_daily_reward import main as task_main

        monkeypatch.setattr(
            "abilities.daily.check_daily_claimed", lambda ctx, g: False
        )
        monkeypatch.setattr(
            "abilities.daily.claim_daily_reward", lambda ctx, g, country: True
        )
        g = _g()
        result = task_main(_ctx(), g, country="蒙德")
        assert result["claimed"] is True

    def test_claim_fail_raises(self, monkeypatch):
        from tasks.claim_daily_reward import main as task_main

        monkeypatch.setattr(
            "abilities.daily.check_daily_claimed", lambda ctx, g: False
        )
        monkeypatch.setattr(
            "abilities.daily.claim_daily_reward", lambda ctx, g, country: False
        )
        g = _g()
        with pytest.raises(TaskError, match="每日奖励领取失败"):
            task_main(_ctx(), g)


# ── daily 总控编排 ──


class TestDailyOrchestrator:
    def test_calls_subtasks(self, monkeypatch):
        from tasks.daily import main as daily_main

        # mock g.run 返回各子任务结果
        run_results = {
            "auto_boss": {"boss": "急冻树", "count": 5},
            "auto_ley_line": {"region": "蒙德", "count": 4},
            "claim_daily_reward": {"claimed": True, "method": "encounter_points"},
        }
        g = _g()
        g.run.side_effect = lambda name, **kw: run_results.get(name, {})

        result = daily_main(_ctx(), g)

        assert result["mail"] == {}
        assert result["craft"] == {}
        assert result["domain"] == {}
        assert result["boss"]["boss"] == "急冻树"
        assert result["ley_line"]["region"] == "蒙德"
        assert result["daily_reward"]["claimed"] is True
        assert result["pot"] == {}
        # 验证调用顺序（BGI 一条龙顺序）
        calls = g.run.call_args_list
        assert calls[0] == call("claim_mail")
        assert calls[1] == call("craft_resin", country="蒙德")
        assert calls[2] == call("auto_domain", domain_name="绝缘之境", count=5)
        assert calls[3] == call("auto_boss", boss_name="急冻树", count=5)
        assert calls[4] == call("auto_ley_line", region="蒙德", count=4)
        assert calls[5] == call("claim_daily_reward", country="蒙德")
        assert calls[6] == call("enter_pot")

    def test_boss_exhausted_continues(self, monkeypatch):
        """首领树脂耗尽（NormalEnd）→ 记录但继续地脉+每日。"""
        from tasks.daily import main as daily_main

        g = _g()

        def fake_run(name, **kw):
            if name == "auto_boss":
                raise NormalEnd("树脂耗尽")
            if name == "auto_ley_line":
                raise NormalEnd("树脂耗尽")
            return {"claimed": True, "method": "guild"}

        g.run.side_effect = fake_run
        result = daily_main(_ctx(), g)
        assert result["boss"]["exhausted"] is True
        assert result["ley_line"]["exhausted"] is True
        assert result["daily_reward"]["claimed"] is True

    def test_boss_error_continues(self, monkeypatch):
        """首领 TaskError → 记录但继续。"""
        from tasks.daily import main as daily_main

        g = _g()

        def fake_run(name, **kw):
            if name == "auto_boss":
                raise TaskError("首领路径缺失")
            return {"region": "蒙德", "count": 4} if name == "auto_ley_line" else {"claimed": True}

        g.run.side_effect = fake_run
        result = daily_main(_ctx(), g)
        assert "error" in result["boss"]
        assert result["daily_reward"]["claimed"] is True


# ── Registry 可发现 ──


class TestDailyRegistryDiscover:
    def test_daily_tasks_discoverable(self):
        from framework.registry import TaskRegistry

        roots = (str(Path(__file__).parent.parent / "src" / "tasks"),)
        reg = TaskRegistry()
        reg.discover(roots=roots)
        assert reg.get("claim_daily_reward") is not None
        assert reg.get("daily") is not None
        assert reg.get("auto_boss") is not None
        assert reg.get("auto_ley_line") is not None
