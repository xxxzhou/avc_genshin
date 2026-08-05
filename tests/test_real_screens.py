"""真实画面黄金测试：验证检测器对**真实游戏画面**是否正确（合成测试补不了的）。

用法（实机/有截图的机器）：
    1. 截几张 1080p 全屏（战斗有怪 / 主界面 / 小地图站某地 / 大地图）
    2. 放 ``tests/fixtures/screens/``（gitignored，带名字如 fight.png / mainui.png）
    3a. 想自动断言：写 ``<名字>.expected.json``（schema 见下），跑 pytest 断言
    3b. 只想看诊断：直接跑，打印每张图各检测器结果，对着截图肉眼核

    python -m pytest tests/test_real_screens.py -v -s

expected.json schema（键可选，缺省只打印不断言）：
    {"blood_bars_min": 2,          # 至少 2 条血条
     "has_enemy": true,            # 是否有敌
     "has_paimon_menu": true,      # 场景特征（主界面等）
     "has_map_scale_btn": true,    # 大地图
     "position": [2000, 1000],     # 期望游戏坐标（需玩家真实位置）
     "position_tol": 100,          # 位置容差（单位）
     "orientation": 90}            # 期望朝向（度）
     "orientation_tol": 10         # 朝向容差

无截图时本测试 skip（不报失败）。
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

FIXTURES = Path(__file__).parent / "fixtures" / "screens"


def _has_avc() -> bool:
    try:
        import avc  # noqa: F401

        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _has_avc(), reason="需 avc")


def _screens():
    return sorted(FIXTURES.glob("*.png")) if FIXTURES.exists() else []


def _make_ctx():
    """最小 GameContext 替身：只需 tm/ocr（模板/OCR 匹配器，无需游戏窗口）。"""
    from avc import Vision

    return SimpleNamespace(
        tm=Vision.createTemplateMatcher(), ocr=Vision.createTextRecognizer()
    )


def _diagnose(ctx, frame) -> dict:
    """对一张真实帧跑所有检测器，返回诊断 dict。"""
    from abilities import game_state as gs
    from abilities.fighter import detect_blood_bars
    from abilities.navigation.camera import CameraControl
    from abilities.navigation.position import PositionGetter

    out: dict = {}
    out["size"] = f"{frame.width}x{frame.height}"
    bars = detect_blood_bars(frame)
    out["blood_bars"] = len(bars)
    out["has_enemy"] = bool(bars)
    if bars:
        b = min(bars, key=lambda r: abs(r.cx - 960) + abs(r.cy - 480))
        out["nearest_enemy"] = [b.cx, b.cy]
    for name in (
        "has_paimon_menu",
        "has_disabled_ui_btn",
        "has_map_scale_btn",
        "has_flower_f_icon",
        "has_chest_f_icon",
    ):
        try:
            out[name] = bool(getattr(gs, name)(ctx, frame))
        except Exception as e:  # noqa: BLE001 — 诊断逐项独立
            out[name] = f"ERR {e!r}"
    try:
        pos = PositionGetter(ctx).get_position(frame)
        out["position"] = list(pos) if pos else None
    except Exception as e:  # noqa: BLE001
        out["position"] = f"ERR {e!r}"
    try:
        out["orientation"] = CameraControl(ctx).get_orientation(frame)
    except Exception as e:  # noqa: BLE001
        out["orientation"] = f"ERR {e!r}"
    return out


def _load_expected(p: Path) -> dict | None:
    ep = p.with_suffix(".expected.json")
    return json.loads(ep.read_text(encoding="utf-8")) if ep.exists() else None


def _assert_expected(out: dict, exp: dict, name: str) -> None:
    """按 expected.json 逐键断言。"""
    for k, v in exp.items():
        if k == "blood_bars_min":
            assert out.get("blood_bars", 0) >= v, f"{name}: 血条 {out.get('blood_bars')} < {v}"
        elif k == "position":
            pos = out.get("position")
            assert isinstance(pos, list), f"{name}: position={pos!r}"
            assert abs(pos[0] - v[0]) < exp.get("position_tol", 100), f"{name}: X 偏 {pos[0]-v[0]}"
            assert abs(pos[1] - v[1]) < exp.get("position_tol", 100), f"{name}: Y 偏 {pos[1]-v[1]}"
        elif k == "orientation":
            o = out.get("orientation")
            assert isinstance(o, (int, float)), f"{name}: orientation={o!r}"
            assert abs(o - v) < exp.get("orientation_tol", 10), f"{name}: 朝向偏 {o-v}"
        else:  # has_enemy / has_paimon_menu 等布尔特征
            assert out.get(k) == v, f"{name}: {k}={out.get(k)!r} != {v}"


def test_real_screens():
    """对 fixtures/screens/ 下每张真实帧跑检测：有 expected 断言，否则打印诊断。"""
    screens = _screens()
    if not screens:
        pytest.skip("tests/fixtures/screens/ 无 1080p 截图（实机截屏放入后自动跑诊断）")
    import avc

    ctx = _make_ctx()
    for p in screens:
        frame = avc.Image.loadImage(str(p))
        assert frame is not None, f"加载失败 {p}"
        out = _diagnose(ctx, frame)
        exp = _load_expected(p)
        if exp:
            _assert_expected(out, exp, p.name)
            print(f"[{p.name}] OK  {out}")
        else:
            print(f"[{p.name}] (无 expected.json, 打印诊断供肉眼核)  {out}")
