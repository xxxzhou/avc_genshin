"""A+B 方向新增能力测试（Phase D 续）。

覆盖：
- A1 领邮件：claim_all_mail（有邮件/无邮件/找不到 collect）
- A2 合成树脂：craft_condensed_resin（成功/无路径/进不了/无可合成）
- A3 尘歌壶：enter_serenitea_pot / claim_pot_rewards / exit_serenitea_pot
- B1 自动秘境：get_domain_coords / enter_domain / claim_domain_reward / exit_domain
- auto_domain 任务插件（循环/树脂耗尽）

纯控制流 mock 测试，不依赖游戏。
运行: python -m pytest tests/test_phase_daily_new.py -v
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

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
    g.teleport_to.return_value = True
    for k, v in overrides.items():
        setattr(g, k, v)
    return g


def _ctx() -> MagicMock:
    return MagicMock()


# ── A1 领邮件 ──


class TestClaimAllMail:
    def test_with_mail_claimed(self, monkeypatch):
        """有邮件：点邮件图标→点全部领取→关闭。"""
        from abilities import mail as mail_mod

        mail_rect = Rect(50, 600, 40, 30, 0.9)
        collect_rect = Rect(100, 800, 60, 40, 0.9)
        monkeypatch.setattr(
            mail_mod.vu, "find_template",
            lambda ctx, path, **kw: mail_rect if "esc_mail_reward" in str(path) else collect_rect,
        )
        g = _g(wait_main_ui=MagicMock(return_value=True))
        assert mail_mod.claim_all_mail(_ctx(), g) is True
        g.click.assert_any_call(mail_rect.cx, mail_rect.cy)
        g.click.assert_any_call(collect_rect.cx, collect_rect.cy)
        g.press.assert_called()  # ESC 至少按了一次

    def test_no_mail_closes(self, monkeypatch):
        """无邮件图标：关闭菜单返回 True。"""
        from abilities import mail as mail_mod

        monkeypatch.setattr(mail_mod.vu, "find_template", lambda *a, **kw: None)
        g = _g(wait_main_ui=MagicMock(return_value=True))
        assert mail_mod.claim_all_mail(_ctx(), g) is True
        g.click.assert_not_called()  # 没有点击任何东西

    def test_mail_but_no_collect(self, monkeypatch):
        """有邮件但无 collect：点邮件图标→跳过领取→关闭。"""
        from abilities import mail as mail_mod

        mail_rect = Rect(50, 600, 40, 30, 0.9)
        monkeypatch.setattr(
            mail_mod.vu, "find_template",
            lambda ctx, path, **kw: mail_rect if "esc_mail_reward" in str(path) else None,
        )
        g = _g(wait_main_ui=MagicMock(return_value=True))
        assert mail_mod.claim_all_mail(_ctx(), g) is True
        g.click.assert_any_call(mail_rect.cx, mail_rect.cy)


# ── A2 合成浓缩树脂 ──


class TestCraftCondensedResin:
    def test_success_path(self, monkeypatch):
        """成功：走路径→F 进入→选选项→点树脂→双确认→退出。"""
        from abilities import craft as craft_mod

        # 假路径文件存在
        monkeypatch.setattr(craft_mod.res, "path_json", lambda _: Path("."))
        monkeypatch.setattr(Path, "exists", lambda self: True)
        monkeypatch.setattr(
            "abilities.navigation.path_executor.load_path_task",
            lambda pf: {"points": []},
        )
        monkeypatch.setattr(
            "abilities.navigation.path_executor.PathExecutor",
            lambda ctx, g: MagicMock(),
        )
        monkeypatch.setattr(craft_mod, "_press_f_to_enter", lambda ctx, g: True)
        monkeypatch.setattr(craft_mod, "_select_last_option", lambda ctx, g: None)
        resin_rect = Rect(1000, 300, 60, 50, 0.9)
        monkeypatch.setattr(
            craft_mod.vu, "find_template",
            lambda ctx, path, **kw: resin_rect if "craft_condensed_resin" in str(path) else None,
        )

        g = _g(wait_scene=MagicMock(return_value=True), wait_main_ui=MagicMock(return_value=True))
        assert craft_mod.craft_condensed_resin(_ctx(), g, country="蒙德") is True
        g.click.assert_any_call(resin_rect.cx, resin_rect.cy)

    def test_no_path_file(self, monkeypatch):
        """路径文件不存在 → False。"""
        from abilities import craft as craft_mod

        monkeypatch.setattr(craft_mod.res, "path_json", lambda _: Path("."))
        monkeypatch.setattr(Path, "exists", lambda self: False)
        g = _g()
        assert craft_mod.craft_condensed_resin(_ctx(), g, country="蒙德") is False

    def test_cannot_enter(self, monkeypatch):
        """F 按 3 次未进对话 → False。"""
        from abilities import craft as craft_mod

        monkeypatch.setattr(craft_mod.res, "path_json", lambda _: Path("."))
        monkeypatch.setattr(Path, "exists", lambda self: True)
        monkeypatch.setattr(
            "abilities.navigation.path_executor.load_path_task",
            lambda pf: {"points": []},
        )
        monkeypatch.setattr(
            "abilities.navigation.path_executor.PathExecutor",
            lambda ctx, g: MagicMock(),
        )
        monkeypatch.setattr(craft_mod, "_press_f_to_enter", lambda ctx, g: False)
        g = _g()
        assert craft_mod.craft_condensed_resin(_ctx(), g) is False

    def test_bench_path_name(self):
        """国家→路径名。"""
        from abilities.craft import _bench_path_name

        assert _bench_path_name("蒙德") == "合成台_蒙德.json"
        assert _bench_path_name("枫丹") == "合成台_枫丹.json"


# ── A3 尘歌壶 ──


class TestSereniteaPot:
    def test_enter_pot(self, monkeypatch):
        """进入尘歌壶：teleport_to + 等主界面。"""
        from abilities import pot as pot_mod

        g = _g(wait_main_ui=MagicMock(return_value=True))
        assert pot_mod.enter_serenitea_pot(_ctx(), g) is True
        g.teleport_to.assert_called_once_with("尘歌壶", map_name="SereniteaPot")

    def test_enter_pot_teleport_fails(self, monkeypatch):
        """传送异常 → False。"""
        from abilities import pot as pot_mod

        g = _g(teleport_to=MagicMock(side_effect=Exception("传送失败")))
        assert pot_mod.enter_serenitea_pot(_ctx(), g) is False

    def test_claim_rewards_found(self, monkeypatch):
        """找到阿圆→领好感→领宝钱。"""
        from abilities import pot as pot_mod

        monkeypatch.setattr(pot_mod, "_find_and_press_f", lambda ctx, g: True)
        monkeypatch.setattr(
            pot_mod, "_click_template",
            lambda ctx, g, name, **kw: "sereniteapot_love.png" in name,
        )
        monkeypatch.setattr(pot_mod, "time", MagicMock(sleep=lambda *a: None))
        g = _g()
        g.talk = MagicMock()
        assert pot_mod.claim_pot_rewards(_ctx(), g) is True

    def test_claim_rewards_no_tubby(self, monkeypatch):
        """找不到阿圆 → False。"""
        from abilities import pot as pot_mod

        monkeypatch.setattr(pot_mod, "_find_and_press_f", lambda ctx, g: False)
        monkeypatch.setattr(pot_mod, "time", MagicMock(sleep=lambda *a: None))
        g = _g()
        g.talk = MagicMock()
        assert pot_mod.claim_pot_rewards(_ctx(), g) is False

    def test_exit_pot(self, monkeypatch):
        """退出尘歌壶：先关面板→talk 再见→传送回提瓦特。"""
        from abilities import pot as pot_mod

        g = _g(wait_main_ui=MagicMock(return_value=True))
        g.talk = MagicMock()
        monkeypatch.setattr(pot_mod, "_click_template", lambda *a, **kw: True)
        monkeypatch.setattr(pot_mod, "time", MagicMock(sleep=lambda *a: None))
        assert pot_mod.exit_serenitea_pot(_ctx(), g) is True
        g.talk.assert_called_once_with("再见")
        g.teleport_to.assert_called_once_with((4508.97, 3630.56))


# ── B1 自动秘境 ──


class TestDomain:
    def test_get_coords_known(self):
        """坐标来自 tp.json（BGI 同源）：别名→真名→坐标。"""
        from abilities.domain import get_domain_coords

        # 别名：绝缘之境 → 椛染之庭（BGI id=361 真值）
        assert get_domain_coords("绝缘之境") == (-3775.004, -2367.516)
        # tp.json 标准名直查
        assert get_domain_coords("椛染之庭") == (-3775.004, -2367.516)
        assert get_domain_coords("太山府") == (659.0738, 1168.46)
        assert get_domain_coords("未知秘境") is None

    def test_enter_domain_success(self, monkeypatch):
        """进入秘境全流程成功。"""
        from abilities import domain as dom_mod

        monkeypatch.setattr(dom_mod, "_walk_and_press_f", lambda ctx, g, **kw: True)
        monkeypatch.setattr(dom_mod, "_click_template", lambda *a, **kw: True)
        monkeypatch.setattr(dom_mod, "_has_template", lambda *a, **kw: True)
        g = _g(wait_until=MagicMock(return_value=True), find_text=MagicMock(return_value=Rect(960, 500, 100, 40, 0.9)))
        assert dom_mod.enter_domain(_ctx(), g, "绝缘之境") is True
        g.teleport_to.assert_called_once()

    def test_enter_domain_unknown(self, monkeypatch):
        """未知秘境 → False，不传送。"""
        from abilities import domain as dom_mod

        g = _g()
        assert dom_mod.enter_domain(_ctx(), g, "不存在") is False
        g.teleport_to.assert_not_called()

    def test_claim_reward_resin_exhausted(self, monkeypatch):
        """树脂耗尽：OCR 出现「补充原粹树脂」→ False。"""
        from abilities import domain as dom_mod

        g = _g(
            wait_until=MagicMock(side_effect=lambda pred, timeout: True),
            find_text=MagicMock(return_value=Rect(500, 500, 80, 30, 0.9)),
        )
        # _RESIN_EXHAUSTED 文字匹配时返回 False
        from abilities.domain import _RESIN_EXHAUSTED

        real_find = g.find_text
        g.find_text = MagicMock(side_effect=lambda text: real_find() if text == _RESIN_EXHAUSTED else Rect(500, 500, 80, 30, 0.9))
        assert dom_mod.claim_domain_reward(_ctx(), g) is False

    def test_exit_domain(self, monkeypatch):
        """退出秘境：ESC + 黑确认 + 等主界面。"""
        from abilities import domain as dom_mod

        monkeypatch.setattr(dom_mod, "_click_template", lambda *a, **kw: True)
        g = _g(wait_main_ui=MagicMock(return_value=True))
        assert dom_mod.exit_domain(_ctx(), g) is True
        g.press.assert_called()  # ESC 至少按了一次
        g.wait_main_ui.assert_called()


# ── auto_domain 任务插件 ──


class TestAutoDomainTask:
    def test_success_loop(self, monkeypatch):
        """刷 N 次后正常返回。"""
        from abilities import domain as dom_mod
        from tasks import auto_domain as ad_mod

        monkeypatch.setattr(dom_mod, "enter_domain", lambda ctx, g, name: True)
        monkeypatch.setattr(ad_mod, "fight_domain_safe", lambda ctx, g, **kw: True)
        monkeypatch.setattr(dom_mod, "claim_domain_reward", lambda ctx, g: True)
        monkeypatch.setattr(dom_mod, "exit_domain", lambda ctx, g: True)
        g = _g()
        result = ad_mod.main(_ctx(), g, domain_name="绝缘之境", count=2)
        assert result == {"domain": "绝缘之境", "count": 2}

    def test_resin_exhausted_normal_end(self, monkeypatch):
        """树脂耗尽 → NormalEnd。"""
        from abilities import domain as dom_mod
        from tasks import auto_domain as ad_mod

        monkeypatch.setattr(dom_mod, "enter_domain", lambda ctx, g, name: True)
        monkeypatch.setattr(ad_mod, "fight_domain_safe", lambda ctx, g, **kw: True)
        monkeypatch.setattr(dom_mod, "claim_domain_reward", lambda ctx, g: False)
        monkeypatch.setattr(dom_mod, "exit_domain", lambda ctx, g: True)
        g = _g()
        with pytest.raises(NormalEnd):
            ad_mod.main(_ctx(), g, domain_name="绝缘之境", count=5)

    def test_unknown_domain_task_error(self, monkeypatch):
        """未知秘境 → TaskError。"""
        from tasks import auto_domain as ad_mod

        g = _g()
        with pytest.raises(TaskError):
            ad_mod.main(_ctx(), g, domain_name="不存在", count=5)

    def test_fight_timeout_task_error(self, monkeypatch):
        """战斗超时 → TaskError。"""
        from abilities import domain as dom_mod
        from tasks import auto_domain as ad_mod

        monkeypatch.setattr(dom_mod, "enter_domain", lambda ctx, g, name: True)
        monkeypatch.setattr(ad_mod, "fight_domain_safe", lambda ctx, g, **kw: False)
        g = _g()
        with pytest.raises(TaskError):
            ad_mod.main(_ctx(), g, domain_name="绝缘之境", count=1)


# ── 任务插件可发现 ──


class TestNewTasksDiscoverable:
    def test_all_new_tasks_in_registry(self):
        from framework.registry import TaskRegistry

        roots = (str(Path(__file__).parent.parent / "src" / "tasks"),)
        reg = TaskRegistry()
        reg.discover(roots=roots)
        for name in ("claim_mail", "craft_resin", "enter_pot", "auto_domain"):
            assert reg.get(name) is not None, f"任务 {name} 未注册"
