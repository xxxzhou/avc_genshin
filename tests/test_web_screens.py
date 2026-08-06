"""网上公开原神测试图：用真实游戏画面验证检测器的**识别能力**。

与 ``test_real_screens.py`` 的区别（两者互补）：
- **图源**：网上公开图（``script/test_images_manifest.json`` + ``fetch_test_images.py``
  下载到 ``tests/fixtures/web/``），非用户自截实机图（``tests/fixtures/screens/``）。
- **断言**：manifest 集中管理（非每图旁 ``.expected.json``）。
- **分辨率**：不限。加载后用 avc ``IImageBuffer.resize(1920,1080)`` 归一化——项目
  所有坐标基于 1080p，非 1080p 图 resize 后检测器坐标才对齐（avc/image.py 原生 API）。
- **边界**：网上图无玩家坐标 ground truth → ``position``/``orientation`` **仅打印诊断，
  不断言精度**（精度测试留实机 ``test_real_screens.py``）。

用法：
  python script/fetch_test_images.py --all       # 下载图
  python -m pytest tests/test_web_screens.py -v -s

无图时本测试 skip（不报失败）。
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
WEB_DIR = Path(__file__).parent / "fixtures" / "web"
MANIFEST = ROOT / "script" / "test_images_manifest.json"
NATIVE_W, NATIVE_H = 1920, 1080


def _has_avc() -> bool:
    try:
        import avc  # noqa: F401

        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _has_avc(), reason="需 avc")


def _make_ctx():
    """最小 GameContext 替身：tm/ocr（模板/OCR 匹配器）+ _dpi_scale（CameraControl 需）。
    与 test_real_screens.py 一致，仅补 _dpi_scale 让朝向诊断可跑。"""
    from avc import Vision

    return SimpleNamespace(
        tm=Vision.createTemplateMatcher(), ocr=Vision.createTextRecognizer(),
        _dpi_scale=1.0,
    )


def _load_frame(path: Path):
    """加载图；非 1080p 则 avc resize 归一化到 1920×1080。返回 IImageBuffer。"""
    import avc

    frame = avc.Image.loadImage(str(path))
    assert frame is not None, f"加载失败 {path}（loadImage 支持 BMP/PNG/JPG/TGA）"
    if frame.width != NATIVE_W or frame.height != NATIVE_H:
        resized = frame.resize(NATIVE_W, NATIVE_H)  # avc 原生 resize（拉伸）
        assert resized is not None, f"resize 失败 {path}"
        frame = resized
    return frame


def _maybe_detector():
    """bgi_world YOLO 模型在则建检测器，不在返回 None（yolo 诊断可选）。"""
    try:
        from framework.resources import res

        from abilities.detector import GenshinDetector

        model = res.model("bgi_world.onnx")
        if model.exists():
            return GenshinDetector(str(model))
    except Exception:
        pass
    return None


def _load_manifest() -> dict:
    if not MANIFEST.exists():
        return {}
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


# 跑诊断的 scene 原语子集（挑与场景判别相关的；has_*/is_* 签名都是 (ctx, frame)->bool）
_SCENE_FLAGS = (
    "has_paimon_menu",       # 主界面
    "has_disabled_ui_btn",   # 对话
    "has_map_scale_btn",     # 大地图
    "has_map_settings_btn",  # 大地图(备选)
    "has_in_domain",         # 秘境
    "is_loading_screen",     # 加载
    "has_chest_f_icon",      # 宝箱 F
    "has_flower_f_icon",     # 地脉花 F
    "has_go_teleport",       # 传送按钮
    "has_page_close",        # 弹窗关闭
    "has_icon_option",       # 对话选项
    "has_pick_f",            # 拾取 F
)


def _diagnose(ctx, frame, *, classifier=None, detector=None) -> dict:
    """对一张真实帧跑所有检测器，返回诊断 dict。每项独立 try/except 不互相连累。"""
    from abilities import game_state as gs
    from abilities.fighter import detect_blood_bars
    from abilities.navigation.camera import CameraControl
    from abilities.navigation.position import PositionGetter

    out: dict = {"size": f"{frame.width}x{frame.height}"}

    # 场景分类（一键给出 main_ui/combat/dialog/map/...）
    try:
        clf = classifier if classifier is not None else gs.make_classifier(ctx)
        ss = clf(frame)
        # Scene 是 Enum（非纯 str），str() 给 'Scene.UNKNOWN'；取 .value 得 'unknown'
        out["scene"] = ss.scene.value if hasattr(ss.scene, "value") else str(ss.scene)
    except Exception as e:  # noqa: BLE001 — 诊断逐项独立
        out["scene"] = f"ERR {e!r}"

    # 血条 / 敌人
    try:
        bars = detect_blood_bars(frame)
        out["blood_bars"] = len(bars)
        out["has_enemy"] = bool(bars)
    except Exception as e:  # noqa: BLE001
        out["blood_bars"] = f"ERR {e!r}"
        out["has_enemy"] = False

    # 场景原语
    for name in _SCENE_FLAGS:
        fn = getattr(gs, name, None)
        if fn is None:
            continue
        try:
            out[name] = bool(fn(ctx, frame))
        except Exception as e:  # noqa: BLE001
            out[name] = f"ERR {e!r}"

    # YOLO 世界目标检测（模型在则跑，仅诊断）
    if detector is not None:
        try:
            dets = detector.detect(frame)
            out["yolo"] = {k: len(v) for k, v in dets.items()} if dets else {}
        except Exception as e:  # noqa: BLE001
            out["yolo"] = f"ERR {e!r}"

    # 位置 / 朝向（仅诊断；网上图无 ground truth，不做精度断言）
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


def _assert_expected(ctx, frame, out: dict, expect: dict, name: str) -> None:
    """按 manifest 的 expect 逐键断言。位置/朝向不断言（无 ground truth）。"""
    from abilities import vision_utils as vu

    for k, v in expect.items():
        if k == "scene":
            assert str(out.get("scene")) == str(v), \
                f"{name}: scene={out.get('scene')!r} != {v!r}"
        elif k == "has_enemy":
            assert out.get("has_enemy") == v, f"{name}: has_enemy={out.get('has_enemy')!r} != {v}"
        elif k == "blood_bars_min":
            assert out.get("blood_bars", 0) >= v, f"{name}: blood_bars {out.get('blood_bars')} < {v}"
        elif k == "scene_flags":
            for flag, fv in v.items():
                got = out.get(flag)
                assert got == fv, f"{name}: scene_flags.{flag}={got!r} != {fv}"
        elif k == "contains_text":
            for kw in v:
                hit = vu.find_text(ctx, kw, frame=frame)
                assert hit is not None, f"{name}: 未找到文字 {kw!r}"
        elif k == "yolo_min":
            yolo = out.get("yolo", {})
            if isinstance(yolo, dict):
                for cls, cnt in v.items():
                    assert yolo.get(cls, 0) >= cnt, f"{name}: yolo[{cls}]={yolo.get(cls, 0)} < {cnt}"
        # position / orientation / position_tol / orientation_tol：网上图忽略


def test_web_screens():
    """对 tests/fixtures/web/ 下每张已下载的网上图跑检测：有 expect 断言，否则打印诊断。"""
    manifest = _load_manifest()
    items = manifest.get("items", []) if manifest else []
    cases = [it for it in items if (WEB_DIR / it["filename"]).exists()]
    if not cases:
        pytest.skip("tests/fixtures/web/ 无已下载图（先 python script/fetch_test_images.py --all）")

    from abilities import game_state as gs

    ctx = _make_ctx()
    classifier = gs.make_classifier(ctx)
    detector = _maybe_detector()

    for item in cases:
        path = WEB_DIR / item["filename"]
        frame = _load_frame(path)
        out = _diagnose(ctx, frame, classifier=classifier, detector=detector)
        expect = item.get("expect", {})
        if expect:
            _assert_expected(ctx, frame, out, expect, item["id"])
            print(f"[{item['id']}] OK  {out}")
        else:
            print(f"[{item['id']}] (无 expect, 打印诊断)  {out}")
