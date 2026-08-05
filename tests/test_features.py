"""功能列表冒烟测试（按耗时 快→慢 排列）。

每条 = 一个能力冒烟（全量回归的"总览版"，不是逐条重复）。快（纯逻辑/无依赖）
在前，慢（avc + 地图 SIFT）在后。用法：

    python -m pytest tests/test_features.py -v --durations=5   # 全跑 + 看各条耗时
    python -m pytest tests/test_features.py --lf               # 只重测上次失败项

失败项由 pytest 记录在 .pytest_cache/lastfailed（``--lf`` 依赖，自动维护）；
需 avc / 地图的条目标 skip（无 avc 环境自动跳过）。
"""

from __future__ import annotations

from unittest.mock import MagicMock

import numpy as np
import pytest


# ── 辅助 ──


class _Buf:
    """模拟 avc IImageBuffer（to_bytes/width/height）。"""

    def __init__(self, bgra: np.ndarray):
        self._arr = bgra
        self.height, self.width = bgra.shape[:2]

    def to_bytes(self):
        return self._arr.tobytes()


def _has_avc() -> bool:
    try:
        import avc  # noqa: F401

        return True
    except Exception:
        return False


def _has_avc_and_map() -> bool:
    try:
        import avc  # noqa: F401
        from framework.resources import res

        return res.map("Assets/Map/Teyvat/Teyvat_0_256.png").exists()
    except Exception:
        return False


def _has_tp_json() -> bool:
    from framework.resources import res

    return res.map("tp.json").exists()


def _pick_textured(img):
    """找一块适合 SIFT 的纹理区（60×60 std 最大，避开图内边界），返回 (cx, cy)。"""
    h, w = img.shape[:2]
    best = (w // 2, h // 2)
    best_var = -1.0
    for y in range(300, h - 300, 300):
        for x in range(300, w - 300, 300):
            v = float(img[y : y + 60, x : x + 60].std())
            if v > best_var:
                best_var = v
                best = (x + 30, y + 30)
    return best


# ── 快：纯逻辑 / 无依赖 ──


def test_f01_task_registry():
    """任务注册：持久任务可按名发现。"""
    from pathlib import Path

    from framework.registry import TaskRegistry

    reg = TaskRegistry()
    reg.discover(roots=(str(Path(__file__).parent.parent / "src" / "tasks"),))
    for name in ("ping", "auto_boss", "auto_ley_line", "verify"):
        assert reg.get(name) is not None, f"{name} 未注册"


def test_f02_task_params():
    """任务参数 schema：auto_boss 声明了必填 boss_name。"""
    from tasks.auto_boss import main

    p = main.task_descriptor.params
    assert "boss_name" in p and p["boss_name"]["required"] is True
    assert "count" in p


def test_f03_coord_roundtrip():
    """定位坐标转换：256 像素 ↔ 游戏坐标。"""
    from abilities.navigation.position import PositionGetter

    pg = PositionGetter(MagicMock())
    px, py = pg._game_to_map256(2000.0, -1000.0)
    gx, gy = pg._map256_to_game(px, py)
    assert abs(gx - 2000.0) < 0.01 and abs(gy + 1000.0) < 0.01


def test_f04_scene_features():
    """场景特征原语：核心几个可调用。"""
    from abilities import game_state as gs

    for fn in (
        "has_paimon_menu",
        "has_disabled_ui_btn",
        "has_flower_f_icon",
        "has_chest_f_icon",
        "is_low_hp",
        "has_recovery_icon",
    ):
        assert callable(getattr(gs, fn)), f"{fn} 缺失"


def test_f05_blood_bar_cv2():
    """血条检测（cv2 回退）：合成帧检出红条。"""
    from abilities.fighter import detect_blood_bars

    arr = np.zeros((300, 400, 4), dtype=np.uint8)
    arr[50:58, 250:280] = (90, 90, 255, 255)  # BGRA 血条色
    bars = detect_blood_bars(_Buf(arr))
    assert len(bars) == 1 and bars[0].x == 250


def test_f06_path_action():
    """路径 action 派发：fight→fight_until_clear；未知→warning。"""
    from abilities.navigation.path_executor import PathExecutor, Waypoint

    g = MagicMock()
    pe = PathExecutor(MagicMock(), g)
    pe._handle_action(Waypoint(x=0, y=0, action="fight"))
    g.fight_until_clear.assert_called_once()
    pe._handle_action(Waypoint(x=1, y=1, action="mystery"))
    assert any("mystery" in w for w in pe.warnings)


def test_f07_notify():
    """通知系统：pub/sub 广播。"""
    from framework import notify as N

    got = []
    N.register(lambda e, f: got.append(e))
    N.notify("ping")
    assert "ping" in got


@pytest.mark.skipif(not _has_tp_json(), reason="tp.json 未提取（script/fetch_resources.py --select tp_json）")
def test_f08_tp_by_name():
    """按名传送：tp.json 查"七天神像-风"。"""
    from abilities.navigation.tp import TpDatabase

    p = TpDatabase().find_by_name("七天神像-风")
    assert p is not None


def test_f09_fighter_logic():
    """战斗逻辑（mock）：_fight_finished 默认血条消失。"""
    from abilities.fighter import SimpleFighter

    f = SimpleFighter(MagicMock(), MagicMock())
    f.has_enemy = lambda: False
    assert f._fight_finished() is True
    f.has_enemy = lambda: True
    assert f._fight_finished() is False


# ── 慢：需 avc（视觉下沉链）──


@pytest.mark.skipif(not _has_avc(), reason="需 avc")
def test_f10_avc_blood_bar():
    """血条检测 avc 路径（IColorDetector）：合成红条命中。"""
    import avc
    from avc._core import ColorSpace, ImageType

    w, h = 400, 300
    data = bytearray(w * h * 4)
    for yy in range(50, 58):
        for xx in range(250, 280):
            off = (yy * w + xx) * 4
            data[off : off + 4] = bytes((90, 90, 255, 255))
    buf = avc.Image.IImageBuffer()
    buf.setFormat(w, h, ImageType.bgra8)
    buf.from_bytes(bytes(data))
    cd = avc.Vision.createColorDetector()
    assert cd is not None, "avc_opencv 插件未加载"
    cd.setColorSpace(ColorSpace.bgr)
    cd.setRange(90, 90, 255, 90, 90, 255)
    cd.setMinArea(5)
    assert cd.detect(buf) == 1


@pytest.mark.skipif(not _has_avc(), reason="需 avc")
def test_f11_orientation_faithful():
    """朝向 avc↔Python 忠实性（合成噪声图角度一致）。"""
    import avc
    from avc._core import ImageType

    from abilities.navigation.camera import compute_orientation

    rng = np.random.default_rng(7)
    gray = rng.integers(0, 256, (212, 212), dtype=np.uint8)
    ang_py = compute_orientation(gray.copy())
    buf = avc.Image.IImageBuffer()
    buf.setFormat(212, 212, ImageType.r8)
    buf.from_bytes(gray.tobytes())
    ang_avc = avc.Vision.createOrientationDetector().compute(buf)
    assert ang_py == ang_avc


# ── 最慢：需 avc + 地图（SIFT 匹配）──


@pytest.mark.skipif(not _has_avc_and_map(), reason="需 avc + 256 全地图")
def test_f12_minimap_position():
    """小地图 SIFT 定位（合成还原）：试前几个高纹理候选点，任一稳定匹配即过。"""
    import cv2

    import avc
    from avc._core import ImageType

    from abilities.navigation.position import PositionGetter
    from framework.resources import res

    img = cv2.imread(str(res.map("Assets/Map/Teyvat/Teyvat_0_256.png")))
    pg = PositionGetter(MagicMock())
    h, w = img.shape[:2]
    # 高纹理候选（std 降序；个别点临海/低纹理会匹配不稳，故试多个）
    cands = []
    for y in range(300, h - 300, 300):
        for x in range(300, w - 300, 300):
            cands.append((float(img[y : y + 60, x : x + 60].std()), x + 30, y + 30))
    cands.sort(reverse=True)
    half = 150  # 300×300 → 212（0.7× 缩小，SIFT 稳）
    for _std, cx, cy in cands[:10]:
        patch = img[cy - half : cy + half, cx - half : cx + half]
        resized = cv2.resize(patch, (212, 212), interpolation=cv2.INTER_AREA)
        bgra = cv2.cvtColor(resized, cv2.COLOR_BGR2BGRA)
        buf = avc.Image.IImageBuffer()
        buf.setFormat(212, 212, ImageType.bgra8)
        buf.from_bytes(bgra.tobytes())
        gx, gy = pg._map256_to_game(float(cx), float(cy))
        pg.set_prev_position(gx, gy)
        r = pg._match_sift(buf)
        if r is not None and abs(r[0] - gx) < 200 and abs(r[1] - gy) < 200:
            return  # 有候选稳定匹配即通过
    raise AssertionError("前 10 个高纹理点都未能稳定 SIFT 定位")


@pytest.mark.skipif(not _has_avc_and_map(), reason="需 avc + 256 全地图")
def test_f13_bigmap_recovery():
    """大图恢复定位（合成视口还原）。"""
    import cv2

    import avc
    from avc._core import ImageType

    from abilities.navigation.position import PositionGetter, _BIG_MAP_ROI
    from framework.resources import res

    img = cv2.imread(str(res.map("Assets/Map/Teyvat/Teyvat_0_256.png")))
    pg = PositionGetter(MagicMock())
    h, w = img.shape[:2]
    vw, vh = _BIG_MAP_ROI[2], _BIG_MAP_ROI[3]
    px, py = _pick_textured(img)
    x0 = max(0, min(px - vw // 2, w - vw))
    y0 = max(0, min(py - vh // 2, h - vh))
    viewport = img[y0 : y0 + vh, x0 : x0 + vw]
    ex, ey = pg._map256_to_game(float(x0 + vw / 2.0), float(y0 + vh / 2.0))
    frame = np.zeros((1080, 1920, 4), dtype=np.uint8)
    frame[0:vh, 0:vw] = cv2.cvtColor(viewport, cv2.COLOR_BGR2BGRA)
    buf = avc.Image.IImageBuffer()
    buf.setFormat(1920, 1080, ImageType.bgra8)
    buf.from_bytes(frame.tobytes())
    r = pg.get_position_from_big_map(buf)
    assert r is not None, "大图恢复定位失败"
    assert abs(r[0] - ex) < 200 and abs(r[1] - ey) < 200
